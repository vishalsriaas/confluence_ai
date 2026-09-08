# Call Log Reliability

## Scope

One actual call/attempt has one canonical AI Call Log. Phone numbers are display
data, not call identities. This change does not modify sales prompts, MCP tools,
voice settings, or customer retry/follow-up schedules.

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

Worker verification:

```sh
cd "/home/confluence frappe 15"
.venv-livekit-local/bin/python -m unittest discover -s tests -p test_provider_call_identity.py
```

This is not a live SIP/provider delivery test or proof that every product feature
is bug-free. Full backend and worker syntax checks are also required.

## Deployment Gate

No cloud deployment or live historical merge was performed in this change.

1. Back up the live database before migration. Deploy Confluence with the new
   AI Call Identity DocType, Call Log/Webhook fields, settings, and initializer patch.
2. Deploy the reviewed LiveKit worker identity-reporting code. Do not deploy an
   older worker that omits sip.callIDFull. Review other pre-existing local worker
   changes separately rather than assuming they belong to this change.
3. Verify provider/room/attempt aliases on one controlled inbound and outbound call.
4. Verify the four event states and grace-period behavior on that same Call Log.
5. Only then freeze. Do not mark production verified based only on local tests.

Legacy duplicates are not merged by phone. The explicit
merge_legacy_provider_identity helper requires an exact attempt and a verified
provider identity, preserves raw source data in an audit event, and updates Link
references through Frappe. Ambiguous historical rows require review; there is no
blind bulk merge or destructive migration.

## Rollback

Use a maintenance window; stop new dispatches and let active calls finish.
Restore the backed-up worker and deploy the backend baseline together. Additive
fields/tables can remain for audit preservation; do not drop them or discard
newly received call data. A database restore is only appropriate after explicitly
accounting for all calls and events received since the backup. No automatic live
rollback or destructive reset command is included.
