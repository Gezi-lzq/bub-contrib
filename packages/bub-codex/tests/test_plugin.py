from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from bub.turn_admission import AdmitAction, TurnSnapshot

from bub_codex import plugin


@pytest.fixture(autouse=True)
def reset_plugin_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    original_backend = plugin._backend
    settings = SimpleNamespace(
        model=None,
        yolo_mode=False,
        timeout_seconds=None,
        resume_threads=True,
    )
    monkeypatch.setattr(plugin, "_settings", lambda: settings)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())
    yield
    plugin.set_backend_for_testing(original_backend)


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def run(
        self, *, session_id: str, prompt: str, state: dict[str, object]
    ) -> str:
        self.calls.append((session_id, prompt, state))
        return "internal-command-result"


def test_run_model_delegates_internal_commands_to_runtime_agent() -> None:
    state: dict[str, object] = {"_runtime_agent": FakeAgent()}

    result = asyncio.run(plugin.run_model(",help", session_id="session-1", state=state))

    agent = state["_runtime_agent"]
    assert result == "internal-command-result"
    assert isinstance(agent, FakeAgent)
    assert agent.calls == [("session-1", ",help", state)]


class FakeSdkFactory:
    def __init__(self, codex: object) -> None:
        self.codex = codex
        self.sandbox_calls: list[bool] = []

    @contextlib.asynccontextmanager
    async def open(self) -> AsyncGenerator[object, None]:
        yield self.codex

    def sandbox(self, *, yolo_mode: bool) -> str:
        self.sandbox_calls.append(yolo_mode)
        return "full_access" if yolo_mode else "workspace_write"


class FakeCodex:
    def __init__(self, thread: object) -> None:
        self.thread = thread
        self.started: list[dict[str, object]] = []
        self.resumed: list[str] = []

    async def thread_start(self, **kwargs: object) -> object:
        self.started.append(kwargs)
        return self.thread

    async def thread_resume(self, thread_id: str) -> object:
        self.resumed.append(thread_id)
        return self.thread


class StreamingThread:
    id = "thread-stream"

    def __init__(self) -> None:
        self.runs: list[tuple[str, dict[str, object]]] = []
        self.steered: list[str] = []

    async def run(self, prompt: str, **kwargs: object) -> AsyncGenerator[object, None]:
        self.runs.append((prompt, kwargs))

        async def events() -> AsyncGenerator[object, None]:
            yield {"method": "item/agentMessage/delta", "params": {"delta": "hello "}}
            yield {"method": "item/agentMessage/delta", "params": {"delta": "world"}}

        return events()

    async def steer(self, message: str) -> None:
        self.steered.append(message)


class FakeSteering:
    def __init__(self) -> None:
        self._messages = [{"content": "use the SDK"}]

    def drain_injected(self) -> list[dict[str, str]]:
        messages = self._messages
        self._messages = []
        return messages


def test_sdk_backend_starts_thread_streams_output_and_drains_steering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        plugin,
        "_settings",
        lambda: SimpleNamespace(
            model="gpt-test",
            yolo_mode=False,
            timeout_seconds=None,
            resume_threads=True,
        ),
    )
    thread = StreamingThread()
    codex = FakeCodex(thread)
    backend = plugin.CodexSdkBackend(FakeSdkFactory(codex))

    result = asyncio.run(
        backend.run(
            "hello",
            workspace=tmp_path,
            thread_id=None,
            state={"_runtime_steering": FakeSteering()},
        )
    )

    assert result.final_response == "hello world"
    assert result.thread_id == "thread-stream"
    assert result.status == "completed"
    assert codex.started == [{"model": "gpt-test", "sandbox": "workspace_write"}]
    assert codex.resumed == []
    assert thread.runs == [("hello", {"cwd": str(tmp_path)})]
    assert thread.steered == ["use the SDK"]


def test_sdk_backend_resumes_existing_thread(tmp_path: Path) -> None:
    thread = StreamingThread()
    codex = FakeCodex(thread)
    backend = plugin.CodexSdkBackend(FakeSdkFactory(codex))

    asyncio.run(
        backend.run(
            "continue",
            workspace=tmp_path,
            thread_id="thread-existing",
            state={},
        )
    )

    assert codex.resumed == ["thread-existing"]
    assert codex.started == []


def test_sdk_backend_raises_on_failed_turn(tmp_path: Path) -> None:
    class FailedThread:
        id = "thread-failed"

        async def run(self, prompt: str, **kwargs: object) -> object:
            return SimpleNamespace(
                final_response="partial",
                thread_id="thread-failed",
                turn_id="turn-failed",
                status="failed",
            )

    backend = plugin.CodexSdkBackend(FakeSdkFactory(FakeCodex(FailedThread())))

    with pytest.raises(plugin.CodexTurnError):
        asyncio.run(backend.run("fail", workspace=tmp_path, thread_id=None, state={}))


def test_sdk_backend_accepts_enum_like_completed_status(tmp_path: Path) -> None:
    class CompletedStatus:
        name = "completed"

        def __str__(self) -> str:
            return "TurnStatus.completed"

    class CompletedThread:
        id = "thread-completed"

        async def run(self, prompt: str, **kwargs: object) -> object:
            return SimpleNamespace(
                final_response="ok",
                thread_id="thread-completed",
                turn_id="turn-completed",
                status=CompletedStatus(),
            )

    backend = plugin.CodexSdkBackend(FakeSdkFactory(FakeCodex(CompletedThread())))

    result = asyncio.run(
        backend.run("ok", workspace=tmp_path, thread_id=None, state={})
    )

    assert result.status == "completed"
    assert result.final_response == "ok"


class FakeTapes:
    def __init__(self) -> None:
        self.anchor_entries = [
            SimpleNamespace(name="session/start", state={"owner": "human"})
        ]
        self.events: list[tuple[str, str, dict[str, object]]] = []
        self.handoffs: list[tuple[str, str, dict[str, object]]] = []
        self.forks: list[tuple[str, bool]] = []

    def session_tape(self, session_id: str, workspace: Path) -> object:
        return SimpleNamespace(name=f"tape-{session_id}")

    async def ensure_bootstrap_anchor(self, tape_name: str) -> None:
        if not self.anchor_entries:
            self.anchor_entries.append(
                SimpleNamespace(name="session/start", state={"owner": "human"})
            )

    async def anchors(self, tape_name: str) -> list[object]:
        return self.anchor_entries

    async def append_event(
        self, tape_name: str, name: str, payload: dict[str, object]
    ) -> None:
        self.events.append((tape_name, name, payload))

    async def handoff(
        self, tape_name: str, *, name: str, state: dict[str, object]
    ) -> None:
        self.handoffs.append((tape_name, name, state))
        self.anchor_entries.append(SimpleNamespace(name=name, state=state))

    @contextlib.asynccontextmanager
    async def fork_tape(
        self, tape_name: str, merge_back: bool = True
    ) -> AsyncGenerator[None, None]:
        self.forks.append((tape_name, merge_back))
        yield


class FakeRuntimeAgent:
    def __init__(self) -> None:
        self.tapes = FakeTapes()


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run(
        self,
        prompt: str,
        *,
        workspace: Path,
        thread_id: str | None,
        state: dict[str, object],
    ) -> plugin.CodexRunResult:
        self.calls.append(
            {"prompt": prompt, "workspace": workspace, "thread_id": thread_id}
        )
        return plugin.CodexRunResult(
            final_response="done",
            thread_id="thread-new",
            turn_id="turn-new",
            status="completed",
            steering_messages=1,
        )


def test_run_model_uses_sdk_backend_and_writes_codex_handoff_anchor(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend()
    plugin.set_backend_for_testing(backend)  # type: ignore[arg-type]
    agent = FakeRuntimeAgent()
    state = {"_runtime_agent": agent, "_runtime_workspace": str(tmp_path)}

    result = asyncio.run(plugin.run_model("hello", session_id="session-2", state=state))

    assert result == "done"
    assert backend.calls == [
        {"prompt": "hello", "workspace": tmp_path, "thread_id": None}
    ]
    assert agent.tapes.forks == [("tape-session-2", True)]
    assert [event[1] for event in agent.tapes.events] == [
        "codex.run.start",
        "codex.run.finish",
    ]
    assert agent.tapes.handoffs
    tape_name, anchor_name, anchor_state = agent.tapes.handoffs[-1]
    assert tape_name == "tape-session-2"
    assert anchor_name == "codex/handoff"
    assert anchor_state["bub_session_id"] == "session-2"
    assert anchor_state["codex_thread_id"] == "thread-new"
    assert anchor_state["codex_turn_id"] == "turn-new"
    assert anchor_state["summary"] == "done"


def test_run_model_reads_thread_pointer_only_from_handoff_anchor(
    tmp_path: Path,
) -> None:
    backend = RecordingBackend()
    plugin.set_backend_for_testing(backend)  # type: ignore[arg-type]
    agent = FakeRuntimeAgent()
    agent.tapes.anchor_entries.append(
        SimpleNamespace(
            name="codex/handoff",
            state={
                "summary": "previous result",
                "next_steps": "continue",
                "codex_thread_id": "thread-anchor",
            },
        )
    )
    state = {"_runtime_agent": agent, "_runtime_workspace": str(tmp_path)}

    asyncio.run(plugin.run_model("next", session_id="session-3", state=state))

    assert backend.calls[-1]["thread_id"] == "thread-anchor"
    assert not hasattr(plugin, "THREADS_FILE")


def test_normal_turns_do_not_use_codex_e_subprocess() -> None:
    source = inspect.getsource(plugin)

    assert "create_subprocess_exec" not in source
    assert '"codex", "e"' not in source
    assert "codex e" not in source


def test_admit_message_steers_active_turns_and_falls_back_to_follow_up() -> None:
    running = TurnSnapshot(
        session_id="session-1",
        is_running=True,
        running_count=1,
        pending_count=0,
        steering_count=0,
        supports_steering=True,
    )

    decision = plugin.admit_message("session-1", {"content": "fix it"}, running)

    assert decision is not None
    assert decision.action == AdmitAction.INJECT
    assert decision.fallback == AdmitAction.WAIT


def test_admit_message_prevents_concurrent_turn_when_steering_is_unavailable() -> None:
    running = TurnSnapshot(
        session_id="session-1",
        is_running=True,
        running_count=1,
        pending_count=0,
        steering_count=0,
        supports_steering=False,
    )
    idle = TurnSnapshot(
        session_id="session-1",
        is_running=False,
        running_count=0,
        pending_count=0,
        steering_count=0,
        supports_steering=True,
    )

    wait_decision = plugin.admit_message("session-1", {"content": "next"}, running)
    idle_decision = plugin.admit_message("session-1", {"content": "next"}, idle)

    assert wait_decision is not None
    assert wait_decision.action == AdmitAction.WAIT
    assert idle_decision is None
