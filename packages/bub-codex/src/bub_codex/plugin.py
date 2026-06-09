from __future__ import annotations

import contextlib
import inspect
import time
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import bub
from bub import hookimpl
from bub.envelope import content_of
from bub.turn_admission import AdmitAction, AdmitDecision, TurnSnapshot
from bub.types import Envelope, State
from pydantic import Field
from pydantic_settings import SettingsConfigDict

from bub_codex.utils import with_bub_skills

if TYPE_CHECKING:
    from bub.builtin.agent import Agent


@dataclass(frozen=True)
class CodexRunResult:
    """Normalized result from one Codex SDK turn."""

    final_response: str
    thread_id: str | None = None
    turn_id: str | None = None
    status: str = "completed"
    events: list[dict[str, Any]] = field(default_factory=list)
    steering_messages: int = 0


class CodexTurnError(RuntimeError):
    """Raised when the SDK reports a failed Codex turn."""


@bub.config(name="codex")
class CodexSettings(bub.Settings):
    """Configuration for Codex plugin."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_CODEX_", env_file=".env", extra="ignore"
    )
    model: str | None = Field(default=None)
    yolo_mode: bool = False
    timeout_seconds: float | None = Field(default=None, gt=0)
    resume_threads: bool = True


supports_steering = True


def _settings() -> CodexSettings:
    return bub.ensure_config(CodexSettings)


def workspace_from_state(state: State) -> Path:
    raw = state.get("_runtime_workspace")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def _runtime_agent_from_state(state: State) -> Agent | None:
    agent = state.get("_runtime_agent")
    if agent is None:
        return None
    return cast("Agent", agent)


def _format_continuation(anchor_state: dict[str, Any]) -> str:
    parts: list[str] = ["[Continuation from previous Codex handoff]"]
    if summary := anchor_state.get("summary"):
        parts.append(f"Summary: {summary}")
    if next_steps := anchor_state.get("next_steps"):
        parts.append(f"Next steps: {next_steps}")
    return "\n".join(parts) if len(parts) > 1 else ""


def _prompt_to_text(prompt: str | list[dict]) -> str:
    if isinstance(prompt, str):
        return prompt
    return "\n".join(
        str(part.get("text", ""))
        for part in prompt
        if isinstance(part, dict) and part.get("type") == "text"
    ).strip()


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _signature_supported_kwargs(
    callable_obj: Any, kwargs: dict[str, Any]
) -> dict[str, Any]:
    if not kwargs:
        return kwargs
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return kwargs
    params = signature.parameters.values()
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params):
        return kwargs
    supported = {
        param.name
        for param in params
        if param.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return {name: value for name, value in kwargs.items() if name in supported}


def _type_error_rejects_kwarg(exc: TypeError, name: str) -> bool:
    message = str(exc)
    return name in message and "keyword" in message


async def _call_first(
    obj: Any, names: tuple[str, ...], *args: Any, **kwargs: Any
) -> Any:
    last_type_error: TypeError | None = None
    for name in names:
        method = getattr(obj, name, None)
        if not callable(method):
            continue
        call_kwargs = _signature_supported_kwargs(method, kwargs)
        try:
            return await _maybe_await(method(*args, **call_kwargs))
        except TypeError as exc:
            last_type_error = exc
            if "timeout" in call_kwargs and _type_error_rejects_kwarg(exc, "timeout"):
                retry_kwargs = dict(call_kwargs)
                retry_kwargs.pop("timeout")
                return await _maybe_await(method(*args, **retry_kwargs))
            raise
    if last_type_error is not None:
        raise last_type_error
    raise AttributeError(f"object exposes none of: {', '.join(names)}")


def _object_field(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _event_dict(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return dict(event)
    data: dict[str, Any] = {}
    for name in ("method", "type", "kind", "status", "delta", "text", "message"):
        value = getattr(event, name, None)
        if value is not None:
            data[name] = value
    params = getattr(event, "params", None)
    if isinstance(params, dict):
        data["params"] = params
    return data


def _event_text_delta(event: Any) -> str:
    event_data = _event_dict(event)
    params = event_data.get("params")
    if isinstance(params, dict):
        delta = params.get("delta")
        if isinstance(delta, str):
            return delta
        data = params.get("data")
        if isinstance(data, dict) and isinstance(data.get("delta"), str):
            return data["delta"]
    for name in ("delta", "text", "message"):
        value = event_data.get(name)
        if isinstance(value, str):
            return value
    return ""


def _result_text(result: Any, streamed_text: str) -> str:
    for name in ("final_response", "content", "text", "message"):
        value = _object_field(result, name)
        if isinstance(value, str) and value.strip():
            return value
    return streamed_text


def _result_status(result: Any) -> str:
    status = _object_field(result, "status", "state")
    if status is None:
        return "completed"
    value = getattr(status, "value", None)
    if value is not None:
        return str(value)
    name = getattr(status, "name", None)
    if name is not None:
        return str(name).lower()
    text = str(status)
    if "." in text:
        return text.rsplit(".", 1)[-1].lower()
    return text


def _result_thread_id(result: Any, thread: Any) -> str | None:
    value = _object_field(result, "thread_id", "threadId")
    if isinstance(value, str) and value:
        return value
    value = _object_field(thread, "id", "thread_id", "threadId")
    return str(value) if value else None


def _result_turn_id(result: Any) -> str | None:
    value = _object_field(result, "turn_id", "turnId")
    return str(value) if value else None


def _summarize_response(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= 500:
        return compact
    return f"{compact[:497]}..."


class OpenAICodexSdkFactory:
    """Small import boundary for the openai-codex Python SDK."""

    @contextlib.asynccontextmanager
    async def open(self) -> AsyncIterator[Any]:
        from openai_codex import AsyncCodex

        async with AsyncCodex() as codex:
            yield codex

    def sandbox(self, *, yolo_mode: bool) -> Any:
        from openai_codex import Sandbox

        return Sandbox.full_access if yolo_mode else Sandbox.workspace_write


class CodexSdkBackend:
    """Adapter that keeps SDK/app-server details below the Bub hook boundary."""

    def __init__(self, factory: OpenAICodexSdkFactory | Any | None = None) -> None:
        self._factory = factory or OpenAICodexSdkFactory()

    async def run(
        self,
        prompt: str,
        *,
        workspace: Path,
        thread_id: str | None,
        state: State,
    ) -> CodexRunResult:
        async with self._factory.open() as codex:
            thread = await self._open_thread(codex, thread_id)
            run_kwargs = self._turn_kwargs(workspace)
            with with_bub_skills(workspace):
                raw_result = await self._run_thread(thread, prompt, state, run_kwargs)
            status = _result_status(raw_result)
            result = CodexRunResult(
                final_response=_result_text(raw_result, ""),
                thread_id=_result_thread_id(raw_result, thread),
                turn_id=_result_turn_id(raw_result),
                status=status,
                events=getattr(raw_result, "_bub_events", []),
                steering_messages=getattr(raw_result, "_bub_steering_messages", 0),
            )
            if status.lower() not in {"completed", "success", "succeeded", "done"}:
                raise CodexTurnError(f"Codex turn finished with status {status}")
            return result

    async def _open_thread(self, codex: Any, thread_id: str | None) -> Any:
        settings = _settings()
        if thread_id and settings.resume_threads:
            try:
                return await _call_first(
                    codex,
                    ("thread_resume", "resume_thread"),
                    thread_id,
                )
            except (AttributeError, RuntimeError, ValueError):
                pass
        kwargs: dict[str, Any] = {}
        if settings.model:
            kwargs["model"] = settings.model
        try:
            sandbox = self._factory.sandbox(yolo_mode=settings.yolo_mode)
        except Exception:
            sandbox = None
        if sandbox is not None:
            kwargs["sandbox"] = sandbox
        return await _call_first(codex, ("thread_start", "start_thread"), **kwargs)

    def _turn_kwargs(self, workspace: Path) -> dict[str, Any]:
        settings = _settings()
        kwargs: dict[str, Any] = {"cwd": str(workspace)}
        if settings.timeout_seconds is not None:
            kwargs["timeout"] = settings.timeout_seconds
        return kwargs

    async def _run_thread(
        self,
        thread: Any,
        prompt: str,
        state: State,
        kwargs: dict[str, Any],
    ) -> Any:
        run_result = await _call_first(thread, ("run", "turn"), prompt, **kwargs)
        if hasattr(run_result, "__aiter__"):
            return await self._consume_stream(thread, run_result, state)
        return run_result

    async def _consume_stream(
        self,
        thread: Any,
        stream: AsyncGenerator[Any, None],
        state: State,
    ) -> Any:
        parts: list[str] = []
        events: list[dict[str, Any]] = []
        steering_messages = 0
        final_result: Any = None
        async for event in stream:
            event_data = _event_dict(event)
            events.append(event_data)
            if delta := _event_text_delta(event):
                parts.append(delta)
            if result := _object_field(event, "result"):
                final_result = result
            steering_messages += await self._drain_steering(thread, state)
        steering_messages += await self._drain_steering(thread, state)
        if final_result is None:
            final_result = SimpleNamespace(
                final_response="".join(parts),
                status="completed",
                thread_id=_object_field(thread, "id", "thread_id", "threadId"),
            )
        elif isinstance(final_result, dict):
            final_result = SimpleNamespace(**final_result)
        setattr(final_result, "_bub_events", events)
        setattr(final_result, "_bub_steering_messages", steering_messages)
        return final_result

    async def _drain_steering(self, thread: Any, state: State) -> int:
        control = state.get("_runtime_steering")
        drain = getattr(control, "drain_injected", None)
        if not callable(drain):
            return 0
        messages = drain()
        sent = 0
        for message in messages:
            text = content_of(message).strip()
            if not text:
                continue
            await _call_first(thread, ("steer", "turn_steer"), text)
            sent += 1
        return sent


_backend: CodexSdkBackend = CodexSdkBackend()


def set_backend_for_testing(backend: CodexSdkBackend) -> None:
    global _backend
    _backend = backend


async def _run_internal_command(
    prompt: str, session_id: str, state: State
) -> str | None:
    if not prompt.strip().startswith(","):
        return None
    agent = _runtime_agent_from_state(state)
    if agent is None:
        return None
    return await agent.run(session_id=session_id, prompt=prompt, state=state)


async def _latest_anchor_state(agent: Agent, tape_name: str) -> dict[str, Any]:
    anchors = await agent.tapes.anchors(tape_name)
    for anchor in reversed(anchors):
        if anchor.name == "codex/handoff":
            return dict(anchor.state)
    if anchors:
        return dict(anchors[-1].state)
    return {}


def _thread_id_from_anchor(anchor_state: dict[str, Any]) -> str | None:
    value = anchor_state.get("codex_thread_id")
    return value if isinstance(value, str) and value.strip() else None


async def _write_handoff_anchor(
    agent: Agent,
    *,
    tape_name: str,
    session_id: str,
    workspace: Path,
    result: CodexRunResult,
) -> None:
    settings = _settings()
    state: dict[str, Any] = {
        "kind": "codex/handoff",
        "bub_session_id": session_id,
        "tape_name": tape_name,
        "cwd": str(workspace),
        "summary": _summarize_response(result.final_response),
        "next_steps": "Await the next Bub message.",
        "status": result.status,
        "steering_messages": result.steering_messages,
    }
    if settings.model:
        state["model"] = settings.model
    if result.thread_id:
        state["codex_thread_id"] = result.thread_id
    if result.turn_id:
        state["codex_turn_id"] = result.turn_id
    await agent.tapes.handoff(tape_name, name="codex/handoff", state=state)


@hookimpl
async def run_model(prompt: str | list[dict], session_id: str, state: State) -> str:
    prompt_text = _prompt_to_text(prompt)
    internal_command_result = await _run_internal_command(prompt_text, session_id, state)
    if internal_command_result is not None:
        return internal_command_result

    workspace = workspace_from_state(state)
    agent = _runtime_agent_from_state(state)
    tape_name: str | None = None
    anchor_state: dict[str, Any] = {}
    thread_id: str | None = None

    if agent is not None:
        tape = agent.tapes.session_tape(session_id, workspace)
        tape_name = tape.name

    start = time.monotonic()
    async with (
        agent.tapes.fork_tape(tape_name, merge_back=True)
        if agent and tape_name
        else _noop_context()
    ):
        if agent and tape_name:
            await agent.tapes.ensure_bootstrap_anchor(tape_name)
            anchor_state = await _latest_anchor_state(agent, tape_name)
            thread_id = _thread_id_from_anchor(anchor_state)
            if thread_id is None:
                continuation = _format_continuation(anchor_state)
                if continuation:
                    prompt_text = f"{continuation}\n\n---\n\n{prompt_text}"
            await agent.tapes.append_event(
                tape_name,
                "codex.run.start",
                {"thread_id": thread_id, "backend": "openai-codex-sdk"},
            )

        result = await _backend.run(
            prompt_text,
            workspace=workspace,
            thread_id=thread_id,
            state=state,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if agent and tape_name:
            await agent.tapes.append_event(
                tape_name,
                "codex.run.finish",
                {
                    "thread_id": result.thread_id,
                    "turn_id": result.turn_id,
                    "status": result.status,
                    "elapsed_ms": elapsed_ms,
                    "backend": "openai-codex-sdk",
                },
            )
            await _write_handoff_anchor(
                agent,
                tape_name=tape_name,
                session_id=session_id,
                workspace=workspace,
                result=result,
            )

    return result.final_response


@contextlib.asynccontextmanager
async def _noop_context() -> AsyncGenerator[None, None]:
    yield


@hookimpl
def admit_message(
    session_id: str, message: Envelope, turn: TurnSnapshot
) -> AdmitDecision | None:
    """Steer active Codex turns; otherwise queue as follow-up instead of racing."""
    if not turn.is_running:
        return None
    if turn.supports_steering:
        return AdmitDecision(
            action=AdmitAction.INJECT,
            fallback=AdmitAction.WAIT,
            reason="codex turn in progress",
        )
    return AdmitDecision(action=AdmitAction.WAIT, reason="codex turn in progress")


__all__ = [
    "CodexRunResult",
    "CodexSdkBackend",
    "CodexSettings",
    "CodexTurnError",
    "run_model",
    "set_backend_for_testing",
    "supports_steering",
]
