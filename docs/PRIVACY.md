# Privacy and data boundaries

Token Budget is designed to never ingest task content. Adapters must not send prompts, model responses, source files, diffs, tool arguments, environment variables, or free-form logs to the ledger. Task labels and identifiers should be user-chosen opaque values where practical.

The local record is limited to accounting metadata: provider, model, timestamps, task id, token counts, count quality, status, schema version, and deduplication information. Integrations should redact error strings before recording them, because provider errors can contain request content.

This boundary is an application guarantee, not a claim about the underlying AI service. Codex, Claude Code, and their hosting providers may have their own telemetry and retention policies. Users should review those policies separately and configure credentials according to each provider’s guidance.

Logs, exports, backups, and crash reports must follow the same rule. A diagnostic mode may include field names and counts, but never payload text. Any future feature that needs content requires a separate design review and an explicit opt-in.
