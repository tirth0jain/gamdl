from pathlib import Path

import structlog

from ..interface.enums import CoverFormat
from ..interface.types import AppleMusicMedia, DecryptionKeyAv, StreamInfoAv
from ..utils import async_subprocess
from .ammuxer import decrypt_and_mux_hex, decrypt_and_mux_wrapper
from .base import AppleMusicBaseDownloader
from .enums import TranscodeCodec
from .exceptions import GamdlDownloaderDependencyNotFoundError
from .types import DownloadItem

logger = structlog.get_logger(__name__)


class AppleMusicSongDownloader:
    def __init__(
        self,
        base: AppleMusicBaseDownloader,
        transcode_codec: TranscodeCodec = TranscodeCodec.NONE,
    ):
        self.base = base
        self.transcode_codec = transcode_codec

    async def get_download_item(self, media: AppleMusicMedia) -> DownloadItem:
        download_item = DownloadItem(media)

        if media.stream_info:
            download_item.staged_path = self.base.get_temp_path(
                media.media_metadata["id"],
                download_item.uuid_,
                "staged",
                "." + media.stream_info.file_format.value,
            )

        download_item.final_path = self.base.get_final_path(
            media.tags,
            (
                "." + self.transcode_codec.value
                if self._should_transcode(media.stream_info)
                else ".m4a"
            ),
            media.playlist_tags,
        )

        if media.playlist_tags:
            download_item.playlist_file_path = self.base.get_playlist_file_path(
                media.playlist_tags,
            )

        download_item.synced_lyrics_path = self.get_synced_lyrics_path(
            download_item.final_path
        )

        download_item.cover_path = self.get_cover_path(
            download_item.final_path,
            media.cover.file_extension,
        )

        return download_item

    async def _decrypt_ammuxer(
        self,
        input_path: str,
        output_path: str,
        media_id: str,
        fairplay_key: str,
        use_single_content_key: bool = False,
    ) -> None:
        wrapper_api = self.base.interface.base.wrapper_api
        if wrapper_api is None:
            raise ValueError("wrapper_api is required for FairPlay decrypt")

        await decrypt_and_mux_wrapper(
            wrapper_api,
            media_id,
            input_path,
            output_path,
            fairplay_key_audio=fairplay_key,
            use_single_content_key=use_single_content_key,
        )

    async def _decrypt_ammuxer_hex(
        self,
        input_path: str,
        output_path: str,
        decryption_key: str,
        *,
        use_cenc: bool = False,
        use_single_content_key: bool = False,
    ) -> None:
        await decrypt_and_mux_hex(
            decryption_key,
            input_path,
            output_path,
            use_cenc=use_cenc,
            use_single_content_key=use_single_content_key,
        )

    async def stage(
        self,
        encrypted_path: str,
        staged_path: str,
        media_id: str,
        decryption_key: DecryptionKeyAv | None = None,
        fairplay_key: str = None,
        use_cenc: bool = False,
        use_single_content_key: bool = False,
    ):
        log = logger.bind(
            action="stage_song",
            media_id=media_id,
            encrypted_path=encrypted_path,
            staged_path=staged_path,
        )

        if decryption_key:
            await self._decrypt_ammuxer_hex(
                encrypted_path,
                staged_path,
                decryption_key.audio_track.key,
                use_cenc=use_cenc,
                use_single_content_key=use_single_content_key,
            )
        else:
            await self._decrypt_ammuxer(
                encrypted_path,
                staged_path,
                media_id,
                fairplay_key,
                use_single_content_key=use_single_content_key,
            )

        log.debug("success")

    def get_synced_lyrics_path(self, final_path: str) -> str:
        log = logger.bind(action="get_synced_lyrics_path", final_path=final_path)

        synced_lyrics_path = str(
            Path(final_path).with_suffix(
                "." + self.base.interface.song.synced_lyrics_format.value
            )
        )

        log.debug("success", synced_lyrics_path=synced_lyrics_path)

        return synced_lyrics_path

    def get_cover_path(
        self,
        final_path: str,
        file_extension: str,
    ) -> str:
        log = logger.bind(
            action="get_song_cover_path",
            final_path=final_path,
            file_extension=file_extension,
        )

        cover_path = str(Path(final_path).parent / ("Cover" + file_extension))

        log.debug("success", cover_path=cover_path)

        return cover_path

    def _should_transcode(self, stream_info: StreamInfoAv | None) -> bool:
        if self.transcode_codec == TranscodeCodec.NONE or not stream_info:
            return False

        audio_codec = stream_info.audio_track.codec
        return bool(audio_codec) and audio_codec.startswith("alac")

    async def _transcode(
        self,
        input_path: str,
        output_path: str,
    ) -> None:
        log = logger.bind(
            action="transcode_song",
            input_path=input_path,
            output_path=output_path,
            transcode_codec=self.transcode_codec.value,
        )

        await async_subprocess(
            self.base.full_ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            input_path,
            "-vn",
            "-c:a",
            self.transcode_codec.value,
            "-compression_level",
            "8",
            output_path,
            silent=self.base.silent,
        )

        log.debug("success")

    async def download(
        self,
        download_item: DownloadItem,
    ) -> None:
        if download_item.media.stream_info.audio_track.drm_free:
            await self.base.download_stream(
                download_item.media.stream_info.audio_track.stream_url,
                download_item.staged_path,
            )
        else:
            encrypted_path = self.base.get_temp_path(
                download_item.media.media_metadata["id"],
                download_item.uuid_,
                "encrypted",
                ".m4a",
            )
            await self.base.download_stream(
                download_item.media.stream_info.audio_track.stream_url,
                encrypted_path,
            )

            await self.stage(
                encrypted_path,
                download_item.staged_path,
                download_item.media.media_id,
                download_item.media.decryption_key,
                download_item.media.stream_info.audio_track.fairplay_key,
                download_item.media.stream_info.audio_track.use_cenc,
                download_item.media.stream_info.audio_track.use_single_content_key,
            )

        if self._should_transcode(download_item.media.stream_info):
            if not self.base.full_ffmpeg_path:
                raise GamdlDownloaderDependencyNotFoundError("FFmpeg")

            transcoded_path = self.base.get_temp_path(
                download_item.media.media_metadata["id"],
                download_item.uuid_,
                "transcoded",
                "." + self.transcode_codec.value,
            )
            await self._transcode(
                download_item.staged_path,
                transcoded_path,
            )
            download_item.staged_path = transcoded_path
        elif self.transcode_codec != TranscodeCodec.NONE:
            logger.debug(
                "skip_transcode_non_lossless_source",
                audio_codec=(
                    download_item.media.stream_info.audio_track.codec
                    if download_item.media.stream_info
                    else None
                ),
            )

        cover_bytes = (
            await self.base.interface.base.get_cover_bytes(
                download_item.media.cover.url
            )
            if self.base.interface.base.cover_format != CoverFormat.RAW
            else None
        )
        if self._should_transcode(download_item.media.stream_info):
            await self.base.apply_flac_tags(
                download_item.staged_path,
                download_item.media.tags,
                cover_bytes,
            )
        else:
            await self.base.apply_tags(
                download_item.staged_path,
                download_item.media.tags,
                cover_bytes,
            )
