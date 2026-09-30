from hermex_push.relay import RelaySender, allowed_relay_url, notify_url


def test_notify_url_shape():
    assert notify_url("https://r.test/", "ab" * 32) == "https://r.test/installs/" + "ab" * 32 + "/notify"


def test_deliver_retries_server_errors_once_and_gives_up_on_client_errors():
    calls = []

    def post(url, body):
        calls.append(body)
        return (503, "delivery_retry") if len(calls) < 2 else (200, "accepted")

    sender = RelaySender(post=post, sleep=lambda s: None)
    assert sender.deliver("https://r", {"kind": "reply"}) is True
    assert len(calls) == 2 and b'"kind":"reply"' in calls[0]

    sender = RelaySender(post=lambda u, b: (401, "invalid_request"), sleep=lambda s: None)
    assert sender.deliver("https://r", {"kind": "reply"}) is False


def test_progress_is_not_retried_unless_it_ends_the_turn():
    def attempts(event, failure):
        calls = []

        def post(url, body):
            calls.append(body)
            if isinstance(failure, Exception):
                raise failure
            return failure, "delivery_retry"

        assert RelaySender(post=post, sleep=lambda s: None).deliver("https://r", event) is False
        return len(calls)

    for failure in (503, ConnectionError("relay down")):
        for status in ("running", "waiting"):
            assert attempts({"kind": "progress", "status": status}, failure) == 1
        # Nothing replaces a lost done/failed, and the activity it ends holds back the reply banner.
        for status in ("done", "failed"):
            assert attempts({"kind": "progress", "status": status}, failure) == 2
        for kind in ("reply", "approval", "clarify", "turn_error"):
            assert attempts({"kind": kind}, failure) == 2


def test_enqueue_drains_on_a_background_thread():
    seen = []
    sender = RelaySender(post=lambda url, body: seen.append(url) or (200, "accepted"))
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
        assert _urllib_post(f"http://127.0.0.1:{srv.server_port}/installs/k/notify", b"{}") == (302, "")
    finally:
        srv.shutdown()


def test_deliver_hands_the_relay_result_to_on_result_only_after_a_2xx():
    import http.server, threading

    replies = iter([(200, b'{"result":"no_activity"}'), (200, b"<html>proxy</html>")])

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            status, body = next(replies)
            self.send_response(status); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        results = []
        sender = RelaySender(sleep=lambda s: None)
        url = f"http://127.0.0.1:{srv.server_port}/installs/k/notify"
        assert sender.deliver(url, {"kind": "progress", "status": "running"}, on_result=results.append) is True
        assert sender.deliver(url, {"kind": "progress", "status": "running"}, on_result=results.append) is True
        assert results == ["no_activity", ""]
    finally:
        srv.shutdown()

    def failing(status):
        def post(url, body):
            if isinstance(status, Exception):
                raise status
            return status, "no_activity"
        return post

    for failure in (400, 503, ConnectionError("relay down")):
        calls = []
        sender = RelaySender(post=failing(failure), sleep=lambda s: None)
        assert sender.deliver("https://r", {"kind": "progress", "status": "running"}, on_result=calls.append) is False
        assert calls == []


def test_enqueue_passes_on_result_to_the_drain_thread():
    results = []
    sender = RelaySender(post=lambda url, body: (200, "no_activity"))
    assert sender.enqueue("https://r/installs/k/notify", {"kind": "progress"}, on_result=results.append)
    sender.wait_idle()
    assert results == ["no_activity"]
