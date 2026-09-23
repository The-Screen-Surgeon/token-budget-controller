# Universal Deterministic Usage Budget Protocol

Version: 1.0.0

Use this protocol when an agent can read factual usage for its own provider.
Follow it before starting a project, before every model call or delegated agent
call, at every phase boundary, and when producing the final receipt.

## Enforcement boundary

This document defines deterministic decisions. It does not itself intercept a
model request. A host hook, wrapper, proxy, or agent runtime provides hard
technical enforcement by refusing calls when this protocol returns `DENY`.
Without an enforcing host, the agent must treat `DENY` as an absolute stop, but
that remains cooperative enforcement.

Never claim a hard cap when the runtime cannot block calls.

## Required adapter contract

The host supplies one normalized snapshot per provider window:

```json
{
  "provider": "provider-name",
  "observed_at": 0,
  "windows": {
    "short": {"used_micropct": 0, "resets_at": 0},
    "long": {"used_micropct": 0, "resets_at": 0}
  },
  "task_tokens": 0,
  "token_quality": "reported"
}
```

`used_micropct` is percentage used multiplied by 1,000,000. Use integers only.
The adapter may rename `short` and `long` for display, but decision logic uses
these stable keys. Provider-reported token fields remain provider specific and
must never be converted into subscription percentages without a sourced,
timestamped calibration.

If factual usage, reset time, or observation time is missing or stale, return
`DENY_USAGE_UNAVAILABLE`. Never estimate missing factual usage from text length.

## Project intake

From the user's build request, establish the complete project before work:

1. Deliverables.
2. Explicit exclusions.
3. Acceptance checks.
4. Ordered phases from scope through final handoff.
5. Model assigned to each phase.
6. Token ceiling for the entire project and each session.
7. Worst-case short- and long-window reservation for every model call.
8. Coordinator reserve for planning, integration, and reporting.

Ask the user only when missing information materially changes scope, budget, or
acceptance. Do not begin implementation with an incomplete plan.

Prefer one session. When the ordered phases cannot fit, preserve the entire plan
and add checkpoints containing completed phases, verified artifacts, remaining
budgets, next phase, and the usage snapshot required before resuming.

## Deterministic gate

Store, for each window:

- `baseline_used_micropct`
- `cap_micropct`
- `current_used_micropct`
- `reserved_micropct` for the proposed next call

Calculate with integer arithmetic:

```text
consumed = max(0, current_used - baseline_used)
projected = consumed + reserved
remaining_task_budget = max(0, cap - consumed)
```

Return `ALLOW` only when every condition is true:

1. Snapshot age is within the configured freshness limit.
2. The reset identifier or timestamp matches the active budget segment.
3. The project and phase are active.
4. `projected <= cap` for every provider window.
5. The task token ceiling and session token ceiling both have enough remaining
   reservation.
6. The coordinator reserve remains intact after the proposed call.
7. The call has a recorded model, purpose, and worst-case reservation source.

Otherwise return `DENY` with stable reason codes. A denial stops all model work.
Local deterministic commands that consume no model usage may still save state,
run tests, or prepare a checkpoint.

## Reservation calibration

Start conservatively. A reservation must use the largest relevant completed
call observed for that provider, model, role, and task class, plus a configured
safety margin. Never use an average as a hard-cap reservation.

After each completed call:

1. Refresh factual usage.
2. Record actual token categories and subscription-window movement.
3. Update calibration history without rewriting prior receipts.
4. Run the gate again before another call.

If one call exceeds its reservation, stop immediately. Increase the future
worst-case calibration; do not borrow silently from later phases.

## Adaptive modes

Adapt the current model's approach before considering a model change. Model
switching is optional and must never substitute for replanning the work.

Use the strictest mode selected by provider headroom or the percentage of this
project's allotted budget already consumed:

| Used | Mode | Required behavior |
|---|---|---|
| below 50% | NORMAL | Execute the validated plan within its caps. |
| 50–69% | REPLAN | Pause before another model call and rewrite the complete remaining plan to fit the remaining allocation. Consolidate agents, batch related file reads and edits, reduce handoffs, reuse verified context, and keep required reviews. |
| 70–79% | CONSERVE | Essential work only; no optional research, parallel work, or scope growth. |
| 80% or higher | STOP | Deny all new model calls until reset or explicit user override. |

An override must name the task, window, added cap, reason, and expiry. Record it
in the receipt. Never infer an override from “continue,” urgency, or prior work.

## Mid-budget replan

When any project budget reaches 50% consumed, emit a warning and pause before
the next model call. This is a mandatory phase boundary.

Create a new revision of the complete remaining plan that:

1. Lists every unfinished deliverable and acceptance check.
2. Uses the factual remaining task tokens and provider-window allocation.
3. Preserves project completion rather than silently dropping scope.
4. Consolidates compatible agent roles and removes duplicate exploration.
5. Batches related file discovery, reads, edits, and tests.
6. Minimizes repeated context loading and cross-agent handoffs.
7. Reserves enough usage for integration, required review, fixes, verification,
   and the final receipt.
8. Fits every remaining call behind the hard pre-call gate.

Validate the revised plan before resuming. If the unchanged scope cannot fit,
stop and create a durable checkpoint for a later budget segment. Ask the user
before changing scope or increasing the cap.

## Review policy

Implementation and review are separate reservations. Start an audit only when
the remaining budget covers the audit, one correction pass, one verification
pass, and coordinator reporting. Otherwise checkpoint the project and defer the
review. Repeated audit loops may not consume unreserved budget.

## Build receipt

At completion or checkpoint, record:

- Original scope and complete plan.
- Planned caps and reservations.
- Baseline and final provider-window usage.
- Actual increase and any overrun for every window.
- Per-model factual token categories.
- One or two sentences describing each model's work.
- Unknown measurements as `null`, never zero.
- Remaining short- and long-window percentages.
- Absolute reset timestamps and countdown seconds.
- Overrides, denied calls, checkpoints, and budget violations.
- Every warning threshold crossed, including usage at the warning.
- Each plan revision, its trigger, and the behavioral changes it introduced.
- Agent consolidation, batching, reduced handoffs, or other token-saving actions.
- The remaining budget predicted by each revision and the actual final result.
- Verification results and unfinished work.

Do not claim counterfactual tokens “saved” unless they were measured against a
comparable recorded baseline. Report the actions and actual consumption instead.

Receipts are append-only evidence. A project that exceeds any cap must say
`BUDGET_VIOLATED`; successful output does not change that result.

## Universal decision rule

When an integration cannot satisfy this protocol's required inputs, the result
is deterministic:

```text
DENY_USAGE_UNAVAILABLE
```

The user may choose to proceed outside managed mode, but the agent must not call
that work monitored, budgeted, or hard-capped.
