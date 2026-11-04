from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from filebot.engine import Engine, Session  # noqa: E402
from filebot.security import Workspace  # noqa: E402


class FakeClient:
    """Stands in for OllamaClient: returns queued raw responses, records requests."""

    def __init__(self, *responses: str | dict | Exception):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []
        self.model = "fake-model"

    def chat_json(self, messages, schema):
        self.calls.append(messages)
        if not self.responses:
            raise AssertionError("FakeClient ran out of responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item) if isinstance(item, dict) else item


def intent_json(action: str, path: str = "", destination: str = "", content: str = "", mode: str = "") -> dict:
    return {"action": action, "path": path, "destination": destination, "content": content, "mode": mode}


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path / "ws")


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A folder next to the workspace that must never be modified."""
    d = tmp_path / "outside"
    d.mkdir()
    (d / "secret.txt").write_text("top secret", encoding="utf-8")
    return d


@pytest.fixture
def make_engine(ws):
    def _make(*responses, session: Session | None = None) -> tuple[Engine, FakeClient]:
        client = FakeClient(*responses)
        return Engine(ws, client, 1_048_576, session or Session()), client

    return _make


def make_junction(link: Path, target: Path) -> None:
    """Create a directory junction (no admin rights needed on Windows). Skips if impossible."""
    if os.name != "nt":
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"cannot create directory symlink: {exc}")
        return
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, AttributeError, OSError) as exc:
        pytest.skip(f"cannot create a junction on this system: {exc}")


def make_symlink(link: Path, target: Path, is_dir: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(
            f"cannot create symlinks here ({exc}); enable Windows Developer Mode or run as admin to run this test"
        )
