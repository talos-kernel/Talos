from __future__ import annotations

import http.server
import json
import threading
import urllib.error

import pytest

from talos import localcheck


class _Handler(http.server.BaseHTTPRequestHandler):
    calls: list[tuple[str, object]] = []

    def do_GET(self):  # noqa: N802
        self.__class__.calls.append((self.path, None))
        body = json.dumps({"data": [{"id": "local-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.__class__.calls.append((self.path, payload))
        body = json.dumps({"choices": [{"message": {"content": "LOCAL_OK"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture()
def local_server():
    _Handler.calls = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:" + str(server.server_address[1]) + "/v1"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_local_check_proves_models_and_inference_without_exposing_content(local_server) -> None:
    result = localcheck.check("ollama", base_url=local_server)

    assert result["ok"] is True
    assert result["transport"] == "loopback only"
    assert result["model"] == "local-model"
    assert result["models_available"] == 1
    assert result["response_present"] is True
    assert result["latency_ms"] >= 0
    assert _Handler.calls[0] == ("/v1/models", None)
    path, payload = _Handler.calls[1]
    assert path == "/v1/chat/completions"
    assert payload["model"] == "local-model"
    assert "LOCAL_OK" not in json.dumps(result)


@pytest.mark.parametrize("url", [
    "https://example.com/v1",
    "http://192.168.1.10:11434/v1",
    "http://user:pass@127.0.0.1:11434/v1",
    "file:///tmp/model",
])
def test_local_check_rejects_every_non_loopback_or_credentialed_url(url: str) -> None:
    with pytest.raises(ValueError, match="loopback|credentials|http"):
        localcheck.check("ollama", base_url=url)


def test_cli_json_reports_only_metadata(local_server, capsys) -> None:
    code = localcheck.run_local_check(["--provider", "lm-studio", "--base-url", local_server,
                                       "--json"])
    output = capsys.readouterr().out
    data = json.loads(output)

    assert code == 0
    assert data["ok"] is True
    assert data["provider"] == "lm-studio"
    assert "LOCAL_OK" not in output


def test_cli_help_names_the_privacy_boundary(capsys) -> None:
    assert localcheck.run_local_check(["--help"]) == 0
    output = capsys.readouterr().out
    assert "loopback" in output
    assert "synthetic" in output


def test_local_check_refuses_redirect_before_contacting_destination(local_server) -> None:
    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", local_server + "/models")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            localcheck.check("ollama", base_url=f"http://127.0.0.1:{server.server_port}/v1")
        assert error.value.code == 302
        assert _Handler.calls == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_localhost_is_pinned_to_loopback_without_dns() -> None:
    assert localcheck._base_url("http://localhost:11434/v1") == "http://127.0.0.1:11434/v1"
    assert localcheck._base_url("http://localhost/v1") == "http://127.0.0.1/v1"
    assert localcheck._base_url("http://[::1]:1234/v1") == "http://[::1]:1234/v1"
