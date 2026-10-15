"""Crash-safe JSON publication and the runtime-directory process lease."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import sys
import threading
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_state = sys.modules.setdefault("_roborock_durable_state", types.ModuleType("_roborock_durable_state"))
if not hasattr(_state, "lock"):
    _state.lock = threading.RLock()
    _state.errors = 0
    _state.uncertain = 0
    _state.writes = 0
    _state.incarnation = uuid.uuid4().hex
    _state.started_at = datetime.now(timezone.utc).isoformat()


def durable_state_status() -> dict[str, Any]:
    with _state.lock:
        return {"process_id": os.getpid(), "incarnation": _state.incarnation,
                "started_at": _state.started_at, "persistence_errors": _state.errors,
                "uncertain_publications": _state.uncertain, "successful_publications": _state.writes}


class PersistenceError(RuntimeError):
    """Authoritative state could not be loaded or durably published."""
    def __init__(self, *args):
        with _state.lock:
            _state.errors += 1
            if isinstance(self, PersistenceUncertainError):
                _state.uncertain += 1
        super().__init__(*args)


class PersistenceUncertainError(PersistenceError):
    """Replacement succeeded but directory durability could not be confirmed."""


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise PersistenceError(f"Cannot load authoritative state {path.name}; restore or migrate state") from exc
    if not isinstance(value, dict):
        raise PersistenceError(f"Invalid authoritative state {path.name}; expected object")
    return value


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """Fsync a private sibling, replace, then fsync the containing directory."""
    temporary = None
    replaced = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".pending", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        replaced = True
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
            with _state.lock:
                _state.writes += 1
        finally:
            os.close(directory_fd)
    except Exception as exc:
        if replaced:
            raise PersistenceUncertainError(f"State replacement for {path.name} requires durability recovery") from exc
        raise PersistenceError(f"State publication for {path.name} failed") from exc
    finally:
        if temporary is not None and not replaced:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass  # Preserve the original publication failure.


class RuntimeDirectoryLease:
    """Exclusive advisory lease shared by service, migration and repair tools."""
    def __init__(self, directory: Path, *, allow_migration_pending: bool = False):
        directory.mkdir(parents=True, exist_ok=True)
        self._handle = (directory / ".runtime.lock").open("a+b")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not allow_migration_pending and (directory / "migration_pending.json").exists():
                raise PersistenceError("Runtime migration is pending; resume or recover migration before startup")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
