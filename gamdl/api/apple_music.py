import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from http.cookiejar import MozillaCookieJar
from urllib.parse import parse_qs, urlparse

import httpx
import structlog
from httpx_retries import Retry, RetryTransport

from .constants import (
    APPLE_MUSIC_ACCOUNT_INFO_API_URI,
    APPLE_MUSIC_ALBUM_API_URI,
    APPLE_MUSIC_AMP_API_URL,
    APPLE_MUSIC_ARTIST_API_URI,
    APPLE_MUSIC_ASSETS_API_URI,
    APPLE_MUSIC_COOKIE_DOMAIN,
    APPLE_MUSIC_HOMEPAGE_URL,
    APPLE_MUSIC_LIBRARY_ALBUM_API_URI,
    APPLE_MUSIC_LIBRARY_PLAYLIST_API_URI,
    APPLE_MUSIC_LIBRARY_PLAYLISTS_API_URI,
    APPLE_MUSIC_LICENSE_API_URL,
    APPLE_MUSIC_LIBRARY_MUSIC_VIDEO_API_URI,
    APPLE_MUSIC_MUSIC_VIDEO_API_URI,
    APPLE_MUSIC_LIBRARY_ALBUMS_API_URI,
    APPLE_MUSIC_PLAYLIST_API_URI,
    APPLE_MUSIC_SEARCH_API_URI,
    APPLE_MUSIC_LIBRARY_MUSIC_VIDEOS_API_URI,
    APPLE_MUSIC_LIBRARY_SONG_API_URI,
    APPLE_MUSIC_LIBRARY_SONGS_API_URI,
    APPLE_MUSIC_SONG_API_URI,
    APPLE_MUSIC_UPLOADED_VIDEO_API_URL,
    APPLE_MUSIC_WEBPLAYBACK_API_URL,
)
from .exceptions import GamdlApiResponseError
from .wrapper import WrapperApi

logger = structlog.get_logger(__name__)


class AppleMusicApi:
    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        storefront: str,
        language: str,
        media_user_token: str | None = None,
        account_info: dict | None = None,
    ) -> None:
        self.token = token
        self.storefront = storefront
        self.language = language
        self.media_user_token = media_user_token
        self.account_info = account_info
        self.client = client

    @property
    def active_subscription(self) -> bool:
        if not self.account_info:
            return False

        return (
            self.account_info.get("meta", {})
            .get("subscription", {})
            .get("active", False)
        )

    @property
    def account_restrictions(self) -> dict | None:
        if not self.account_info:
            return None

        data = self.account_info.get("data", [])
        if not data:
            return None
        return data[0].get("attributes", {}).get("restrictions")

    @staticmethod
    async def get_token() -> str:
        log = logger.bind(action="get_token")

        response = None
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    APPLE_MUSIC_HOMEPAGE_URL,
                    follow_redirects=True,
                )
                response.raise_for_status()
                home_page = response.text
            except httpx.HTTPError:
                raise GamdlApiResponseError(
                    "Error fetching Apple Music homepage",
                    status_code=response.status_code if response is not None else None,
                )

        index_js_uri_match = re.search(
            r"/(assets/index[~-][^/\"]+\.js)",
            home_page,
        )
        if not index_js_uri_match:
            raise GamdlApiResponseError(
                "Error finding index.js URI in Apple Music homepage"
            )
        index_js_uri = index_js_uri_match.group(1)

        response = None
        async with httpx.AsyncClient(follow_redirects=True) as client:
            try:
                response = await client.get(
                    f"{APPLE_MUSIC_HOMEPAGE_URL}/{index_js_uri}"
                )
                response.raise_for_status()
                index_js_page = response.text
            except httpx.HTTPError:
                raise GamdlApiResponseError(
                    "Error fetching index.js page",
                    status_code=response.status_code if response is not None else None,
                )

        token_match = re.search(r'"(eyJ[A-Za-z0-9\-_]+\.eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+)"', index_js_page)
        if not token_match:
            raise GamdlApiResponseError("Error finding token in index.js page")
        token = token_match.group(1)

        log.debug("success")

        return token

    @staticmethod
    async def get_account_info(
        token: str,
        media_user_token: str,
        meta: str = "subscription",
        max_retries: int = 3,
        retry_backoff_base: float = 1.0,
    ) -> dict:
        log = logger.bind(action="get_account_info", meta=meta)

        for attempt in range(max_retries + 1):
            response = None
            async with httpx.AsyncClient() as client:
                try:
                    response = await client.get(
                        APPLE_MUSIC_AMP_API_URL + APPLE_MUSIC_ACCOUNT_INFO_API_URI,
                        params={
                            "meta": meta,
                        },
                        headers={
                            "authorization": f"Bearer {token}",
                            "origin": APPLE_MUSIC_HOMEPAGE_URL,
                            "cookie": f"media-user-token={media_user_token}",
                        },
                    )
                    response.raise_for_status()
                    account_info = response.json()
                    log.debug("success", account_info=account_info)
                    return account_info
                except httpx.HTTPStatusError as e:
                    status_code = e.response.status_code
                    if status_code == 429 or status_code >= 500:
                        if attempt >= max_retries:
                            raise GamdlApiResponseError(
                                "Error fetching account info",
                                status_code=status_code,
                            ) from e
                        retry_after = e.response.headers.get("Retry-After")
                        if retry_after:
                            try:
                                # Cap the wait so a long Retry-After can't burn
                                # the whole rip timeout budget.
                                delay = min(float(retry_after), 10.0)
                            except ValueError:
                                delay = retry_backoff_base * (2**attempt)
                        else:
                            delay = retry_backoff_base * (2**attempt)
                        log.warning(
                            "retry_account_info",
                            status_code=status_code,
                            attempt=attempt + 1,
                            max_retries=max_retries,
                            delay=delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise GamdlApiResponseError(
                        "Error fetching account info",
                        status_code=status_code,
                    ) from e
                except httpx.HTTPError:
                    raise GamdlApiResponseError(
                        "Error fetching account info",
                        status_code=(
                            response.status_code if response is not None else None
                        ),
                    )

        raise GamdlApiResponseError(
            "Error fetching account info",
            status_code=None,
        )

    _ACCOUNT_INFO_CACHE_TTL_SECONDS = 600

    @classmethod
    def _get_account_info_cache_paths(
        cls,
        token: str,
        media_user_token: str,
    ) -> tuple[str, str]:
        key = hashlib.sha256(
            f"{token}:{media_user_token}".encode("utf-8")
        ).hexdigest()[:16]
        cache_dir = os.path.join(tempfile.gettempdir(), "gamdl")
        os.makedirs(cache_dir, exist_ok=True)
        return (
            os.path.join(cache_dir, f"account_info_{key}.json"),
            os.path.join(cache_dir, f"account_info_{key}.lock"),
        )

    @classmethod
    def _load_account_info_cache(
        cls,
        cache_path: str,
        meta: str,
    ) -> dict | None:
        try:
            if not os.path.exists(cache_path):
                return None
            if (
                time.time() - os.path.getmtime(cache_path)
                >= cls._ACCOUNT_INFO_CACHE_TTL_SECONDS
            ):
                return None
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("meta") != meta:
                return None
            return cached["account_info"]
        except (OSError, ValueError, KeyError):
            return None

    @classmethod
    def _write_account_info_cache(
        cls,
        cache_path: str,
        meta: str,
        account_info: dict,
    ) -> None:
        try:
            tmp_path = cache_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"meta": meta, "account_info": account_info}, f)
            os.replace(tmp_path, cache_path)
        except OSError:
            logger.warning(
                "account_info_cache_write_failed",
                cache_path=cache_path,
            )

    @classmethod
    async def _get_account_info_cached(
        cls,
        token: str,
        media_user_token: str,
        meta: str = "subscription",
    ) -> dict:
        cache_path, lock_path = cls._get_account_info_cache_paths(
            token,
            media_user_token,
        )

        cached = cls._load_account_info_cache(cache_path, meta)
        if cached is not None:
            logger.debug("account_info_cache_hit")
            return cached

        # Serialize concurrent fetches: multiple gamdl processes rip at once
        # (one per track), so without a lock they'd all hammer Apple's
        # account-info endpoint simultaneously and trip its 429.
        lock_fd = None
        try:
            import fcntl

            lock_fd = open(lock_path, "w")
            await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            lock_fd = None

        try:
            cached = cls._load_account_info_cache(cache_path, meta)
            if cached is not None:
                logger.debug("account_info_cache_hit_after_lock")
                return cached

            account_info = await cls.get_account_info(token, media_user_token, meta)
            cls._write_account_info_cache(cache_path, meta, account_info)
            return account_info
        finally:
            if lock_fd is not None:
                try:
                    await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_UN)
                except (OSError, ValueError):
                    pass
                lock_fd.close()

    @classmethod
    async def create(
        cls,
        storefront: str | None = None,
        language: str = "en-US",
        token: str | None = None,
        media_user_token: str | None = None,
    ) -> "AppleMusicApi":
        token = token or await cls.get_token()
        account_info = None
        if media_user_token:
            try:
                account_info = await cls._get_account_info_cached(
                    token,
                    media_user_token,
                )
            except GamdlApiResponseError as e:
                if storefront:
                    # The caller explicitly knows which catalog storefront to
                    # use, so a transient account-info failure (Apple 429s
                    # under load) must not kill the whole rip.
                    logger.warning(
                        "account_info_unavailable",
                        reason=str(e),
                        storefront=storefront,
                        error="continuing_with_explicit_storefront",
                    )
                    account_info = None
                else:
                    raise
        # An explicitly-passed storefront (CLI --storefront / config.ini)
        # overrides the account's subscription storefront. Otherwise fall back
        # to the account storefront, then to the US catalog (the default for
        # anonymous downloads). This fixes 404 "Resource Not Found" for tracks
        # that exist in the US catalog but not in the account's regional
        # storefront.
        if not storefront and account_info:
            storefront = account_info["meta"]["subscription"]["storefront"]
        if not storefront:
            storefront = "us"

        client = httpx.AsyncClient(
            headers={
                "authorization": f"Bearer {token}",
                "origin": APPLE_MUSIC_HOMEPAGE_URL,
            },
            # Bounded retries: each rip has a tight timeout budget (the addon
            # SIGKILLs gamdl after ~60s), so retries must fit inside it.
            # total_timeout caps cumulative sleep (so a long Retry-After can't
            # burn the whole budget) while max_backoff_wait caps each wait and
            # 5 attempts ride out short Apple throttle windows.
            transport=RetryTransport(
                retry=Retry(
                    total=5,
                    backoff_factor=0.5,
                    max_backoff_wait=10,
                    total_timeout=15,
                    status_forcelist=[429, 500, 502, 503, 504],
                )
            ),
        )

        if media_user_token:
            client.headers.update(
                {
                    "cookie": f"media-user-token={media_user_token}",
                }
            )

        api = cls(
            client=client,
            token=token,
            storefront=storefront,
            language=language,
            media_user_token=media_user_token,
            account_info=account_info,
        )
        return api

    @classmethod
    async def create_from_netscape_cookies(
        cls,
        cookies_path: str = "./cookies.txt",
        *args,
        **kwargs,
    ) -> "AppleMusicApi":
        cookies = MozillaCookieJar(cookies_path)
        cookies.load(ignore_discard=True, ignore_expires=True)
        parse_cookie = lambda name: next(
            (
                cookie.value
                for cookie in cookies
                if cookie.name == name and cookie.domain == APPLE_MUSIC_COOKIE_DOMAIN
            ),
            None,
        )

        media_user_token = parse_cookie("media-user-token")
        if not media_user_token:
            raise ValueError(
                '"media-user-token" cookie not found in cookies. '
                "Make sure you have exported the cookies from the Apple Music webpage "
                "and are logged in with an active subscription."
            )

        return await cls.create(
            media_user_token=media_user_token,
            *args,
            **kwargs,
        )

    @classmethod
    async def create_from_wrapper(
        cls,
        wrapper_api: WrapperApi,
        *args,
        **kwargs,
    ) -> "AppleMusicApi":
        auth = wrapper_api.me.get("auth", {})
        media_user_token = auth.get("music_user_token")
        token = auth.get("dev_token")
        if not media_user_token or not token:
            raise GamdlApiResponseError(
                "Wrapper account info is missing auth tokens",
                status_code=None,
            )

        return await cls.create(
            media_user_token=media_user_token,
            token=token,
            *args,
            **kwargs,
        )

    async def _amp_request(
        self,
        uri: str,
        params: dict | None = None,
    ) -> dict:
        response = None
        try:
            response = await self.client.get(
                APPLE_MUSIC_AMP_API_URL + uri,
                params=params,
            )
            response.raise_for_status()
            response_json = response.json()
        except httpx.HTTPError:
            raise GamdlApiResponseError(
                "Error fetching from AMP API",
                content=response.text if response is not None else None,
                status_code=response.status_code if response is not None else None,
            )

        if "errors" in response_json:
            raise GamdlApiResponseError(
                "Error fetching from AMP API",
                content=response_json["errors"],
            )

        return response_json

    async def get_song(
        self,
        song_id: str,
        extend: str = "extendedAssetUrls",
        include: str = "lyrics,albums",
    ) -> dict:
        log = logger.bind(action="get_song", song_id=song_id)

        song = await self._amp_request(
            APPLE_MUSIC_SONG_API_URI.format(
                storefront=self.storefront,
                song_id=song_id,
            ),
            {
                "extend": extend,
                "include": include,
            },
        )

        log.debug("success", song=song)

        return song

    async def get_music_video(
        self,
        music_video_id: str,
        include: str = "albums",
    ) -> dict:
        log = logger.bind(action="get_music_video", music_video_id=music_video_id)

        music_video = await self._amp_request(
            APPLE_MUSIC_MUSIC_VIDEO_API_URI.format(
                storefront=self.storefront,
                music_video_id=music_video_id,
            ),
            {
                "include": include,
            },
        )

        log.debug("success", music_video=music_video)

        return music_video

    async def get_uploaded_video(
        self,
        uploaded_video_id: str,
    ) -> dict:
        log = logger.bind(
            action="get_uploaded_video", uploaded_video_id=uploaded_video_id
        )

        uploaded_video = await self._amp_request(
            APPLE_MUSIC_UPLOADED_VIDEO_API_URL.format(
                storefront=self.storefront,
                uploaded_video_id=uploaded_video_id,
            )
        )

        log.debug("success", uploaded_video=uploaded_video)

        return uploaded_video

    async def get_album(
        self,
        album_id: str,
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_album", album_id=album_id)

        album = await self._amp_request(
            APPLE_MUSIC_ALBUM_API_URI.format(
                storefront=self.storefront,
                album_id=album_id,
            ),
            {
                "extend": extend,
            },
        )

        log.debug("success", album=album)

        return album

    async def get_playlist(
        self,
        playlist_id: str,
        limit_tracks: int = 300,
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_playlist", playlist_id=playlist_id)

        playlist = await self._amp_request(
            APPLE_MUSIC_PLAYLIST_API_URI.format(
                storefront=self.storefront,
                playlist_id=playlist_id,
            ),
            {
                "limit[tracks]": limit_tracks,
                "extend": extend,
            },
        )

        log.debug("success", playlist=playlist)

        return playlist

    async def get_artist(
        self,
        artist_id: str,
        include: str = "albums,music-videos",
        views: str = "full-albums,compilation-albums,live-albums,singles,top-songs",
        limit: int = 100,
    ) -> dict:
        log = logger.bind(action="get_artist", artist_id=artist_id)

        artist = await self._amp_request(
            APPLE_MUSIC_ARTIST_API_URI.format(
                storefront=self.storefront,
                artist_id=artist_id,
            ),
            {
                "include": include,
                "views": views,
                **{
                    f"limit[{_include}]": limit
                    for _include in [*include.split(","), *views.split(",")]
                },
            },
        )

        log.debug("success", artist=artist)

        return artist

    async def get_library_song(
        self,
        song_id: str,
        include: str = "catalog",
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_library_song", song_id=song_id)

        song = await self._amp_request(
            APPLE_MUSIC_LIBRARY_SONG_API_URI.format(
                song_id=song_id,
            ),
            {
                "include": include,
                "extend": extend,
            },
        )

        log.debug("success", song=song)

        return song

    async def get_library_music_video(
        self,
        music_video_id: str,
        include: str = "catalog",
    ) -> dict:
        log = logger.bind(
            action="get_library_music_video", music_video_id=music_video_id
        )

        music_video = await self._amp_request(
            APPLE_MUSIC_LIBRARY_MUSIC_VIDEO_API_URI.format(
                music_video_id=music_video_id,
            ),
            {
                "include": include,
            },
        )

        log.debug("success", music_video=music_video)

        return music_video

    async def get_library_album(
        self,
        album_id: str,
        include: str = "catalog",
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_library_album", album_id=album_id)

        album = await self._amp_request(
            APPLE_MUSIC_LIBRARY_ALBUM_API_URI.format(
                album_id=album_id,
            ),
            {
                "include": include,
                "extend": extend,
            },
        )

        log.debug("success", album=album)

        return album

    async def get_library_playlist(
        self,
        playlist_id: str,
        include: str = "catalog,tracks",
        limit: int = 100,
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_library_playlist", playlist_id=playlist_id)

        playlist = await self._amp_request(
            APPLE_MUSIC_LIBRARY_PLAYLIST_API_URI.format(
                playlist_id=playlist_id,
            ),
            {
                "include": include,
                **{f"limit[{_include}]": limit for _include in include.split(",")},
                "extend": extend,
            },
        )

        log.debug("success", playlist=playlist)

        return playlist

    async def get_library_songs(
        self,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
        extend: str = "extendedAssetUrls",
    ) -> dict:
        log = logger.bind(action="get_library_songs")

        library_songs = await self._amp_request(
            APPLE_MUSIC_LIBRARY_SONGS_API_URI,
            {
                "limit": limit,
                "offset": offset,
                "include": include,
                "extend": extend,
            },
        )

        log.debug("success", library_songs=library_songs)

        return library_songs

    async def get_library_music_videos(
        self,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
    ) -> dict:
        log = logger.bind(action="get_library_music_videos")

        library_music_videos = await self._amp_request(
            APPLE_MUSIC_LIBRARY_MUSIC_VIDEOS_API_URI,
            {
                "limit": limit,
                "offset": offset,
                "include": include,
            },
        )

        log.debug("success", library_music_videos=library_music_videos)

        return library_music_videos

    async def get_library_albums(
        self,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
    ) -> dict:
        log = logger.bind(action="get_library_albums")

        library_albums = await self._amp_request(
            APPLE_MUSIC_LIBRARY_ALBUMS_API_URI,
            {
                "limit": limit,
                "offset": offset,
                "include": include,
            },
        )

        log.debug("success", library_albums=library_albums)

        return library_albums

    async def get_library_playlists(
        self,
        limit: int = 100,
        offset: int = 0,
        include: str = "catalog",
    ) -> dict:
        log = logger.bind(action="get_library_playlists")

        library_playlists = await self._amp_request(
            APPLE_MUSIC_LIBRARY_PLAYLISTS_API_URI,
            {
                "limit": limit,
                "offset": offset,
                "include": include,
            },
        )

        log.debug("success", library_playlists=library_playlists)

        return library_playlists

    async def get_search_results(
        self,
        term: str,
        types: str = "songs,music-videos,albums,playlists,artists",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        log = logger.bind(action="get_search_results", term=term, types=types)

        search_results = await self._amp_request(
            APPLE_MUSIC_SEARCH_API_URI.format(
                storefront=self.storefront,
            ),
            {
                "term": term,
                "types": types,
                "limit": limit,
                "offset": offset,
            },
        )

        log.debug("success", search_results=search_results)

        return search_results

    async def get_assets(
        self,
        media_id: str,
        kind: str = "song",
        include_license_urls: bool = True,
        hls_encryption: str = "CBC",
        hls_profile: str = "enhancedHls",
    ) -> dict:
        log = logger.bind(
            action="get_assets",
            media_id=media_id,
            kind=kind,
            include_license_urls=include_license_urls,
            hls_encryption=hls_encryption,
            hls_profile=hls_profile,
        )

        params = {
            "id": media_id,
            "kind": kind,
            "includeLicenseUrls": include_license_urls,
            "hlsEncryption": hls_encryption,
        }
        if hls_profile:
            params["hlsProfile"] = hls_profile

        assets = await self._amp_request(
            APPLE_MUSIC_ASSETS_API_URI,
            params,
        )

        log.debug("success", assets=assets)

        return assets

    async def get_extended_api_data(
        self,
        next_uri: str | None,
        href_uri: str,
    ) -> dict:
        log = logger.bind(
            action="extend_api_data", next_uri=next_uri, href_uri=href_uri
        )

        if not next_uri:
            log.debug("no_next_uri")
            return

        href_params = parse_qs(urlparse(href_uri).query)
        next_params = parse_qs(urlparse(next_uri).query)

        if href_params.get("limit"):
            limit = int(href_params["limit"][0])
        else:
            limit = None

        extended_data = await self._amp_request(
            urlparse(next_uri).path,
            {
                **({"limit": limit} if limit else {}),
                **{k: v for k, v in next_params.items() if k not in ["limit"]},
            },
        )

        log.debug("success", extended_data=extended_data)

        return extended_data

    async def get_webplayback(
        self,
        track_id: str,
        is_library: bool = False,
    ) -> dict:
        log = logger.bind(action="get_webplayback", track_id=track_id)

        response = None

        if is_library:
            request_body = {
                "universalLibraryId": track_id,
            }
        else:
            request_body = {
                "salableAdamId": track_id,
            }
        request_body["language"] = self.language

        try:
            response = await self.client.post(
                APPLE_MUSIC_WEBPLAYBACK_API_URL,
                json=request_body,
            )
            response.raise_for_status()
            webplayback = response.json()
        except httpx.HTTPError:
            raise GamdlApiResponseError(
                "Error fetching webplayback data",
                content=response.text if response is not None else None,
                status_code=response.status_code if response is not None else None,
            )

        if "dialog" in webplayback:
            raise GamdlApiResponseError(
                "Error fetching webplayback data",
                content=webplayback["dialog"],
            )

        log.debug("success", webplayback=webplayback)

        return webplayback

    async def get_license_exchange(
        self,
        track_id: str,
        track_uri: str,
        challenge: str,
        key_system: str = "com.widevine.alpha",
        is_library: bool = False,
    ) -> dict:
        log = logger.bind(action="get_license_exchange", track_id=track_id)

        response = None
        try:
            response = await self.client.post(
                APPLE_MUSIC_LICENSE_API_URL,
                json={
                    "challenge": challenge,
                    "key-system": key_system,
                    "uri": track_uri,
                    "adamId": track_id,
                    "isLibrary": is_library,
                    "user-initiated": True,
                },
            )
            response.raise_for_status()
            license_exchange = response.json()
        except httpx.HTTPError:
            raise GamdlApiResponseError(
                "Error fetching license exchange data",
                content=response.text if response is not None else None,
                status_code=response.status_code if response is not None else None,
            )

        if license_exchange.get("status") != 0:
            raise GamdlApiResponseError(
                "Error fetching license exchange data",
                content=response.text,
                status_code=response.status_code,
            )

        log.debug("success", license_exchange=license_exchange)

        return license_exchange
