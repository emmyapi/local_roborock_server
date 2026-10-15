# Runtime state and diagnostic storage

Credentials, inventory, protocol sessions, broker state and `zone_ranges.json`
are durable application data. Include the entire data directory and configuration
in consistent backups. Diagnostic files are disposable only after the explicit
legacy migration and an isolated restore have established independent recovery.
Normal startup and scene dispatch never reconstruct state from diagnostic history.

The six subsystem text logs and `decompiled_http.jsonl` / `decompiled_mqtt.jsonl`
use 4 MiB active files and three archives each. Encoded records are limited to
256 KiB; oversized JSONL becomes a valid summary. Sensitive fields and raw payload
previews are redacted. Configure the container log driver separately, for example
`max-size: 10m` and `max-file: '3'`. The nominal application budget is 128 MiB.
A file that already exceeds the limit requires explicit migration rather than
being silently overwritten on startup.

## Legacy migration

Use `python -m roborock_local_server.legacy_migration` from the selected release.
The operator must stop dependent controllers before the server and prove there
is no active physical operation. Never issue robot commands to repair persistence.
Keep the output directory private, outside source control and outside disposable
cache storage; it contains credentials, coordinates, checksums and a journal.

1. `audit --data-dir DATA/runtime --output PRIVATE/audit` performs a bounded,
   read-only source scan and reports counts. With live writers this is an
   informational snapshot, never cutover authorization or proof of final state.
2. Stop writers. `dry-run --data-dir DATA/runtime --output PRIVATE/migration`
   builds candidates without changing production state. It rejects changed source
   identity, malformed/incomplete/oversized records, unsupported state, unresolved
   device identity and missing or conflicting required scene geometry.
3. Verify the private candidates and compact backup by restoring into an isolated
   instance with dummy peers and no production endpoints. Preserve the old image.
4. `apply --data-dir DATA/runtime --output PRIVATE/migration --writers-stopped`
   installs the exact candidates. A durable pending marker prevents normal
   startup across a partial multi-file installation. A retry reconciles actual
   originals/candidates; unexpected content requires operator reconciliation.
5. `retire --data-dir DATA/runtime --output PRIVATE/migration --writers-stopped`
   renames the exact eight diagnostic names into `DATA/runtime/retired-diagnostics`
   on the same filesystem. Originals retain their inodes. It never deletes them.
   Only after state and retirement complete does it clear the pending marker.
6. Start the server, verify authenticated reconnect, fresh non-actuating state and
   complete scene hydration, then resume controllers in the installation's order.
   Observe at least 24 hours, including a successful log-free restart, and prove
   old-image rollback from compact state before removing the exact retired files.

Missing credential material is deliberately blocked: a historical CONNECT preview
alone does not prove successful authentication. Preserve populated credentials;
resolve missing identities from authenticated evidence before migration. Zone
reconstruction accepts complete successful request/response exchanges, applies
response-assigned IDs and retains current authoritative coordinates over history.
All required scene tuples must be present. Conflicting embedded/current geometry
and zone tuple collisions between devices require reconciliation.

The journal records `prepared`, `writers_stopped`, `candidates_validated`,
`state_installed`, `logs_retiring` and `logs_retired`. Installation acceptance
receipts own subsequent `service_verified` / `committed` decisions and cleanup.
A diagnostic error cannot justify clearing a pending marker, replaying commands,
replacing newer authoritative state or deleting legacy evidence.
