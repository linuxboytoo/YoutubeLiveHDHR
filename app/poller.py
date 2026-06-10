import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import _resolve_handle_to_uc
from app import guide
from app.models import AppConfig, ChannelConfig
from app.resolver import is_channel_live

logger = logging.getLogger(__name__)

live_state: dict[int, Optional[int]] = {}

_scheduler: Optional[BackgroundScheduler] = None
_config: Optional[AppConfig] = None


def _check_channel(channel: ChannelConfig) -> tuple[ChannelConfig, bool]:
    uc_id = channel.youtube
    if uc_id.startswith("@"):
        resolved = _resolve_handle_to_uc(uc_id)
        if resolved:
            channel.youtube = resolved
            logger.info("Late-resolved %s -> %s", uc_id, resolved)
        else:
            logger.warning("Still cannot resolve %s, skipping poll", uc_id)
            return channel, False
    return channel, is_channel_live(channel.youtube)


def _poll():
    if _config is None:
        return

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_check_channel, ch): ch for ch in _config.channels}
        for future in as_completed(futures):
            try:
                channel, is_live = future.result()
            except Exception as e:
                logger.warning("Poll error: %s", e)
                continue
            was_live = live_state.get(channel.id, False)
            live_state[channel.id] = is_live
            if is_live != was_live:
                logger.info("Channel %s (%s): %s", channel.id, channel.name, "LIVE" if is_live else "offline")

    # Groups: pick highest-priority live member
    for group in _config.groups:
        prev_active = live_state.get(group.id)
        active_cid = next((cid for cid in group.channels if live_state.get(cid)), None)
        live_state[group.id] = active_cid
        if active_cid != prev_active:
            logger.info("Group %s now using channel %s", group.id, active_cid)


def _fetch_logos(channels):
    for ch in channels:
        if not ch.youtube.startswith("@"):
            guide.fetch_channel_logo(ch.youtube)


def start(config: AppConfig, interval_seconds: int = 60):
    global _scheduler, _config
    _config = config

    threading.Thread(target=_fetch_logos, args=(config.channels,), daemon=True).start()
    threading.Thread(target=guide.refresh_all_programs, args=(config.channels,), daemon=True).start()

    _poll()  # immediate first run

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_poll, "interval", seconds=interval_seconds, id="poll_live")
    _scheduler.add_job(lambda: guide.refresh_all_programs(_config.channels),
                       "interval", minutes=15, id="poll_guide")
    _scheduler.start()
    logger.info("Poller started, interval=%ss", interval_seconds)


def reload(config: AppConfig):
    global _config
    _config = config
    logger.info("Poller config reloaded")


def stop():
    if _scheduler:
        _scheduler.shutdown(wait=False)
