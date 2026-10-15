import asyncio
from pathlib import Path

from conftest import write_release_config
from roborock_local_server import server as server_module
from roborock_local_server.config import load_config, resolve_paths
from roborock_local_server.server import ReleaseSupervisor


class _DummyProxy:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _DummyHttpServer:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def test_release_supervisor_start_stop_external_mode(tmp_path: Path, monkeypatch) -> None:
    config_file = write_release_config(tmp_path, broker_mode="external", enable_topic_bridge=False)
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    supervisor = ReleaseSupervisor(config=config, paths=paths)

    monkeypatch.setattr(server_module, "_connectivity_check", lambda host, port: None)

    async def fake_start_http_server(self: ReleaseSupervisor) -> None:
        self._http_server = _DummyHttpServer()  # type: ignore[assignment]
        self.runtime_state.set_service("https_server", running=True, required=True, enabled=True)

    def fake_start_mqtt_proxy(self: ReleaseSupervisor) -> None:
        self._mqtt_proxy = _DummyProxy()  # type: ignore[assignment]
        self.runtime_state.set_service("mqtt_tls_proxy", running=True, required=True, enabled=True)

    monkeypatch.setattr(ReleaseSupervisor, "_start_http_server", fake_start_http_server)
    monkeypatch.setattr(ReleaseSupervisor, "_start_mqtt_proxy", fake_start_mqtt_proxy)

    asyncio.run(supervisor.start())
    health = supervisor.runtime_state.health_snapshot()
    service_map = {service["name"]: service for service in health["services"]}
    assert service_map["https_server"]["running"] is True
    assert service_map["mqtt_tls_proxy"]["running"] is True
    assert service_map["mqtt_backend_broker"]["running"] is True

    asyncio.run(supervisor.stop())
    health = supervisor.runtime_state.health_snapshot()
    service_map = {service["name"]: service for service in health["services"]}
    assert service_map["https_server"]["running"] is False
    assert service_map["mqtt_tls_proxy"]["running"] is False
    assert service_map["mqtt_backend_broker"]["running"] is False


def test_supervisor_refuses_second_owner_pending_and_releases_initialization_failure(tmp_path, monkeypatch):
    import pytest
    from roborock_local_server.bundled_backend.shared.durable_state import PersistenceError
    config_file = write_release_config(tmp_path)
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    first = ReleaseSupervisor(config=config, paths=paths)
    with pytest.raises(BlockingIOError):
        ReleaseSupervisor(config=config, paths=paths)
    asyncio.run(first.stop())
    paths.runtime_dir.joinpath("migration_pending.json").write_text("{}")
    with pytest.raises(PersistenceError, match="pending"):
        ReleaseSupervisor(config=config, paths=paths)
    paths.runtime_dir.joinpath("migration_pending.json").unlink()
    with monkeypatch.context() as patch:
        patch.setattr(ReleaseSupervisor, "_initialize", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic init failure")))
        with pytest.raises(RuntimeError):
            ReleaseSupervisor(config=config, paths=paths)
    last = ReleaseSupervisor(config=config, paths=paths)
    asyncio.run(last.stop())


def test_startup_never_opens_diagnostics_for_recovery(tmp_path, monkeypatch):
    config_file = write_release_config(tmp_path)
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    paths.runtime_dir.joinpath("mqtt_server.log").write_text("historical credential source")
    paths.mqtt_jsonl_path.write_text("invalid history")
    original = Path.open
    def reject_diagnostic_reads(path, mode="r", *args, **kwargs):
        if path.suffix in {".log", ".jsonl"} and "r" in mode:
            raise AssertionError("startup read diagnostic history")
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", reject_diagnostic_reads)
    supervisor = ReleaseSupervisor(config=config, paths=paths)
    assert supervisor.context.zone_ranges_store.get_all() == {}
    asyncio.run(supervisor.stop())


def test_start_failure_releases_process_lease(tmp_path, monkeypatch):
    import pytest
    config_file = write_release_config(tmp_path)
    config = load_config(config_file)
    paths = resolve_paths(config_file, config)
    supervisor = ReleaseSupervisor(config=config, paths=paths)
    async def fail(self):
        raise RuntimeError("synthetic start failure")
    with monkeypatch.context() as patch:
        patch.setattr(ReleaseSupervisor, "_start_services", fail)
        with pytest.raises(RuntimeError):
            asyncio.run(supervisor.start())
    replacement = ReleaseSupervisor(config=config, paths=paths)
    asyncio.run(replacement.stop())
