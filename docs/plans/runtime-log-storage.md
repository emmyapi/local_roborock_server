# Bounded diagnostic logs and independent recovery state

Status: implementation and isolated validation in progress; production migration
and deployment have not started. Source baseline: `812f23d`. This is the owning implementation plan;
installation-specific measurements and maintenance details belong to the operator.

## Outcome and proposed defaults

Authentication and scene coordinates must survive removal of every diagnostic
file. Normal startup and scene handling must never scan historical logs.

Use size-based rotation for the six subsystem logs and two HTTP/MQTT JSONL
streams: **4 MiB per file, three archives plus the active file**, a total nominal
file budget of **128 MiB**. Limit Docker stdout/stderr to **10 MB × 3 files** at
the installation. Target **under 200 MiB combined diagnostic storage**, including
record-size and rotation overhead. These are proposed defaults, not installed
limits. Durable credentials, inventory, broker/session state and zone coordinates
are outside this diagnostic budget and must remain in backed-up data storage.

Use a single serialized writer per resolved log path, byte-aware rotation and a
256 KiB maximum encoded diagnostic record. Oversized JSONL must become a valid,
bounded summary object; never cut serialized JSON midway. Retain useful bounded
metadata while redacting credentials, raw CONNECT payloads and sensitive fields.
Diagnostic failures must be observable without blocking device control. Durable
state failures must prevent false success acknowledgements.

## Findings in the current source

| Area | Current behavior and consequence |
| --- | --- |
| `bundled_backend/shared/io_utils.py` | Plain `FileHandler` and append-open JSONL; no file-size limit or rotation. Six text streams are also mirrored to stderr. |
| `server.py` and `shared/runtime_credentials.py` | Startup can recover missing known device passwords from CONNECT hex in `mqtt_server.log`. Already populated passwords are preserved. Historical CONNECT previews may be incomplete; a parseable packet does not prove successful authentication. |
| `server.py` and `shared/zone_ranges_store.py` | Zone seeding streams MQTT history, but only when the entire store is empty. Partial stores are not repaired. Historical replay lacks the proxy's successful-response error gate. |
| `https_server/routes/user/scene/service.py` | Scene fallback calls `readlines()` on the complete MQTT log. Reverse replay and early exit do not reliably establish every required zone's latest coordinates. |
| Both durable stores | Direct JSON overwrite is not crash-safe. Zone write errors are swallowed and a later identical update may never retry. Corrupt/unreadable state can be mistaken for empty state. |
| MQTT proxy and routine runner | Proxy observations update zones; the routine runner has no direct durable zone update despite the store's docstring. Both paths need a verified successful-command persistence contract. |

Paths in the table are relative to `src/roborock_local_server`; shared modules
are under `bundled_backend`. Directory uses of `http_jsonl.parent` are not log
content readers. Preserve runtime data-directory discovery when separating logs.

## L1 — Harden authoritative stores

- [ ] Preserve current credential schema and zone key identity. Zone keys are
  currently `(tid, zid)`; do not silently introduce another identity scheme or
  assume those keys are globally unique across devices.
- [ ] Stage candidate state under the existing store lock; use a private unique
  temporary file in the same directory, flush/fsync, atomic replace and directory
  fsync. Enforce intended ownership/modes and reject unexpected symlinks.
- [ ] Publish in-memory state only after successful durable commit. Distinguish
  failure before replacement from uncertain durability after replacement; reconcile
  the latter without claiming success or overwriting newer state.
- [ ] Distinguish absent, valid empty, complete, partial, corrupt, unsupported and
  unreadable state. Existing invalid authoritative data must not silently reset.
- [ ] Batch a zone exchange into one commit. Persist successful response-assigned
  IDs and coordinates for proxy and routine-runner paths, preserving existing
  acknowledgement/unknown-outcome semantics; never resend a command to repair a
  persistence failure.
- [ ] Enforce one process owning a data directory, or implement and test genuine
  cross-process consistency. Thread locks alone do not protect multiple servers.

Acceptance: authenticated reconnect and non-actuating scene hydration work after
all diagnostic files are removed in an isolated fixture and the process restarts.

## L2 — Explicit, bounded legacy migration

- [ ] Provide a dry-run and separately explicit apply. Never construct production
  store objects during dry-run if constructors/methods can save.
- [ ] Stream complete records chronologically with bounded record buffers and a
  bounded number of retained keys. Record source device/inode, fixed byte boundary,
  format version, counts and private fingerprints. Do not chase live EOF forever.
- [ ] Preserve populated valid credentials. Recover only missing known identities;
  ambiguous identities/passwords, unsuccessful or incomplete CONNECT evidence
  require explicit reconciliation before acceptance.
- [ ] Merge missing zone tuples using validated request/successful-response pairs,
  including device-assigned IDs. Compare against every active scene's required
  `(tid, zid)`, not just a nonempty store or one coordinate per scene.
- [ ] Preserve valid current coordinates over older history; report conflicting
  or unorderable evidence for reconciliation. Do not guess new coordinates.
- [ ] Define strict numeric/schema validation, oversized/invalid UTF-8 records,
  malformed recovery-bearing entries, incomplete final records and unknown formats.
  Unresolved required state blocks destructive cleanup. A genuinely unused/empty
  zone set is acceptable only when observed scene references establish it.
- [ ] Use a private recoverable journal with intended/result hashes and phases:
  `prepared → writers_stopped → candidates_validated → state_installed →
  logs_retired → service_verified → committed`. Multi-file installation is not
  atomic; unresolved partial installation must block normal startup.
- [ ] Optional online preparation scans only through its captured boundary. At
  cutover stop writers, verify source identity/prefix continuity, process the
  bounded final delta and revalidate against final state/scene references.
  Inode replacement, truncation or unexplained prefix changes invalidate preparation.
  A fully offline scan is acceptable if measured duration fits maintenance.

Acceptance: private output contains complete validated state; public reports contain
counts/status only. Interrupted apply resumes or rolls back from reconciled actual
state, never by blindly repeating uncertain writes.

## L3 — Remove log dependencies and bound every writer

- [ ] Remove normal startup credential backfill, startup zone seeding and runtime
  scene log fallback. Migration is an explicit compatibility tool, not an automatic
  request-time or every-startup scan.
- [ ] Surface missing required state with an actionable diagnostic/readiness result;
  do not dispatch scenes with unresolved geometry.
- [ ] Implement the proposed eight-stream rotation policy, per-path writer registry,
  complete-record locking, size accounting, bounded archives and descriptor closure
  on shutdown/reconfiguration. Test the backend's actual module import aliases.
- [ ] Handle preexisting oversized files only through migration/cutover. Do not
  silently rotate an unmigrated recovery source out of existence on first start.
- [ ] Bound record size and redact secrets at the producer. No concurrent external
  `copytruncate`, duplicate cleanup timer or global prune policy is introduced.
- [ ] Make the byte/archive limits and diagnostic-error counters observable without
  exposing payloads; update installation/backup guidance and configuration examples.

Acceptance: tiny-threshold stress tests retain valid whole JSONL records, respect
bounds through restarts/concurrent writers and close superseded descriptors.

## L4 — Test the complete failure and recovery boundaries

- [ ] Add persistence fault tests for partial writes, fsync/replace failure,
  disk-full/permission errors and process interruption at journal transitions.
- [ ] Cover partial stores, repeated/newer zone updates, multiple zones per scene,
  response-assigned IDs, failed responses, conflicts, malformed/oversized records,
  append-after-boundary, source replacement and truncation.
- [ ] Compare streaming reconstruction with independent expected synthetic results;
  measure bounded RSS against a generated large stream without a second huge file.
- [ ] Test concurrent JSONL/text rotation, Unicode byte limits, exact thresholds,
  setup/reload/shutdown, log directory failure and second-process rejection.
- [ ] Replace the old scene-log fallback test with explicit migration plus
  log-free restart/hydration tests. Run the existing meaningful I/O, credential,
  MQTT proxy, admin API, routine runner, protocol-auth, runtime-state, supervisor
  and container-entrypoint suites, followed by the full isolated test suite.
- [ ] Restore compact private state into an isolated instance with dummy peers
  and no production endpoints. Verify selected backup checksums/schema as well
  as application-level authentication and scene hydration without robot commands.

No fixture, network test or deployment check may actuate a physical vacuum.

## L5 — Controlled cutover, acceptance and space reclamation

- [ ] Re-read the actual installation, source/image/config identities, active
  clients, current robot/controller activity and available space. Keep local
  source changes and deployed credentials intact. Build one tested identified
  candidate in registered private outputs; record its footprint first.
- [ ] Confirm no active cleaning using fresh state. Stop dependent controllers
  first, then the server; verify all log writers have stopped. The operator owns
  the exact installation sequence. No pairing/reset/cloud import is required.
- [ ] Capture compact consistent authoritative state, configuration, broker/session
  dependencies and the actual old image identity. Prove an isolated restore;
  a successful backup job alone is insufficient.
- [ ] Complete and validate migration. Preserve original logs by same-filesystem
  rename while stopped; start with fresh bounded logs. This provides temporary
  rollback without copying another entire legacy log set. Record exact private
  paths, owner and removal condition in the migration journal.
- [ ] Start the new server, verify healthy endpoints, authenticated vacuum and
  client reconnection, fresh state updates, retained identities/keys/sessions and
  complete non-actuating scene hydration. Then resume the dependent controller.
- [ ] Observe a **24-hour proposed acceptance window** with no migration pending,
  repeated authenticated traffic and at least one successful restart without old
  logs. Exercise rotation on isolated fixtures, not by flooding production.
  Physical scene/cleaning acceptance is separate and requires a person present.
- [ ] Once acceptance and compact-state restore pass, remove only the explicitly
  retired legacy log allowlist. If a gate fails, preserve originals with a named
  owner, concrete failure and next review time; never retain them without a decision.
- [ ] Verify no deleted-but-open log descriptors, actual free-space change and
  bounded diagnostic files under normal traffic. Remove superseded ordinary
  build/test outputs; retain only a needed candidate and compact recovery material.

Rollback before cleanup: stop controllers/server, reconcile any post-cutover state
and traffic, then restore the captured compatible state/image and old log names.
Do not overwrite new records or replay robot commands blindly. After cleanup,
rollback must use the tested compact recovery package with a compatible image;
validate that route before deleting the original logs. Backup retention/pruning
is unchanged. Host cache cleanup never decides that authoritative data is disposable.

## Delivery boundary

Planning provides source analysis, proposed limits and execution/acceptance gates.
It does not prove historical zone coverage, successful private recovery, production
installation, physical acceptance or reclaimed space. Those are recorded here as
L1–L5 are actually completed. Code and generic migration behavior belong in this
repository; operator-specific measurements, access and cutover details stay in
private installation documentation.

## Current validation evidence

The bounded writer and explicit offline migration implementations have 22 targeted
passing tests (2026-10-15), including whole-record concurrent rotation, import
aliases, multi-token header redaction, required multi-zone coverage, failed-response
exclusion, preservation of current coordinates, changed-source refusal, interrupted
apply reconciliation and inode-preserving retirement. This does not yet establish
full-suite, restore, installation or elapsed acceptance evidence. Migration blocks
missing credentials rather than guessing authentication from CONNECT previews.
