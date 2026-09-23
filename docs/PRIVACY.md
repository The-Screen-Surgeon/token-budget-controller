# Privacy and data boundaries

Token Budget is designed to never ingest task content. Adapters must not send prompts, model responses, source files, diffs, tool arguments, environment variables, or free-form logs to the ledger. Task labels and identifiers should be user-chosen opaque values where practical.

The local record is limited to accounting metadata: provider, model, timestamps, task id, token counts, count quality, status, schema version, and deduplication information. Integrations should redact error strings before recording them, because provider errors can contain request content.

This boundary is an application guarantee, not a claim about the underlying AI service. Codex, Claude Code, and their hosting providers may have their own telemetry and retention policies. Users should review those policies separately and configure credentials according to each provider’s guidance.

Logs, exports, backups, and crash reports must follow the same rule. A diagnostic mode may include field names and counts, but never payload text. Any future feature that needs content requires a separate design review and an explicit opt-in.

The live Codex app-server adapter requests only account rate limits and aggregate
account usage. It validates then discards account token totals; it stores only
window percentages, reset timestamps, observation time, and controller audit
metadata. The managed launcher does not capture the Codex process's stdout or
stderr. It records the exit status and post-call aggregate window percentages,
but does not treat those changes as per-call usage. Reconciliation therefore
remains pending unless a factual per-call source is provided. Calls through the
Codex UI or ordinary CLI outside the explicit managed launcher are not
intercepted or recorded by this path.
