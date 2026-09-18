from hermex_push.relay import RelaySender, allowed_relay_url, notify_url


def test_notify_url_shape():
    assert notify_url("https://r.test/", "ab" * 32) == "https://r.test/installs/" + "ab" * 32 + "/notify"


def test_deliver_retries_server_errors_once_and_gives_up_on_client_errors():
    calls = []

    def post(url, body):
        calls.append(body)
        return 503 if len(calls) < 2 else 200

    sender = RelaySender(post=post, sleep=lambda s: None)
    assert sender.deliver("https://r", {"kind": "reply"}) is True
    assert len(calls) == 2 and b'"kind":"reply"' in calls[0]

    sender = RelaySender(post=lambda u, b: 401, sleep=lambda s: None)
    assert sender.deliver("https://r", {"kind": "reply"}) is False


def test_enqueue_drains_on_a_background_thread():
    seen = []
    sender = RelaySender(post=lambda url, body: seen.append(url) or 200)
    assert sender.enqueue("https://r/installs/k/notify", {"kind": "reply"})
    sender.wait_idle()
    assert seen == ["https://r/installs/k/notify"]


def test_relay_url_must_be_https_unless_loopback():
    assert allowed_relay_url("https://relay.example")
    assert allowed_relay_url("http://127.0.0.1:8977")
    assert allowed_relay_url("http://localhost:8977/")
    assert not allowed_relay_url("http://relay.example")
    assert not allowed_relay_url("ftp://relay.example")
    assert not allowed_relay_url("relay.example")


def test_redirects_are_refused():
    import http.server, threading
    from hermex_push.relay import _urllib_post

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302); self.send_header("Location", "http://127.0.0.1:9/steal"); self.end_headers()
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _urllib_post(f"http://127.0.0.1:{srv.server_port}/installs/k/notify", b"{}") == 302
    finally:
        srv.shutdown()
