"""Direct public media URLs only. Pin the validated IP to prevent DNS rebinding."""

import http.client
import ipaddress
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit


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


def download_public(url: str, destination: Path, limit: int, *, headers=None):
    request_headers = {"User-Agent": "gigaam-api/0.1", "Accept-Encoding": "identity"}
    for key, value in (headers or {}).items():
        if key.lower() not in {"user-agent", "referer", "origin"} or not isinstance(value, str):
            raise ValueError("Недопустимый заголовок источника")
        if len(value) > 2048 or any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ValueError("Недопустимый заголовок источника")
        request_headers[key.title()] = value
    deadline = time.monotonic() + 300
    for _ in range(6):
        parsed = validate_url(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        address = public_addresses(parsed.hostname, port)[0]
        # The socket connects to the checked IP, while TLS verifies the original hostname.
        sock = socket.create_connection((address, port), timeout=20)
        if parsed.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
        conn = http.client.HTTPConnection(parsed.hostname, port, timeout=20)
        conn.sock = sock
        try:
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            conn.request("GET", target, headers=request_headers)
            response = conn.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Некорректное перенаправление")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError("Источник не отдал медиафайл")
            content_type = response.getheader("Content-Type", "").lower()
            if "text/html" in content_type or "application/json" in content_type:
                raise ValueError("Поддерживаются прямые ссылки на файлы, а не страницы сайтов")
            size = 0
            with destination.open("wb") as output:
                while chunk := response.read(256 * 1024):
                    size += len(chunk)
                    if size > limit or time.monotonic() > deadline:
                        raise ValueError("Превышен размер файла или время загрузки")
                    output.write(chunk)
            if not size:
                raise ValueError("Источник вернул пустой файл")
            return
        finally:
            conn.close()
    raise ValueError("Слишком много перенаправлений")
