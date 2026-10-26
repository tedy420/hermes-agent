# Handoff context (opt-in)

This plugin reads ONE explicitly selected local document when Hermes loads plugins
and every 3,600 seconds thereafter, even while idle. It makes no model, network,
publication, payment, or approval calls. It does not execute handoff instructions.

Only exact session IDs in the allow-list receive text, once per content digest
per process/session, via the existing `pre_llm_call` context result on the next
user turn. No background user messages are injected, no historical messages or
system prompts are rewritten, and no idle agent run is started. A new process
reads again and supplies context to its new conversations even if the persistent
read receipt says `unchanged`. Existing conversations are not silently rewritten.

## Configure after review

The plugin is standalone and disabled by default. Install the directory as a user
plugin or use the bundled directory in a reviewed build. Select the actual source
and the exact private conversation IDs; do not infer them from platform, sender,
parent sessions, or a document claiming authority. An empty allow-list denies all
text delivery, including public/group chats and subagents.

Use the supported configuration commands (not direct live YAML edits):

```
hermes config set plugins.entries.handoff-context.settings.source handoff/context.md
hermes config set plugins.entries.handoff-context.settings.allowed_session_ids '["EXACT_PRIVATE_SESSION_ID"]'
hermes plugins enable handoff-context
```

The path is relative to `get_hermes_home()` (profile-safe), or an explicit absolute
local path. Only UTF-8 `.md`/`.txt` regular files up to 1 MiB are accepted. Direct
symlinks and UNC paths are rejected; do not configure paths through network mounts
or junctions. There is no discovery heuristic, fallback source, or merge of
multiple candidates. If the intended source is ambiguous, leave `source` unset,
record the candidates on the task, and obtain an ownership decision. Missing or
invalid selection logs a generic warning without content and leaves Hermes usable.

A reviewed activation takes effect on the next normal plugin load. Do not restart
a gateway holding active workers just to activate this plugin. Source changes
require reload; allow-list changes are read at the next turn. Disabling/unloading
stops the timer. Previously supplied context remains conversation history; removing
a session from the list cannot retroactively erase it.

## Versioned read receipts

Profile-local SQLite store:
`state/plugins/handoff-context/receipts.sqlite3`.

Tables: `versions(source PRIMARY KEY, sha)` and append-only
`receipts(id INTEGER PRIMARY KEY, body JSON TEXT)`. Updates and receipts share one
transaction so concurrent readers cannot both report a changed version as new.
Each receipt includes:

- `schema_version`: 1
- `action`: `handoff.read`
- `at`: timezone-aware UTC observation time
- `trigger`: `startup` or `hourly`
- `result`: `loaded`, `unchanged`, `unavailable`, or `receipt_store_unavailable`
- `reference`: `plugin:handoff-context`
- `evidence.source_id`: hash of configured absolute source path (no raw path)
- On successful read: byte SHA-256, byte count, descriptor modification time in
  nanoseconds and `verification_scope: source_bytes_only`.
- On read failure: exception TYPE only, not its message, content, or secret path.

A successful receipt verifies bytes read, NOT any claim, completion, permission,
PR status, or evidence cited INSIDE the document. A changed version is read and
staged; this is not proof the model acted on it. No document text, source snapshots,
auth configuration, environment values, or secrets are stored in the receipt DB.
The DB is a local audit aid, not a signed or tamper-proof attestation.

Failure withdraws the cached document, continues hourly retries and never promotes
stale text as a current read. Store failure also withholds context and emits a
generic warning; a returned store-failure receipt cannot itself be durable there.
The last successful digest remains in durable state across inaccessible periods.
Receipts accumulate hourly; retain/export according to the operator's evidence
policy (this plugin intentionally does not delete audit history).

## Initiative action receipt contract

For tasks that perform work, publish a separate action receipt with these fields.
This is a TEMPLATE, not evidence of an executed action:

```json
{
  "schema_version": 1,
  "action": "<specific action and bounded scope>",
  "at": "<actual ISO-8601 UTC observation time>",
  "result": "<passed|failed|blocked|pending>",
  "reference": {"task": "<canonical board/task ID>", "pr": "<URL or null>", "commit": "<exact SHA or null>"},
  "evidence": [{
    "location": "<durable task attachment or exact PR/run URL>",
    "sha256": "<digest for a local artifact, or null>",
    "check": "<reproducible command or read-back method>",
    "observed_at": "<actual check time>",
    "observed_result": "<actual result, including failures>",
    "verified": false,
    "verification_scope": "<what this check proves and does not prove>"
  }],
  "limits": ["<unverified dependencies, rollout or access limits>"]
}
```

Never set `verified: true` from an agent self-report, a URL's existence, an expected
hash, scheduling alone, green tests on another SHA, or a claimed permission in a
handoff. Open the exact artifact/PR/task, check the actual evidence and identity,
then record the scope and timestamp. Preserve failed observations. A passed test
is not deployment; a read receipt is not an authorization receipt. Do not include
credentials, private chat content, full environment dumps, or raw personal data.
Use task attachments for private evidence rather than publishing it in a public PR.

## Publish to the existing handoff

1. Read the live source; identify its owner-controlled receipt section. Do not use
   a similarly named task scratch HANDOFF as a replacement for the actual source.
2. Save the action receipt as a task artifact/attachment. Reopen it and verify its
   evidence (file hashes, exact PR state/head, command output, task read-back).
3. Append a short receipt to YOUR owned section: action/time/result/task/PR/exact
   SHA, checked evidence references and explicit limitations. Do not rewrite the
   other owner's section or turn historical notes into a new task queue.
4. Re-read the source immediately before writing; if it changed since your read,
   reconcile with the owner instead of overwriting. After publication, read back
   that exact section. Do not claim published/acknowledged until checked.
5. The next hourly poll stages the new digest; only permitted sessions receive it
   on a subsequent turn. This is not notification delivery, remote ACK, or task
   execution. No publication by this plugin is automatic.

## Tests

`python -m pytest tests/plugins/test_handoff_context.py -q`

Tests use local temp files and an actual PluginManager, not a model or provider.
The hourly test advances a fake wait clock through 3,600-second intervals (no
one-hour sleep). Coverage includes startup, hourly changes, unchanged input,
missing/recovered source, persistent deduplication/restart, invalid/oversized
input, receipt-store failure, private-session isolation and unload cleanup.
