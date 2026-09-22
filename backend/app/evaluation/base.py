"""Evaluator interface (data plane, evaluation group).

Start/poll split so the supervisor tick loop never blocks: start() kicks off an
evaluation and returns an external reference string (stored on the run row —
this is what makes crash recovery re-attachable); poll() checks progress.
Fast evaluators (health checks) simply complete within start() and return a
terminal outcome from the first poll().
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EvaluatorBusy(RuntimeError):
    """The evaluator is momentarily unwilling (quota, cordon, maintenance) —
    the config is fine. Callers should retry later rather than fail the run."""


class EvaluatorRejected(RuntimeError):
    """The evaluator refused this endpoint (e.g. a failed preflight probe).
    That IS a verdict on the run: fail it instead of retrying."""


class EvalStatus(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


@dataclass
class EvalOutcome:
    status: EvalStatus
    metrics: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class Evaluator(ABC):
    name: str = "base"

    @abstractmethod
    def start(self, endpoint_url: str, served_model_name: str, context: dict[str, Any]) -> str:
        """Begin evaluation; returns an external reference (opaque string)."""

    @abstractmethod
    def poll(self, external_ref: str) -> EvalOutcome: ...

    def cancel(self, external_ref: str) -> None:  # noqa: B027  (optional hook)
        """Best-effort cancel; default no-op."""
