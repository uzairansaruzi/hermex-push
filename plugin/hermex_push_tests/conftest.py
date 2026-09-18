import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermex_push.keys import Keys  # noqa: E402
from hermex_push.relay import RelaySender  # noqa: E402


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMEX_PUSH_RELAY_URL", raising=False)
    return home


@pytest.fixture
def keys():
    return Keys(install_key="ab" * 32, preview_key=bytes(range(32)))


class CapturingSender(RelaySender):
    """Records events instead of posting; delivery is synchronous."""

    def __init__(self):
        super().__init__(post=lambda url, body: 200)
        self.events = []

    def enqueue(self, url, event):
        self.events.append((url, event))
        return True


@pytest.fixture
def sender():
    return CapturingSender()
