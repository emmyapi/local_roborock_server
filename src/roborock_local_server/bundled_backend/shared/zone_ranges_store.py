"""Persistent store for scene zone range coordinates.

Zone ranges map (tid, zid) -> [x1, y1, x2, y2].  The app never includes
range coordinates in scene creation requests — it relies on the server
to remember them from previous ``set_scenes_zones`` MQTT exchanges.

This module provides a lightweight JSON-backed store that is updated when
the MQTT proxy observes ``set_scenes_zones`` traffic and when the routine
runner sends zone sync commands.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .durable_state import atomic_json, load_json_object, PersistenceError, PersistenceUncertainError


_ZONE_RANGES_FILE = "zone_ranges.json"


def _key(tid: str, zid: int) -> str:
    return f"{tid}:{zid}"


def _parse_key(key: str) -> tuple[str, int] | None:
    parts = key.rsplit(":", 1)
    if len(parts) != 2:
        return None
    try:
        return parts[0], int(parts[1])
    except (TypeError, ValueError):
        return None



def _validated_zone(tid: Any, zid: Any, coords: Any) -> tuple[str, list[int]]:
    if (not isinstance(tid, str) or not tid.strip() or type(zid) is not int or zid < 0
            or not isinstance(coords, list) or len(coords) != 4
            or any(type(value) is not int for value in coords)):
        raise PersistenceError("Invalid zone geometry or assigned identity; state was not published")
    return _key(tid.strip(), zid), list(coords)

class ZoneRangesStore:
    """Thread-safe, JSON-backed cache of zone range coordinates."""

    def __init__(self, directory: Path) -> None:
        self._path = directory / _ZONE_RANGES_FILE
        self._lock = threading.Lock()
        self._data: dict[str, list[int]] = self._load()

    def _load(self) -> dict[str, list[int]]:
        raw = load_json_object(self._path)
        for key, value in raw.items():
            parsed = _parse_key(key)
            if (parsed is None or not parsed[0] or parsed[1] < 0
                    or not isinstance(value, list) or len(value) != 4
                    or any(type(coord) is not int for coord in value)):
                raise PersistenceError("Invalid authoritative zone ranges; restore or migrate state")
        return raw

    def _commit(self, updates: dict[str, list[int]]) -> None:
        with self._lock:
            candidate = {**self._data, **updates}
            if candidate == self._data:
                return
            try:
                atomic_json(self._path, candidate)
            except PersistenceUncertainError:
                self._data = self._load()
                raise
            self._data = candidate

    def get(self, tid: str, zid: int) -> list[int] | None:
        with self._lock:
            value = self._data.get(_key(tid, zid))
            if isinstance(value, list) and len(value) >= 4:
                return list(value)
            return None

    def get_all(self, tids: set[str] | None = None) -> dict[tuple[str, int], list[int]]:
        with self._lock:
            result: dict[tuple[str, int], list[int]] = {}
            for k, v in self._data.items():
                parsed = _parse_key(k)
                if parsed is None:
                    continue
                if tids is not None and parsed[0] not in tids:
                    continue
                result[parsed] = list(v)
            return result

    def put(self, tid: str, zid: int, range_coords: list[int]) -> None:
        key, coords = _validated_zone(tid, zid, range_coords)
        self._commit({key: coords})

    def merge_set_scenes_zones_request(self, params: Any) -> None:
        """Extract and store ranges from a set_scenes_zones RPC request."""
        if not isinstance(params, dict) or not isinstance(params.get("data"), list):
            raise PersistenceError("Invalid zone request batch")
        updates = {}
        for entry in params["data"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("zones"), list):
                raise PersistenceError("Invalid zone request batch")
            for zone in entry["zones"]:
                if not isinstance(zone, dict):
                    raise PersistenceError("Invalid zone request entry")
                key, coords = _validated_zone(entry.get("tid"), zone.get("zid"), zone.get("range"))
                updates[key] = coords
        self._commit(updates)

    def seed_from_mqtt_jsonl(self, mqtt_jsonl: Path) -> int:
        """Populate the store from an existing MQTT JSONL log file.

        Returns the number of new entries added.
        """
        if not mqtt_jsonl.exists():
            return 0
        count_before = len(self._data)
        try:
            with mqtt_jsonl.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    decoded_messages = entry.get("decoded_messages")
                    if not isinstance(decoded_messages, list):
                        continue
                    for decoded in decoded_messages:
                        if not isinstance(decoded, dict):
                            continue
                        rpc = decoded.get("rpc")
                        if isinstance(rpc, dict) and str(rpc.get("method") or "").strip() == "set_scenes_zones":
                            self.merge_set_scenes_zones_request(rpc.get("params"))
                        response_to = decoded.get("response_to")
                        if isinstance(response_to, dict) and str(response_to.get("request_method") or "").strip() == "set_scenes_zones":
                            self.merge_set_scenes_zones_response(
                                request_params=response_to.get("request_params"),
                                result=response_to.get("result"),
                            )
        except OSError:
            pass
        return len(self._data) - count_before

    def merge_set_scenes_zones_response(
        self,
        request_params: Any,
        result: Any,
    ) -> None:
        """Extract and store ranges from a set_scenes_zones RPC response.

        The response's ``result`` provides the device-assigned ``tid`` and
        ``zid`` while the request's ``params`` contains the ``range`` values.
        """
        if not isinstance(request_params, dict) or not isinstance(result, list):
            raise PersistenceError("Invalid successful zone response")
        request_data = request_params.get("data")
        if not isinstance(request_data, list) or len(request_data) != len(result):
            raise PersistenceError("Incomplete successful zone response batch")
        updates = {}
        for req_entry, res_entry in zip(request_data, result):
            if not isinstance(req_entry, dict) or not isinstance(res_entry, dict):
                raise PersistenceError("Invalid successful zone response entry")
            req_zones = req_entry.get("zones")
            res_zones = res_entry.get("zones")
            if not isinstance(req_zones, list) or not isinstance(res_zones, list) or len(req_zones) != len(res_zones):
                raise PersistenceError("Incomplete successful zone response entries")
            for req_zone, res_zone in zip(req_zones, res_zones):
                if not isinstance(req_zone, dict) or not isinstance(res_zone, dict):
                    raise PersistenceError("Invalid successful zone response zone")
                key, coords = _validated_zone(res_entry.get("tid"), res_zone.get("zid"), req_zone.get("range"))
                updates[key] = coords
        self._commit(updates)
