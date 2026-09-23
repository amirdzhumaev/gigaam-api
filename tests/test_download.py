import socket

import pytest

from gigaam_api.download import download_public, public_addresses, validate_url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:pass@example.com/a",
        "http://example.com:22/a",
        "http://example.com/\nhello",
        "http://example.com\\x/a",
    ],
)
def test_unsafe_url_rejected(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["8.8.8.8", "192.168.1.1"],
        ["::ffff:127.0.0.1"],
    ],
)
def test_all_dns_answers_must_be_public(monkeypatch, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", (ip, 80)) for ip in addresses])
    with pytest.raises(ValueError):
        public_addresses("example.com", 80)


def test_redirect_to_private_address_blocked(monkeypatch, tmp_path):
    from gigaam_api import download

    connected = []

    def dns(host, *a, **k):
        return [(0, 0, 0, "", ("8.8.8.8" if host == "public.example" else "127.0.0.1", 80))]

    class Connection:
        def __init__(self, *a, **k):
            pass

        def request(self, *a, **k):
            pass

        def getresponse(self):
            return self

        status = 302

        def getheader(self, key, default=None):
            return "http://internal.example/secret"

        def close(self):
            pass

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(socket, "create_connection", lambda addr, **kw: connected.append(addr))
    monkeypatch.setattr(download.http.client, "HTTPConnection", Connection)
    with pytest.raises(ValueError):
        download_public("http://public.example/media", tmp_path / "file", 100)
    assert connected == [("8.8.8.8", 80)]


@pytest.mark.parametrize(
    "headers", [{"Cookie": "private"}, {"Host": "internal"}, {"User-Agent": "ok\r\nInjected: header"}]
)
def test_extractor_cannot_inject_headers_or_credentials(tmp_path, headers):
    with pytest.raises(ValueError):
        download_public("https://public.example/media", tmp_path / "file", 100, headers=headers)


def fake_transfer(monkeypatch, chunks, *, seconds_per_chunk=0, status=200, read_timeout=False):
    """Exercise the downloader without waiting for a slow public server."""
    from gigaam_api import download

    clock = [0]
    content = iter(chunks)

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return self

        def getheader(self, key, default=None):
            return default

        def read(self, _):
            if read_timeout:
                raise TimeoutError()
            chunk = next(content, b"")
            if chunk:
                clock[0] += seconds_per_chunk
            return chunk

        def close(self):
            pass

    Connection.status = status
    monkeypatch.setattr(download, "public_addresses", lambda *_: ["8.8.8.8"])
    monkeypatch.setattr(download.socket, "create_connection", lambda *a, **k: object())
    monkeypatch.setattr(download.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(download.time, "monotonic", lambda: clock[0])


def test_slow_download_can_exceed_old_five_minute_limit(monkeypatch, tmp_path):
    monkeypatch.delenv("MEDIA_DOWNLOAD_TIMEOUT", raising=False)
    fake_transfer(monkeypatch, [b"first", b"second", b"last"], seconds_per_chunk=110)
    destination = tmp_path / "audio"
    download_public("http://public.example/audio", destination, 100)
    assert destination.read_bytes() == b"firstsecondlast"


@pytest.mark.parametrize(
    "timeout,chunks,limit,seconds,status,read_timeout,code",
    [
        (300, [b"a", b"b"], 100, 150, 200, False, "download_timeout"),
        (1800, [b"audio"], 4, 1, 200, False, "download_too_large"),
        (1800, [], 100, 0, 403, False, "download_failed"),
        (1800, [], 100, 0, 200, True, "download_timeout"),
    ],
)
def test_download_limits_have_distinct_safe_codes(
    monkeypatch, tmp_path, timeout, chunks, limit, seconds, status, read_timeout, code
):
    from gigaam_api.download import DownloadError

    monkeypatch.setenv("MEDIA_DOWNLOAD_TIMEOUT", str(timeout))
    fake_transfer(monkeypatch, chunks, seconds_per_chunk=seconds, status=status, read_timeout=read_timeout)
    with pytest.raises(DownloadError) as error:
        download_public("http://public.example/audio", tmp_path / "audio", limit)
    assert error.value.code == code and str(error.value) == code


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-10", "3601", "invalid"])
def test_download_timeout_cannot_disable_resource_bound(monkeypatch, value):
    from gigaam_api.download import download_timeout

    monkeypatch.setenv("MEDIA_DOWNLOAD_TIMEOUT", value)
    with pytest.raises(ValueError):
        download_timeout()
