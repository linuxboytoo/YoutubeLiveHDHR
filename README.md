# YoutubeLiveHDHR

Emulates an HDHomeRun network tuner so that Jellyfin (and any HDHR-compatible media server) can discover and play YouTube live streams as virtual TV channels.

## How it works

- Reads a `channels.yaml` config listing YouTube channels (by `@handle`) and optional groups
- Resolves handles to YouTube channel IDs at startup via yt-dlp
- Polls each channel on a configurable interval to detect active live streams
- Exposes the HDHomeRun HTTP API so Jellyfin auto-discovers it as a tuner
- Stream requests return a **302 redirect** to the direct stream URL — no proxying

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /discover.json` | HDHR device discovery |
| `GET /device.xml` | SSDP/UPnP descriptor |
| `GET /lineup_status.json` | Tuner status |
| `GET /lineup.json` | Channel list |
| `GET /auto/v{channel}` | Stream redirect (302) |
| `POST /reload` | Reload channels.yaml without restart |

## Quick start

```bash
cp config/channels.yaml /your/config/path/channels.yaml
# Edit channels.yaml with your YouTube channels

docker compose up -d
```

Then add an HDHomeRun tuner in Jellyfin pointing at `http://<host>:5004`.

## Configuration

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `HDHR_DEVICE_ID` | `12345678` | Unique 8-char hex device ID |
| `HDHR_DEVICE_AUTH` | _(empty)_ | Optional HDHR device auth string |
| `HDHR_FRIENDLY_NAME` | `YouTubeHDHR` | Name shown in Jellyfin |
| `POLL_INTERVAL` | `60` | Seconds between live-stream checks |
| `CONFIG_PATH` | `/config/channels.yaml` | Path to config file inside container |
| `PORT` | `5004` | HTTP port |

### channels.yaml

```yaml
channels:
  - id: 1001
    name: My Favorite Streamer
    youtube: "@handle"
    fallback:
      type: playlist          # or "url"
      url: "@handle/videos"   # shown when not live

groups:
  - id: 1000
    name: Best Live Right Now
    channels: [1001, 1002, 1003]   # priority order, first live one wins
    fallback:
      type: playlist
      url: "@somebackgroundchannel/videos"
```

**Groups** are virtual channels: the poller picks the highest-priority member that is currently live. If none are live, the group's own fallback is used.

## Requirements

- Docker (recommended) or Python 3.12+
- `ffmpeg` (bundled in Docker image)
- `yt-dlp` and `streamlink` (installed via requirements.txt)

## Notes

- YouTube handle → UC ID resolution happens at startup; failed channels are skipped with a warning (no crash)
- Config can be reloaded at runtime via `POST /reload`
- No stream data is proxied through this container — Jellyfin fetches directly from YouTube CDN

---

> This project was built with assistance from [Claude](https://claude.ai) (Anthropic).
