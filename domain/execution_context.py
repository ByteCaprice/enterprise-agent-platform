"""Runtime-only execution budgets shared by Task, Workflow, Agent and gateways.

A budget is an absolute monotonic deadline, not a lease, permission, persisted
checkpoint or client-supplied timeout. Descendants may narrow it, never extend it.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


class ExecutionDeadlineExceeded(TimeoutError):
    """The inherited execution budget is exhausted before starting more work."""


def _duration(seconds: float) -> float:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Execution timeout must be a positive finite duration")
    return float(seconds)


@dataclass(frozen=True)
class ExecutionBudget:
    deadline: float = field(repr=False)

    def __post_init__(self):
        if isinstance(self.deadline, bool) or not isinstance(self.deadline, (int, float)) or not math.isfinite(self.deadline):
            raise ValueError("Execution deadline must be a finite monotonic timestamp")

    @classmethod
    def start(cls, seconds: float) -> ExecutionBudget:
        return cls(time.monotonic() + _duration(seconds))

    def child(self, seconds: float) -> ExecutionBudget:
        """A node or operation cannot increase the budget inherited from its parent."""
        return ExecutionBudget(min(self.deadline, time.monotonic() + _duration(seconds)))

    def limit_timeout(self, seconds: float) -> float:
        configured = _duration(seconds)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ExecutionDeadlineExceeded("Execution budget exhausted")
        return min(configured, remaining)


def effective_timeout(budget: ExecutionBudget | None, seconds: float) -> float:
    """Standalone adapters keep their explicit policy timeout; owned calls inherit it."""
    return _duration(seconds) if budget is None else budget.limit_timeout(seconds)
