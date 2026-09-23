"""Deterministic adaptive execution policy.

The policy consumes only numeric usage facts and an explicit override.  It does
not inspect task content or attempt to estimate provider billing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any


_BANDS = ("normal", "efficient", "conserve", "stop")


@dataclass(frozen=True)
class PolicyDecision:
    """The reproducible result of evaluating the adaptive policy."""

    mode: str
    reason: str
    allowed_behaviors: tuple[str, ...]
    model_guidance: str
    provider_window_used_percent: float
    task_observed_tokens: int
    task_cap: int
    task_used_percent: float
    explicit_override: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON friendly representation of this decision."""
        value = asdict(self)
        value["allowed_behaviors"] = list(self.allowed_behaviors)
        return value


def _validate(provider_percent: float, observed: int, cap: int) -> tuple[float, int, int]:
    try:
        provider_percent = float(provider_percent)
    except (TypeError, ValueError) as exc:
        raise ValueError("provider window usage must be numeric") from exc
    if not isfinite(provider_percent) or not 0 <= provider_percent <= 100:
        raise ValueError("provider window usage must be between 0 and 100")
    if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
        raise ValueError("task observed tokens must be a non-negative integer")
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        raise ValueError("task cap must be a positive integer")
    return provider_percent, observed, cap


def _band(percent: float) -> int:
    if percent < 50:
        return 0
    if percent < 70:
        return 1
    if percent < 80:
        return 2
    return 3


def evaluate_policy(
    provider_window_used_percent: float,
    task_observed_tokens: int,
    task_cap: int,
    explicit_override: bool = False,
) -> PolicyDecision:
    """Evaluate the initial policy from DESIGN.md.

    The stricter of the provider window band and task budget band wins.  At or
    above the task cap, the task is in ``stop`` regardless of the provider
    window.  An override does not erase the stop signal; it records that the
    caller explicitly authorized the next model call.
    """
    provider_percent, observed, cap = _validate(
        provider_window_used_percent, task_observed_tokens, task_cap
    )
    if type(explicit_override) is not bool:
        raise ValueError("explicit override must be a boolean")
    override = explicit_override
    task_percent = (observed / cap) * 100
    provider_level = _band(provider_percent)
    task_level = _band(task_percent)
    level = max(provider_level, task_level)
    mode = _BANDS[level]

    if observed >= cap:
        reason = f"task usage is {observed} tokens against its {cap}-token cap"
    elif task_level > provider_level:
        reason = (
            f"task usage is {task_percent:g}% of its {cap}-token cap, "
            f"stricter than the provider window at {provider_percent:g}%"
        )
    else:
        reason = f"provider window usage is {provider_percent:g}%"

    if mode == "normal":
        behaviors = (
            "use the agreed task budget",
            "use planned checkpoints",
        )
        guidance = "Use the agreed model and task budget."
    elif mode == "efficient":
        behaviors = (
            "prefer cheaper models for bounded implementation",
            "run one worker at a time",
            "keep context compact",
            "run local deterministic tests",
            "perform only required reviews",
        )
        guidance = "Prefer a cheaper model for bounded work and keep the context compact."
    elif mode == "conserve":
        behaviors = (
            "perform essential work only",
            "pause optional research",
            "avoid parallel agents",
            "pause broad audits",
            "avoid scope expansion",
        )
        guidance = "Use the least expensive model that can complete essential work."
    else:
        behaviors = (
            "do not begin another model call",
            "reset the usage window or obtain an explicit override",
        )
        guidance = "Do not start another model call without an explicit override or reset."
        if override:
            behaviors = (
                "make one explicitly authorized model call",
                "keep the call narrowly scoped",
                "re-evaluate usage after the call",
            )
            guidance = "An explicit override permits one narrowly scoped call; re-evaluate afterward."
            reason += "; explicit override supplied"

    return PolicyDecision(
        mode=mode,
        reason=reason,
        allowed_behaviors=behaviors,
        model_guidance=guidance,
        provider_window_used_percent=provider_percent,
        task_observed_tokens=observed,
        task_cap=cap,
        task_used_percent=task_percent,
        explicit_override=override,
    )


# A short alias for callers that prefer imperative policy terminology.
decide_policy = evaluate_policy
