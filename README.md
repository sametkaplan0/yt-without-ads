# yt-without-ads

A small, privacy-friendly Flask application for searching YouTube and playing
videos in a focused HTML5 player. `yt-dlp` performs extraction on the server;
the app proxies short-lived media URLs so signed YouTube URLs are not put in
the page source. Progressive formats are streamed directly and separate video
and audio tracks are merged with the bundled `ffmpeg` from `imageio-ffmpeg`.

## Run locally

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

Open <http://127.0.0.1:5000>. For a LAN-only development server, set
`HOST=0.0.0.0`; do not expose this development server directly to the
internet.

## Features

- Search results with thumbnails, channel names, and durations
- Quality selection from available 144p–2160p formats
- HTTP range forwarding for progressive playback
- On-demand ffmpeg remuxing for separate video and audio streams
- Bounded, short-lived search cache (disable with `SEARCH_CACHE_TTL=0`)
- Input validation, upstream-host allowlisting, request timeouts, safe process
  cleanup, security headers, and friendly error pages
- Responsive, keyboard-friendly UI with no client-side tracking scripts

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Bind address |
| `PORT` | `5000` | Listening port |
| `FLASK_DEBUG` | disabled | Enable Flask debug mode for local development only |
| `LOG_LEVEL` | `INFO` | Python log level |
| `SEARCH_CACHE_TTL` | `60` | Search cache lifetime in seconds |

YouTube availability, age restrictions, rate limits, and format support can
change independently of this project. The service does not download or retain
media, and dynamically remuxed streams cannot provide byte-range seeking.

## Smoke check

With dependencies installed:

```powershell
python -m unittest discover -s tests -v
```
