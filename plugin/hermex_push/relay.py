"""Background delivery to the relay. Hooks run on the agent's turn path, so they only enqueue;
one daemon thread POSTs with a short timeout and a single retry. Nothing is persisted: a push
that cannot be delivered within seconds is stale anyway."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

logger = logging.getLogger("hermex_push")

QUEUE_LIMIT = 256
TIMEOUT_SECONDS = 10.0
RETRY_DELAY_SECONDS = 2.0

PostFn = Callable[[str, bytes], int]  # (url, body) -> HTTP status


def notify_url(relay_url: str, install_key: str) -> str:
    return f"{relay_url.rstrip('/')}/installs/{install_key}/notify"


def _urllib_post(url: str, body: bytes) -> int:
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "hermex-push-plugin/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


class RelaySender:
    """Queue of relay-bound events drained by one daemon thread."""

    def __init__(self, post: PostFn = _urllib_post, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self._post = post
        self._sleep = sleep
        self._queue: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue(maxsize=QUEUE_LIMIT)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def enqueue(self, url: str, event: dict[str, Any]) -> bool:
        try:
            self._queue.put_nowait((url, event))
        except queue.Full:
            logger.warning("hermex-push: relay queue full; dropping a %s event", event.get("kind"))
            return False
        self._ensure_thread()
        return True

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._drain, name="hermex-push-relay", daemon=True)
                self._thread.start()

    def _drain(self) -> None:
        while True:
            url, event = self._queue.get()
            try:
                self.deliver(url, event)
            except Exception:
                logger.debug("hermex-push: relay delivery raised", exc_info=True)
            finally:
                self._queue.task_done()

    def deliver(self, url: str, event: dict[str, Any]) -> bool:
        """Synchronous POST with one retry on a network error or 5xx. Never logs the body."""
        body = json.dumps(event, separators=(",", ":")).encode("utf-8")
        for attempt in (1, 2):
            try:
                status = self._post(url, body)
            except Exception as exc:
                status, reason = 0, type(exc).__name__
            else:
                reason = f"HTTP {status}"
            if 200 <= status < 300:
                return True
            if status and status < 500:
                logger.warning("hermex-push: relay rejected a %s event (%s)", event.get("kind"), reason)
                return False
            if attempt == 1:
                self._sleep(RETRY_DELAY_SECONDS)
        logger.warning("hermex-push: relay unreachable for a %s event (%s)", event.get("kind"), reason)
        return False

    def wait_idle(self, timeout: float = 5.0) -> None:
        """Test helper: block until the queue drains."""
        deadline = time.monotonic() + timeout
        while not self._queue.empty() or self._queue.unfinished_tasks:
            if time.monotonic() > deadline:
                raise TimeoutError("relay queue did not drain")
            time.sleep(0.01)
