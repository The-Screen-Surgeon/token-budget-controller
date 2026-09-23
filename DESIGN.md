# Token Budget

Token Budget is a local planning and accounting tool for AI-assisted work. It turns a user-defined task budget into deterministic decisions, records factual usage events supplied by supported clients, and reports progress without reading the task’s content.

The product separates two concerns:

1. **Observed usage:** exact token counts and metadata reported by an integration for a completed request.
2. **Planning controls:** a local budget and policy that decide whether a task may start, continue, or requires an explicit override.

The tool does not claim to reconstruct provider billing, predict hidden context costs, or expose subscription quota that a provider does not publish.

## Scope

The first integrations target Codex and Claude Code. Each integration emits a normalized event after a request completes. The event contains provider/model identity, request and response token counts when available, timestamp, task identifier, and status. It contains no prompt, response, file contents, tool arguments, or diffs.

The budget engine is provider neutral. A task has a stable identifier, a configured token ceiling, an optional event-count ceiling, and a deterministic policy for unknown or missing counts. Every decision can be reproduced from the budget configuration and the stored event ledger.

## Project workflow

The workflow API accepts a build prompt and a complete semantic scope supplied
by the agent or user. Scope must contain deliverables, exclusions,
acceptance_checks, and nonempty phases. Each phase supplies its name, objective,
model, and either a weight or token budget. Missing planning fields return
`needs_clarification`; the engine does not invent generic phases.

It also requires current 5-hour and weekly windows with source metadata,
observation timestamps and a maximum snapshot age, explicit percentage point
caps and sourced positive demand objects for each window, plus separate
demand and calibration freshness limits. It returns either a complete
beginning-to-end schedule or an explicit infeasibility result. Phase budgets
are allocated across actual sessions, every session is covered, and checkpoints
include explicit next-session start epochs and reset timing.

Weighted phases reserve one token per phase before distributing the remainder.
Ordered phases stay contiguous within sessions. Session starts must increase
strictly. If a schedule crosses either current reset, the result is
`needs_refresh_checkpoint` with the complete phase schedule and a checkpoint at
the reset; current-window feasibility is never claimed across stale windows.

The build receipt accepts factual provider usage grouped by model and an
explicit one or two sentence summary of each model's supplied work. Every token
category, including cache creation input, must be present as a number or null.
Null values require `quality: partial` and an `unknown_fields` declaration;
partial aggregates retain unknown fields as null. It reports aggregate factual
tokens separately from provider subscription usage. The subscription section
includes final used and remaining percentages, reset epochs, and countdown
seconds computed from the supplied clock. Counts are never inferred from
prompts, responses, or summaries.

Any model entry marked `partial` makes the receipt aggregate partial, even when
all of that entry's numeric fields happen to be present. This keeps quality
metadata conservative and deterministic. Final subscription snapshots are
validated against the plan's snapshot freshness limit.

## Adaptive execution

Monitoring must change agent behavior. A policy maps factual usage and provider
headroom to an execution mode. Given the same ledger, limits, task class, and
policy version, it must return the same mode and allowed actions.

The initial policy bands are:

| Provider window used | Mode | Required behavior |
|---|---|---|
| below 50% | normal | Use the agreed task budget and checkpoints. |
| 50–69% | efficient | Prefer cheaper models for bounded implementation, one worker at a time, compact context, local deterministic tests, and only required reviews. |
| 70–79% | conserve | Essential work only; pause optional research, parallel agents, broad audits, and scope expansion. |
| 80% or higher | stop | Do not begin another model call for the task without an explicit override or a reset. |

At 50% of a project's allocated budget, the agent pauses before its next model
call and revises the complete remaining plan. The revision preserves all
unfinished acceptance checks while consolidating agents, batching related file
operations, reducing repeated context and handoffs, and reserving usage for
integration, required review, fixes, verification, and the receipt. Model
switching is optional. If the unchanged scope cannot fit, the engine creates a
checkpoint for another budget segment rather than exceeding the cap.

Task token ceilings can move a task into a stricter mode before an account
window does. Policy decisions must name the triggering fact and permitted next
action. Provider-reported subscription windows and locally observed task tokens
remain separate inputs because providers may weight them differently.

## Truthful claims

Token Budget can say:

- “This integration reported N input and M output tokens for this request.”
- “The recorded events for this task total N tokens.”
- “Starting this task would exceed its configured local budget.”
- “The last event was incomplete, unavailable, or estimated,” when that is what the adapter reports.

It must not say:

- that the local ledger is the provider’s official usage or billing record;
- that a subscription’s remaining quota is known when the service gives no machine-readable quota endpoint;
- that a token count is exact when it was inferred or estimated;
- that no provider retains content, since provider retention is outside this tool’s control.

## Non-goals

The product does not proxy model traffic, store conversations, inspect repositories, enforce provider-side limits, or replace provider dashboards and invoices. It also does not promise cross-provider token equivalence: tokenizers and accounting rules differ.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/PRIVACY.md](docs/PRIVACY.md), and [docs/ROADMAP.md](docs/ROADMAP.md).
