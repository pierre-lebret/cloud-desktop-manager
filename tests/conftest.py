from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rdpm import rdp  # noqa: E402
from rdpm.config import AppConfig, SessionLog  # noqa: E402
from rdpm.hetzner.fake import SEED_PATH, FakeCloud  # noqa: E402
from rdpm.hetzner.service import HetznerService  # noqa: E402
from rdpm.models import StaticData  # noqa: E402
from rdpm.ops.base import OpContext, Operation  # noqa: E402

rdp.SIMULATE = True


@pytest.fixture(scope="session")
def static() -> StaticData:
    raw = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    return StaticData.from_api(raw["server_types"], raw["locations"], raw["pricing"])


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch, tmp_path):
    """Les opérations dorment en temps réel ; le FakeCloud accéléré avance pendant ce temps."""
    def sleep(self, seconds):
        self.check()
        time.sleep(0.01)
        self.check()
    monkeypatch.setattr(Operation, "sleep", sleep)
    monkeypatch.setattr(rdp, "RDP_DIR", tmp_path / "rdp")


class Harness:
    def __init__(self, fake: FakeCloud, static: StaticData, tmp_path: Path) -> None:
        self.fake = fake
        self.backend = HetznerService(fake, probe_fn=fake.probe, public_ip_fn=fake.public_ip, remote=fake.remote)
        self.config = AppConfig(path=tmp_path / "config.json")
        self.config.settings.update(shutdown_timeout_s=60, snapshot_timeout_s=3600)
        self.creds = rdp.MemoryCredentialStore()
        self.sessions = SessionLog(tmp_path / "sessions.jsonl")
        self.events: list = []
        self.decisions: list[str] = []
        self.ctx = OpContext(self.backend, self.config, self.creds, self.sessions, self._emit,
                             "testhost", threading.Event(), lambda: static)

    def _emit(self, event) -> None:
        self.events.append(event)
        if type(event).__name__ == "NeedDecision":
            choice = self.decisions.pop(0) if self.decisions else event.default
            event.future.set_result(choice)

    def run(self, op: Operation) -> Operation:
        op.run()
        return op


@pytest.fixture
def harness(static, tmp_path):
    def make(fail=(), seed="account", speed=2000.0) -> Harness:
        return Harness(FakeCloud(speed=speed, fail=set(fail), seed=seed), static, tmp_path)
    return make
