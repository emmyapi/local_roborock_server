import logging
from pathlib import Path

from roborock_local_server.bundled_backend.shared.io_utils import append_jsonl, setup_file_logger


def test_setup_file_logger_mirrors_messages_to_file_and_stream(
    tmp_path: Path,
    capsys,
) -> None:
    log_path = tmp_path / "api_server.log"
    logger = setup_file_logger("api_test", log_path)

    logger.info("device initialization failed")

    captured = capsys.readouterr()
    assert "device initialization failed" in captured.err
    assert "[real_stack.api_test]" in captured.err
    assert "device initialization failed" in log_path.read_text(encoding="utf-8")


def test_append_jsonl_writes_file_and_hides_structured_entry_by_default(
    tmp_path: Path,
    capsys,
) -> None:
    logger = logging.getLogger("real_stack.jsonl")
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)

    jsonl_path = tmp_path / "decompiled_http.jsonl"
    append_jsonl(jsonl_path, {"route": "get_home_data", "success": True})

    captured = capsys.readouterr()
    written = jsonl_path.read_text(encoding="utf-8")

    assert '"route":"get_home_data"' in written
    assert captured.err == ""


def test_append_jsonl_streams_structured_entry_when_jsonl_logger_is_debug(
    tmp_path: Path,
    capsys,
) -> None:
    logger = logging.getLogger("real_stack.jsonl")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)

    jsonl_path = tmp_path / "decompiled_http.jsonl"
    append_jsonl(jsonl_path, {"route": "get_home_data", "success": True})

    captured = capsys.readouterr()

    assert "[decompiled_http.jsonl]" in captured.err
    assert '"success":true' in captured.err


def test_rotation_concurrent_alias_writers_keep_whole_records(tmp_path, monkeypatch):
    import importlib
    import json
    from concurrent.futures import ThreadPoolExecutor
    from roborock_local_server.bundled_backend.shared import io_utils as io
    monkeypatch.syspath_prepend(str(Path(io.__file__).resolve().parents[1]))
    alias = importlib.import_module("shared.io_utils")
    assert alias._state is io._state
    io.close_log_writers()
    monkeypatch.setattr(io, "LOG_MAX_BYTES", 1024)
    monkeypatch.setattr(alias, "LOG_MAX_BYTES", 1024)
    path = tmp_path / "records.jsonl"
    def write(index):
        (io if index % 2 else alias).append_jsonl(path, {"index": index, "value": "x" * 100})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(100)))
    files = list(tmp_path.iterdir())
    assert len(files) == 4
    for file in files:
        assert file.stat().st_size <= 1024
        for line in file.read_text().splitlines():
            assert isinstance(json.loads(line)["index"], int)
    io.close_log_writers()


def test_record_summary_and_secret_redaction(tmp_path):
    import json
    from roborock_local_server.bundled_backend.shared import io_utils as io
    path = tmp_path / "records.jsonl"
    io.append_jsonl(path, {"password": "hidden", "nested": {"localKey": "hidden"}, "payload_hex": "hidden"})
    io.append_jsonl(path, {"route": "get_status", "value": "a" * (300 * 1024)})
    lines = path.read_bytes().splitlines()
    assert all(len(line) + 1 <= io.LOG_MAX_RECORD_BYTES for line in lines)
    assert b"hidden" not in lines[0]
    assert json.loads(lines[1])["diagnostic_summary"] == "oversized_record"
    logger = io.setup_file_logger("redaction_test", tmp_path / "mqtt.log")
    logger.info('CONNECT len=5 hex=010203 password=hidden')
    assert "010203" not in (tmp_path / "mqtt.log").read_text()
    assert "hidden" not in (tmp_path / "mqtt.log").read_text()


def test_preexisting_oversized_source_is_preserved(tmp_path, monkeypatch):
    import pytest
    from roborock_local_server.bundled_backend.shared import io_utils as io
    io.close_log_writers()
    monkeypatch.setattr(io, "LOG_MAX_BYTES", 64)
    path = tmp_path / "old.log"
    path.write_bytes(b"x" * 65)
    with pytest.raises(ValueError, match="migration required"):
        io.setup_file_logger("oversized_test", path)
    io.append_jsonl(path, {"event": "test"})
    assert path.read_bytes() == b"x" * 65
    assert not path.with_name("old.log.1").exists()


def test_reconfigure_closes_handlers_and_shutdown_registry(tmp_path):
    from roborock_local_server.bundled_backend.shared import io_utils as io
    logger = io.setup_file_logger("lifecycle_test", tmp_path / "first.log")
    old_handlers = logger.handlers[:]
    io.setup_file_logger("lifecycle_test", tmp_path / "second.log")
    assert all(handler._closed for handler in old_handlers)
    logger.info("second only")
    assert not (tmp_path / "first.log").exists()
    io.close_log_writers()
    assert io.diagnostic_log_status()["writers"] == 0


def test_rotation_after_registry_restart_and_utf8_text_bound(tmp_path, monkeypatch):
    from roborock_local_server.bundled_backend.shared import io_utils as io
    io.close_log_writers()
    monkeypatch.setattr(io, "LOG_MAX_BYTES", 1024)
    monkeypatch.setattr(io, "LOG_MAX_RECORD_BYTES", 256)
    path = tmp_path / "restart.jsonl"
    for _ in range(20):
        io.append_jsonl(path, {"text": "é" * 30})
        io.close_log_writers()
    for file in tmp_path.iterdir():
        assert file.stat().st_size <= 1024
    logger = io.setup_file_logger("utf8_bound", tmp_path / "text.log")
    logger.info("é" * 1000)
    assert len((tmp_path / "text.log").read_bytes()) <= 256
    io.close_log_writers()


def test_header_redaction_consumes_multitoken_values(tmp_path):
    from roborock_local_server.bundled_backend.shared.io_utils import payload_preview
    for text in ('Authorization: Bearer PRIVATE_TOKEN', 'Cookie: first=PRIVATE_A; second=PRIVATE_B', 'authorization="Bearer PRIVATE_TOKEN"'):
        value = payload_preview(text.encode())
        assert 'PRIVATE' not in value
