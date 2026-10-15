"""Bounded diagnostic writers; runtime state must never depend on these files."""
from __future__ import annotations

import atexit
import json
import logging
import re
import sys
import threading
import types
from pathlib import Path
from typing import Any

LOG_MAX_BYTES = 4 * 1024 * 1024
LOG_BACKUP_COUNT = 3
LOG_MAX_RECORD_BYTES = 256 * 1024
_FILE_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_STREAM_LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
_JSONL_STREAM_LOGGER_NAME = "real_stack.jsonl"
# The bundled backend is imported both as shared.* and by its package name.
_state = sys.modules.setdefault("_roborock_diagnostic_writers", types.ModuleType("_roborock_diagnostic_writers"))
if not hasattr(_state, "lock"):
    _state.lock = threading.RLock()
    _state.writers = {}
    _state.errors = 0

_SECRET = re.compile(r"password|passwd|token|secret|authorization|cookie|local_?key|signature|pin|credential", re.I)
_TEXT_SECRET = re.compile(r"(?i)(\b(?:password|passwd|token|secret|authorization|cookie|local_?key|signature|pin)\b[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)")


def _redact_text(text: str) -> str:
    # Header values may contain spaces (Bearer auth, cookie pairs). Consume the
    # complete field through a newline, or the closing quote for quoted values.
    text = re.sub(r'(?i)(\b(?:authorization|cookie)\b["\']?\s*[:=]\s*)(?:"[^"\r\n]*"|\'[^\'\r\n]*\'|[^\r\n]*)', r'\1[REDACTED]', text)
    text = _TEXT_SECRET.sub(r"\1[REDACTED]", text)
    if "CONNECT" in text:
        text = re.sub(r"\bhex=\S+", "hex=[REDACTED]", text)
    return text


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 20:
        return "[depth limited]"
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if _SECRET.search(str(k)) or str(k) in {"payload_hex", "payload_preview", "payload_utf8"} else _redact(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, depth + 1) for v in value]
    return _redact_text(value) if isinstance(value, str) else value


def _failure() -> None:
    with _state.lock:
        _state.errors += 1
    # Never include paths, exception details or payloads in error reporting.
    try:
        sys.stderr.write("Roborock diagnostic write failed; inspect diagnostic_log_status().\n")
    except Exception:
        pass


class _Writer:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Cutover must preserve historical recovery sources explicitly.
        for candidate in [path, *(Path(f"{path}.{i}") for i in range(1, LOG_BACKUP_COUNT + 1))]:
            if candidate.exists() and candidate.stat().st_size > LOG_MAX_BYTES:
                raise ValueError("preexisting diagnostic exceeds limit; explicit migration required")

    def write(self, data: bytes) -> None:
        with self.lock:
            if self.closed:
                raise ValueError("diagnostic writer closed")
            if len(data) > LOG_MAX_BYTES:
                raise ValueError("diagnostic record exceeds file limit")
            size = self.path.stat().st_size if self.path.exists() else 0
            if size > LOG_MAX_BYTES:
                raise ValueError("diagnostic grew outside managed writer; explicit migration required")
            if size + len(data) > LOG_MAX_BYTES:
                oldest = Path(f"{self.path}.{LOG_BACKUP_COUNT}")
                oldest.unlink(missing_ok=True)
                for index in range(LOG_BACKUP_COUNT - 1, 0, -1):
                    source = Path(f"{self.path}.{index}")
                    if source.exists():
                        source.replace(Path(f"{self.path}.{index + 1}"))
                if self.path.exists():
                    self.path.replace(Path(f"{self.path}.1"))
            # No descriptor survives a record or a reconfiguration.
            with self.path.open("ab") as handle:
                handle.write(data)


def _writer(path: Path) -> _Writer:
    resolved = Path(path).resolve()
    with _state.lock:
        writer = _state.writers.get(resolved)
        if writer is None or writer.closed:
            writer = _Writer(resolved)
            _state.writers[resolved] = writer
        return writer


def close_log_writers() -> None:
    """Close all writers after producers stop; safe to call repeatedly."""
    with _state.lock:
        for writer in _state.writers.values():
            with writer.lock:
                writer.closed = True
        _state.writers.clear()


def diagnostic_log_status() -> dict[str, int]:
    with _state.lock:
        return {"max_file_bytes": LOG_MAX_BYTES, "archives": LOG_BACKUP_COUNT,
                "max_record_bytes": LOG_MAX_RECORD_BYTES, "write_errors": _state.errors,
                "writers": len(_state.writers)}


atexit.register(close_log_writers)


def _make_stream_handler() -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setFormatter(_SafeFormatter(_STREAM_LOG_FORMAT))
    return handler


class _SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = _redact_text(super().format(record))
        data = text.encode("utf-8", errors="replace")
        if len(data) + 1 > LOG_MAX_RECORD_BYTES:
            return data[:max(0, LOG_MAX_RECORD_BYTES - 32)].decode("utf-8", errors="ignore") + "...[record truncated]"
        return text


class _BoundedHandler(logging.Handler):
    def __init__(self, path: Path):
        super().__init__()
        self.writer = _writer(path)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.writer.write((self.format(record) + "\n").encode("utf-8"))
        except Exception:
            _failure()


def _jsonl_stream_logger() -> logging.Logger:
    logger = logging.getLogger(_JSONL_STREAM_LOGGER_NAME)
    logger.propagate = False
    if not logger.handlers:
        if logger.level == logging.NOTSET:
            logger.setLevel(logging.INFO)
        logger.addHandler(_make_stream_handler())
    return logger


def append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    try:
        encoded = json.dumps(_redact(entry), ensure_ascii=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) + 1 > LOG_MAX_RECORD_BYTES:
            summary = {"diagnostic_summary": "oversized_record", "encoded_bytes": len(encoded.encode("utf-8"))}
            for key in ("time", "conn", "direction", "topic", "route", "decoder_variant"):
                if key in entry:
                    summary[key] = str(_redact(entry[key]))[:256]
            encoded = json.dumps(summary, ensure_ascii=True, separators=(",", ":"))
            if len(encoded) + 1 > LOG_MAX_RECORD_BYTES:
                encoded = '{"diagnostic_summary":"oversized_record"}'
        _writer(path).write((encoded + "\n").encode("utf-8"))
        _jsonl_stream_logger().debug("[%s] %s", path.name, encoded)
    except Exception:
        _failure()


def setup_file_logger(name: str, path: Path) -> logging.Logger:
    logger = logging.getLogger(f"real_stack.{name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Validate before replacing the previous usable configuration.
    handler = _BoundedHandler(path)
    handler.setFormatter(_SafeFormatter(_FILE_LOG_FORMAT))
    with _state.lock:
        for previous in logger.handlers[:]:
            logger.removeHandler(previous)
            previous.close()
        logger.addHandler(handler)
        logger.addHandler(_make_stream_handler())
    return logger


def payload_preview(payload: bytes, max_chars: int = 280) -> str:
    if not payload:
        return ""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        # Encrypted/binary payload previews can contain authentication material.
        return "[binary payload omitted]"
    text = _redact_text(text)
    return text[:max_chars] + ("...[truncated]" if len(text) > max_chars else "")
