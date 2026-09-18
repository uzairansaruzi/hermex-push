from hermex_push.relay import RelaySender, notify_url


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
