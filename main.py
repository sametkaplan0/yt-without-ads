from __future__ import annotations
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse
import imageio_ffmpeg
import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    stream_with_context,
    url_for,
)

app = Flask(__name__)
comments_by_video: dict[str, list[dict[str, str]]] = {}
app.config.update(
    MAX_CONTENT_LENGTH=16 * 1024,
    SEARCH_CACHE_TTL=int(os.environ.get("SEARCH_CACHE_TTL", "60")),
    SEARCH_LIMIT=10,
    UPSTREAM_TIMEOUT=(10, 30),
)

QUALITY_VALUES = (144, 240, 360, 480, 720, 1080, 1440, 2160)
QUALITY_SET = set(QUALITY_VALUES)
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
ALLOWED_MEDIA_HOST_SUFFIXES = (".googlevideo.com",)
ALLOWED_THUMBNAIL_HOST_SUFFIXES = (".ytimg.com", ".youtube.com")
MAX_SEARCH_LENGTH = 200

YDL_SEARCH_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": True,
}
YDL_VIDEO_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "format": "best[acodec!=none][vcodec!=none]/18/best",
    "extractor_args": {"youtube": {"player_client": ["android"]}},
}


class TtlCache:
    def __init__(self, ttl: int, max_entries: int = 64) -> None:
        self.ttl = max(0, ttl)
        self.max_entries = max_entries
        self._items: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        if not self.ttl:
            return None
        with self._lock:
            item = self._items.get(key)
            if not item:
                return None
            expires, value = item
            if expires <= time.monotonic():
                self._items.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        if not self.ttl:
            return
        with self._lock:
            if len(self._items) >= self.max_entries:
                oldest = min(self._items, key=lambda entry: self._items[entry][0])
                self._items.pop(oldest, None)
            self._items[key] = (time.monotonic() + self.ttl, value)


search_cache = TtlCache(app.config["SEARCH_CACHE_TTL"])


def valid_video_id(video_id: str) -> bool:
    return isinstance(video_id, str) and bool(VIDEO_ID_RE.fullmatch(video_id))


def parse_quality(value: str | None) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    if not value.isdecimal() or int(value) not in QUALITY_SET:
        raise ValueError("Invalid video quality.")
    return int(value)


def normalize_query(value: str | None) -> str:
    query = " ".join((value or "").split())
    if len(query) > MAX_SEARCH_LENGTH:
        raise ValueError(f"Search terms must be {MAX_SEARCH_LENGTH} characters or fewer.")
    return query


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return ""
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def search_videos(query: str) -> list[dict[str, Any]]:
    cached = search_cache.get(query.casefold())
    if cached is not None:
        return cached
    with YoutubeDL(YDL_SEARCH_OPTIONS) as ydl:
        result = ydl.extract_info(f"ytsearch{app.config['SEARCH_LIMIT']}:{query}", download=False)

    videos = []
    for entry in (result.get("entries") or []):
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id")
        if not valid_video_id(video_id):
            continue
        videos.append(
            {
                "id": video_id,
                "title": entry.get("title") or "Untitled video",
                "thumbnail": thumbnail_url(entry.get("thumbnail"))
                or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                "channel": entry.get("channel") or entry.get("uploader"),
                "duration": entry.get("duration"),
                "duration_text": format_duration(entry.get("duration")),
            }
        )
    search_cache.set(query.casefold(), videos)
    return videos


def get_video(video_id: str, quality: int | None = None) -> dict[str, Any]:
    if quality == "":
        quality = None
    options = YDL_VIDEO_OPTIONS.copy()
    if quality is not None:
        options.pop("extractor_args", None)
        options["format"] = (
            f"bestvideo[height<={quality}]+bestaudio/"
            f"best[height<={quality}][acodec!=none][vcodec!=none]/"
            "best[acodec!=none][vcodec!=none]"
        )
    with YoutubeDL(options) as ydl:
        return ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False
        )


def media_url_is_safe(media_url: str | None) -> bool:
    if not media_url:
        return False
    try:
        parsed = urlparse(media_url)
    except (TypeError, ValueError):
        return False
    hostname = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and bool(hostname)
        and any(hostname.endswith(suffix) for suffix in ALLOWED_MEDIA_HOST_SUFFIXES)
    )


def thumbnail_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlparse(value)
    except (TypeError, ValueError):
        return None
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not hostname
        or not any(hostname.endswith(suffix) for suffix in ALLOWED_THUMBNAIL_HOST_SUFFIXES)
    ):
        return None
    return value


def _response_headers(upstream: requests.Response) -> dict[str, str]:
    allowed = (
        "Content-Type",
        "Content-Length",
        "Content-Range",
        "Accept-Ranges",
        "ETag",
        "Last-Modified",
    )
    return {name: upstream.headers[name] for name in allowed if name in upstream.headers}


def _merged_stream(video_format: dict[str, Any], audio_format: dict[str, Any]) -> Response:
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        video_format["url"],
        "-i",
        audio_format["url"],
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        "-movflags",
        "frag_keyframe+empty_moov",
        "-f",
        "mp4",
        "pipe:1",
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        app.logger.exception("Unable to start ffmpeg")
        return Response("The selected quality is temporarily unavailable.", status=502)

    def chunks() -> Iterator[bytes]:
        assert process.stdout is not None
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    return Response(
        stream_with_context(chunks()),
        content_type="video/mp4",
        headers={"Cache-Control": "no-store", "Accept-Ranges": "none"},
        direct_passthrough=True,
    )


def _proxy_direct_stream(media_url: str) -> Response:
    headers = {}
    range_header = request.headers.get("Range")
    if range_header:
        headers["Range"] = range_header
    if request.headers.get("If-Range"):
        headers["If-Range"] = request.headers["If-Range"]
    try:
        upstream = requests.get(
            media_url,
            headers=headers,
            stream=True,
            allow_redirects=False,
            timeout=app.config["UPSTREAM_TIMEOUT"],
        )
    except requests.RequestException:
        app.logger.exception("Media upstream request failed")
        return Response("The video stream could not be loaded.", status=502)

    if not 200 <= upstream.status_code < 300:
        upstream.close()
        return Response("The video stream could not be loaded.", status=502)

    response_headers = _response_headers(upstream)
    response_headers.setdefault("Content-Type", "video/mp4")
    response_headers["Cache-Control"] = "no-store"
    if request.method == "HEAD":
        upstream.close()
        return Response(status=upstream.status_code, headers=response_headers)

    def chunks() -> Iterator[bytes]:
        try:
            yield from upstream.iter_content(chunk_size=64 * 1024)
        finally:
            upstream.close()

    return Response(
        stream_with_context(chunks()),
        status=upstream.status_code,
        headers=response_headers,
        direct_passthrough=True,
    )


@app.after_request
def add_security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' https: data:; media-src 'self' https:; "
        "style-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    return response


@app.errorhandler(404)
def not_found(error: Any) -> Response:
    if request.path.startswith("/api/"):
        return jsonify(error="Not found"), 404
    return render_template("error.html", error="That page could not be found."), 404


@app.errorhandler(413)
def request_too_large(error: Any) -> Response:
    return render_template("error.html", error="That request is too large."), 413


@app.route("/")
def index() -> str:
    return render_template("index.html")


@app.route("/search")
def search() -> Response | str:
    try:
        query = normalize_query(request.args.get("q"))
    except ValueError as error:
        return render_template("search.html", query="", videos=[], error=str(error)), 400
    if not query:
        return render_template("search.html", query="", videos=[], error=None)

    try:
        videos = search_videos(query)
    except (DownloadError, requests.RequestException):
        app.logger.exception("YouTube search failed for query %r", query)
        return render_template(
            "search.html",
            query=query,
            videos=[],
            error="Search is temporarily unavailable. Please try again.",
        ), 502
    return render_template("search.html", query=query, videos=videos, error=None)


@app.route("/v")
def video() -> Response | str:
    video_id = request.args.get("id", "").strip()
    if not valid_video_id(video_id):
        return render_template(
            "video.html", video=None, error="A valid YouTube video id is required."
        ), 400
    try:
        quality = parse_quality(request.args.get("quality"))
    except ValueError as error:
        return render_template("video.html", video=None, error=str(error)), 400

    try:
        video_info = get_video(video_id, quality)
    except (DownloadError, requests.RequestException):
        app.logger.exception("YouTube video extraction failed for id %r", video_id)
        return render_template(
            "video.html",
            video=None,
            error="This video could not be loaded. Please try another one.",
        ), 502
    if not video_info.get("url") and not video_info.get("requested_formats"):
        return render_template(
            "video.html",
            video=None,
            error="No playable stream was found. Please try another video.",
        ), 502

    available_qualities = sorted(
        {
            int(format_info["height"])
            for format_info in (video_info.get("formats") or [])
            if isinstance(format_info, dict)
            if format_info.get("height") in QUALITY_SET
            and format_info.get("url")
            and format_info.get("vcodec") not in (None, "none")
        }
    )
    video_info.update(
        stream_url=url_for("stream", video_id=video_id, quality=quality),
        thumbnail=thumbnail_url(video_info.get("thumbnail")),
        available_qualities=available_qualities,
        selected_quality=quality or video_info.get("height"),
        duration_text=format_duration(video_info.get("duration")),
    )
    return render_template(
        "video.html",
        video=video_info,
        comments=comments_by_video.get(video_id, []),
        error=None,
    )


@app.post("/v/<video_id>/comments")
def add_comment(video_id: str) -> Response:
    if not valid_video_id(video_id):
        abort(400)
    text = " ".join(request.form.get("comment", "").split())
    if not text or len(text) > 1000:
        return Response("Comment must be between 1 and 1000 characters.", status=400)
    comments_by_video.setdefault(video_id, []).append({"text": text})
    return Response(status=303, headers={"Location": url_for("video", id=video_id)})


@app.route("/stream/<video_id>", methods=["GET", "HEAD"])
def stream(video_id: str) -> Response:
    if not valid_video_id(video_id):
        return Response("A valid YouTube video id is required.", status=400)
    try:
        quality = parse_quality(request.args.get("quality"))
    except ValueError as error:
        return Response(str(error), status=400)

    try:
        video_info = get_video(video_id, quality)
    except (DownloadError, requests.RequestException):
        app.logger.exception("YouTube stream extraction failed for id %r", video_id)
        return Response("The video stream could not be loaded.", status=502)

    requested_formats = video_info.get("requested_formats") or []
    if requested_formats and (quality is not None or not video_info.get("url")):
        video_format = next(
            (
                item
                for item in requested_formats
                if isinstance(item, dict) and item.get("vcodec") not in (None, "none")
            ),
            None,
        )
        audio_format = next(
            (
                item
                for item in requested_formats
                if isinstance(item, dict) and item.get("acodec") not in (None, "none")
            ),
            None,
        )
        if not video_format or not audio_format:
            return Response("No playable audio and video streams were found.", status=502)
        if not media_url_is_safe(video_format.get("url")) or not media_url_is_safe(
            audio_format.get("url")
        ):
            return Response("The video stream could not be loaded.", status=502)
        if request.method == "HEAD":
            return Response(
                status=200,
                headers={
                    "Content-Type": "video/mp4",
                    "Cache-Control": "no-store",
                    "Accept-Ranges": "none",
                },
            )
        return _merged_stream(video_format, audio_format)

    media_url = video_info.get("url")
    if not media_url_is_safe(media_url):
        return Response("No playable video stream was found.", status=502)
    return _proxy_direct_stream(media_url)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes"},
    )
