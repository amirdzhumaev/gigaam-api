import json
import socket
import sys
import threading

import pytest

from gigaam_api import page_import
from gigaam_api.page_import import (
    PageImportError,
    canonical_page,
    child_environment,
    download_source,
    extractor_command,
    public_proxy,
    run_extractor,
    select_media,
)
from gigaam_api.schemas import Transcript
from gigaam_api.worker import process_one


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://youtu.be/abcdefghijk?si=secret", "https://www.youtube.com/watch?v=abcdefghijk"),
        (
            "http://m.youtube.com/watch?v=abcdefghijk&list=anything",
            "https://www.youtube.com/watch?v=abcdefghijk",
        ),
        ("https://www.youtube.com/shorts/abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk"),
        ("https://vk.com/video-123_456?access_key=secret", "https://vkvideo.ru/video-123_456"),
        ("https://m.vk.com/videos-123?z=video-123_456%2Falbum-123", "https://vkvideo.ru/video-123_456"),
        ("https://vkvideo.ru/video123_456", "https://vkvideo.ru/video123_456"),
        ("https://example.com/audio.m4a", None),
        ("https://youtube.com.example.com/audio.mp4", None),
    ],
)
def test_canonical_single_video_urls(url, expected):
    assert canonical_page(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com/playlist?list=abc",
        "https://youtube.com/@channel",
        "https://youtu.be/short",
        "https://youtube.com/watch?v=abcdefghijk&v=other",
        "https://youtu.be/abcdefghijk/extra",
        "https://youtube.com/watch?v=../../secret",
        "https://user:password@youtube.com/watch?v=abcdefghijk",
        "https://vk.com/wall-1_2",
        "https://vkvideo.ru/playlist/-1_2",
        "https://vk.com/video-1_2:other",
        "https://youtube.com:22/watch?v=abcdefghijk",
        "https://youtu.be/abcdefghijk\n",
    ],
)
def test_pages_cannot_address_playlists_channels_or_arbitrary_resources(url):
    with pytest.raises(ValueError):
        canonical_page(url)


def metadata(**overrides):
    return {
        "extractor_key": "Youtube",
        "duration": 100,
        "formats": [
            {
                "url": "https://cdn.example/media",
                "protocol": "https",
                "ext": "m4a",
                "acodec": "aac",
                "vcodec": "none",
                "abr": 64,
                "filesize": 800,
            },
        ],
        **overrides,
    }


def test_selects_direct_audio_and_only_safe_origin_headers():
    info = metadata(http_headers={"User-Agent": "Extractor/1", "Cookie": "private", "Host": "evil"})
    info["formats"] += [
        {
            "url": "https://cdn.example/video",
            "protocol": "https",
            "ext": "mp4",
            "acodec": "aac",
            "vcodec": "h264",
            "height": 1080,
            "abr": 192,
        },
        {
            "url": "https://cdn.example/manifest",
            "protocol": "m3u8_native",
            "ext": "m4a",
            "acodec": "aac",
            "vcodec": "none",
            "abr": 320,
        },
    ]
    url, headers = select_media(info, max_bytes=1000, max_duration=200)
    assert url == "https://cdn.example/media"
    assert headers == {"user-agent": "Extractor/1"}


@pytest.mark.parametrize(
    "override",
    [
        {"_type": "playlist"},
        {"entries": []},
        {"extractor_key": "Generic"},
        {"is_live": True},
        {"live_status": "is_upcoming"},
        {"has_drm": True},
        {"duration": 201},
        {"duration": None},
        {"duration": float("nan")},
    ],
)
def test_unsupported_metadata_rejected(override):
    with pytest.raises(PageImportError):
        select_media(metadata(**override), max_bytes=1000, max_duration=200)


@pytest.mark.parametrize(
    "override",
    [
        {"protocol": "http_dash_segments"},
        {"protocol": "m3u8_native"},
        {"url": "https://cdn.example/media.m3u8"},
        {"url": "file:///etc/passwd"},
        {"url": "https://user:password@cdn.example/media"},
        {"filesize": 1001},
        {"acodec": "none"},
        {"has_drm": True},
        {"fragments": [{"url": "anything"}]},
        {"manifest_url": "https://cdn.example/manifest"},
    ],
)
def test_manifests_drm_oversized_or_silent_formats_not_downloaded(override):
    info = metadata()
    info["formats"][0].update(override)
    with pytest.raises(PageImportError, match="unsupported_video_format"):
        select_media(info, max_bytes=1000, max_duration=200)


def test_video_fallback_prefers_smaller_resolution():
    first = metadata()["formats"][0] | {"vcodec": "h264", "height": 720}
    second = first | {"height": 240, "url": "https://cdn.example/small"}
    assert select_media(metadata(formats=[first, second]), max_bytes=1000, max_duration=200)[0].endswith(
        "small"
    )


def test_vk_progressive_format_does_not_require_codec_metadata():
    info = metadata(
        extractor_key="VK",
        formats=[
            {
                "format_id": "url240",
                "url": "https://cdn.example/video.mp4",
                "protocol": "https",
                "ext": "mp4",
                "height": 240,
            }
        ],
    )
    assert select_media(info, max_bytes=1000, max_duration=200)[0] == "https://cdn.example/video.mp4"


def proxy_request(proxy, line, *, authorization=True, extra=""):
    connection = socket.create_connection(proxy.server_address, timeout=3)
    head = f"{line}\r\n"
    if authorization:
        head += f"Proxy-Authorization: {proxy.authorization}\r\n"
    connection.sendall((head + extra + "\r\n").encode())
    return connection


@pytest.mark.parametrize(
    "request_line",
    [
        "CONNECT localhost:443 HTTP/1.1",
        "CONNECT 169.254.169.254:443 HTTP/1.1",
        "CONNECT [::1]:443 HTTP/1.1",
        "GET http://127.0.0.1/private HTTP/1.1",
        "CONNECT public.example:22 HTTP/1.1",
        "GET file:///etc/passwd HTTP/1.1",
    ],
)
def test_proxy_rejects_private_or_unsupported_destinations(monkeypatch, request_line):
    real_connect = socket.create_connection
    connected = []

    def connect(address, *args, **kwargs):
        # The client-to-proxy connection is local; no origin connection may occur.
        if address[0] == "127.0.0.1" and address[1] != 443:
            return real_connect(address, *args, **kwargs)
        connected.append(address)
        raise AssertionError("Origin connection must be rejected before dial")

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port))],
    )
    monkeypatch.setattr(socket, "create_connection", connect)
    with public_proxy() as proxy, proxy_request(proxy, request_line) as connection:
        assert connection.recv(4096).startswith(b"HTTP/1.1 403")
    assert connected == []


def test_proxy_pin_checked_dns_address_for_connect(monkeypatch):
    real_connect = socket.create_connection
    local, origin = socket.socketpair()
    called = []

    def connect(address, *args, **kwargs):
        if address[0] == "127.0.0.1":
            return real_connect(address, *args, **kwargs)
        called.append(address)
        return local

    monkeypatch.setattr(page_import, "public_addresses", lambda host, port: ["8.8.8.8"])
    monkeypatch.setattr(socket, "create_connection", connect)
    with (
        origin,
        public_proxy() as proxy,
        proxy_request(proxy, "CONNECT media.example:443 HTTP/1.1") as connection,
    ):
        assert connection.recv(4096).startswith(b"HTTP/1.1 200")
        connection.sendall(b"client-tls-data")
        assert origin.recv(4096) == b"client-tls-data"
        origin.sendall(b"server-tls-data")
        assert connection.recv(4096) == b"server-tls-data"
    assert called == [("8.8.8.8", 443)]


def test_proxy_http_redirect_is_returned_and_private_second_hop_denied(monkeypatch):
    local, origin = socket.socketpair()
    hosts = []

    def dial(host, port):
        hosts.append((host, port))
        if host != "public.example":
            raise ValueError("private address")
        return local

    monkeypatch.setattr(page_import, "public_dial", dial)

    def reply():
        with origin:
            request = origin.recv(4096)
            assert b"host: public.example\r\n" in request
            assert b"proxy-authorization" not in request
            origin.sendall(
                b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/secret\r\nContent-Length: 0\r\n\r\n"
            )

    thread = threading.Thread(target=reply)
    thread.start()
    with public_proxy() as proxy:
        with proxy_request(
            proxy, "GET http://public.example/file HTTP/1.1", extra="Host: injected.example\r\n"
        ) as connection:
            assert b"302 Found" in connection.recv(4096)
        with proxy_request(proxy, "GET http://127.0.0.1/secret HTTP/1.1") as connection:
            assert b"403 Forbidden" in connection.recv(4096)
    thread.join(timeout=2)
    assert hosts == [("public.example", 80), ("127.0.0.1", 80)]


def test_proxy_budget_and_auth_limits_precede_dial(monkeypatch):
    monkeypatch.setattr(page_import, "public_dial", lambda *a: pytest.fail("Must not dial"))
    with public_proxy(max_requests=1) as proxy:
        with proxy_request(proxy, "CONNECT example.com:443 HTTP/1.1", authorization=False) as connection:
            assert b"403 Forbidden" in connection.recv(4096)
        with proxy_request(proxy, "CONNECT example.com:443 HTTP/1.1") as connection:
            assert b"403 Forbidden" in connection.recv(4096)
        with pytest.raises(PageImportError):
            proxy.check(100 * 1024 * 1024)


def test_extractor_does_not_inherit_keys_configs_or_browser_access(monkeypatch, tmp_path):
    monkeypatch.setenv("ASR_WORKER_TOKEN", "private-worker-token")
    monkeypatch.setenv("LLM_API_KEY", "private-llm-key")
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted")
    env = child_environment(tmp_path)
    assert not {"ASR_WORKER_TOKEN", "LLM_API_KEY"} & env.keys()
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:9" and env["NO_PROXY"] == ""
    cmd = extractor_command("https://youtube.com/watch?v=abcdefghijk", "http://safe-proxy")
    assert "--no-plugin-dirs" in cmd and "--no-remote-components" in cmd
    assert "--no-cookies-from-browser" in cmd and "--ignore-config" in cmd
    assert cmd[cmd.index("--proxy") + 1] == "http://safe-proxy"
    assert cmd[cmd.index("--use-extractors") + 1] == "youtube$,vk$"


def test_extractor_subprocess_json_timeout_output_and_exit_limits(tmp_path):
    assert run_extractor([sys.executable, "-c", 'print("{\\"ok\\": true}")'], tmp_path) == {"ok": True}
    with pytest.raises(PageImportError, match="page_import_timeout"):
        run_extractor([sys.executable, "-c", "import time; time.sleep(5)"], tmp_path, timeout=0.05)
    with pytest.raises(PageImportError, match="page_import_limit"):
        run_extractor(
            [sys.executable, "-c", "import sys; sys.stderr.write('x'*8192)"], tmp_path, max_bytes=100
        )
    with pytest.raises(PageImportError, match="page_import_failed"):
        run_extractor([sys.executable, "-c", "raise SystemExit(2)"], tmp_path)


def test_real_ytdlp_cli_emits_metadata_without_download_limit_exit(tmp_path):
    # Exercise the pinned CLI, not a mocked extractor subprocess. --max-downloads
    # 1 used to exit 101 before --dump-single-json could emit its first result,
    # even though --skip-download was enabled. No network/media is needed here.
    fixture = tmp_path / "video.info.json"
    fixture.write_text(
        json.dumps(
            metadata(
                id="fixture",
                title="Offline fixture",
                extractor="youtube",
                webpage_url="https://www.youtube.com/watch?v=abcdefghijk",
            )
        )
    )
    command = extractor_command("https://www.youtube.com/watch?v=abcdefghijk", "http://127.0.0.1:9")
    command = command[:-2] + ["--load-info-json", str(fixture)]
    result = run_extractor(command, tmp_path)
    assert result["id"] == "fixture"
    assert select_media(result, max_bytes=1000, max_duration=200)[0] == "https://cdn.example/media"
    assert list(tmp_path.iterdir()) == [fixture]


def test_child_network_uses_proxy_even_for_loopback_target(tmp_path):
    # This is an actual child Python request, not a mocked transport. It must
    # receive the proxy's 403, rather than connecting to the private destination.
    script = (
        "import json,urllib.request,urllib.error\n"
        "try: urllib.request.urlopen('http://127.0.0.1:80/private',timeout=3)\n"
        "except urllib.error.HTTPError as e: print(json.dumps({'status':e.code}))\n"
    )
    with public_proxy() as proxy:
        result = run_extractor([sys.executable, "-c", script], tmp_path, proxy_url=proxy.url)
    assert result == {"status": 403}


def test_download_source_keeps_direct_urls_and_gates_page_metadata(monkeypatch, tmp_path):
    downloaded = []
    monkeypatch.setattr(page_import, "download_public", lambda *a, **kw: downloaded.append((a, kw)))
    destination = tmp_path / "source"
    download_source("https://files.example/a.mp4", destination, 1000)
    assert downloaded == [(("https://files.example/a.mp4", destination, 1000), {})]
    observed = []

    def extract(command, root, **kwargs):
        assert kwargs["proxy_url"] == command[command.index("--proxy") + 1]
        observed.append(command)
        return metadata()

    monkeypatch.setattr(page_import, "run_extractor", extract)
    download_source("https://youtu.be/abcdefghijk?si=ignored", destination, 1000)
    assert observed[0][-1] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert downloaded[-1] == (("https://cdn.example/media", destination, 1000), {"headers": {}})


def test_page_import_flows_through_worker_and_reports_terminal_format_error(api, worker, monkeypatch):
    monkeypatch.setattr(page_import, "run_extractor", lambda *a, **kw: metadata())
    monkeypatch.setattr(
        page_import, "download_public", lambda url, dest, limit, **kw: dest.write_bytes(b"audio")
    )

    class Recognizer:
        def transcribe(self, source, root):
            assert source.read_bytes() == b"audio"
            return Transcript(model="fixture", duration=1, text="", segments=[])

    first = api.post("/v1/imports", json={"url": "https://youtu.be/abcdefghijk"}).json()["id"]
    assert process_one(worker, Recognizer())
    assert api.get(f"/v1/transcriptions/{first}").json()["state"] == "completed"
    monkeypatch.setattr(page_import, "run_extractor", lambda *a, **kw: metadata(formats=[]))

    second = api.post("/v1/imports", json={"url": "https://vk.com/video1_2"}).json()["id"]
    assert process_one(worker, Recognizer())
    result = api.get(f"/v1/transcriptions/{second}").json()
    assert (result["state"], result["error_code"]) == ("failed", "unsupported_video_format")
