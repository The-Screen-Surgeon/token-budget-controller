"""Validate and account for agent supplied project plans."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil, isfinite
import re
from typing import Any


class WorkflowError(ValueError):
    pass


TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_creation_input_tokens",
                "output_tokens", "reasoning_tokens", "total_tokens")


def _real(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise WorkflowError(f"{name} must be a finite real number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowError(f"{name} must be a finite real number") from exc
    if not isfinite(result) or (result <= 0 if positive else result < 0):
        raise WorkflowError(f"{name} must be {'positive' if positive else 'non-negative'}")
    return result


def _integer(value: Any, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        raise WorkflowError(f"{name} must be an integer")
    return value


def _window(raw: Any, name: str, now: int, max_age: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WorkflowError(f"usage.{name} must be an object")
    used = _real(raw.get("used_percent"), f"usage.{name}.used_percent")
    if used > 100:
        raise WorkflowError(f"usage.{name}.used_percent must be <= 100")
    reset = _real(raw.get("resets_at"), f"usage.{name}.resets_at", positive=True)
    if now >= reset:
        raise WorkflowError(f"usage.{name} snapshot is at or after its reset")
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise WorkflowError(f"usage.{name}.source is required")
    observed = _real(raw.get("observed_at"), f"usage.{name}.observed_at")
    if observed > now or now - observed > max_age:
        raise WorkflowError(f"usage.{name} observation is stale or from the future")
    return {"used_percent": used, "resets_at": int(reset), "source": source.strip(), "observed_at": int(observed)}


def _demand(raw: Any, name: str, now: int, max_age: int, max_calibration_age: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WorkflowError(f"{name} must be a sourced object")
    points = _real(raw.get("points"), f"{name}.points", positive=True)
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise WorkflowError(f"{name}.source is required")
    observed = _real(raw.get("observed_at"), f"{name}.observed_at", positive=True)
    calibrated = _real(raw.get("calibrated_at"), f"{name}.calibrated_at", positive=True)
    if observed > now or calibrated > now or now - observed > max_age or now - calibrated > max_calibration_age:
        raise WorkflowError(f"{name} timestamps cannot be from the future")
    return {"points": points, "source": source.strip(), "observed_at": int(observed), "calibrated_at": int(calibrated)}


def _partition(budgets: list[int], sessions: int, capacity: int) -> list[int] | None:
    """Find ordered contiguous nonempty groups with bounded sums."""
    n = len(budgets)
    dp: dict[tuple[int, int], tuple[int, ...] | None] = {(0, 0): ()}
    for group in range(sessions):
        next_dp: dict[tuple[int, int], tuple[int, ...] | None] = {}
        for (start, _), cuts in dp.items():
            total = 0
            for end in range(start, n):
                total += budgets[end]
                if total > capacity:
                    break
                remaining = n - (end + 1)
                if remaining < sessions - group - 1:
                    continue
                candidate = cuts + (end + 1,)
                next_dp.setdefault((end + 1, group + 1), candidate)
        dp = next_dp
    return dp.get((n, sessions))


def _greedy_session_count(budgets: list[int], capacity: int) -> int | None:
    """Count contiguous sessions by deterministic next-fit packing."""
    if any(budget > capacity for budget in budgets):
        return None
    count, used = 1, 0
    for budget in budgets:
        if used and used + budget > capacity:
            count += 1
            used = 0
        used += budget
    return count


def _countdown(epoch: int, now: int) -> int:
    return max(0, epoch - now)


@dataclass(frozen=True)
class Phase:
    number: int
    name: str
    objective: str
    model: str
    token_budget: int
    session: int


def _clarification(prompt: Any, reasons: list[str]) -> dict[str, Any]:
    return {"status": "needs_clarification", "prompt": prompt, "reasons": reasons}


def plan_project(request: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
    """Validate and budget a complete semantic plan supplied by the caller."""
    if not isinstance(request, dict):
        raise WorkflowError("request must be an object")
    prompt = request.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise WorkflowError("prompt must be a non-empty string")
    now = _integer(now if now is not None else request.get("now"), "now")
    scope = request.get("scope")
    if not isinstance(scope, dict):
        return _clarification(prompt, ["scope must provide deliverables, exclusions, acceptance_checks, and phases"])
    missing = []
    for key in ("deliverables", "acceptance_checks", "phases"):
        if not isinstance(scope.get(key), list) or not scope[key]:
            missing.append(f"scope.{key} must be a nonempty list")
    if not isinstance(scope.get("exclusions"), list):
        missing.append("scope.exclusions must be a list")
    if missing:
        return _clarification(prompt, missing)
    max_age = _integer(request.get("max_snapshot_age_seconds"), "max_snapshot_age_seconds")
    usage = request.get("usage")
    if not isinstance(usage, dict):
        raise WorkflowError("usage is required")
    primary = _window(usage.get("primary"), "primary", now, max_age)
    secondary = _window(usage.get("secondary"), "secondary", now, max_age)
    caps = request.get("caps")
    if not isinstance(caps, dict):
        raise WorkflowError("caps with primary_points and secondary_points are required")
    primary_cap = _real(caps.get("primary_points"), "caps.primary_points")
    secondary_cap = _real(caps.get("secondary_points"), "caps.secondary_points")
    demand_age = _integer(request.get("max_demand_age_seconds"), "max_demand_age_seconds")
    calibration_age = _integer(request.get("max_calibration_age_seconds"), "max_calibration_age_seconds")
    primary_demand = _demand(request.get("required_primary_points"), "required_primary_points", now, demand_age, calibration_age)
    secondary_demand = _demand(request.get("required_secondary_points"), "required_secondary_points", now, demand_age, calibration_age)
    max_task = _integer(request.get("max_task_tokens"), "max_task_tokens", positive=True)
    max_session = _integer(request.get("max_session_tokens", max_task), "max_session_tokens", positive=True)
    if primary_demand["points"] > primary_cap or primary["used_percent"] + primary_demand["points"] > 100:
        return {"status": "infeasible", "reasons": ["5-hour demand exceeds its proposed cap or remaining window"]}
    if secondary_demand["points"] > secondary_cap or secondary["used_percent"] + secondary_demand["points"] > 100:
        return {"status": "infeasible", "reasons": ["weekly demand exceeds its proposed cap or remaining window"]}
    phases: list[dict[str, Any]] = []
    for index, raw in enumerate(scope["phases"], 1):
        if not isinstance(raw, dict):
            return _clarification(prompt, [f"scope.phases[{index - 1}] must be an object"])
        if any(not isinstance(raw.get(k), str) or not raw[k].strip() for k in ("name", "objective", "model")):
            return _clarification(prompt, [f"scope.phases[{index - 1}] needs name, objective, and model"])
        if ("token_budget" in raw) == ("weight" in raw):
            return _clarification(prompt, [f"scope.phases[{index - 1}] must provide exactly one of weight or token_budget"])
        phase = {"name": raw["name"].strip(), "objective": raw["objective"].strip(), "model": raw["model"].strip()}
        phase["token_budget"] = _integer(raw["token_budget"], f"scope.phases[{index}].token_budget", positive=True) if "token_budget" in raw else None
        phase["weight"] = _real(raw["weight"], f"scope.phases[{index}].weight", positive=True) if "weight" in raw else None
        phases.append(phase)
    explicit = [p["token_budget"] for p in phases if p["token_budget"] is not None]
    if explicit and len(explicit) != len(phases):
        return _clarification(prompt, ["all phases must use token_budget when any phase uses token_budget"])
    if explicit:
        total = sum(explicit)
        if total > max_task:
            return {"status": "infeasible", "reasons": ["phase budgets exceed max_task_tokens"]}
    else:
        weight_sum = sum(p["weight"] for p in phases)
        total = max_task
        if total < len(phases):
            return {"status": "infeasible", "reasons": ["max_task_tokens must cover at least one token per phase"]}
        allocations = [1] * len(phases)
        remaining = total - len(phases)
        maximum = max(p["weight"] for p in phases)
        normalized = [p["weight"] / maximum for p in phases]
        normalized_sum = sum(normalized)
        raw = [remaining * weight / normalized_sum for weight in normalized]
        floors = [int(value) for value in raw]
        allocations = [base + extra for base, extra in zip(allocations, floors)]
        for index in sorted(range(len(phases)), key=lambda i: (-(raw[i] - floors[i]), i))[:remaining - sum(floors)]:
            allocations[index] += 1
        for phase, budget in zip(phases, allocations): phase["token_budget"] = budget
    total = sum(p["token_budget"] for p in phases)
    required_sessions = _greedy_session_count([p["token_budget"] for p in phases], max_session)
    if required_sessions is None:
        return {"status": "infeasible", "reasons": ["a phase exceeds max_session_tokens"]}
    requested = request.get("session_count")
    session_count = _integer(requested, "session_count", positive=True) if requested is not None else required_sessions
    if session_count < required_sessions or session_count > len(phases):
        return {"status": "infeasible", "reasons": ["session_count cannot cover phase budgets and every session"]}
    starts = request.get("session_start_epochs")
    if not isinstance(starts, list) or len(starts) != session_count:
        return _clarification(prompt, ["session_start_epochs must provide one explicit start epoch per session"])
    starts = [_integer(value, f"session_start_epochs[{i}]") for i, value in enumerate(starts)]
    if any(starts[i] >= starts[i + 1] for i in range(len(starts) - 1)):
        return {"status": "infeasible", "reasons": ["session starts must be strictly increasing"]}
    if any(value < now for value in starts):
        return {"status": "infeasible", "reasons": ["session starts must be at or after now"]}
    cuts = _partition([p["token_budget"] for p in phases], session_count, max_session)
    if cuts is None:
        return {"status": "infeasible", "reasons": ["ordered phases cannot fit nonempty sessions within max_session_tokens"]}
    used, start = [], 0
    for session, end in enumerate(cuts, 1):
        used.append(sum(p["token_budget"] for p in phases[start:end]))
        for phase in phases[start:end]: phase["session"] = session
        start = end
    phase_models = [asdict(Phase(i, p["name"], p["objective"], p["model"], p["token_budget"], p["session"])) for i, p in enumerate(phases, 1)]
    checkpoints = [{"after_session": i, "next_session_start_epoch": starts[i], "reset_epoch": primary["resets_at"]} for i in range(1, session_count)]
    reset_boundary = min(primary["resets_at"], secondary["resets_at"])
    if any(value >= reset_boundary for value in starts):
        checkpoints.insert(0, {"after_session": 0, "at_reset_epoch": reset_boundary,
                               "reason": "refresh both usage windows before continuing"})
        return {"status": "needs_refresh_checkpoint", "prompt": prompt, "scope": scope,
                "session_count": session_count, "phases": phase_models,
                "checkpoints": checkpoints, "budget": {"task_tokens": max_task, "max_session_tokens": max_session,
                "required_primary_points": primary_demand, "required_secondary_points": secondary_demand},
                "max_snapshot_age_seconds": max_age, "max_demand_age_seconds": demand_age,
                "max_calibration_age_seconds": calibration_age,
                "quota_provenance": {"primary": primary_demand, "secondary": secondary_demand}}
    return {"status": "feasible", "prompt": prompt, "scope": scope, "session_count": session_count,
            "phases": phase_models, "session_token_budgets": used, "checkpoints": checkpoints,
            "budget": {"task_tokens": max_task, "max_session_tokens": max_session,
                       "required_primary_points": primary_demand, "required_secondary_points": secondary_demand},
            "usage": {"primary": primary, "secondary": secondary}, "caps": {"primary_points": primary_cap, "secondary_points": secondary_cap},
            "max_snapshot_age_seconds": max_age, "max_demand_age_seconds": demand_age,
            "max_calibration_age_seconds": calibration_age,
            "resets_in_seconds": {"primary": _countdown(primary["resets_at"], now), "secondary": _countdown(secondary["resets_at"], now)},
            "quota_provenance": {"primary": primary_demand, "secondary": secondary_demand}}


def build_receipt(plan: dict[str, Any], *, model_usage: list[dict[str, Any]], final_usage: dict[str, Any], now: int) -> dict[str, Any]:
    """Build a receipt without turning missing provider measurements into zero."""
    if not isinstance(plan, dict) or plan.get("status") != "feasible":
        raise WorkflowError("a feasible plan is required")
    if not isinstance(model_usage, list):
        raise WorkflowError("model_usage must be a list")
    if not model_usage:
        raise WorkflowError("completed build receipt requires at least one model usage entry")
    by_model, aggregate, unknown, partial_seen = [], {key: 0 for key in TOKEN_FIELDS}, set(), False
    for entry in model_usage:
        if not isinstance(entry, dict) or not isinstance(entry.get("model"), str):
            raise WorkflowError("each model usage entry needs a model")
        summary = entry.get("work_summary")
        sentences = len([part for part in re.split(r"[.!?]+", summary.strip()) if part.strip()]) if isinstance(summary, str) else 0
        if not isinstance(summary, str) or len(summary.strip().split()) < 3 or sentences not in (1, 2):
            raise WorkflowError("each model needs an explicit 1-2 sentence work_summary")
        quality = entry.get("quality", "complete")
        if quality not in ("complete", "partial"): raise WorkflowError("usage quality must be complete or partial")
        partial_seen = partial_seen or quality == "partial"
        unknown_fields = entry.get("unknown_fields", [])
        if not isinstance(unknown_fields, list) or any(not isinstance(x, str) for x in unknown_fields): raise WorkflowError("unknown_fields must be a list")
        null_fields = {key for key in TOKEN_FIELDS if entry.get(key) is None}
        if set(unknown_fields) != null_fields:
            raise WorkflowError("unknown_fields must exactly equal the null token fields")
        if quality == "complete" and null_fields:
            raise WorkflowError("complete usage cannot contain unknown token fields")
        for key in TOKEN_FIELDS:
            if key not in entry: raise WorkflowError(f"{key} must be present as a number or null")
            value = entry[key]
            if value is None:
                if quality != "partial" or key not in unknown_fields: raise WorkflowError(f"unknown {key} requires partial quality and unknown_fields")
                unknown.add(key)
            else:
                aggregate[key] += _integer(value, f"{key} for {entry['model']}")
        by_model.append({"model": entry["model"], "work_summary": summary.strip(), "quality": quality, "unknown_fields": unknown_fields, **{key: entry[key] for key in TOKEN_FIELDS}})
    for key in unknown: aggregate[key] = None
    windows = {}
    for name in ("primary", "secondary"):
        window = _window(final_usage.get(name), name, _integer(now, "now"), plan["max_snapshot_age_seconds"])
        windows[name] = {**window, "remaining_percent": 100 - window["used_percent"], "reset_countdown_seconds": _countdown(window["resets_at"], now)}
    return {"scope": plan["scope"], "plan_sessions": plan["session_count"], "model_usage": by_model,
            "aggregate_factual_tokens": aggregate, "aggregate_status": "partial" if (unknown or partial_seen) else "complete",
            "unknown_token_fields": sorted(unknown), "subscription_usage": windows}
