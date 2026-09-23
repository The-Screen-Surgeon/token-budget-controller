# Portfolio roadmap

The roadmap keeps the core useful with factual events while integrations mature.

## Near term

- Stabilize the normalized event schema and idempotent local ledger.
- Add Codex and Claude Code adapters that consume their supported per-request usage metadata.
- Implement deterministic task budgets, reservations, unknown-count handling, and clear decision reasons.
- Provide privacy-safe summaries and exportable accounting records.
- Deliver an explicit managed Codex CLI path backed by a read-only app-server
  rate-limit snapshot and safe user-scoped Linux/macOS installer.

## Next

- Add adapter conformance fixtures for reported, estimated, missing, retried, and late events.
- Add provider/model breakdowns while preserving separate tokenizer provenance.
- Add optional local alerts and dashboards for task burn rate and budget exhaustion.
- Document migration rules when an upstream event format changes.

## Later

- Support additional clients through the same event contract.
- Reconcile local records with provider exports when a provider offers an authoritative export, labeling reconciliation status and discrepancies.
- Add signed or append-only ledger options for teams that need stronger auditability.

## Explicit limits

Subscription plans often expose rate limits, rolling windows, or qualitative usage messages instead of exact remaining tokens. Token Budget cannot derive a remaining subscription quota from local request counts unless the provider publishes the required denominator, window, and accounting rules. The product will show “unknown” or a provider-reported status in that case and will not convert local estimates into a false quota percentage.

The Codex app-server currently provides account rate-limit percentages and
aggregate account usage, but not factual per-call tokens through this adapter.
The managed launcher therefore leaves reconciliation pending. Ordinary Codex
UI/CLI calls remain unmanaged; transparent hooks, system-wide installation,
other live providers, and release tagging are unsupported in this milestone.
