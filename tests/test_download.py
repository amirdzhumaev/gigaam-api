import socket
from types import SimpleNamespace

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

    def connect(addr, **kw):
        connected.append(addr)
        return SimpleNamespace(settimeout=lambda _: None)

    monkeypatch.setattr(socket, "create_connection", connect)
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
    return fake_responses(
        monkeypatch,
        [(status, {}, chunks)],
        seconds_per_chunk=seconds_per_chunk,
        read_timeout=read_timeout,
    )


def fake_responses(monkeypatch, responses, *, seconds_per_chunk=0, read_timeout=False):
    """Exercise the downloader without waiting for a slow public server."""
    from gigaam_api import download

    clock = [0]
    replies = iter(responses)
    requests, timeouts = [], []

    class Connection:
        def __init__(self, *args, **kwargs):
            self.status, self.headers, chunks = next(replies)
            self.content = iter(chunks)

        def request(self, *args, **kwargs):
            requests.append(kwargs["headers"])

        def getresponse(self):
            return self

        def getheader(self, key, default=None):
            return self.headers.get(key, default)

        def read1(self, _):
            if read_timeout:
                raise TimeoutError()
            chunk = next(self.content, b"")
            if chunk:
                clock[0] += seconds_per_chunk
            return chunk

        def close(self):
            pass

    monkeypatch.setattr(download, "public_addresses", lambda *_: ["8.8.8.8"])
    monkeypatch.setattr(
        download.socket,
        "create_connection",
        lambda *a, **k: SimpleNamespace(settimeout=timeouts.append),
    )
    monkeypatch.setattr(download.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(download.time, "monotonic", lambda: clock[0])
    return requests, timeouts


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


def test_ranges_assemble_file_in_order_with_short_final_range(monkeypatch, tmp_path):
    requests, _ = fake_responses(
        monkeypatch,
        [
            (206, {"Content-Range": "bytes 0-3/10", "Content-Length": "4"}, [b"ab", b"cd"]),
            (206, {"Content-Range": "bytes 4-7/10"}, [b"efgh"]),
            (206, {"Content-Range": "bytes 8-9/10"}, [b"ij"]),
        ],
    )
    destination = tmp_path / "audio"
    download_public("http://public.example/audio", destination, 10, chunk_bytes=4)
    assert destination.read_bytes() == b"abcdefghij"
    assert [r["Range"] for r in requests] == ["bytes=0-3", "bytes=4-7", "bytes=8-11"]


def test_range_ignored_on_first_request_falls_back_to_bounded_full_body(monkeypatch, tmp_path):
    requests, _ = fake_transfer(monkeypatch, [b"audio"])
    destination = tmp_path / "audio"
    download_public("http://public.example/audio", destination, 5, chunk_bytes=4)
    assert destination.read_bytes() == b"audio"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "headers,body,code",
    [
        ({}, b"abcd", "download_failed"),
        ({"Content-Range": "bytes 1-4/10"}, b"abcd", "download_failed"),
        ({"Content-Range": "bytes 0-4/10"}, b"abcde", "download_failed"),
        ({"Content-Range": "bytes 0-3/*"}, b"abcd", "download_failed"),
        ({"Content-Range": "bytes 0-3/3"}, b"abcd", "download_failed"),
        ({"Content-Range": "bytes 0-3/10", "Content-Length": "3"}, b"abc", "download_failed"),
        ({"Content-Range": "bytes 0-3/10"}, b"abc", "download_failed"),
        ({"Content-Range": "bytes 0-3/10"}, b"abcde", "download_failed"),
        ({"Content-Range": "bytes 0-3/11"}, b"abcd", "download_too_large"),
    ],
)
def test_bad_or_oversized_ranges_are_rejected(monkeypatch, tmp_path, headers, body, code):
    from gigaam_api.download import DownloadError

    fake_responses(monkeypatch, [(206, headers, [body])])
    with pytest.raises(DownloadError, match=code):
        download_public("http://public.example/audio", tmp_path / "audio", 10, chunk_bytes=4)


@pytest.mark.parametrize(
    "second",
    [
        (200, {}, [b"abcdefgh"]),
        (206, {"Content-Range": "bytes 4-7/9"}, [b"efgh"]),
        (206, {"Content-Range": "bytes 0-3/8"}, [b"abcd"]),
    ],
)
def test_range_stream_cannot_restart_overlap_or_change_total(monkeypatch, tmp_path, second):
    from gigaam_api.download import DownloadError

    fake_responses(monkeypatch, [(206, {"Content-Range": "bytes 0-3/8"}, [b"abcd"]), second])
    with pytest.raises(DownloadError, match="download_failed"):
        download_public("http://public.example/audio", tmp_path / "audio", 10, chunk_bytes=4)
    assert (tmp_path / "audio").read_bytes() == b"abcd"


def test_ranges_share_one_deadline_and_shrink_socket_timeout(monkeypatch, tmp_path):
    from gigaam_api.download import DownloadError

    monkeypatch.setenv("MEDIA_DOWNLOAD_TIMEOUT", "3")
    requests, timeouts = fake_responses(
        monkeypatch,
        [
            (206, {"Content-Range": "bytes 0-3/8"}, [b"abcd"]),
            (206, {"Content-Range": "bytes 4-7/8"}, [b"efgh"]),
        ],
        seconds_per_chunk=2,
    )
    with pytest.raises(DownloadError, match="download_timeout"):
        download_public("http://public.example/audio", tmp_path / "audio", 10, chunk_bytes=4)
    assert len(requests) == 2 and timeouts[-1] == 1


def test_each_range_revalidates_dns_before_connecting(monkeypatch, tmp_path):
    from gigaam_api import download

    requests, _ = fake_responses(monkeypatch, [(206, {"Content-Range": "bytes 0-3/8"}, [b"abcd"])])
    addresses = iter([["8.8.8.8"], ["127.0.0.1"]])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", (next(addresses)[0], 80))])
    monkeypatch.setattr(download, "public_addresses", public_addresses)
    with pytest.raises(ValueError, match="запрещены"):
        download_public("http://public.example/audio", tmp_path / "audio", 10, chunk_bytes=4)
    assert len(requests) == 1


@pytest.mark.parametrize("length", ["6", "invalid", "-1"])
def test_full_body_cannot_be_truncated_or_have_invalid_length(monkeypatch, tmp_path, length):
    from gigaam_api.download import DownloadError

    fake_responses(monkeypatch, [(200, {"Content-Length": length}, [b"audio"])])
    with pytest.raises(DownloadError, match="download_failed"):
        download_public("http://public.example/audio", tmp_path / "audio", 10)


def test_connection_timeout_has_safe_download_code(monkeypatch, tmp_path):
    from gigaam_api.download import DownloadError

    fake_transfer(monkeypatch, [])

    def timeout(*args, **kwargs):
        raise TimeoutError()

    monkeypatch.setattr(socket, "create_connection", timeout)
    with pytest.raises(DownloadError, match="download_timeout"):
        download_public("http://public.example/audio", tmp_path / "audio", 10)
