from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bub_codex import plugin


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def run(
        self, *, session_id: str, prompt: str, state: dict[str, object]
    ) -> str:
        self.calls.append((session_id, prompt, state))
        return "internal-command-result"


class FakeTapeService:
    def __init__(self) -> None:
        self.ensure_bootstrap_anchor_calls: list[str] = []
        self.info_calls: list[str] = []
        self.anchor_calls: list[str] = []
        self.append_event_calls: list[tuple[str, str, dict[str, object]]] = []
        self.handoff_calls: list[tuple[str, str, dict[str, object] | None]] = []
        self._anchors = [SimpleNamespace(name="session/start", state={"owner": "human"})]
        self._info_anchors = 1

    def session_tape(self, session_id: str, workspace: Path):
        class Tape:
            name = f"tape:{session_id}"

        return Tape()

    async def ensure_bootstrap_anchor(self, tape_name: str) -> None:
        self.ensure_bootstrap_anchor_calls.append(tape_name)

    async def info(self, tape_name: str):
        self.info_calls.append(tape_name)

        class Info:
            anchors = self._info_anchors

        return Info()

    async def anchors(self, tape_name: str):
        self.anchor_calls.append(tape_name)
        return list(self._anchors)

    async def append_event(self, tape_name: str, name: str, payload: dict[str, object]) -> None:
        self.append_event_calls.append((tape_name, name, payload))

    async def handoff(self, tape_name: str, *, name: str, state: dict[str, object] | None = None) -> None:
        self.handoff_calls.append((tape_name, name, state))
        self._anchors.append(SimpleNamespace(name=name, state=state or {}))
        self._info_anchors = len(self._anchors)


class FakeRuntimeAgent:
    def __init__(self) -> None:
        self.tapes = FakeTapeService()


def test_run_model_delegates_internal_commands_to_runtime_agent() -> None:
    state: dict[str, object] = {"_runtime_agent": FakeAgent()}

    result = asyncio.run(plugin.run_model(",help", session_id="session-1", state=state))

    agent = state["_runtime_agent"]
    assert result == "internal-command-result"
    assert isinstance(agent, FakeAgent)
    assert agent.calls == [("session-1", ",help", state)]


def test_run_model_uses_codex_for_normal_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"codex-output\n", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())

    state = {"_runtime_workspace": str(tmp_path)}
    result = asyncio.run(plugin.run_model("hello", session_id="session-2", state=state))

    assert result == "codex-output\n"
    assert calls
    args, kwargs = calls[0]
    assert args[:2] == ("codex", "e")
    assert args[-1] == "hello"
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.PIPE


def test_run_model_saves_session_id_from_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (
                b"codex-output\n",
                b"booting\nsession id: thread-123\nconnected\n",
            )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())

    state = {"_runtime_workspace": str(tmp_path)}
    result = asyncio.run(plugin.run_model("hello", session_id="session-3", state=state))

    assert result == "codex-output\n"
    threads_file = tmp_path / plugin.THREADS_FILE
    data = json.loads(threads_file.read_text())
    assert data["session-3"]["thread_id"] == "thread-123"
    assert data["session-3"]["anchor_count"] == 0


def test_run_model_consumes_handoff_signal_and_creates_anchor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"codex-output\n", b"session id: thread-456\n")

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())

    signal_path = tmp_path / plugin.HANDOFF_SIGNAL_FILE
    signal_path.write_text(
        json.dumps(
            {
                "name": "phase-2",
                "summary": "validated handoff path",
                "next_steps": "continue in fresh thread",
            }
        )
    )
    agent = FakeRuntimeAgent()
    state = {"_runtime_workspace": str(tmp_path), "_runtime_agent": agent}

    result = asyncio.run(plugin.run_model("hello", session_id="session-4", state=state))

    assert result == "codex-output\n"
    assert agent.tapes.handoff_calls == [
        (
            "tape:session-4",
            "phase-2",
            {
                "summary": "validated handoff path",
                "next_steps": "continue in fresh thread",
            },
        )
    ]
    assert not signal_path.exists()
    data = json.loads((tmp_path / plugin.THREADS_FILE).read_text())
    assert data["session-4"]["thread_id"] is None
    assert data["session-4"]["anchor_count"] == 2


def test_save_state_still_consumes_handoff_signal_as_fallback(
    tmp_path: Path,
) -> None:
    signal_path = tmp_path / plugin.HANDOFF_SIGNAL_FILE
    signal_path.write_text(json.dumps({"name": "phase-fallback", "summary": "fallback path"}))
    agent = FakeRuntimeAgent()
    state = {"_runtime_workspace": str(tmp_path), "_runtime_agent": agent}

    asyncio.run(plugin.save_state("session-5", state, message={}, model_output="done"))

    assert agent.tapes.handoff_calls == [
        ("tape:session-5", "phase-fallback", {"summary": "fallback path"})
    ]
    assert not signal_path.exists()
    data = json.loads((tmp_path / plugin.THREADS_FILE).read_text())
    assert data["session-5"]["thread_id"] is None
