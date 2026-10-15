import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from roborock_local_server.bundled_backend.shared import durable_state as durable
from roborock_local_server.bundled_backend.shared.runtime_credentials import RuntimeCredentialsStore
from roborock_local_server.bundled_backend.shared.zone_ranges_store import ZoneRangesStore


@pytest.mark.parametrize("failure", ["partial_write", "file_fsync", "replace", "directory_fsync"])
@pytest.mark.parametrize("kind", ["credentials", "zones"])
def test_publication_failure_memory_disk_and_retry(tmp_path, monkeypatch, failure, kind):
    if kind == "credentials":
        path = tmp_path / "credentials.json"
        store = RuntimeCredentialsStore(path)
        store.update_base(mqtt_usr="before")
        change = lambda: store.update_base(mqtt_usr="after")
        visible = lambda: store.bootstrap_value("mqtt_usr")
        disk_visible = lambda: json.loads(path.read_text())["mqtt_usr"]
    else:
        path = tmp_path / "zone_ranges.json"
        store = ZoneRangesStore(tmp_path)
        store.put("tid", 1, [1, 2, 3, 4])
        change = lambda: store.put("tid", 1, [5, 6, 7, 8])
        visible = lambda: store.get("tid", 1)
        disk_visible = lambda: json.loads(path.read_text())["tid:1"]
    before = visible()
    previous = path.read_bytes()
    with monkeypatch.context() as patch:
        if failure == "partial_write":
            def partial(payload, handle, **kwargs):
                handle.write('{"partial":')
                raise OSError("synthetic disk full")
            patch.setattr(durable.json, "dump", partial)
        elif failure == "replace":
            patch.setattr(durable.os, "replace", lambda *args: (_ for _ in ()).throw(PermissionError("synthetic")))
        else:
            original = os.fsync
            calls = 0
            def fsync(fd):
                nonlocal calls
                calls += 1
                if calls == (1 if failure == "file_fsync" else 2):
                    raise OSError("synthetic fsync failure")
                return original(fd)
            patch.setattr(durable.os, "fsync", fsync)
        with pytest.raises(durable.PersistenceError):
            change()
    assert visible() == disk_visible()
    if failure != "directory_fsync":
        assert visible() == before
        assert path.read_bytes() == previous
    else:
        assert visible() != before
    assert not list(tmp_path.glob(".*.pending"))
    change()
    assert visible() == disk_visible()
    assert visible() != before


@pytest.mark.parametrize("contents", ["{broken", "[]", '{"tid:1":[1,2]}', '{"tid:1":[1,2,3,"bad"]}'])
def test_corrupt_zone_authority_fails_closed(tmp_path, contents):
    (tmp_path / "zone_ranges.json").write_text(contents)
    with pytest.raises(durable.PersistenceError):
        ZoneRangesStore(tmp_path)
    assert (tmp_path / "zone_ranges.json").read_text() == contents


@pytest.mark.parametrize("contents", ["{broken", "[]", '{"schema_version":99}', '{"devices":[7]}'])
def test_corrupt_credential_authority_fails_closed(tmp_path, contents):
    path = tmp_path / "credentials.json"
    path.write_text(contents)
    with pytest.raises(durable.PersistenceError):
        RuntimeCredentialsStore(path)
    assert path.read_text() == contents


def test_unreadable_authority_is_not_empty(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    path.write_text("{}")
    original = Path.open
    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(durable.PersistenceError):
        RuntimeCredentialsStore(path)


def test_zone_response_publishes_one_batch_with_assigned_ids(tmp_path, monkeypatch):
    from roborock_local_server.bundled_backend.shared import zone_ranges_store as zones
    store = ZoneRangesStore(tmp_path)
    store.put("existing", 1, [1, 2, 3, 4])
    calls = []
    original = zones.atomic_json
    def capture(path, payload):
        calls.append(payload.copy())
        original(path, payload)
    monkeypatch.setattr(zones, "atomic_json", capture)
    request = {"data": [{"tid": "temporary", "zones": [{"zid": -1, "range": [10,20,30,40]}, {"zid": -2, "range": [50,60,70,80]}]}]}
    store.merge_set_scenes_zones_response(request, [{"tid": "assigned", "zones": [{"zid": 9}, {"zid": 10}]}])
    assert len(calls) == 1
    assert store.get("temporary", -1) is None
    assert store.get("assigned", 9) == [10,20,30,40]
    assert store.get("assigned", 10) == [50,60,70,80]
    assert ZoneRangesStore(tmp_path).get("existing", 1) == [1,2,3,4]
    store.merge_set_scenes_zones_response(request, [{"zones": [{"zid": 5}]}])
    assert len(calls) == 1


def test_process_lease_excludes_another_process_and_pending_marker(tmp_path):
    script = "from pathlib import Path; from roborock_local_server.bundled_backend.shared.durable_state import RuntimeDirectoryLease; import sys; RuntimeDirectoryLease(Path(sys.argv[1]))"
    with durable.RuntimeDirectoryLease(tmp_path):
        second = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True)
        assert second.returncode != 0
    assert subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True).returncode == 0
    (tmp_path / "migration_pending.json").write_text("{}")
    with pytest.raises(durable.PersistenceError, match="pending"):
        durable.RuntimeDirectoryLease(tmp_path)
    with durable.RuntimeDirectoryLease(tmp_path, allow_migration_pending=True):
        pass


def test_partial_store_blocks_missing_scene_zone_without_log_reads(tmp_path):
    from roborock_local_server import server  # prepares bundled backend import paths
    from roborock_local_server.bundled_backend.https_server.routes.user.scene.service import _hydrate_scene_param_with_zone_ranges, RoutineExecutionError
    store = ZoneRangesStore(tmp_path)
    store.put("tid", 1, [1,2,3,4])
    payload = {"action": {"items": [{"param": {"method": "do_scenes_zones", "params": {"data": [{"tid": "tid", "zones": [{"zid":1}, {"zid":2}]}]}}}]}}
    with pytest.raises(RoutineExecutionError, match="restore zone_ranges.json"):
        _hydrate_scene_param_with_zone_ranges(SimpleNamespace(zone_ranges_store=store), payload)
    store.put("tid", 2, [5,6,7,8])
    hydrated, changed = _hydrate_scene_param_with_zone_ranges(SimpleNamespace(zone_ranges_store=store), payload)
    assert changed
    assert hydrated["action"]["items"][0]["param"]["params"]["data"][0]["zones"][1]["range"] == [5,6,7,8]
