"""Direct public media URLs only. Pin the validated IP to prevent DNS rebinding."""

import http.client
import ipaddress
import math
import os
import re
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit


class DownloadError(ValueError):
    """Safe source-transfer failure code, separate from invalid audio."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def download_timeout():
    value = float(os.environ.get("MEDIA_DOWNLOAD_TIMEOUT", "1800"))
    if not math.isfinite(value) or not 1 <= value <= 3600:
        raise ValueError("MEDIA_DOWNLOAD_TIMEOUT must be between 1 and 3600 seconds")
    return value


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Нужна публичная HTTP(S)-ссылка без логина и пароля")
    if parsed.port not in {None, 80, 443} or any(ord(c) < 32 for c in url) or "\\" in url:
        raise ValueError("Недопустимый URL")
    return parsed


def public_addresses(host, port):
    addresses = list(
        dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
    )
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError("Ссылки на локальные, служебные и частные адреса запрещены")
    return addresses


def _remaining_timeout(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DownloadError("download_timeout")
    return min(20, remaining)


def _response_length(response):
    length = response.getheader("Content-Length")
    if length is None:
        return None
    if not re.fullmatch(r"[0-9]+", length):
        raise DownloadError("download_failed")
    return int(length)


def _copy_response(response, sock, output, size, limit, deadline, expected):
    received = 0
    while True:
        sock.settimeout(_remaining_timeout(deadline))
        # read1 returns available data without waiting to fill a large buffer.
        chunk = response.read1(256 * 1024)
        _remaining_timeout(deadline)
        if not chunk:
            break
        size += len(chunk)
        received += len(chunk)
        if size > limit:
            raise DownloadError("download_too_large")
        if expected is not None and received > expected:
            raise DownloadError("download_failed")
        output.write(chunk)
    if expected is not None and received != expected:
        raise DownloadError("download_failed")
    return size


def _googlevideo_host(hostname):
    return re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.googlevideo\.com", hostname) is not None


def _googlevideo_tls(sock, hostname):
    """Omit CDN SNI but still require a trusted chain and the original DNS name."""
    if not _googlevideo_host(hostname):
        raise ValueError("SNI fallback is restricted to Google video hosts")
    context = ssl.create_default_context()
    context.check_hostname = False
    # OpenSSL still verifies the complete chain, validity and server purpose.
    secured = context.wrap_socket(sock)
    names = {name.lower() for kind, name in secured.getpeercert().get("subjectAltName", ()) if kind == "DNS"}
    # Only an exact name or this single-label wildcard can identify this CDN.
    if hostname not in names and "*.googlevideo.com" not in names:
        secured.close()
        raise ssl.SSLCertVerificationError("Certificate does not identify the requested CDN host")
    return secured


def download_public(url: str, destination: Path, limit: int, *, headers=None, chunk_bytes=None):
    """Pin every request, optionally fetching sequential validated byte ranges."""
    if chunk_bytes is not None and (not isinstance(chunk_bytes, int) or chunk_bytes <= 0):
        raise ValueError("Invalid download chunk size")
    request_headers = {"User-Agent": "gigaam-api/0.1", "Accept-Encoding": "identity"}
    for key, value in (headers or {}).items():
        if key.lower() not in {"user-agent", "referer", "origin"} or not isinstance(value, str):
            raise ValueError("Недопустимый заголовок источника")
        if len(value) > 2048 or any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ValueError("Недопустимый заголовок источника")
        request_headers[key.title()] = value
    deadline = time.monotonic() + download_timeout()
    size, total, redirects = 0, None, 0
    without_sni = set()
    with destination.open("wb") as output:
        while True:
            _remaining_timeout(deadline)
            parsed = validate_url(url)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            address = public_addresses(parsed.hostname, port)[0]
            conn = http.client.HTTPConnection(parsed.hostname, port, timeout=20)
            try:
                # Connect to the checked IP; TLS verifies the original hostname.
                sock = socket.create_connection((address, port), timeout=_remaining_timeout(deadline))
                conn.sock = sock
                if parsed.scheme == "https":
                    if parsed.hostname in without_sni:
                        sock = _googlevideo_tls(sock, parsed.hostname)
                    else:
                        try:
                            sock = ssl.create_default_context().wrap_socket(
                                sock, server_hostname=parsed.hostname
                            )
                        except TimeoutError:
                            if not _googlevideo_host(parsed.hostname):
                                raise
                            conn.close()
                            sock = socket.create_connection(
                                (address, port), timeout=_remaining_timeout(deadline)
                            )
                            conn.sock = sock
                            sock = _googlevideo_tls(sock, parsed.hostname)
                            without_sni.add(parsed.hostname)
                    conn.sock = sock
                sock.settimeout(_remaining_timeout(deadline))
                target = parsed.path or "/"
                if parsed.query:
                    target += "?" + parsed.query
                outgoing = request_headers.copy()
                if chunk_bytes is not None:
                    outgoing["Range"] = f"bytes={size}-{size + chunk_bytes - 1}"
                conn.request("GET", target, headers=outgoing)
                response = conn.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise ValueError("Некорректное перенаправление")
                    redirects += 1
                    if redirects > 5:
                        raise ValueError("Слишком много перенаправлений")
                    url = urljoin(url, location)
                    continue
                expected = _response_length(response)
                if chunk_bytes is not None and response.status == 206:
                    matched = re.fullmatch(
                        r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", response.getheader("Content-Range", "")
                    )
                    if not matched:
                        raise DownloadError("download_failed")
                    start, end, current_total = map(int, matched.groups())
                    if (
                        start != size
                        or not start <= end < current_total
                        or end >= start + chunk_bytes
                        or (total is not None and total != current_total)
                        or (expected is not None and expected != end - start + 1)
                    ):
                        raise DownloadError("download_failed")
                    total, expected = current_total, end - start + 1
                    if total > limit:
                        raise DownloadError("download_too_large")
                elif response.status != 200 or size:
                    # A server may ignore the first Range; never append a full body.
                    raise DownloadError("download_failed")
                elif expected is not None and expected > limit:
                    raise DownloadError("download_too_large")
                content_type = response.getheader("Content-Type", "").lower()
                if "text/html" in content_type or "application/json" in content_type:
                    raise ValueError("Поддерживаются прямые ссылки на файлы, а не страницы сайтов")
                size = _copy_response(response, sock, output, size, limit, deadline, expected)
                if not size:
                    raise ValueError("Источник вернул пустой файл")
                if response.status == 200 or size == total:
                    return
            except TimeoutError:
                raise DownloadError("download_timeout") from None
            except ssl.SSLError:
                raise DownloadError("download_failed") from None
            finally:
                conn.close()
