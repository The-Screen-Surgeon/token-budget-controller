# Token Budget

Token Budget is a privacy-preserving, deterministic token accounting and
adaptive execution tool for coding agents. The MVP supports Codex and Claude
Code session records.

The portable [Universal Usage Budget Protocol](UNIVERSAL-USAGE-BUDGET.md) can be
given to any agent or runtime that can expose factual usage. It defines one
provider-neutral, fail-closed planning, gating, adaptation, and receipt format.
Markdown supplies cooperative enforcement; a runtime hook, wrapper, or proxy is
required to technically block disallowed calls.

It reads provider-reported usage metadata, stores no prompts or responses,
deduplicates replays, and changes agent behavior as provider or task budgets are
consumed.

## Current capabilities

- Factual Codex and Claude Code token ingestion
- Replay-safe SQLite ledger
- Input, cached input, cache creation, output, and reasoning categories
- Deterministic `normal`, `efficient`, `conserve`, and `stop` policy modes
- Explicit, strictly typed override for one narrowly scoped call
- Fresh and incremental ingestion convergence
- Complete project intake and planning with one-session preference, feasibility
  checks, and deterministic multi-session checkpoints
- JSON `plan` and `receipt` commands with per-model factual usage and reset
  countdowns

## Run

```bash
git clone https://github.com/The-Screen-Surgeon/token-budget-controller.git
cd token-budget-controller
python3 -m pip install -e .
python3 -m token_budget.cli --db ~/.local/state/token-budget/ledger.sqlite ingest \
  --codex ~/.codex/sessions --claude ~/.claude/projects
python3 -m token_budget.cli --db ~/.local/state/token-budget/ledger.sqlite summary
python3 -m token_budget.cli policy \
  --provider-used-percent 58 --task-observed-tokens 100000 --task-cap 500000

# Plan and receipt accept JSON on stdin (or --input FILE).
python3 -m token_budget.cli plan < plan.json
python3 -m token_budget.cli receipt < receipt-input.json
```

`plan` requires a build `prompt`, a complete structured `scope` (deliverables,
exclusions, acceptance checks, and phases), current sourced `usage.primary` and
`usage.secondary` windows, explicit percentage demands and caps, and total and
per-session token limits. Usage snapshots carry source, observation time, and
maximum age; quota demands carry points, source, observation, and calibration
times plus independent freshness limits. It returns a complete validated schedule,
`needs_clarification`, or `infeasible`; it never invents phases. `receipt`
requires every factual token category, including cache creation input, as a
number or explicit null with partial quality metadata. It reports factual
tokens separately from subscription percentages, remaining balances, and
deterministic seconds until both resets.
Any model entry marked `partial` makes the aggregate partial; final usage
snapshots use the plan's freshness limit.

The optional user-scoped installer provides a managed Codex CLI launcher. It
does not intercept Codex UI or ordinary Codex CLI invocations. Calls made
outside `codex-managed` remain unmanaged. Its live adapter reads Codex app-server
account limits and aggregate account activity without making a model call;
post-call reconciliation remains pending without factual per-call usage.

```bash
token-budget managed-install [--dry-run]
token-budget managed-doctor
token-budget managed-uninstall [--dry-run]
token-budget codex-managed --input managed-codex-call.json
```

By default the installer puts `codex-managed` in `~/.local/bin` and the skill
plus protocol in `~/.codex/skills/token-budget`; its ownership manifest lives in
the user's Token Budget configuration directory. Add `~/.local/bin` to `PATH`
and install Token Budget into the launcher's Python environment. `managed-doctor`
checks Python/package import, Codex CLI, hashes, PATH discovery, skill location,
and reports whether the install is usable. `--root DIR` creates an isolated
staging install that doctor intentionally marks unusable. Install and uninstall
refuse collisions or modified owned files. See [DESIGN.md](DESIGN.md) and
[docs/PRIVACY.md](docs/PRIVACY.md) for boundaries.

## Verify

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q token_budget tests
```

## Controller MVP

The managed controller stores project caps, per-window reservations and actuals,
plan revisions, decisions, and a global append-only hash chain in SQLite. Project
creation validates all configuration before one atomic transaction. Database
migration is versioned and atomic; legacy projects with incomplete per-window
reservation or actual evidence are quarantined instead of assigned guessed
balances.

Every call reserves each configured provider window explicitly. Snapshots must
include current usage, observation time, reset identity and expiry, and
`covered_call_ids`. Coverage is cumulative within a reset window: later facts
cannot omit previously covered calls, and the counter must cover each listed
call's reported actuals. A repeated counter therefore cannot be reused to cover
new consumption. Denied gates still atomically save a validated complete
snapshot, so a 50% replan can be based on the latest factual counter; use
`refresh_snapshots` to update facts while replanning is required.
The gate uses the same validation for supplied and cached snapshots and fails
closed on stale, expired, regressing, contradictory, or insufficient evidence.
Window headroom includes factual consumption plus outstanding reservations that
the snapshot does not cover, counted once.

Reservations count against task, session, coordinator, and per-window limits.
The 50% barrier tracks crossings for task tokens and every provider window; a
validated remaining-plan revision must list deliverables, assigned calls and
per-window allocations, matching budgets, and acceptance checks. It must fit
fresh factual usage and strict projected 80% limits. The universal 80% stop
checks proposed reservations for task tokens and provider windows. No override
is supported.

Only a successfully claimed `reserved` call can launch. Claiming rechecks the
project state, violation flags, task and window limits, 50% barrier ownership,
and snapshot freshness/expiry in the same transaction; it includes that call's
reservation once. Ordinary reconciliation requires a launched call, and
cancellation accepts only reserved calls. A subprocess spawn failure releases
liability only with the wrapper's one-time launch claim and becomes the distinct
`launch_failed` state. Running or completed calls retain their liability until
factual reconciliation. If the controller crashes after launch, it does not
infer that the call was unused.

```bash
python3 -m token_budget.cli --db budget.sqlite create-project --input project.json
python3 -m token_budget.cli --db budget.sqlite status demo
python3 -m token_budget.cli --db budget.sqlite reserve --input call.json
python3 -m token_budget.cli --db budget.sqlite reconcile --input actual.json
python3 -m token_budget.cli --db budget.sqlite revise --input revision.json
python3 -m token_budget.cli --db budget.sqlite export-receipt demo
python3 -m token_budget.cli --db budget.sqlite exec --input wrapped-command.json
```

`exec` is the technical enforcement boundary: it launches a subprocess only
after `ALLOW` and claims that call's reservation atomically. Calls made outside
this wrapper are outside technical enforcement. Receipts report available
tokens after outstanding reservations and task/session/80% limits, and preserve
per-window actuals and overruns individually. The MVP includes static/manual
snapshots and a read-only live Codex account snapshot adapter. Codex support
requires both primary and secondary percentages and valid reset metadata; the
adapter does not provide factual per-call tokens. Claude, Hermes, OpenClaw, and
Grok live adapters, system-wide installation, transparent interception, and
release packaging remain unsupported.

Earlier ledger and workflow components received scoped GPT-6 Astra clearance.
After correction of four additional state-machine defects, the final scoped
controller review returned CLEAR and independently passed all 30 packaged
controller tests. This clearance covers the managed-wrapper controller and its
static/manual snapshot boundary; the Codex adapter and installer have not
received that review. See
[the roadmap](docs/ROADMAP.md).
