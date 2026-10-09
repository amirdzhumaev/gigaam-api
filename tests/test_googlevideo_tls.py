import socket
import ssl
import subprocess
import threading
from types import SimpleNamespace

import pytest

from gigaam_api import download


@pytest.mark.parametrize(
    "hostname",
    ["googlevideo.com", "x.googlevideo.com.evil.example", "x.y.googlevideo.com", "-x.googlevideo.com"],
)
def test_sni_fallback_cannot_be_used_for_other_hosts(hostname):
    with pytest.raises(ValueError):
        download._googlevideo_tls(None, hostname)


@pytest.mark.parametrize(
    "names,accepted",
    [
        ([("DNS", "*.googlevideo.com")], True),
        ([("DNS", "edge.googlevideo.com")], True),
        ([("DNS", "EDGE.GOOGLEVIDEO.COM")], True),
        ([("DNS", "other.googlevideo.com")], False),
        ([("DNS", "*.com")], False),
        ([("DNS", "*.*.googlevideo.com")], False),
        ([("DNS", "*.googlevideo.com.evil.example")], False),
        ([("IP Address", "8.8.8.8")], False),
        ([], False),
    ],
)
def test_original_cdn_name_is_required_after_handshake(monkeypatch, names, accepted):
    closed = []
    secured = SimpleNamespace(
        getpeercert=lambda: {"subjectAltName": names}, close=lambda: closed.append(True)
    )
    context = ssl.create_default_context()
    monkeypatch.setattr(context, "wrap_socket", lambda sock: secured)
    monkeypatch.setattr(download.ssl, "create_default_context", lambda: context)
    if accepted:
        assert download._googlevideo_tls(object(), "edge.googlevideo.com") is secured
        assert not closed
    else:
        with pytest.raises(ssl.SSLCertVerificationError):
            download._googlevideo_tls(object(), "edge.googlevideo.com")
        assert closed == [True]
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_timed_out_sni_reconnects_to_same_pinned_ip_and_remembers_mode(monkeypatch, tmp_path):
    from test_download import fake_responses

    requests, _ = fake_responses(
        monkeypatch,
        [
            (206, {"Content-Range": "bytes 0-3/8"}, [b"abcd"]),
            (206, {"Content-Range": "bytes 4-7/8"}, [b"efgh"]),
        ],
    )
    connections, closed, sni_names = [], [], []

    def connect(address, **kwargs):
        connections.append(address)
        return SimpleNamespace(settimeout=lambda _: None, close=lambda: closed.append(True))

    def normal_tls(sock, *, server_hostname):
        sni_names.append(server_hostname)
        raise TimeoutError()

    def fallback(sock):
        sock.getpeercert = lambda: {"subjectAltName": [("DNS", "*.googlevideo.com")]}
        return sock

    context = SimpleNamespace(check_hostname=True, wrap_socket=fallback)
    contexts = iter([SimpleNamespace(wrap_socket=normal_tls), context, context])
    monkeypatch.setattr(download.socket, "create_connection", connect)
    monkeypatch.setattr(download.ssl, "create_default_context", lambda: next(contexts))
    destination = tmp_path / "audio"
    download.download_public("https://edge.googlevideo.com/audio", destination, 8, chunk_bytes=4)
    assert destination.read_bytes() == b"abcdefgh"
    assert connections == [("8.8.8.8", 443)] * 3
    assert sni_names == ["edge.googlevideo.com"]
    assert len(requests) == 2


@pytest.mark.parametrize("error", [TimeoutError, ssl.SSLCertVerificationError])
def test_other_host_timeouts_and_certificate_errors_do_not_use_fallback(monkeypatch, tmp_path, error):
    from test_download import fake_transfer

    fake_transfer(monkeypatch, [b"audio"])

    def reject(*args, **kwargs):
        raise error()

    def forbidden(*args):
        pytest.fail("Must not use the CDN fallback")

    monkeypatch.setattr(download.ssl, "create_default_context", lambda: SimpleNamespace(wrap_socket=reject))
    monkeypatch.setattr(download, "_googlevideo_tls", forbidden)
    with pytest.raises(download.DownloadError) as failure:
        download.download_public("https://public.example/audio", tmp_path / "audio", 100)
    assert failure.value.code == ("download_timeout" if error is TimeoutError else "download_failed")


def test_cdn_certificate_error_is_not_retried_without_sni(monkeypatch, tmp_path):
    from test_download import fake_transfer

    fake_transfer(monkeypatch, [b"audio"])

    def reject(*args, **kwargs):
        raise ssl.SSLCertVerificationError()

    monkeypatch.setattr(download.ssl, "create_default_context", lambda: SimpleNamespace(wrap_socket=reject))
    monkeypatch.setattr(download, "_googlevideo_tls", lambda *a: pytest.fail("Certificate failure is final"))
    with pytest.raises(download.DownloadError, match="download_failed"):
        download.download_public("https://edge.googlevideo.com/audio", tmp_path / "audio", 100)


def test_fallback_cannot_extend_download_deadline(monkeypatch, tmp_path):
    from test_download import fake_transfer

    requests, _ = fake_transfer(monkeypatch, [b"audio"])
    clock, addresses = [0], []

    def connect(address, **kwargs):
        addresses.append(address)
        return SimpleNamespace(settimeout=lambda _: None)

    def timeout(*args, **kwargs):
        clock[0] = 2
        raise TimeoutError()

    monkeypatch.setenv("MEDIA_DOWNLOAD_TIMEOUT", "1")
    monkeypatch.setattr(download.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(download.socket, "create_connection", connect)
    monkeypatch.setattr(download.ssl, "create_default_context", lambda: SimpleNamespace(wrap_socket=timeout))
    with pytest.raises(download.DownloadError, match="download_timeout"):
        download.download_public("https://edge.googlevideo.com/audio", tmp_path / "audio", 100)
    assert addresses == [("8.8.8.8", 443)]
    assert not requests


@pytest.fixture
def local_cdn_certificate(tmp_path):
    certificate, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=Test CDN",
            "-addext",
            "subjectAltName=DNS:*.googlevideo.com",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    return context, certificate


@pytest.mark.parametrize("trusted", [True, False])
def test_real_handshake_keeps_chain_validation(monkeypatch, local_cdn_certificate, trusted):
    server_context, certificate = local_cdn_certificate
    client, server = socket.socketpair()
    client.settimeout(3)
    server.settimeout(3)
    observed_sni = []
    server_context.sni_callback = lambda sock, name, ctx: observed_sni.append(name)

    def serve():
        try:
            with server_context.wrap_socket(server, server_side=True):
                pass
        except ssl.SSLError:
            server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    if trusted:
        context = ssl.create_default_context(cafile=str(certificate))
        monkeypatch.setattr(download.ssl, "create_default_context", lambda: context)
    try:
        if trusted:
            with download._googlevideo_tls(client, "edge.googlevideo.com") as secured:
                assert secured.getpeercert()
        else:
            with pytest.raises(ssl.SSLCertVerificationError):
                download._googlevideo_tls(client, "edge.googlevideo.com")
        assert observed_sni == [None]
    finally:
        client.close()
        thread.join(timeout=3)
        assert not thread.is_alive()
