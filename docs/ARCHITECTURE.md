# Architecture

## Event flow

```text
Codex JSONL adapter ─┐
                     ├─> normalized usage event ─> local ledger
Claude JSONL adapter ┘

Codex app-server adapter ─> rate-limit snapshot ─> managed controller gate
Codex CLI via codex-managed ─────────────────────> reservation claim + launch
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

## Live Codex boundary

`CodexAppServerAdapter` starts `codex app-server`, performs JSON-RPC initialize,
and reads `account/rateLimits/read` plus `account/usage/read`. It makes no model
call and discards aggregate account usage after validation. Primary and
secondary `usedPercent` integer values map to millionth-percent controller
units (`percent * 1,000,000`). Reset timestamps are both reset identities and
expiries. Missing windows, missing resets, malformed fields, RPC errors, and
timeouts fail closed. Account usage is aggregate and does not provide factual
per-call token usage, so the launcher leaves completed calls launched and
reconciliation pending. The adapter accepts the app-server's historical
`rateLimits` bucket or the keyed `rateLimitsByLimitId` shape, selecting the
exact `codex` key when only keyed buckets are supplied.

`codex-managed` is an explicit managed CLI launch path, not a hook or proxy.
Ordinary Codex UI and CLI calls outside this path remain unmanaged. Installation
is user scoped on Linux and macOS, with a manifest that records owned file
hashes; modified files block overwrite and uninstall. The default launcher path
is `~/.local/bin/codex-managed` and the skill/protocol path is
`~/.codex/skills/token-budget`. Doctor checks executable and package
availability, PATH discovery, manifest hashes, and skill location. A `--root`
installation is staging only and doctor marks it unusable. The installer does
not write Codex configuration or system directories.

The controller separately enforces the provider's absolute reported window
utilization below 80% at reservation and launch time. Project caps continue to
measure movement since the project's baseline. Managed launch snapshots carry
forward only previously validated reconciled call coverage for the same reset
window; the current call is never marked covered before factual reconciliation.
