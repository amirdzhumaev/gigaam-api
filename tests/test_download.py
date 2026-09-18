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
