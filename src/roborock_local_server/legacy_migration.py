"""Explicit offline recovery preparation. Never invoked by normal server startup.

Outputs contain private state. Keep the journal and candidates outside source control.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterator

MAX_RECORD = 1024 * 1024
MAX_KEYS = 10000
VERSION = 1


class MigrationError(RuntimeError):
    pass


def reject_path(path: Path) -> None:
    if any(candidate.is_symlink() for candidate in (path, *path.parents)):
        raise MigrationError('symlink path component refused')


def digest(path: Path) -> str:
    reject_path(path)
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise MigrationError('nonregular source refused')
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    reject_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise MigrationError('symlink destination refused')
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path: Path, *, absent: Any = None) -> Any:
    reject_path(path)
    if path.is_symlink():
        raise MigrationError('symlink source refused')
    try:
        if path.stat().st_size > 64 * 1024 * 1024:
            raise MigrationError('authoritative state exceeds supported bound')
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return absent
    except (ValueError, OSError) as exc:
        raise MigrationError('invalid or unreadable authoritative state') from exc


def coords(value: Any) -> list[int]:
    if not isinstance(value, list) or len(value) != 4 or any(type(v) is not int or abs(v) > 10000000 for v in value):
        raise MigrationError('invalid zone coordinates')
    return value


def zone_key(tid: Any, zid: Any) -> str:
    if type(tid) is int:
        tid = str(tid)
    if not isinstance(tid, str) or not tid.strip() or len(tid) > 256 or type(zid) is not int or zid < 0:
        raise MigrationError('invalid zone identity')
    return f'{tid}:{zid}'


def required_zones(inventory: Any) -> tuple[set[str], dict[str, list[int]]]:
    if not isinstance(inventory, dict) or not isinstance(inventory.get('scenes'), list):
        raise MigrationError('scene references unavailable')
    required: set[str] = set()
    embedded: dict[str, list[int]] = {}
    for scene in inventory['scenes']:
        if not isinstance(scene, dict):
            raise MigrationError('invalid scene')
        payload = scene.get('param', {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError as exc:
                raise MigrationError('invalid scene payload') from exc
        if not isinstance(payload, dict):
            raise MigrationError('invalid scene payload')
        action = payload.get('action', {})
        if not isinstance(action, dict):
            raise MigrationError('invalid scene action')
        items = action.get('items', [])
        if not isinstance(items, list):
            raise MigrationError('invalid scene actions')
        for item in items:
            if not isinstance(item, dict):
                raise MigrationError('invalid scene item')
            nested = item.get('param', {})
            if isinstance(nested, str):
                try:
                    nested = json.loads(nested)
                except ValueError as exc:
                    raise MigrationError('invalid scene command') from exc
            if not isinstance(nested, dict):
                raise MigrationError('invalid scene command')
            if nested.get('method') != 'do_scenes_zones':
                continue
            params = nested.get('params')
            if not isinstance(params, dict):
                raise MigrationError('invalid scene parameters')
            entries = params.get('data')
            if not isinstance(entries, list):
                raise MigrationError('invalid zone scene')
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get('zones'), list):
                    raise MigrationError('invalid scene zone entry')
                for zone in entry['zones']:
                    if not isinstance(zone, dict):
                        raise MigrationError('invalid scene zone')
                    key = zone_key(entry.get('tid'), zone.get('zid'))
                    required.add(key)
                    if 'range' in zone:
                        value = coords(zone['range'])
                        if key in embedded and embedded[key] != value:
                            raise MigrationError('conflicting embedded scene geometry')
                        embedded[key] = value
    if len(required) > MAX_KEYS:
        raise MigrationError('zone key limit exceeded')
    return required, embedded


def records(path: Path, boundary: int) -> Iterator[dict[str, Any]]:
    with path.open('rb') as stream:
        remaining = boundary
        while remaining:
            line = stream.readline(min(MAX_RECORD + 1, remaining))
            remaining -= len(line)
            if not line or len(line) > MAX_RECORD or not line.endswith(b'\n'):
                raise MigrationError('oversized or incomplete legacy record requires reconciliation')
            try:
                value = json.loads(line.decode('utf-8'))
            except (ValueError, UnicodeError) as exc:
                raise MigrationError('malformed legacy record requires reconciliation') from exc
            if not isinstance(value, dict):
                raise MigrationError('unknown legacy record format')
            yield value


def response_zones(response: dict[str, Any]) -> dict[str, list[int]]:
    if response.get('error') is not None:
        return {}
    params = response.get('request_params')
    if not isinstance(params, dict):
        raise MigrationError('malformed recovery-bearing parameters')
    requests = params.get('data')
    results = response.get('result')
    if not isinstance(requests, list) or not isinstance(results, list) or len(requests) != len(results):
        raise MigrationError('malformed recovery-bearing response')
    found = {}
    for request, result in zip(requests, results):
        if not isinstance(request, dict) or not isinstance(result, dict):
            raise MigrationError('malformed zone exchange')
        req_zones, res_zones = request.get('zones'), result.get('zones')
        if not isinstance(req_zones, list) or not isinstance(res_zones, list) or len(req_zones) != len(res_zones):
            raise MigrationError('incomplete zone response')
        for req_zone, res_zone in zip(req_zones, res_zones):
            if not isinstance(req_zone, dict) or not isinstance(res_zone, dict):
                raise MigrationError('malformed recovery-bearing zone')
            if 'range' not in req_zone:
                continue
            key = zone_key(result.get('tid'), res_zone.get('zid'))
            found[key] = coords(req_zone['range'])
    return found


def _prepare(data: Path, output: Path, *, audit: bool = False) -> dict[str, Any]:
    reject_path(data)
    reject_path(output)
    if output.resolve() == data.resolve() or data.resolve() in output.resolve().parents:
        raise MigrationError('candidate output must be outside runtime directory')
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    if (output / 'journal.json').exists():
        raise MigrationError('existing journal requires explicit reconciliation')
    credentials_path = data / 'runtime_credentials.json'
    inventory_path = data / 'web_api_inventory.json'
    zones_path = data / 'zone_ranges.json'
    credentials = read_json(credentials_path)
    if not isinstance(credentials, dict) or credentials.get('schema_version') != 2:
        raise MigrationError('unsupported credentials state')
    devices = credentials.get('devices')
    if isinstance(devices, dict):
        devices = list(devices.values())
    if not isinstance(devices, list) or not devices or len(devices) > MAX_KEYS:
        raise MigrationError('known device identities unavailable')
    for device in devices:
        if not isinstance(device, dict) or not (device.get('did') or device.get('duid')) or any(not isinstance(device.get(k), str) or not device[k] for k in ('localkey', 'device_mqtt_usr', 'device_mqtt_pass')):
            raise MigrationError('missing credentials require authenticated identity reconciliation; CONNECT previews are insufficient')
    inventory = read_json(inventory_path)
    required, embedded = required_zones(inventory)
    current = read_json(zones_path, absent={})
    if not isinstance(current, dict) or len(current) > MAX_KEYS:
        raise MigrationError('invalid zone store')
    for key, value in current.items():
        if not isinstance(key, str):
            raise MigrationError('invalid zone key')
        tid, sep, zid = key.rpartition(':')
        if not sep or not zid.isdecimal() or zone_key(tid, int(zid)) != key:
            raise MigrationError('invalid zone key')
        coords(value)
    initial_state = {credentials_path: credentials, inventory_path: inventory, zones_path: current}
    source = data / 'decompiled_mqtt.jsonl'
    if source.is_symlink():
        raise MigrationError('symlink legacy source refused')
    info = source.stat()
    if not stat.S_ISREG(info.st_mode):
        raise MigrationError('legacy source is not regular')
    history: dict[str, list[int]] = {}
    owners: dict[str, int] = {}
    count = 0
    for entry in records(source, info.st_size):
        count += 1
        decoded = entry.get('decoded_messages', [])
        if not isinstance(decoded, list):
            raise MigrationError('unknown decoded-message format')
        for message in decoded:
            if not isinstance(message, dict):
                raise MigrationError('invalid decoded message')
            response = message.get('response_to')
            if isinstance(response, dict) and response.get('request_method') == 'set_scenes_zones':
                recovered = response_zones(response)
                if recovered:
                    parts = str(entry.get('topic') or '').split('/')
                    matches = [i for i, device in enumerate(devices)
                               if (len(parts) >= 5 and parts[:2] == ['rr', 'd'] and parts[3] == device.get('did'))
                               or (len(parts) >= 6 and parts[:2] == ['rr', 'm'] and parts[5] == device.get('duid'))]
                    if len(matches) != 1:
                        raise MigrationError('zone exchange device identity unresolved')
                    for key in recovered:
                        if key in owners and owners[key] != matches[0]:
                            raise MigrationError('zone tuple collides across devices')
                        owners[key] = matches[0]
                    history.update(recovered)
                if len(history) > MAX_KEYS:
                    raise MigrationError('historical zone key limit exceeded')
    merged = dict(history)
    merged.update(embedded)
    merged.update(current)
    if len(merged) > MAX_KEYS:
        raise MigrationError('candidate zone key limit exceeded')
    conflicts = sum(key in embedded and key in current and embedded[key] != current[key] for key in required)
    missing = required - merged.keys()
    if missing or conflicts:
        raise MigrationError(f'incomplete required geometry: missing={len(missing)} conflicts={conflicts}')
    counts = {'records': count, 'devices': len(devices), 'required_zones': len(required), 'zones': len(merged)}
    if audit:
        return {'phase': 'audit_only', 'counts': counts}
    final = source.stat()
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns):
        raise MigrationError('legacy source changed during preparation; retry with writers stopped')
    candidates = {'runtime_credentials.json': credentials, 'zone_ranges.json': merged}
    for path, original in initial_state.items():
        if read_json(path, absent={}) != original:
            raise MigrationError('authoritative state changed during preparation')
    sources = {}
    for path in (credentials_path, inventory_path, zones_path, source):
        if path.exists():
            st = path.stat()
            fingerprint = digest(path)
            final_stat = path.stat()
            if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != (final_stat.st_dev, final_stat.st_ino, final_stat.st_size, final_stat.st_mtime_ns):
                raise MigrationError('source changed while fingerprinting')
            sources[path.name] = {'device': st.st_dev, 'inode': st.st_ino, 'bytes': st.st_size, 'sha256': fingerprint}
            if path in initial_state:
                sources[path.name]['payload_sha256'] = payload_digest(initial_state[path])
    for name, value in candidates.items():
        atomic_json(output / name, value)
    from roborock_local_server.bundled_backend.shared.runtime_credentials import RuntimeCredentialsStore
    from roborock_local_server.bundled_backend.shared.zone_ranges_store import ZoneRangesStore
    from roborock_local_server.bundled_backend.shared.durable_state import PersistenceError
    try:
        RuntimeCredentialsStore(output / 'runtime_credentials.json')
        ZoneRangesStore(output)
    except PersistenceError as exc:
        raise MigrationError('candidate does not satisfy authoritative runtime schema') from exc
    journal = {'version': VERSION, 'phase': 'prepared', 'data_directory': str(data.resolve()), 'sources': sources, 'candidates': {name: digest(output / name) for name in candidates}, 'counts': {'records': count, 'devices': len(devices), 'required_zones': len(required), 'zones': len(merged)}, 'retired_logs': []}
    atomic_json(output / 'journal.json', journal)
    return journal


def payload_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def validated_journal(data: Path, output: Path) -> dict[str, Any]:
    journal = read_json(output / 'journal.json')
    if not isinstance(journal, dict) or journal.get('version') != VERSION or journal.get('data_directory') != str(data.resolve()):
        raise MigrationError('journal identity mismatch')
    candidates = journal.get('candidates')
    sources = journal.get('sources')
    if not isinstance(candidates, dict) or set(candidates) != {'runtime_credentials.json', 'zone_ranges.json'}:
        raise MigrationError('journal candidate allowlist mismatch')
    if not isinstance(sources, dict) or not {'runtime_credentials.json', 'web_api_inventory.json', 'decompiled_mqtt.jsonl'} <= set(sources) <= {'runtime_credentials.json', 'web_api_inventory.json', 'decompiled_mqtt.jsonl', 'zone_ranges.json'}:
        raise MigrationError('journal source allowlist mismatch')
    for value in candidates.values():
        if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
            raise MigrationError('invalid journal fingerprint')
    for name, source in sources.items():
        if not isinstance(source, dict) or any(type(source.get(k)) is not int or source[k] < 0 for k in ('device', 'inode', 'bytes')):
            raise MigrationError('invalid source identity')
        for field in ('sha256',) if name == 'decompiled_mqtt.jsonl' else ('sha256', 'payload_sha256'):
            if not isinstance(source.get(field), str) or re.fullmatch('[0-9a-f]{64}', source[field]) is None:
                raise MigrationError('invalid source fingerprint')
    if not isinstance(journal.get('counts'), dict) or not isinstance(journal.get('retired_logs'), list):
        raise MigrationError('invalid journal metadata')
    seen = set()
    for item in journal['retired_logs']:
        if not isinstance(item, dict) or item.get('name') not in LOG_NAMES or item['name'] in seen or any(type(item.get(k)) is not int or item[k] < 0 for k in ('device', 'inode', 'bytes')):
            raise MigrationError('invalid retired diagnostic identity')
        seen.add(item['name'])
    return journal


def _apply(data: Path, output: Path, *, writers_stopped: bool) -> dict[str, Any]:
    if not writers_stopped:
        raise MigrationError('apply requires explicit --writers-stopped acknowledgement')
    journal = validated_journal(data, output)
    if journal.get('phase') not in ('prepared', 'writers_stopped', 'candidates_validated', 'state_installed'):
        raise MigrationError('journal phase requires operator reconciliation')
    # Resume only if every actual state is exactly the recorded original or candidate.
    for name, source in journal['sources'].items():
        path = data / name
        actual = digest(path)
        if actual == journal['candidates'].get(name):
            continue
        st = path.stat()
        if actual != source['sha256'] or (st.st_dev, st.st_ino, st.st_size) != (source['device'], source['inode'], source['bytes']):
            raise MigrationError('source changed since preparation')
    for name, expected in journal['candidates'].items():
        if digest(output / name) != expected:
            raise MigrationError('candidate checksum mismatch')
        if name not in journal['sources'] and (data / name).exists() and digest(data / name) != expected:
            raise MigrationError('unexpected authoritative state appeared')
    journal['phase'] = 'writers_stopped'
    atomic_json(output / 'journal.json', journal)
    marker = data / 'migration_pending.json'
    atomic_json(marker, {'journal': str((output / 'journal.json').resolve())})
    journal['phase'] = 'candidates_validated'
    atomic_json(output / 'journal.json', journal)
    for name in journal['candidates']:
        original = data / name
        backup = output / ('original-' + name)
        if backup.exists():
            if payload_digest(read_json(backup)) != journal['sources'].get(name, {}).get('payload_sha256'):
                raise MigrationError('original backup fingerprint mismatch')
        elif original.exists() and digest(original) != journal['candidates'][name]:
            atomic_json(backup, read_json(original))
            if payload_digest(read_json(backup)) != journal['sources'][name]['payload_sha256']:
                raise MigrationError('original backup fingerprint mismatch')
        atomic_json(original, read_json(output / name))
    journal['phase'] = 'state_installed'
    atomic_json(output / 'journal.json', journal)
    return journal


LOG_NAMES = ('api_server.log', 'iot_server.log', 'wood_server.log', 'https_server.log',
             'mqtt_server.log', 'unknown_server.log', 'decompiled_http.jsonl', 'decompiled_mqtt.jsonl')


def _retire(data: Path, output: Path, *, writers_stopped: bool) -> dict[str, Any]:
    """Rename the exact legacy allowlist on the same filesystem; never delete."""
    if not writers_stopped:
        raise MigrationError('retirement requires stopped writers')
    journal = validated_journal(data, output)
    if journal.get('phase') not in ('state_installed', 'logs_retiring', 'logs_retired') or journal.get('data_directory') != str(data.resolve()):
        raise MigrationError('retirement journal identity or phase mismatch')
    for name, expected in journal['candidates'].items():
        if digest(data / name) != expected:
            raise MigrationError('installed state differs from validated candidate')
    retired = data / 'retired-diagnostics'
    if retired.is_symlink():
        raise MigrationError('symlink retirement directory refused')
    retired.mkdir(mode=0o700, exist_ok=True)
    if retired.stat().st_dev != data.stat().st_dev:
        raise MigrationError('retirement must stay on same filesystem')
    if journal['phase'] == 'state_installed':
        if any(retired.iterdir()):
            raise MigrationError('unexpected retirement contents')
        inventory = []
        for name in LOG_NAMES:
            source = data / name
            if source.exists():
                st = source.lstat()
                if not stat.S_ISREG(st.st_mode):
                    raise MigrationError('nonregular diagnostic refused')
                inventory.append({'name': name, 'device': st.st_dev, 'inode': st.st_ino, 'bytes': st.st_size})
        journal['retired_logs'] = inventory
        journal['phase'] = 'logs_retiring'
        atomic_json(output / 'journal.json', journal)
    for item in journal['retired_logs']:
        if item['name'] not in LOG_NAMES:
            raise MigrationError('retirement allowlist mismatch')
        source, target = data / item['name'], retired / item['name']
        if target.exists() and source.exists():
            raise MigrationError('both original and retired log exist; reconcile writers')
        path = source if source.exists() else target
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino, st.st_size) != (item['device'], item['inode'], item['bytes']):
            raise MigrationError('retired diagnostic identity changed')
        if source.exists():
            os.rename(source, target)
    for path in (data, retired):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    journal['phase'] = 'logs_retired'
    atomic_json(output / 'journal.json', journal)
    # The multi-file transaction is now durable and reconciled. Service acceptance
    # remains pending in the private journal; old diagnostics remain untouched.
    marker = data / 'migration_pending.json'
    marker.unlink(missing_ok=True)
    fd = os.open(data, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return journal


def _leased(operation, data: Path, output: Path, **kwargs):
    from roborock_local_server.bundled_backend.shared.durable_state import RuntimeDirectoryLease, PersistenceError
    reject_path(data)
    reject_path(output)
    try:
        with RuntimeDirectoryLease(data, allow_migration_pending=operation is not _prepare):
            return operation(data, output, **kwargs)
    except PersistenceError as exc:
        raise MigrationError('runtime directory is active or pending; reconcile before migration') from exc


def prepare(data: Path, output: Path, *, audit: bool = False) -> dict[str, Any]:
    if audit:
        return _prepare(data, output, audit=True)
    return _leased(_prepare, data, output)


def apply(data: Path, output: Path, *, writers_stopped: bool) -> dict[str, Any]:
    return _leased(_apply, data, output, writers_stopped=writers_stopped)


def retire(data: Path, output: Path, *, writers_stopped: bool) -> dict[str, Any]:
    return _leased(_retire, data, output, writers_stopped=writers_stopped)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['audit', 'dry-run', 'apply', 'retire'])
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--writers-stopped', action='store_true')
    args = parser.parse_args()
    try:
        if args.mode in ('audit', 'dry-run'):
            result = prepare(args.data_dir, args.output, audit=args.mode == 'audit')
        elif args.mode == 'apply':
            result = apply(args.data_dir, args.output, writers_stopped=args.writers_stopped)
        else:
            result = retire(args.data_dir, args.output, writers_stopped=args.writers_stopped)
    except (MigrationError, OSError) as exc:
        parser.exit(1, f'migration blocked: {exc}\n')
    print(json.dumps({'phase': result['phase'], 'counts': result['counts']}))


if __name__ == '__main__':
    main()
