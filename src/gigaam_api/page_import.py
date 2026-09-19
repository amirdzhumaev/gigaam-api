"""Bounded public YouTube/VK imports; extractor traffic uses an IP-pinning proxy.

Only metadata extraction runs in the child. Media always passes through the same
size-limited downloader as direct URLs. The child is not a general OS sandbox:
it runs pinned, trusted yt-dlp code, without inherited credentials or plugins.
"""

import base64
import contextlib
import hmac
import json
import math
import os
import re
import secrets
import select
import selectors
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

from .download import download_public, public_addresses, validate_url

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
VK_HOSTS = {
    f"{prefix}{domain}"
    for prefix in ("", "www.", "m.", "new.")
    for domain in ("vk.com", "vk.ru", "vkvideo.ru")
}
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
VK_ID = re.compile(r"/(?:video|clip)(-?\d+_\d+)(?:/.*)?\Z")
MEDIA_EXTENSIONS = {"m4a", "mp4", "webm", "mp3", "ogg", "opus", "wav", "flac", "aac"}
MAX_METADATA_BYTES = 2 * 1024 * 1024
IMPORT_TIMEOUT = 90


class PageImportError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical_page(url: str):
    """Return a canonical single-video URL or None for ordinary direct URLs."""
    if len(url) > 4096:
        raise PageImportError("unsupported_source")
    parsed = validate_url(url)
    query = parse_qs(parsed.query)
    if parsed.hostname in YOUTUBE_HOSTS:
        video_id = None
        if parsed.hostname == "youtu.be":
            video_id = parsed.path.removeprefix("/")
        elif parsed.path == "/watch" and len(query.get("v", [])) == 1:
            video_id = query["v"][0]
        elif parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
            video_id = parsed.path.split("/")[-1]
        if not video_id or not VIDEO_ID.fullmatch(video_id):
            raise PageImportError("unsupported_source")
        return f"https://www.youtube.com/watch?v={video_id}"
    if parsed.hostname in VK_HOSTS:
        # Only IDs survive canonicalisation; access keys, playlists and arbitrary
        # URLs from the query never reach the extractor.
        path = parsed.path
        if len(query.get("z", [])) == 1:
            path = "/" + query["z"][0].lstrip("/")
        matched = VK_ID.fullmatch(path)
        if not matched:
            raise PageImportError("unsupported_source")
        return f"https://vkvideo.ru/video{matched[1]}"
    return None


def public_dial(host, port, *, timeout=5):
    address = public_addresses(host, port)[0]
    # Connecting to the numeric address avoids a second hostname resolution.
    return socket.create_connection((address, port), timeout=timeout)


class _Proxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, timeout, max_bytes, max_requests):
        self.deadline = time.monotonic() + timeout
        self.remaining = max_bytes
        self.requests_remaining = max_requests
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.slots = threading.BoundedSemaphore(8)
        secret = secrets.token_hex(24)
        self.authorization = "Basic " + base64.b64encode(f"import:{secret}".encode()).decode()
        super().__init__(("127.0.0.1", 0), _ProxyHandler)
        self.url = f"http://import:{secret}@127.0.0.1:{self.server_address[1]}"

    def check(self, size=0, *, request=False):
        with self.lock:
            self.remaining -= size
            self.requests_remaining -= int(request)
            if (
                self.stop.is_set()
                or time.monotonic() >= self.deadline
                or self.remaining < 0
                or self.requests_remaining < 0
            ):
                raise PageImportError("page_import_limit")

    def handle_error(self, request, client_address):
        # The stdlib default prints request data and exception details.
        pass

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class _ProxyHandler(socketserver.StreamRequestHandler):
    rbufsize = 0

    def handle(self):
        upstream = None
        tunnel_started = False
        self.connection.settimeout(5)
        try:
            self.server.check(request=True)
            # Deliberately small bounded parser: one request per connection,
            # no chunked request bodies, upgrades, or ambiguous duplicate fields.
            lines, total = [], 0
            for _ in range(65):
                line = self.rfile.readline(8193)
                total += len(line)
                if not line.endswith(b"\r\n") or len(line) > 8192 or total > 32768:
                    raise ValueError("invalid_proxy_request")
                if line == b"\r\n":
                    break
                lines.append(line[:-2].decode("ascii"))
            else:
                raise ValueError("invalid_proxy_request")
            method, target, version = lines[0].split(" ")
            if version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise ValueError("invalid_proxy_request")
            headers = {}
            for line in lines[1:]:
                key, value = line.split(":", 1)
                key, value = key.lower(), value.strip()
                if not re.fullmatch(r"[a-z0-9-]+", key) or key in headers or any(ord(c) < 32 for c in value):
                    raise ValueError("invalid_proxy_request")
                headers[key] = value
            if not hmac.compare_digest(headers.get("proxy-authorization", ""), self.server.authorization):
                raise ValueError("proxy_auth_required")
            if "transfer-encoding" in headers or "upgrade" in headers:
                raise ValueError("invalid_proxy_request")
            length = int(headers.get("content-length", "0"))
            if not 0 <= length <= 2 * 1024 * 1024:
                raise ValueError("invalid_proxy_request")
            if method == "CONNECT":
                parsed = validate_url("https://" + target)
                if parsed.port != 443 or parsed.path or parsed.query or parsed.fragment or length:
                    raise ValueError("invalid_proxy_target")
                upstream = public_dial(parsed.hostname, 443)
                self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                tunnel_started = True
                self._relay(upstream)
            else:
                parsed = validate_url(target)
                if (
                    method not in {"GET", "HEAD", "POST"}
                    or parsed.scheme != "http"
                    or parsed.port not in {None, 80}
                ):
                    raise ValueError("invalid_proxy_target")
                upstream = public_dial(parsed.hostname, 80)
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                clean_headers = {
                    k: v
                    for k, v in headers.items()
                    if k
                    not in {
                        "proxy-authorization",
                        "proxy-connection",
                        "connection",
                        "host",
                        "expect",
                    }
                }
                clean_headers.update({"host": parsed.netloc, "connection": "close"})
                head = f"{method} {path} HTTP/1.1\r\n" + "".join(
                    f"{k}: {v}\r\n" for k, v in clean_headers.items()
                )
                data = head.encode("ascii") + b"\r\n"
                self.server.check(len(data))
                upstream.sendall(data)
                while length:
                    data = self.rfile.read(min(length, 65536))
                    if not data:
                        raise ValueError("incomplete_proxy_request")
                    length -= len(data)
                    self.server.check(len(data))
                    upstream.sendall(data)
                # HTTP redirects are returned to yt-dlp. The next proxy request
                # performs a fresh address check; they are never followed here.
                tunnel_started = True
                self._relay(upstream, response_only=True)
        except (OSError, ValueError, IndexError):
            if not tunnel_started:
                with contextlib.suppress(OSError):
                    self.connection.sendall(
                        b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                    )
        finally:
            if upstream is not None:
                upstream.close()

    def _relay(self, upstream, *, response_only=False):
        upstream.settimeout(1)
        self.connection.settimeout(1)
        sources = [upstream] if response_only else [upstream, self.connection]
        while True:
            self.server.check()
            ready, _, _ = select.select(sources, [], [], 0.2)
            for source in ready:
                data = source.recv(65536)
                if not data:
                    return
                self.server.check(len(data))
                (self.connection if source is upstream else upstream).sendall(data)


@contextlib.contextmanager
def public_proxy(*, timeout=IMPORT_TIMEOUT, max_bytes=32 * 1024 * 1024, max_requests=64):
    server = _Proxy(timeout, max_bytes, max_requests)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.1), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def extractor_command(url, proxy_url):
    deno = Path(sys.executable).parent / "deno"
    return [
        sys.executable,
        "-I",
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-config-locations",
        "--no-plugin-dirs",
        "--no-remote-components",
        "--no-cookies",
        "--no-cookies-from-browser",
        "--no-cache-dir",
        "--no-exec",
        "--no-progress",
        "--no-warnings",
        "--skip-download",
        "--dump-single-json",
        "--no-playlist",
        "--use-extractors",
        "youtube$,vk$",
        "--socket-timeout",
        "10",
        "--retries",
        "0",
        "--extractor-retries",
        "0",
        "--no-js-runtimes",
        "--js-runtimes",
        f"deno:{deno}",
        "--proxy",
        proxy_url,
        "--ignore-no-formats-error",
        "--format",
        "bestaudio[protocol=https]/best[protocol=https]",
        "--",
        url,
    ]


def child_environment(root, proxy_url=None):
    # Never inherit credentials, proxy overrides, PYTHONPATH or runtime flags.
    # urllib on macOS consults system proxy bypass settings when the proxy
    # environment is empty. Explicit proxy variables avoid that fallback.
    proxy_url = proxy_url or "http://127.0.0.1:9"
    return {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "HOME": str(root),
        "TMPDIR": str(root),
        "XDG_CACHE_HOME": str(root),
        "DENO_DIR": str(root / "deno"),
        "DENO_NO_UPDATE_CHECK": "1",
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "ALL_PROXY": proxy_url,
        "NO_PROXY": "",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def run_extractor(command, root, *, proxy_url=None, timeout=IMPORT_TIMEOUT, max_bytes=MAX_METADATA_BYTES):
    """Drain both pipes with a hard byte/time cap; never log upstream output."""
    with subprocess.Popen(
        command,
        cwd=root,
        env=child_environment(root, proxy_url),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        output, total = bytearray(), 0
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, True)
                selector.register(process.stderr, selectors.EVENT_READ, False)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise PageImportError("page_import_timeout")
                    for key, _ in selector.select(timeout=min(0.2, max(0, deadline - time.monotonic()))):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(data)
                        if total > max_bytes:
                            raise PageImportError("page_import_limit")
                        if key.data:
                            output.extend(data)
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if process.returncode:
                raise PageImportError("page_import_failed")
            try:
                return json.loads(output)
            except (ValueError, UnicodeError) as exc:
                raise PageImportError("page_import_failed") from exc
        except subprocess.TimeoutExpired as exc:
            raise PageImportError("page_import_timeout") from exc
        finally:
            # Stop Deno children too if a timeout or malformed output occurs.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def select_media(info, *, max_bytes, max_duration):
    if not isinstance(info, dict) or info.get("_type", "video") != "video" or "entries" in info:
        raise PageImportError("unsupported_source")
    if info.get("extractor_key") not in {"Youtube", "VK"}:
        raise PageImportError("unsupported_source")
    if (
        info.get("is_live")
        or info.get("live_status") not in {None, "not_live", "was_live"}
        or info.get("has_drm")
    ):
        raise PageImportError("unsupported_source")
    duration = info.get("duration")
    if (
        not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or not 0 < duration <= max_duration
    ):
        raise PageImportError("unsupported_duration")
    candidates = []
    formats = info.get("formats")
    if not isinstance(formats, list):
        raise PageImportError("unsupported_video_format")
    for item in formats:
        if not isinstance(item, dict) or item.get("protocol") != "https" or item.get("has_drm"):
            continue
        if item.get("fragments") or item.get("manifest_url") or item.get("ext") not in MEDIA_EXTENSIONS:
            continue
        # VK progressive MP4 metadata omits codecs. Reject explicitly silent
        # streams; ffmpeg validates whether an unknown-codec file has audio.
        if item.get("acodec") == "none":
            continue
        size = item.get("filesize")
        if size is not None and (not isinstance(size, (int, float)) or not 0 < size <= max_bytes):
            continue
        try:
            parsed = validate_url(item.get("url", ""))
            if (
                parsed.scheme != "https"
                or parsed.port not in {None, 443}
                or parsed.path.endswith((".m3u8", ".mpd"))
            ):
                continue
        except (TypeError, ValueError):
            continue
        # Audio first, then a small video. ASR does not benefit from high-resolution video.
        audio_only = item.get("vcodec") == "none"

        def number(name):
            value = item.get(name)
            return value if isinstance(value, (int, float)) and math.isfinite(value) else 0

        candidates.append(((audio_only, number("abr") if audio_only else -number("height")), item))
    if not candidates:
        raise PageImportError("unsupported_video_format")
    selected = max(candidates, key=lambda value: value[0])[1]
    headers = {}
    for source in (info.get("http_headers", {}), selected.get("http_headers", {})):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if key.lower() in {"user-agent", "referer", "origin"} and isinstance(value, str):
                if len(value) <= 2048 and not any(ord(char) < 32 or ord(char) > 126 for char in value):
                    headers[key.lower()] = value
    return selected["url"], headers


def download_source(url, destination, limit):
    canonical = canonical_page(url)
    if canonical is None:
        return download_public(url, destination, limit)
    with public_proxy() as proxy:
        metadata = run_extractor(
            extractor_command(canonical, proxy.url), destination.parent, proxy_url=proxy.url
        )
    media_url, headers = select_media(
        metadata, max_bytes=limit, max_duration=float(os.environ.get("MAX_AUDIO_SECONDS", "14400"))
    )
    return download_public(media_url, destination, limit, headers=headers)
