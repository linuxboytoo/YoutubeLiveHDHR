import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


def is_channel_live(uc_id: str) -> bool:
    """Fast check — is the channel currently live? Does not resolve CDN URL."""
    channel_url = f"https://www.youtube.com/channel/{uc_id}/live"
    try:
        result = subprocess.run(
            ["yt-dlp", "--playlist-items", "1", "--print", "is_live",
             "--no-warnings", channel_url],
            capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip().lower() == "true"
    except Exception as e:
        logger.warning("Error checking live for %s: %s", uc_id, e)
        return False


def get_live_stream_url(uc_id: str) -> Optional[str]:
    """Resolve the CDN stream URL for a live channel (call on demand, not during polling)."""
    channel_url = f"https://www.youtube.com/channel/{uc_id}/live"
    try:
        result = subprocess.run(
            ["yt-dlp", "--no-playlist", "--format", "best[protocol^=m3u8]/best",
             "--get-url", channel_url],
            capture_output=True, text=True, timeout=60
        )
        url = result.stdout.strip()
        if url and url.startswith("http"):
            return url
        return None
    except Exception as e:
        logger.warning("Error resolving stream for %s: %s", uc_id, e)
        return None


def get_stream_url(source_url: str) -> Optional[str]:
    """Resolve any YouTube URL (live, video, playlist) to a direct stream URL."""
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--no-playlist",
                "--format", "best[protocol^=m3u8]/best",
                "--get-url",
                source_url,
            ],
            capture_output=True, text=True, timeout=60
        )
        url = result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
        if url and url.startswith("http"):
            return url
        logger.warning("No stream URL for %s: %s", source_url, result.stderr.strip())
    except Exception as e:
        logger.warning("Error resolving stream %s: %s", source_url, e)
    return None
