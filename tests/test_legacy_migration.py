import json
from pathlib import Path

import pytest

from roborock_local_server.legacy_migration import MigrationError, apply, prepare, records


def fixture(tmp_path):
    data, output = tmp_path / 'data', tmp_path / 'out'
    data.mkdir()
    credentials = {'schema_version': 2, 'devices': [{'did': 'test', 'localkey': 'dummy', 'device_mqtt_usr': 'dummy', 'device_mqtt_pass': 'dummy'}]}
    (data / 'runtime_credentials.json').write_text(json.dumps(credentials))
    scene = {'param': {'action': {'items': [{'param': {'method': 'do_scenes_zones', 'params': {'data': [{'tid': 't', 'zones': [{'zid': 7}, {'zid': 8}]}]}}}]}}}
    (data / 'web_api_inventory.json').write_text(json.dumps({'scenes': [scene]}))
    return data, output


def response(zid=7, value=None, error=None):
    return {'topic': 'rr/d/user/test/device', 'decoded_messages': [{'response_to': {'request_method': 'set_scenes_zones', 'request_params': {'data': [{'tid': 'temporary', 'zones': [{'zid': 0, 'range': value or [1, 2, 3, 4]}]}]}, 'result': [{'tid': 't', 'zones': [{'zid': zid}]}], 'error': error}}]}


def write_log(data, entries):
    (data / 'decompiled_mqtt.jsonl').write_text(''.join(json.dumps(v) + '\n' for v in entries))


def test_explicit_prepare_and_apply_preserve_credentials_and_merge_all_required(tmp_path):
    data, output = fixture(tmp_path)
    write_log(data, [response(7), response(8), response(7, [5, 6, 7, 8])])
    before = (data / 'runtime_credentials.json').read_bytes()
    result = prepare(data, output)
    assert not (data / 'zone_ranges.json').exists()
    assert result['counts']['required_zones'] == 2
    assert json.loads((output / 'zone_ranges.json').read_text()) == {'t:7': [5, 6, 7, 8], 't:8': [1, 2, 3, 4]}
    with pytest.raises(MigrationError):
        apply(data, output, writers_stopped=False)
    apply(data, output, writers_stopped=True)
    assert json.loads((data / 'runtime_credentials.json').read_text()) == json.loads(before)
    assert (data / 'migration_pending.json').exists()
    assert apply(data, output, writers_stopped=True)['phase'] == 'state_installed'


def test_failed_response_does_not_recover_missing_geometry(tmp_path):
    data, output = fixture(tmp_path)
    write_log(data, [response(7), response(8, error={'code': -1})])
    with pytest.raises(MigrationError, match='missing=1'):
        prepare(data, output)


def test_partial_current_store_preserved_and_missing_tuple_recovered(tmp_path):
    data, output = fixture(tmp_path)
    (data / 'zone_ranges.json').write_text(json.dumps({'t:7': [9, 9, 10, 10]}))
    write_log(data, [response(7), response(8)])
    prepare(data, output)
    assert json.loads((output / 'zone_ranges.json').read_text())['t:7'] == [9, 9, 10, 10]


@pytest.mark.parametrize('payload', [b'{}', b'\xff\n', b'[]\n', b'{broken}\n', b'x' * (1024 * 1024 + 1) + b'\n'])
def test_bad_record_blocks_without_unbounded_read(tmp_path, payload):
    path = tmp_path / 'log'
    path.write_bytes(payload)
    with pytest.raises(MigrationError):
        list(records(path, len(payload)))


def test_appended_replaced_or_truncated_source_blocks_apply(tmp_path):
    data, output = fixture(tmp_path)
    write_log(data, [response(7), response(8)])
    prepare(data, output)
    with (data / 'decompiled_mqtt.jsonl').open('a') as stream:
        stream.write('{}\n')
    with pytest.raises(MigrationError, match='source changed'):
        apply(data, output, writers_stopped=True)
    assert not (data / 'zone_ranges.json').exists()


def test_missing_password_never_guessed_from_connect_preview(tmp_path):
    data, output = fixture(tmp_path)
    value = json.loads((data / 'runtime_credentials.json').read_text())
    value['devices'][0]['device_mqtt_pass'] = ''
    (data / 'runtime_credentials.json').write_text(json.dumps(value))
    with pytest.raises(MigrationError, match='identity reconciliation'):
        prepare(data, output)


def test_retire_preserves_exact_original_inodes_and_resumes(tmp_path):
    from roborock_local_server.legacy_migration import retire
    data, output = fixture(tmp_path)
    write_log(data, [response(7), response(8)])
    (data / 'mqtt_server.log').write_text('legacy diagnostic\n')
    original_inode = (data / 'mqtt_server.log').stat().st_ino
    prepare(data, output)
    apply(data, output, writers_stopped=True)
    result = retire(data, output, writers_stopped=True)
    assert result['phase'] == 'logs_retired'
    assert (data / 'retired-diagnostics' / 'mqtt_server.log').stat().st_ino == original_inode
    assert not (data / 'migration_pending.json').exists()
    assert retire(data, output, writers_stopped=True)['phase'] == 'logs_retired'


def test_partial_apply_reconciles_exact_candidate_and_original(tmp_path, monkeypatch):
    import roborock_local_server.legacy_migration as migration
    data, output = fixture(tmp_path)
    write_log(data, [response(7), response(8)])
    prepare(data, output)
    real = migration.atomic_json
    failed = False
    def fail_one(path, value):
        nonlocal failed
        if path == data / 'zone_ranges.json' and not failed:
            failed = True
            raise OSError('synthetic full disk')
        real(path, value)
    monkeypatch.setattr(migration, 'atomic_json', fail_one)
    with pytest.raises(OSError):
        apply(data, output, writers_stopped=True)
    assert (data / 'migration_pending.json').exists()
    assert apply(data, output, writers_stopped=True)['phase'] == 'state_installed'


def test_uncertain_write_can_resume_but_unexpected_state_blocks(tmp_path, monkeypatch):
    import roborock_local_server.legacy_migration as migration
    data, output = fixture(tmp_path)
    write_log(data, [response(7), response(8)])
    prepare(data, output)
    real = migration.atomic_json
    def uncertain(path, value):
        real(path, value)
        if path == data / 'zone_ranges.json':
            raise OSError('synthetic directory fsync failure')
    monkeypatch.setattr(migration, 'atomic_json', uncertain)
    with pytest.raises(OSError):
        apply(data, output, writers_stopped=True)
    (data / 'zone_ranges.json').write_text('{"t:7":[99,99,100,100]}')
    with pytest.raises(MigrationError, match='unexpected authoritative'):
        apply(data, output, writers_stopped=True)


@pytest.mark.parametrize('field,value', [('candidates', {'../outside': '0' * 64}), ('sources', {'../outside': {}}), ('retired_logs', [{'name': '../outside'}])])
def test_corrupt_journal_never_selects_unlisted_paths(tmp_path, field, value):
    data, output = fixture(tmp_path)
    write_log(data, [response(7), response(8)])
    prepare(data, output)
    path = output / 'journal.json'
    journal = json.loads(path.read_text())
    journal[field] = value
    path.write_text(json.dumps(journal))
    with pytest.raises(MigrationError):
        apply(data, output, writers_stopped=True)
    assert not (data / 'migration_pending.json').exists()


@pytest.mark.parametrize('action', [None, [], {'items': [None]}, {'items': [{'param': {'method': 'do_scenes_zones', 'params': []}}]}])
def test_malformed_scene_schema_is_sanitized(tmp_path, action):
    from roborock_local_server.legacy_migration import required_zones
    with pytest.raises(MigrationError):
        required_zones({'scenes': [{'param': {'action': action}}]})


def test_large_stream_memory_is_bounded_by_record_not_history(tmp_path):
    import os
    import subprocess
    import sys
    path = tmp_path / 'large.jsonl'
    line = json.dumps({'padding': 'x' * 1000}).encode() + b'\n'
    with path.open('wb') as stream:
        for _ in range(131072):
            stream.write(line)
    code = """from pathlib import Path
import sys
from roborock_local_server.legacy_migration import records
p=Path(sys.argv[1]); n=0; peak=0
for _ in records(p,p.stat().st_size):
    n += 1
    if n % 8192 == 0:
        rss = next(int(line.split()[1]) for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('VmRSS:'))
        peak = max(peak, rss)
print(n,peak)
"""
    result = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True, text=True, check=True, env=os.environ)
    count, peak_kib = map(int, result.stdout.split())
    assert count == 131072
    assert peak_kib < 64 * 1024


def test_numeric_assigned_tid_has_legacy_canonical_identity():
    from roborock_local_server.legacy_migration import response_zones, zone_key
    value = response()['decoded_messages'][0]['response_to']
    value['result'][0]['tid'] = 123
    assert response_zones(value) == {'123:7': [1, 2, 3, 4]}
    for invalid in (True, 1.5, [], {}):
        with pytest.raises(MigrationError):
            zone_key(invalid, 7)


@pytest.mark.parametrize('missing', ['tid', 'zid'])
def test_success_without_assigned_identity_never_recovers(missing):
    from roborock_local_server.legacy_migration import response_zones
    value = response()['decoded_messages'][0]['response_to']
    if missing == 'tid':
        del value['result'][0]['tid']
    else:
        del value['result'][0]['zones'][0]['zid']
    with pytest.raises(MigrationError):
        response_zones(value)


def test_oversized_current_store_never_becomes_startup_rejecting_candidate(tmp_path):
    data, output = fixture(tmp_path)
    write_log(data, [response(7), response(8)])
    (data / 'zone_ranges.json').write_text(json.dumps({f't:{i}': [1, 2, 3, 4] for i in range(10001)}))
    with pytest.raises(MigrationError):
        prepare(data, output)
    assert not (output / 'journal.json').exists()


def test_invalid_authoritative_field_cannot_bypass_runtime_schema(tmp_path):
    data, output = fixture(tmp_path)
    write_log(data, [response(7), response(8)])
    value=json.loads((data/'runtime_credentials.json').read_text())
    value['api_host']=123
    (data/'runtime_credentials.json').write_text(json.dumps(value))
    with pytest.raises(MigrationError, match='runtime schema'):
        prepare(data, output)
    assert not (output / 'journal.json').exists()


def test_output_symlink_refused_without_chmod_external_target(tmp_path):
    data, output = fixture(tmp_path)
    target=tmp_path/'external'; target.mkdir(mode=0o755)
    output.symlink_to(target, target_is_directory=True)
    with pytest.raises(MigrationError, match='symlink'):
        prepare(data, output)
    assert target.stat().st_mode & 0o777 == 0o755
    assert not list(target.iterdir())


@pytest.mark.parametrize('mutation', ['replace', 'truncate'])
def test_source_identity_and_truncation_invalidate_preparation(tmp_path, mutation):
    data, output = fixture(tmp_path)
    write_log(data, [response(7), response(8)])
    prepare(data, output)
    path = data / 'decompiled_mqtt.jsonl'
    if mutation == 'replace':
        replacement=data/'replacement'; replacement.write_bytes(path.read_bytes()); replacement.replace(path)
    else:
        path.write_bytes(b'{}\n')
    with pytest.raises(MigrationError, match='source changed'):
        apply(data, output, writers_stopped=True)
    assert not (data/'migration_pending.json').exists()


@pytest.mark.parametrize('phase', ['prepared','writers_stopped','candidates_validated','state_installed','logs_retiring','logs_retired','credential_installed','first_log_renamed'])
def test_abrupt_process_exit_reconciles_each_journal_boundary(tmp_path, phase):
    import os, subprocess, sys
    from roborock_local_server.legacy_migration import retire
    data, output = fixture(tmp_path)
    write_log(data, [response(7), response(8)])
    (data/'mqtt_server.log').write_text('synthetic legacy diagnostic\n')
    inode=(data/'mqtt_server.log').stat().st_ino
    if phase != 'prepared':
        prepare(data, output)
    mode='prepare' if phase=='prepared' else 'apply'
    if phase in ('logs_retiring','logs_retired','first_log_renamed'):
        apply(data, output, writers_stopped=True); mode='retire'
    code="""import os,sys
from pathlib import Path
import roborock_local_server.legacy_migration as migration
data,output,phase,mode=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3],sys.argv[4]
real=migration.atomic_json
def interrupted(path,value):
    real(path,value)
    if (path.name=='journal.json' and value.get('phase')==phase) or (phase=='credential_installed' and path==data/'runtime_credentials.json'):
        os._exit(73)
migration.atomic_json=interrupted
real_rename=migration.os.rename
def interrupted_rename(source,target):
    real_rename(source,target)
    if phase=='first_log_renamed': os._exit(73)
migration.os.rename=interrupted_rename
if mode=='prepare': migration.prepare(data,output)
elif mode=='apply': migration.apply(data,output,writers_stopped=True)
else: migration.retire(data,output,writers_stopped=True)
"""
    result=subprocess.run([sys.executable,'-c',code,str(data),str(output),phase,mode],capture_output=True,text=True,env=os.environ)
    assert result.returncode==73, result.stderr
    journal=json.loads((output/'journal.json').read_text())
    if journal['phase'] in ('prepared','writers_stopped','candidates_validated','state_installed'):
        apply(data, output, writers_stopped=True)
    assert retire(data, output, writers_stopped=True)['phase']=='logs_retired'
    assert not (data/'migration_pending.json').exists()
    assert (data/'retired-diagnostics/mqtt_server.log').stat().st_ino==inode
    assert json.loads((data/'zone_ranges.json').read_text())=={'t:7':[1,2,3,4],'t:8':[1,2,3,4]}
