from enum import Enum


class DownloadMode(Enum):
    YTDLP = "ytdlp"
    NM3U8DLRE = "nm3u8dlre"


class TranscodeCodec(Enum):
    NONE = "none"
    FLAC = "flac"


class RemuxMode(Enum):
    FFMPEG = "ffmpeg"
    MP4BOX = "mp4box"


class RemuxFormatMusicVideo(Enum):
    M4V = "m4v"
    MP4 = "mp4"
