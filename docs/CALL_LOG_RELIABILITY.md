# Call Log Reliability

## Scope

One actual call/attempt has one canonical AI Call Log. Phone numbers are display
data, not call identities. This change does not modify sales prompts, MCP tools,
voice settings, or customer retry/follow-up schedules.

## September 8 Follow-up Correction

Live records call-101013 and call-101019 demonstrated a gap in the original tests:
the Vobiz customer-leg SIP ID, hangup UUID and LiveKit SIP leg were different.
The provider's explicit BridgeUUID related these legs, but both startup paths
created tasks. Disposition was also queued before the webhook transaction
committed, so its worker could read an empty transcript and exit.

Corrections:

- Only the LiveKit inbound resolver creates inbound tasks/attempts. Vobiz
  initiation is durably retained as Pending Matching until an exact identity or
  bridge relation is available. It does not create a second customer-leg task.
- BridgeUUID/bridge_uuid explicitly registers the linked SIP/UUID aliases in
  the same company. Missing/ambiguous identities are never inferred from phone.
- A resolver event queues pending receipt replay after commit, off the startup
  response path. Replay revisits earlier receipts unlocked by a later bridge.
- Callbacks without Direction retain the known call direction/customer number.
- Both AI disposition and manual disposition sync enqueue after commit. Jobs
  for a single call serialize; transient deadlocks retry database writes only,
  not paid classification or ERP HTTP requests. A concurrent manual edit is not
  overwritten by an in-flight AI classification.
- Late transcripts for historical attempts still enqueue disposition without
  advancing the latest attempt or follow-up workflow.

Pre-edit archive: /home/confluence frappe 15/call-identity-disposition-backup-20260908.zip
Live evidence snapshots: C:/Users/Admin/Desktop/Agent-handshake-livkit/backups/bridge-disposition-20260908

Additional tests exercise distinct customer/SIP/hangup IDs in all 24 event
orders, duplicate delivery, receipts before the room exists, a separate database
connection consuming disposition after commit, concurrent disposition workers,
deadlock injection, manual edits, and audited legacy bridge repair. Only model
and ERP boundaries are stubbed in the disposition integration scenarios.

Existing duplicates require explicit repair after deployment, not a phone-based
bulk merge. POST confluence_ai.api.call_log.repair_bridge_duplicate with
source_call_log, target_call_log and dry_run=1 first. Only apply dry_run=0 after
the preview is verified. Requires System Manager and write permission on both
records, terminal calls/tasks, matching company/caller evidence, and explicit
Vobiz BridgeUUID pointing to the LiveKit resolver record. The repair stores both
original records in an audit event, preserves task history and transfers Link
references. Disposition is queued after the repaired record commits.

No live records or cloud deployments are changed by the local regression suite.
Calls that never reach the LiveKit resolver remain visible as unmatched webhook
receipts; the system does not fabricate an AI task without an identified room.

Final local verification for this correction: 155 tests passed, zero failures,
errors or skips (47.363 seconds). Full Python compileall and git diff --check
passed. No real customer calls, paid model requests or ERP writes were made by
these tests. LiveKit status was checked read-only: universal_agent was Running
on LoBp3omnpkkx, deployed 2026-08-26T08:33:26Z. This correction does not deploy
the previously pending local worker identity changes or modify worker source.

## Data Flow

1. Before outbound dialing, reserve a unique call reference for the exact attempt.
2. Give each attempt its own LiveKit room and include the attempt in worker metadata.
3. Store room, full provider SIP ID, and Vobiz UUID as typed company-scoped aliases
   in AI Call Identity. Its deterministic primary key prevents alias reassignment.
4. Receive and persist the original callback in AI Webhook Event before processing.
5. Resolve exact identity/attempt and update the canonical call. The database lock
   order is task, attempt, call log, identity aliases. Current reads are required
   after waiting on locks under MariaDB repeatable-read isolation.
6. Unknown identities remain Pending Matching. Identity-bearing callbacks replay
   matching pending receipts. No phone/time-window attachment is permitted.
7. Duplicate payloads do not reapply callbacks or repeat downstream actions.
8. Late callbacks for older attempts update the old call, not the latest attempt.

The Vobiz recovery path uses this same callback handler. Raw receipts are retained
even when processing fails. Call logs expose per-event states. A missing receipt
does not prove that Vobiz failed to deliver it.

## Transcript Recovery

Confluence AI Settings contains a dedicated Vobiz recovery enable switch, grace
period, retry interval, and maximum checks. Defaults: enabled, 5 minutes, 5
minutes, 3 checks. An explicit disable is preserved by migration.

The existing scheduled recovery job waits until both call-end and recording
receipts exist, then uses the later receipt time plus the grace period. Each API
check is reserved durably. Late webhook arrival wins over an in-flight fetch.
After exhaustion, late webhooks are still accepted. No audio transcription model
is called by this recovery path. Failed API/processing attempts remain visible.

## Backups Taken Before Editing

- Backend baseline: a83a74d.
- Windows backup directory:
  C:/Users/Admin/Desktop/Agent-handshake-livkit/backups/call-log-unification-20260908
- Backend source archive: confluence-a83a74d.zip.
- Worker source backup: good-agent.py in that backup directory.
- Live read-only snapshots: call-99344.json, call-99345.json, live-settings.json.
- Local database:
  /home/confluence frappe 15/frappe-bench/sites/localhost/private/backups/20260908_122140-localhost-database.sql.gz

Keep these backups private; call snapshots can contain personal information.

## Verification

Verified locally on 2026-09-08: 143 backend regression tests passed, zero failures,
zero errors, zero skips. Worker identity tests: 4 passed. Full backend compileall,
worker py_compile, and git diff --check passed. Recovery configuration was read
back as enabled with a 5-minute grace, 5-minute retry interval, and 3-check limit.

Run the regression boundary locally without HTTP requests or queued side effects:

```sh
cd "/home/confluence frappe 15/frappe-bench"
env/bin/bench --site localhost execute confluence_ai.tests.run_call_log_verification.run
```

The suite covers actual callback handlers, all 24 webhook arrival permutations,
duplicate receipts, concurrent callbacks, delayed identity, legacy repair,
same-number separate attempts, cross-company rejection, recovery timing/limits,
late transcript arrival, and adjacent follow-up/disposition behavior.

Worker syntax verification (identity-reporting changes were reverted):

```sh
cd "/home/confluence frappe 15"
.venv-livekit-local/bin/python -m py_compile good-agent.py
```

This is not a live SIP/provider delivery test or proof that every product feature
is bug-free. Full backend and worker syntax checks are also required.

## Deployment Gate

No cloud deployment or live historical merge was performed in this change.

1. Back up the live database before migration. Deploy Confluence with the new
   AI Call Identity DocType, Call Log/Webhook fields, settings, and initializer patch.
2. Keep the voice worker unchanged. Confluence's outbound connector reads
   sip.callIDFull from the exact SIP participant after agent dispatch. Inbound
   resolution reads it from the existing participant attributes payload.
3. Verify provider/room/attempt aliases on one controlled inbound and outbound call.
4. Verify the four event states and grace-period behavior on that same Call Log.
5. Only then freeze. Do not mark production verified based only on local tests.

Legacy duplicates are not merged by phone. The explicit
merge_legacy_provider_identity helper requires an exact attempt and a verified
provider identity, preserves raw source data in an audit event, and updates Link
references through Frappe. Ambiguous historical rows require review; there is no
blind bulk merge or destructive migration.

## Legacy Recovery Correction (2026-09-08)

- Completed legacy calls with a recording and exact provider identity no longer
  require receipt timestamps introduced after those calls were created. Their
  last modification is a conservative initial grace baseline, not a fabricated
  provider callback timestamp. Subsequent checks respect the configured retry
  time and maximum count. Active calls without end evidence still wait.
- When an existing recording is first observed by the event handler, its receipt
  timestamp is initialized once. Later callbacks do not reset it.
- A transcript attached by exact identity to a historical call without a Task
  now queues disposition and marks the receipt processed. It does not create a
  Task, place a call, or guess a match from the phone number.
- Recovery still fetches Vobiz's existing transcript only. No audio-to-AI
  transcription is invoked by this flow.

Verification: 159 backend tests passed (zero failures/errors/skips), 4 worker
identity tests passed, syntax checks and git diff --check passed. HTTP and queued
external side effects were blocked in the backend regression run.

Source backup before these corrections:
`C:/Users/Admin/Desktop/Agent-handshake-livkit/backups/call-recovery-fix-20260908-152114`.

Cloud status was checked again: worker `LoBp3omnpkkx`, deployed
2026-08-26T08:33:26Z, still running. The subsequent backend-only correction below
supersedes the earlier worker deployment requirement. Neither deployment was
performed in this correction. Existing
historical rows without exact linkage still require a verified provider bridge;
they are never merged by phone. Calls outside the configured recovery lookback
are not bulk-modified by this correction.

## Backend-Only Identity Capture (2026-09-08)

The local worker's identity helper, attribute-change listener, identity callbacks
and shutdown identity payload were reverted at the user's request. Existing
voice, prompt and WhatsApp greeting behavior was preserved. The four historical
worker identity tests above no longer apply; their removed helper is not deployed.

The existing Confluence outbound connector previously read participant metadata
only once and silently returned None on every exception. It now makes at most
five reads, each with a two-second timeout and one-second gaps (at most fourteen
seconds of lookup waits). This happens AFTER agent dispatch, not before greeting.
It creates no new scheduler, redial or background identity loop.

The lookup checks the exact room, participant identity, participant SID and SIP
session ID returned by call creation. Internal SCL IDs are never stored as Vobiz
identities. A replacement participant or different SIP leg is rejected.
Authentication/permission/configuration errors stop immediately. An unsuccessful
lookup leaves a diagnostic reason in the dispatch result and one AI Error Log;
it does not mark an already dispatched call failed or guess by phone/time.

The executor's existing identity registration and after-commit receipt replay
then attach Vobiz callbacks to the reserved call. No new worker callback is needed.
This still depends on the provider's full SIP ID being available from LiveKit's
server API; removing agent code does not remove that correlation requirement.
Historical rooms that have ended without captured IDs require verified bridge
evidence, not this startup lookup. Missing Vobiz delivery is a separate issue.

Tests cover delayed metadata, transient errors, timeout, bounded exhaustion,
authentication failure, wrong participants/legs, dispatch before lookup, and
pending Vobiz events joined to one log with an ID-free worker end callback.

Backups: `backups/worker-revert-20260908-164907/good-agent.py` and
`backups/backend-identity-20260908/` in the Windows workspace.

Verification: 169 backend tests passed, zero failures/errors/skips (43.851s).
Worker and changed Python files compile; git diff --check passes. Tests used an
isolated temporary Redis on port 16379, shut down afterward. External HTTP and
queued side effects were blocked. No voice worker, scheduler, customer call or
cloud deployment was started for this verification. A post-deployment controlled
call is still required to verify actual provider delivery and identity capture.

## Rollback

Use a maintenance window; stop new dispatches and let active calls finish.
Restore the backed-up worker and deploy the backend baseline together. Additive
fields/tables can remain for audit preservation; do not drop them or discard
newly received call data. A database restore is only appropriate after explicitly
accounting for all calls and events received since the backup. No automatic live
rollback or destructive reset command is included.
