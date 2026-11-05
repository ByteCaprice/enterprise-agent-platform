"""Task-scoped, typed Pi JSONL bridge, independent of the legacy runtime package."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from domain.agent_runtime import (
    AgentRuntimeCallbacks,
    AgentRuntimeDescriptor,
    AgentRuntimeResult,
    AgentRuntimeStart,
    AgentRuntimeUnstoppable,
)
from domain.agent_turn import RuntimeTurn, strict_json
from domain.task_execution import ExecutionFailure

_NODE_DIR = Path(__file__).parent / "node"
PI_PROFILE = "enterprise-ai.pi.v1"
_MAX_FRAME = 2 * 1024 * 1024
_CHILD_ENV = frozenset({"PATH", "LANG", "LC_ALL", "TZ", "TMPDIR", "SystemRoot"})


class _Frame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile: Literal["enterprise-ai.pi.v1"]


class _Ready(_Frame):
    type: Literal["ready"]
    pi_agent_core: Literal["1.4.0"]
    pi_ai: Literal["1.4.0"]


class _ModelRequest(_Frame):
    type: Literal["request"]
    id: int = Field(strict=True, ge=1, le=1024)
    operation: Literal["model"]


class _ToolRequest(_Frame):
    type: Literal["request"]
    id: int = Field(strict=True, ge=1, le=1024)
    operation: Literal["tool"]
    dispatch_id: str = Field(min_length=1, max_length=200)


_Request = Annotated[_ModelRequest | _ToolRequest, Field(discriminator="operation")]


class _Result(AgentRuntimeResult):
    profile: Literal["enterprise-ai.pi.v1"]
    type: Literal["result"]


class _Failure(_Frame):
    type: Literal["error"]
    code: Literal["aborted", "runtime_failed"]


_WIRE = TypeAdapter(Annotated[_Ready | _Request | _Result | _Failure, Field(discriminator="type")])


def child_environment() -> dict[str, str]:
    """No application credentials, proxies, Node preload flags or custom CA paths."""
    return {key: value for key, value in os.environ.items() if key in _CHILD_ENV}


def _command() -> list[str]:
    node = shutil.which("node")
    if node is None or not (_NODE_DIR / "platform-runner.mjs").is_file():
        raise ExecutionFailure("AGENT_PI_UNAVAILABLE")
    try:
        for name in ("pi-agent-core", "pi-ai"):
            path = _NODE_DIR / "node_modules" / "@earendil-works" / name / "package.json"
            if json.loads(path.read_text())["version"] != "1.4.0":
                raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise ExecutionFailure("AGENT_PI_DEPENDENCIES_UNAVAILABLE") from None
    return [node, str(_NODE_DIR / "platform-runner.mjs")]


async def _send(process, frame):
    try:
        body = json.dumps(frame, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode() + b"\n"
        if len(body) > _MAX_FRAME or process.stdin is None or process.stdin.is_closing():
            raise ValueError
        process.stdin.write(body)
        await process.stdin.drain()
    except (ValueError, TypeError, UnicodeError, OSError):
        raise ExecutionFailure("AGENT_PI_PROTOCOL_INVALID") from None


async def _receive(process):
    try:
        assert process.stdout is not None
        line = await process.stdout.readline()
        if not line:
            raise ExecutionFailure("AGENT_PI_EXITED")
        if len(line) > _MAX_FRAME or not line.endswith(b"\n"):
            raise ValueError
        return _WIRE.validate_python(strict_json(line))
    except ExecutionFailure:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ExecutionFailure("AGENT_PI_PROTOCOL_INVALID") from None


async def _discard(stream):
    if stream is not None:
        while await stream.read(8192):
            pass


def _signal_group(process, sig):
    with contextlib.suppress(ProcessLookupError):
        if os.name == "posix":
            os.killpg(process.pid, sig)
        elif process.returncode is None:
            process.kill() if sig == signal.SIGKILL else process.terminate()


async def _shutdown(process, stderr):
    # Draining releases StreamReader backpressure before waiting/reaping.
    stdout = asyncio.create_task(_discard(process.stdout))
    try:
        if process.returncode is None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(_send(process, {"profile": PI_PROFILE, "type": "abort"}), 0.15)
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 0.35)
            except TimeoutError:
                _signal_group(process, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 0.5)
                except TimeoutError:
                    _signal_group(process, signal.SIGKILL)
        # Include descendants which could retain a pipe after the leader exits.
        _signal_group(process, signal.SIGKILL)
        try:
            await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
            raise AgentRuntimeUnstoppable("Pi process could not be reaped") from None
    except AgentRuntimeUnstoppable:
        raise
    except Exception:
        raise AgentRuntimeUnstoppable("Pi process cleanup failed") from None
    finally:
        if process.stdin is not None:
            process.stdin.close()
        for task in (stdout, stderr):
            task.cancel()
        await asyncio.gather(stdout, stderr, return_exceptions=True)


@asynccontextmanager
async def _session(*, check=False):
    try:
        process = await asyncio.create_subprocess_exec(
            *_command(), *(["--check"] if check else []), cwd=_NODE_DIR, env=child_environment(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=_MAX_FRAME, start_new_session=os.name == "posix",
        )
    except OSError:
        raise ExecutionFailure("AGENT_PI_UNAVAILABLE") from None
    stderr = asyncio.create_task(_discard(process.stderr))
    try:
        yield process
    finally:
        cleanup = asyncio.create_task(_shutdown(process, stderr))
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        cleanup.result()
        if interrupted:
            raise asyncio.CancelledError


async def _normal_exit(process):
    assert process.stdout is not None
    if await asyncio.wait_for(process.stdout.read(1), 1):
        raise ExecutionFailure("AGENT_PI_PROTOCOL_INVALID")
    await asyncio.wait_for(process.wait(), 1)
    if process.returncode != 0:
        raise ExecutionFailure("AGENT_PI_EXITED")


async def _callback_while_alive(process, operation):
    """A dead Pi child cannot leave its Python model/tool request running unnoticed."""
    callback = asyncio.create_task(operation)
    exited = asyncio.create_task(process.wait())
    try:
        done, _ = await asyncio.wait({callback, exited}, return_when=asyncio.FIRST_COMPLETED)
        if exited in done:
            raise ExecutionFailure("AGENT_PI_EXITED")
        return callback.result()
    finally:
        for task in (callback, exited):
            if not task.done():
                task.cancel()
        done, pending = await asyncio.wait({callback, exited}, timeout=2)
        for task in done:
            if not task.cancelled():
                task.exception()
        if pending:
            raise AgentRuntimeUnstoppable("Pi callback did not acknowledge cancellation")


class PiRuntime:
    descriptor = AgentRuntimeDescriptor(key="pi", profile=PI_PROFILE)

    async def check_ready(self) -> None:
        try:
            async with asyncio.timeout(5), _session(check=True) as process:
                if not isinstance(await _receive(process), _Ready):
                    raise ExecutionFailure("AGENT_PI_PROTOCOL_INVALID")
                await _normal_exit(process)
        except TimeoutError:
            raise ExecutionFailure("AGENT_PI_UNAVAILABLE") from None

    async def run(self, start: AgentRuntimeStart, callbacks: AgentRuntimeCallbacks) -> AgentRuntimeResult:
        try:
            async with asyncio.timeout(start.timeout_seconds), _session() as process:
                if not isinstance(await asyncio.wait_for(_receive(process), 5), _Ready):
                    raise ExecutionFailure("AGENT_PI_PROTOCOL_INVALID")
                await _send(process, {"type": "start", "profile": PI_PROFILE, **start.model_dump(mode="json")})
                sequence = turns = tools = 0
                pending: list[str] = []
                final_text = None
                while True:
                    frame = await _receive(process)
                    if isinstance(frame, _Result):
                        if (pending or final_text is None or frame.final_text != final_text or
                                frame.turns != turns or frame.tool_calls != tools):
                            raise ExecutionFailure("AGENT_PI_FINAL_MISMATCH")
                        await _normal_exit(process)
                        return AgentRuntimeResult.model_validate(frame.model_dump(exclude={"type", "profile"}))
                    if isinstance(frame, _Failure):
                        raise ExecutionFailure("AGENT_PI_FAILED")
                    if not isinstance(frame, (_ModelRequest, _ToolRequest)) or frame.id != sequence + 1:
                        raise ExecutionFailure("AGENT_PI_PROTOCOL_INVALID")
                    sequence = frame.id
                    if isinstance(frame, _ModelRequest):
                        if pending or final_text is not None or turns >= start.max_turns:
                            raise ExecutionFailure("AGENT_PI_PROTOCOL_INVALID")
                        turns += 1
                        turn = await _callback_while_alive(process, callbacks.request_model())
                        # Isolate callback-held nested values before writing to the child.
                        turn = RuntimeTurn.model_validate_json(turn.model_dump_json())
                        if turn.kind == "tools":
                            pending = [call.dispatch_id for call in turn.tool_calls]
                            if not pending or len(set(pending)) != len(pending) or tools + len(pending) > start.max_tool_calls:
                                raise ExecutionFailure("AGENT_PI_PROTOCOL_INVALID")
                        elif turn.tool_calls or not turn.text.strip():
                            raise ExecutionFailure("AGENT_PI_PROTOCOL_INVALID")
                        else:
                            final_text = turn.text
                        value = turn.model_dump(mode="json")
                    else:
                        if not pending or pending[0] != frame.dispatch_id:
                            raise ExecutionFailure("AGENT_PI_PROTOCOL_INVALID")
                        value = await _callback_while_alive(process, callbacks.dispatch_tool(frame.dispatch_id))
                        pending.pop(0)
                        tools += 1
                    await _send(process, {"profile": PI_PROFILE, "type": "response", "id": frame.id, "value": value})
        except TimeoutError:
            raise ExecutionFailure("AGENT_RUNTIME_TIMEOUT_RESULT_UNKNOWN", status="timed_out") from None
