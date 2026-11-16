"""Small offline ownership/settlement checks; no database, network or runtime."""

import asyncio
import threading
from types import SimpleNamespace as NS

import pytest

from domain.agent_turn import AgentConversation, AgentModelTurn, AgentTurnResponse
from domain.capability import CapabilityDependency
from domain.task_execution import ExecutionFailure
from orchestration.agent_broker import AgentBroker
from orchestration.agent_executor import AgentExecutor
from orchestration.agent_model_gateway import PlatformAgentModelGateway
from orchestration.execution_async import accounting_error, run_sync
from orchestration.platform_model_gateway import PlatformModelGateway
from orchestration.platform_tool_gateway import PlatformToolGateway


@pytest.mark.parametrize("unknown", [False, True])
def test_agent_executor_closes_cancelled_child_without_hiding_settlement_uncertainty(monkeypatch, unknown):
    started, closed = asyncio.Event(), []

    class Broker:
        def __init__(self, *_args, **_kwargs):
            pass

        async def open(self):
            return NS(timeout_seconds=10)

        async def fail(self, code):
            closed.append(code)
            if unknown:
                raise ExecutionFailure("AGENT_ACCOUNTING_RESULT_UNKNOWN")

    async def runtime(*_args):
        started.set()
        await asyncio.Event().wait()

    from domain.execution_requirements import AGENT_RUNTIME_PROFILES

    monkeypatch.setattr("orchestration.agent_executor.AgentBroker", Broker)
    monkeypatch.setattr("orchestration.agent_executor.interpret_definition", lambda *_args: NS(
        materialize=lambda: NS(runtime="pi", limits=NS(timeout_seconds=10)),
    ))
    invocation = NS(artifact=None)
    invocation.with_timeout = lambda _seconds: invocation
    executor = AgentExecutor(None, NS(get=lambda _key: NS(
        descriptor=NS(profile=AGENT_RUNTIME_PROFILES["pi"]), adapter=NS(run=runtime),
    )), lifecycle=None, prompts=None)

    async def run():
        task = asyncio.create_task(executor.invoke(invocation))
        await started.wait()
        task.cancel()
        with pytest.raises(ExecutionFailure if unknown else asyncio.CancelledError) as caught:
            await task
        if unknown:
            assert caught.value.code == "AGENT_ACCOUNTING_RESULT_UNKNOWN"

    asyncio.run(run())
    assert closed == ["AGENT_EXECUTION_CANCELLED"]


class BlockingPort:
    def __init__(self, *, result=True, error=False):
        self.loop = asyncio.get_running_loop()
        self.started = asyncio.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.result = result
        self.error = error
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        self.loop.call_soon_threadsafe(self.started.set)
        try:
            assert self.release.wait(3), "Test did not release its synchronous port"
            if self.error:
                if isinstance(self.error, BaseException):
                    raise self.error
                raise OSError("Synthetic uncertain commit")
            return self.result
        finally:
            self.finished.set()


async def cancel_while_blocked(task, port):
    await asyncio.wait_for(port.started.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not port.finished.is_set()


def test_run_sync_preserves_results_kwargs_and_normal_exceptions():
    async def check():
        assert await run_sync(lambda *, value: value, value="receipt") == "receipt"

        def fail():
            raise ValueError("Synthetic failure")

        with pytest.raises(ValueError, match="Synthetic failure"):
            await run_sync(fail)

    asyncio.run(check())


def test_repeated_cancellation_drains_and_retains_committed_id():
    async def check():
        port = BlockingPort(result="committed-id")
        receipts = []
        task = asyncio.create_task(run_sync(port, _on_cancel_result=receipts.append))
        try:
            await cancel_while_blocked(task, port)
        finally:
            port.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert port.finished.is_set() and receipts == ["committed-id"]
        assert port.calls == 1

    asyncio.run(check())


@pytest.mark.parametrize("classify", [False, True])
def test_late_exception_is_retrieved_and_accounting_uncertainty_is_not_cancelled(classify):
    async def check():
        unhandled = []
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: unhandled.append(context))
        port = BlockingPort(error=True)
        callback = accounting_error("MODEL_ACCOUNTING_RESULT_UNKNOWN") if classify else None
        task = asyncio.create_task(run_sync(port, _on_cancel_error=callback))
        try:
            await cancel_while_blocked(task, port)
        finally:
            port.release.set()
        expected = ExecutionFailure if classify else asyncio.CancelledError
        with pytest.raises(expected):
            await task
        if classify:
            assert task.exception().code == "MODEL_ACCOUNTING_RESULT_UNKNOWN"
        assert port.finished.is_set() and not unhandled

    asyncio.run(check())


@pytest.mark.parametrize("code", ["MODEL_UPSTREAM_UNAVAILABLE", "MODEL_ACCOUNTING_RESULT_UNKNOWN"])
def test_late_ledger_failure_cannot_become_safe_operational_degradation(code):
    async def check():
        port = BlockingPort(error=ExecutionFailure(code))
        task = asyncio.create_task(run_sync(port, _on_cancel_error=accounting_error("MODEL_ACCOUNTING_RESULT_UNKNOWN")))
        try:
            await cancel_while_blocked(task, port)
        finally:
            port.release.set()
        with pytest.raises(ExecutionFailure, match="MODEL_ACCOUNTING_RESULT_UNKNOWN"):
            await task

    asyncio.run(check())


def terminal_operation(kind, port, rejects):
    claim = NS(lease=NS(attempt_id="attempt"), task=NS(project_key="project", submitted_by="service", id="task"),
               artifact=NS(content_hash="agent-hash", output_schema={}),
               limit_timeout=lambda _timeout: 10)
    resources = NS(validation=NS(validate=lambda *_args: None))
    if kind == "model":
        gateway = PlatformModelGateway(None, NS(finish_call=port), None, None, validator=None)
        return gateway._finish(claim, "call", status="succeeded")
    if kind == "tool":
        gateway = PlatformToolGateway.__new__(PlatformToolGateway)
        gateway.calls = NS(finish_call=port)
        return gateway._finish(claim, "call", status="succeeded")
    if kind in {"broker", "broker_fail"}:
        broker = AgentBroker(claim, NS(resolve_agent=lambda *_args: None), None,
                             lifecycle=NS(finish=port, fail=lambda *_a, **_k: rejects.append("fail")), prompts=None)
        broker._phase, broker._final, broker._execution_id = "final", {}, "execution"
        broker._deadline = asyncio.get_running_loop().time() + 10
        if kind == "broker_fail":
            broker._lifecycle.fail = port
            return broker.fail("MODEL_UPSTREAM_UNAVAILABLE")

        async def finish():
            try:
                return await broker.finish()
            finally:
                with pytest.raises(ExecutionFailure, match="AGENT_ACCOUNTING_RESULT_UNKNOWN"):
                    await broker.fail("MODEL_UPSTREAM_UNAVAILABLE")

        return finish()
    if kind == "agent_tool":
        gateway = PlatformToolGateway.__new__(PlatformToolGateway)
        gateway.resources = resources
        gateway.registry = NS(resolve=lambda *_args: object())
        step = NS(id="step", resource_key="tool", resource_version="1", resource_hash="hash")
        gateway.agent_calls = NS(start_tool=lambda *_a, **_k: (step, None), finish_tool=port,
                                 reject_call=lambda *_a, **_k: rejects.append("reject"))
        gateway.resolve = lambda *_args: NS(
            definition=NS(timeout_seconds=10), artifact=NS(input_schema={}, content_hash="hash"),
            binding=NS(handler_key="handler", handler_version="1"))

        async def execute(*args):
            await args[-1](status="succeeded", output={})
            return {}

        gateway._execute = execute
        return gateway.invoke_agent(claim, "execution", "dispatch", CapabilityDependency(key="tool", version="1"), {})

    gateway = PlatformAgentModelGateway.__new__(PlatformAgentModelGateway)
    gateway.resources = resources
    gateway.credentials = NS(resolve=lambda *_args: None)
    gateway.turns = NS(start_turn=lambda *_a, **_k: (NS(id="step"), None), accept_turn=port,
                       reject_call=lambda *_a, **_k: rejects.append("reject"))
    resolved = NS(artifact=NS(input_schema={}, output_schema={}, content_hash="hash"),
                  provider=NS(credential_ref="synthetic", base_url="https://example.invalid"),
                  policy=NS(timeout_seconds=10, model="model", max_tokens=10, temperature=None, request_profile="openai_chat"))
    definition = NS(tools=[])
    gateway.resolve_agent = lambda *_args: (definition, resolved, (), ())

    async def generate(_request):
        return AgentTurnResponse(turn=AgentModelTurn(kind="final", text="{}"), actual_model="model")

    gateway.transport = NS(generate_turn=generate)
    conversation = AgentConversation.model_validate({"messages": [
        {"role": "system", "content": "Instructions"}, {"role": "user", "content": "Input"},
    ]})
    return gateway.generate_turn(claim, "execution", conversation)


@pytest.mark.parametrize("kind", ["model", "tool", "agent_turn", "agent_tool", "broker", "broker_fail"])
@pytest.mark.parametrize("outcome", ["cancel", "error", "cancel_error"])
def test_terminal_write_drains_without_second_settlement(kind, outcome):
    async def check():
        port = BlockingPort(result=[] if kind == "agent_turn" else True, error=outcome != "cancel")
        rejects = []
        task = asyncio.create_task(terminal_operation(kind, port, rejects))
        try:
            if outcome.startswith("cancel"):
                await cancel_while_blocked(task, port)
            else:
                await asyncio.wait_for(port.started.wait(), 1)
        finally:
            port.release.set()
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            prefix = "AGENT" if kind.startswith("broker") else "TOOL" if kind in {"tool", "agent_tool"} else "MODEL"
            with pytest.raises(ExecutionFailure, match=f"{prefix}_ACCOUNTING_RESULT_UNKNOWN"):
                await task
        assert port.finished.is_set() and port.calls == 1
        assert not rejects

    asyncio.run(check())


@pytest.mark.parametrize("error", [False, True])
def test_broker_begin_drains_and_remembers_late_execution(monkeypatch, error):
    async def check():
        definition = NS(runtime="synthetic", limits=NS(timeout_seconds=10))
        monkeypatch.setattr("orchestration.agent_broker.interpret_definition",
                            lambda *_args: NS(materialize=lambda: definition))
        claim = NS(lease="lease", artifact=None, parent_node_id=None, input={},
                   task=NS(project_key="project"), limit_timeout=lambda _timeout: 10)
        claim.with_timeout = lambda _timeout: claim
        definition.instruction_prompt = NS(key="instructions", version="1")
        port = BlockingPort(result=NS(id="late-execution"), error=error)
        broker = AgentBroker(claim, NS(resolve_agent=lambda *_args: (definition, None, (), ())),
                             NS(key="synthetic", profile="synthetic"), lifecycle=NS(begin=port),
                             prompts=NS(render=lambda *_args: {"text": "Instructions"}))
        task = asyncio.create_task(broker.open())
        try:
            await cancel_while_blocked(task, port)
        finally:
            port.release.set()
        expected = ExecutionFailure if error else asyncio.CancelledError
        with pytest.raises(expected):
            await task
        if error:
            assert task.exception().code == "AGENT_ACCOUNTING_RESULT_UNKNOWN"
        else:
            assert broker._execution_id == "late-execution"
        assert port.finished.is_set() and port.calls == 1

    asyncio.run(check())
