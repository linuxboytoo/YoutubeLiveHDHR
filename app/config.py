import json
import logging
import os
import re
import subprocess
from typing import Optional

import yaml

from app.models import AppConfig, ChannelConfig, FallbackConfig, GroupConfig

logger = logging.getLogger(__name__)

CONFIG_PATH = os.getenv("CONFIG_PATH", "/config/channels.yaml")
_CACHE_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "handle_cache.json")

# Internal caches: @handle -> UC ID or PL ID
_handle_cache: dict[str, str] = {}
_playlist_cache: dict[str, str] = {}


def _load_cache():
    try:
        with open(_CACHE_PATH) as f:
            data = json.load(f)
        _handle_cache.update(data.get("handles", {}))
        _playlist_cache.update(data.get("playlists", {}))
        logger.info("Loaded %d cached handle(s) from %s", len(_handle_cache), _CACHE_PATH)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Could not load handle cache: %s", e)


def _save_cache():
    try:
        with open(_CACHE_PATH, "w") as f:
            json.dump({"handles": _handle_cache, "playlists": _playlist_cache}, f, indent=2)
    except Exception as e:
        logger.warning("Could not save handle cache: %s", e)


def _resolve_handle_to_uc(handle: str) -> Optional[str]:
    """Resolve a YouTube @handle to a UC channel ID using yt-dlp."""
    if handle in _handle_cache:
        return _handle_cache[handle]

    url = f"https://www.youtube.com/{handle}"
    for attempt in range(3):
        try:
            result = subprocess.run(
                ["yt-dlp", "--playlist-items", "1", "--print", "channel_id", url],
                capture_output=True, text=True, timeout=60
            )
            uc_id = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
            if uc_id and uc_id.startswith("UC"):
                _handle_cache[handle] = uc_id
                _save_cache()
                logger.info("Resolved %s -> %s", handle, uc_id)
                return uc_id
            logger.warning("Could not resolve handle %s: %s", handle, result.stderr.strip())
            break
        except subprocess.TimeoutExpired:
            logger.warning("Timeout resolving handle %s (attempt %d/3)", handle, attempt + 1)
        except Exception as e:
            logger.warning("Error resolving handle %s: %s", handle, e)
            break
    return None


def _resolve_playlist_handle(url_or_handle: str) -> Optional[str]:
    """Resolve a playlist handle/URL to a PL ID."""
    if url_or_handle in _playlist_cache:
        return _playlist_cache[url_or_handle]

    # If it's already a PL ID, return as-is
    if re.match(r"^PL[A-Za-z0-9_-]+$", url_or_handle):
        return url_or_handle

    url = url_or_handle if url_or_handle.startswith("http") else f"https://www.youtube.com/{url_or_handle}"
    try:
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--print", "playlist_id", "--playlist-items", "1", url],
            capture_output=True, text=True, timeout=30
        )
        pl_id = result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
        if pl_id:
            _playlist_cache[url_or_handle] = pl_id
            _save_cache()
            logger.info("Resolved playlist %s -> %s", url_or_handle, pl_id)
            return pl_id
        logger.warning("Could not resolve playlist %s: %s", url_or_handle, result.stderr.strip())
    except Exception as e:
        logger.warning("Error resolving playlist %s: %s", url_or_handle, e)
    return None


def load_config() -> AppConfig:
    """Load and validate channels.yaml, resolving handles."""
    _load_cache()
    with open(CONFIG_PATH) as f:
        raw = yaml.safe_load(f)

    channels = []
    for ch in raw.get("channels", []):
        fallback = None
        if "fallback" in ch:
            fallback = FallbackConfig(**ch["fallback"])
            if fallback.type == "playlist":
                resolved = _resolve_playlist_handle(fallback.url)
                if resolved:
                    fallback = FallbackConfig(type="playlist", url=f"https://www.youtube.com/playlist?list={resolved}")

        handle = ch["youtube"]
        uc_id = _resolve_handle_to_uc(handle) if handle.startswith("@") else handle
        if not uc_id:
            # Keep the raw handle; poller will retry resolution each cycle
            logger.warning("Could not resolve handle %s for channel %s — will retry during polling", handle, ch["id"])
            uc_id = handle

        channels.append(ChannelConfig(
            id=ch["id"],
            name=ch["name"],
            youtube=uc_id,
            fallback=fallback,
        ))

    groups = []
    for grp in raw.get("groups", []):
        fallback = None
        if "fallback" in grp:
            fallback = FallbackConfig(**grp["fallback"])
            if fallback.type == "playlist":
                resolved = _resolve_playlist_handle(fallback.url)
                if resolved:
                    fallback = FallbackConfig(type="playlist", url=f"https://www.youtube.com/playlist?list={resolved}")

        groups.append(GroupConfig(
            id=grp["id"],
            name=grp["name"],
            channels=grp["channels"],
            fallback=fallback,
        ))

    return AppConfig(channels=channels, groups=groups)
