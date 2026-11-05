"""Small real-Pi bridge smokes; callbacks are synthetic, no DB/provider/network."""

import asyncio

import pytest

from domain.agent_runtime import AgentRuntimeStart
from domain.agent_turn import AgentToolDescriptor, RuntimeToolCall, RuntimeTurn
from domain.task_execution import ExecutionFailure
from framework.agent_runtime.pi.adapter import PiRuntime, child_environment


def start():
    return AgentRuntimeStart(
        instructions="Synthetic instructions", input_text="{}",
        tools=(AgentToolDescriptor(alias="lookup", description="Synthetic lookup", input_schema={
            "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"],
        }),), max_turns=3, max_tool_calls=1, timeout_seconds=10,
    )


def test_locked_pi_core_really_drives_model_tool_model_and_clean_exit(monkeypatch):
    monkeypatch.setenv("NODE_OPTIONS", "--require=/must-not-be-inherited")
    monkeypatch.setenv("HTTPS_PROXY", "http://must-not-be-inherited")
    monkeypatch.setenv("ENTERPRISE_AI_CREDENTIAL_SYNTHETIC", "must-not-be-inherited")
    assert not {"NODE_OPTIONS", "HTTPS_PROXY", "ENTERPRISE_AI_CREDENTIAL_SYNTHETIC"} & child_environment().keys()
    assert not {"base_url", "api_key", "task_id", "token", "project_key"} & start().model_dump().keys()
    calls = []

    class Ports:
        async def request_model(self):
            calls.append("model")
            if len(calls) == 1:
                return RuntimeTurn(kind="tools", text="", tool_calls=(RuntimeToolCall(
                    dispatch_id="accepted-handle", alias="lookup", arguments={"name": "Synthetic"},
                ),))
            return RuntimeTurn(kind="final", text='{"name":"Synthetic"}')

        async def dispatch_tool(self, handle):
            assert handle == "accepted-handle"
            calls.append("tool")
            return {"name": "Synthetic"}

    async def run():
        runtime = PiRuntime()
        await runtime.check_ready()
        result = await runtime.run(start(), Ports())
        assert result.turns == 2 and result.tool_calls == 1 and result.final_text == '{"name":"Synthetic"}'

    asyncio.run(run())
    assert calls == ["model", "tool", "model"]


def test_cancelled_callback_reaps_real_node_before_cancellation_returns(monkeypatch):
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)

    async def run():
        requested = asyncio.Event()

        class Ports:
            async def request_model(self):
                requested.set()
                await asyncio.Event().wait()

            async def dispatch_tool(self, _handle):
                pytest.fail("No accepted tool intent")

        task = asyncio.create_task(PiRuntime().run(start(), Ports()))
        await asyncio.wait_for(requested.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert processes and all(process.returncode is not None for process in processes)

    asyncio.run(run())


def test_child_exit_cancels_pending_host_callback_instead_of_waiting_for_its_model_timeout(monkeypatch):
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)

    async def run():
        stopped = asyncio.Event()

        class Ports:
            async def request_model(self):
                processes[-1].kill()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

            async def dispatch_tool(self, _handle):
                pytest.fail("No accepted tool intent")

        with pytest.raises(ExecutionFailure, match="AGENT_PI_EXITED"):
            await asyncio.wait_for(PiRuntime().run(start(), Ports()), 5)
        assert stopped.is_set() and all(process.returncode is not None for process in processes)

    asyncio.run(run())
