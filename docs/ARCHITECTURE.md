# Architecture

## Event flow

```text
Codex adapter ─┐
               ├─> normalized usage event ─> local ledger ─> budget decision/report
Claude adapter ┘
```

Adapters are deliberately thin. They translate factual per-request metadata into one shared schema and mark unavailable fields explicitly. The ledger validates the schema, assigns an event id, and stores only accounting metadata. The budget engine sums accepted events by task and applies deterministic rules.

## Normalized event

An event should include:

- `event_id` and `task_id`;
- `provider` (`codex` or `claude_code`) and model when available;
- request completion time and adapter schema version;
- `input_tokens`, `output_tokens`, and `total_tokens` when reported;
- a count quality such as `reported`, `estimated`, or `unknown`;
- completion status and an idempotency key.

If a provider supplies only a total, the component fields remain unknown. If it supplies no count, the event remains visible but cannot silently become zero. Replaying an event with the same idempotency key must not double count it.

## Codex and Claude Code

Codex and Claude Code are separate adapters because their hooks, event formats, and token accounting may change independently. The shared contract keeps the budget engine stable while preserving provider-specific provenance. Adapter documentation must identify the exact source field and collection point for each count; a UI estimate is never presented as a provider report.

## Deterministic budgets

A task budget is evaluated against recorded totals plus a configured reservation policy. The policy must specify how to handle unknown counts, late events, retries, and estimated counts. A recommended default is to block new work when a known total reaches the ceiling and to require an explicit override when an event has unknown usage. Decisions should include the input budget, observed total, reservation, and reason so another process can reproduce them.
