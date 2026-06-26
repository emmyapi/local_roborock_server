import asyncio
import json
import logging
from pathlib import Path

import pytest

from roborock.data import StatusV2
from roborock_local_server.bundled_backend.shared.context import ServerContext
import roborock_local_server.bundled_backend.shared.routine_runner as routine_runner_module
from roborock_local_server.bundled_backend.shared.routine_runner import RoutineRunner, parse_scene_steps
from roborock.roborock_typing import RoborockCommand


def _test_context(tmp_path: Path, *, scene_completion_webhook_url: str = "") -> ServerContext:
    return ServerContext(
        api_host="api.example.com",
        mqtt_host="mqtt.example.com",
        wood_host="wood.example.com",
        region="us",
        localkey="local-key",
        duid="default-duid",
        mqtt_usr="mqtt-user",
        mqtt_passwd="mqtt-pass",
        mqtt_clientid="mqtt-client",
        mqtt_tls_port=8883,
        http_jsonl=tmp_path / "http.jsonl",
        mqtt_jsonl=tmp_path / "mqtt.jsonl",
        loggers={"api": logging.getLogger("test-routine-runner")},
        scene_completion_webhook_url=scene_completion_webhook_url,
    )


def _scene(*, scene_id: int, device_id: str, name: str) -> dict[str, object]:
    return {
        "id": scene_id,
        "name": name,
        "device_id": device_id,
        "param": (
            '{"action":{"items":[{"id":1,"type":"CMD","name":"Start",'
            '"finishDpIds":[130],"param":{"method":"do_scenes_app_start","params":[{"repeat":1}]}}]}}'
        ),
    }


def _scene_with_zone_tid(
    *,
    scene_id: int,
    device_id: str,
    name: str,
    tid: str,
    zid: int,
    range_coords: list[int] | None = None,
) -> dict[str, object]:
    zone_payload: dict[str, object] = {"zid": zid, "repeat": 1}
    if range_coords is not None:
        zone_payload["range"] = list(range_coords)
    return {
        "id": scene_id,
        "name": name,
        "device_id": device_id,
        "param": json.dumps(
            {
                "action": {
                    "items": [
                        {
                            "id": 1,
                            "type": "CMD",
                            "name": name,
                            "finishDpIds": [130],
                            "param": json.dumps(
                                {
                                    "method": "do_scenes_zones",
                                    "params": {
                                        "data": [
                                            {
                                                "tid": tid,
                                                "zones": [zone_payload],
                                                "fan_power": 108,
                                                "repeat": 1,
                                            }
                                        ]
                                    },
                                },
                                separators=(",", ":"),
                            ),
                        }
                    ]
                }
            },
            separators=(",", ":"),
        ),
    }


def test_repeating_scene_execute_requests_cancel(tmp_path: Path, monkeypatch) -> None:
    async def exercise() -> None:
        runner = RoutineRunner(_test_context(tmp_path))
        started = asyncio.Event()
        hold = asyncio.Event()
        stop_calls: list[tuple[str, int, str]] = []

        async def fake_run_scene(self: RoutineRunner, *, scene: dict[str, object], steps: list[object]) -> None:
            _ = self, scene, steps
            started.set()
            await hold.wait()

        async def fake_stop_scene(
            self: RoutineRunner,
            *,
            device_id: str,
            scene_id: int,
            scene_name: str,
        ) -> None:
            _ = self
            stop_calls.append((device_id, scene_id, scene_name))

        monkeypatch.setattr(RoutineRunner, "_run_scene", fake_run_scene)
        monkeypatch.setattr(RoutineRunner, "_stop_scene", fake_stop_scene)

        scene = _scene(scene_id=4491073, device_id="6HL2zfniaoYYV01CkVuhkO", name="After dinner")

        first = runner.start_scene(scene)
        assert first["accepted"] is True
        assert first["status"] == "started"

        await started.wait()

        second = runner.start_scene(scene)
        assert second["accepted"] is True
        assert second["status"] == "cancel_requested"

        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert stop_calls == [("6HL2zfniaoYYV01CkVuhkO", 4491073, "After dinner")]

    asyncio.run(exercise())


def test_different_scene_on_busy_device_stays_in_progress(tmp_path: Path, monkeypatch) -> None:
    async def exercise() -> None:
        runner = RoutineRunner(_test_context(tmp_path))
        started = asyncio.Event()
        hold = asyncio.Event()
        stop_calls: list[tuple[str, int, str]] = []

        async def fake_run_scene(self: RoutineRunner, *, scene: dict[str, object], steps: list[object]) -> None:
            _ = self, scene, steps
            started.set()
            await hold.wait()

        async def fake_stop_scene(
            self: RoutineRunner,
            *,
            device_id: str,
            scene_id: int,
            scene_name: str,
        ) -> None:
            _ = self
            stop_calls.append((device_id, scene_id, scene_name))

        monkeypatch.setattr(RoutineRunner, "_run_scene", fake_run_scene)
        monkeypatch.setattr(RoutineRunner, "_stop_scene", fake_stop_scene)

        first_scene = _scene(scene_id=4491073, device_id="6HL2zfniaoYYV01CkVuhkO", name="After dinner")
        second_scene = _scene(scene_id=4491074, device_id="6HL2zfniaoYYV01CkVuhkO", name="Kitchen")

        first = runner.start_scene(first_scene)
        assert first["accepted"] is True
        assert first["status"] == "started"

        await started.wait()

        second = runner.start_scene(second_scene)
        assert second["accepted"] is False
        assert second["status"] == "routine_in_progress"
        assert second["activeSceneId"] == 4491073
        assert second["activeSceneName"] == "After dinner"

        hold.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert stop_calls == []

    asyncio.run(exercise())


def test_run_scene_syncs_scene_tids_before_step_commands(tmp_path: Path, monkeypatch) -> None:
    async def exercise() -> None:
        inventory_path = tmp_path / "web_api_inventory.json"
        device_id = "6HL2zfniaoYYV01CkVuhkO"
        current_scene = _scene_with_zone_tid(
            scene_id=4491073,
            device_id=device_id,
            name="After dinner",
            tid="1773791700088",
            zid=8,
            range_coords=[32800, 22750, 34550, 25350],
        )
        inventory_path.write_text(
            json.dumps(
                {
                    "scenes": [
                        _scene_with_zone_tid(
                            scene_id=4491072,
                            device_id=device_id,
                            name="Night living room",
                            tid="1756774254605",
                            zid=5,
                        ),
                        current_scene,
                        _scene_with_zone_tid(
                            scene_id=4499999,
                            device_id="other-device",
                            name="Other device scene",
                            tid="999",
                            zid=1,
                        ),
                    ]
                }
            ),
            encoding="utf-8",
        )

        sent_commands: list[tuple[RoborockCommand, object]] = []

        class FakeRoutineClient:
            def __init__(self, context, device, logger) -> None:
                _ = context, device, logger

            async def connect(self) -> None:
                return None

            async def close(self) -> None:
                return None

            async def send_command(self, command, params=None):
                sent_commands.append((command, params))
                return ["ok"]

            async def send_command_await_unlock(self, command, params=None):
                return await self.send_command(command, params)

            async def wait_for_step_complete(self) -> None:
                return None

        monkeypatch.setattr(routine_runner_module, "_RoutineMqttClient", FakeRoutineClient)

        runner = RoutineRunner(_test_context(tmp_path))
        await runner._run_scene(scene=current_scene, steps=parse_scene_steps(current_scene))

        assert sent_commands[0] == (
            RoborockCommand.REUNION_SCENES,
            {"data": [{"tid": "1756774254605"}, {"tid": "1773791700088"}]},
        )
        assert sent_commands[1] == (
            "set_scenes_zones",
            {
                "data": [
                    {
                        "tid": "1773791700088",
                        "zones": [{"zid": 8, "repeat": 1, "range": [32800, 22750, 34550, 25350]}],
                    }
                ]
            },
        )
        assert sent_commands[2] == (
            "do_scenes_zones",
            {
                "data": [
                    {
                        "tid": "1773791700088",
                        "zones": [{"zid": 8, "repeat": 1, "range": [32800, 22750, 34550, 25350]}],
                        "fan_power": 108,
                        "repeat": 1,
                    }
                ]
            },
        )
        assert len(sent_commands) == 3

    asyncio.run(exercise())


# ---------------------------------------------------------------------------
# wait_for_step_complete tests
# ---------------------------------------------------------------------------


class _ScriptedStatusClient:
    """Minimal stand-in for _RoutineMqttClient that replays a status sequence.

    Sequence entries that equal the string ``"timeout"`` raise the same
    RoutineExecutionError that ``send_command`` raises on a real 15s MQTT
    poll timeout, so tests can simulate transient stalls.
    """

    def __init__(self, status_sequence: list) -> None:
        self._sequence = [
            entry if entry == "timeout" else StatusV2.from_dict(entry)
            for entry in status_sequence
        ]
        self._index = 0
        self._logger = logging.getLogger("test-wait")
        self.sent_commands: list[tuple[RoborockCommand, list | dict | None]] = []

    async def get_status(self) -> StatusV2:
        if self._index < len(self._sequence):
            entry = self._sequence[self._index]
            self._index += 1
        else:
            entry = self._sequence[-1]
        if entry == "timeout":
            raise routine_runner_module.RoutineExecutionError(
                "Command get_status timed out after 15.0s"
            )
        return entry

    async def send_command(self, command: RoborockCommand, params=None) -> None:
        self.sent_commands.append((command, params))


_ScriptedStatusClient.wait_for_step_complete = (
    routine_runner_module._RoutineMqttClient.wait_for_step_complete
)
_ScriptedStatusClient._poll_status_resilient = (
    routine_runner_module._RoutineMqttClient._poll_status_resilient
)


class _ActionLockedClient:
    """Stand-in that rejects the first N commands with -10003 'action locked'
    (device busy with dock mop wash/refill) then accepts."""

    def __init__(self, reject_count: int) -> None:
        self._logger = logging.getLogger("test-lock")
        self._reject_count = reject_count
        self.calls = 0

    async def send_command(self, command, params=None):
        from roborock.exceptions import RoborockException

        self.calls += 1
        if self.calls <= self._reject_count:
            raise RoborockException({"code": -10003, "message": "action locked"})
        return ["ok"]


_ActionLockedClient._is_action_locked = staticmethod(
    routine_runner_module._RoutineMqttClient._is_action_locked
)
_ActionLockedClient.send_command_await_unlock = (
    routine_runner_module._RoutineMqttClient.send_command_await_unlock
)


def test_send_command_await_unlock_waits_out_action_locked(monkeypatch) -> None:
    """A -10003 'action locked' rejection is retried (not fatal) until it clears."""
    monkeypatch.setattr(routine_runner_module, "_ACTION_LOCKED_RETRY_BACKOFF", 0.0)

    async def exercise() -> None:
        client = _ActionLockedClient(reject_count=2)
        result = await client.send_command_await_unlock("do_scenes_segments", {"data": []})
        assert result == ["ok"]
        assert client.calls == 3  # 2 rejections + 1 success

    asyncio.run(exercise())


def test_send_command_await_unlock_propagates_other_errors(monkeypatch) -> None:
    """Non-action-locked RoborockExceptions are not swallowed by the retry."""
    from roborock.exceptions import RoborockInvalidStatus

    monkeypatch.setattr(routine_runner_module, "_ACTION_LOCKED_RETRY_BACKOFF", 0.0)

    class _C(_ActionLockedClient):
        async def send_command(self, command, params=None):
            self.calls += 1
            raise RoborockInvalidStatus({"code": -10007, "message": "no such tid"})

    async def exercise() -> None:
        client = _C(reject_count=99)
        try:
            await client.send_command_await_unlock("do_scenes_segments", {"data": []})
        except RoborockInvalidStatus:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected RoborockInvalidStatus to propagate")
        assert client.calls == 1  # raised immediately, no retry

    asyncio.run(exercise())


def test_wait_for_step_complete_dock_activity_does_not_end_step(monkeypatch) -> None:
    """Dock activity (emptying bin) followed by ready must not declare step complete."""
    monkeypatch.setattr(routine_runner_module, "_STEP_START_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(routine_runner_module, "_STEP_START_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(routine_runner_module, "_STATUS_POLL_INTERVAL_SECONDS", 0.0)

    async def exercise() -> None:
        client = _ScriptedStatusClient([
            {"state": 22, "in_cleaning": 0},  # emptying bin
            {"state": 15, "in_cleaning": 0},  # docking
            {"state": 8, "in_cleaning": 0},   # charging — should NOT end step
            {"state": 8, "in_cleaning": 0},
            {"state": 8, "in_cleaning": 0},
            {"state": 8, "in_cleaning": 0},
            {"state": 8, "in_cleaning": 0},
            {"state": 8, "in_cleaning": 0},
        ])
        with pytest.raises(routine_runner_module.RoutineExecutionError, match="did not leave ready state"):
            await client.wait_for_step_complete()

    asyncio.run(exercise())


def test_wait_for_step_complete_actual_cleaning_completes(monkeypatch) -> None:
    """Step completes when in_cleaning becomes non-zero then robot returns to ready."""
    monkeypatch.setattr(routine_runner_module, "_STEP_START_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(routine_runner_module, "_STATUS_POLL_INTERVAL_SECONDS", 0.0)

    async def exercise() -> None:
        client = _ScriptedStatusClient([
            {"state": 18, "in_cleaning": 3},  # segment cleaning
            {"state": 18, "in_cleaning": 3},
            {"state": 6, "in_cleaning": 3},   # returning home
            {"state": 8, "in_cleaning": 0},   # charging — step complete
        ])
        await client.wait_for_step_complete()

    asyncio.run(exercise())


def test_wait_for_step_complete_dock_then_cleaning_completes(monkeypatch) -> None:
    """Dock activity followed by actual cleaning should complete after cleaning finishes."""
    monkeypatch.setattr(routine_runner_module, "_STEP_START_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(routine_runner_module, "_STATUS_POLL_INTERVAL_SECONDS", 0.0)

    async def exercise() -> None:
        client = _ScriptedStatusClient([
            {"state": 22, "in_cleaning": 0},  # emptying bin
            {"state": 15, "in_cleaning": 0},  # docking
            {"state": 8, "in_cleaning": 0},   # charging — dock cycle ends, reset
            {"state": 18, "in_cleaning": 3},  # actual cleaning starts
            {"state": 18, "in_cleaning": 3},
            {"state": 6, "in_cleaning": 3},   # returning home
            {"state": 8, "in_cleaning": 0},   # step complete
        ])
        await client.wait_for_step_complete()

    asyncio.run(exercise())


def test_wait_for_step_complete_start_timeout(monkeypatch) -> None:
    """Raises RoutineExecutionError when robot stays in ready state past start deadline."""
    monkeypatch.setattr(routine_runner_module, "_STEP_START_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(routine_runner_module, "_STEP_START_POLL_INTERVAL_SECONDS", 0.0)

    async def exercise() -> None:
        client = _ScriptedStatusClient([
            {"state": 8, "in_cleaning": 0},
            {"state": 8, "in_cleaning": 0},
            {"state": 8, "in_cleaning": 0},
            {"state": 8, "in_cleaning": 0},
            {"state": 8, "in_cleaning": 0},
            {"state": 8, "in_cleaning": 0},
        ])
        with pytest.raises(routine_runner_module.RoutineExecutionError, match="did not leave ready state"):
            await client.wait_for_step_complete()

    asyncio.run(exercise())


def test_wait_for_step_complete_resume_after_mid_clean_charge(monkeypatch) -> None:
    """Robot returns to dock mid-clean with low battery, charges, gets resumed, completes."""
    monkeypatch.setattr(routine_runner_module, "_STEP_START_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(routine_runner_module, "_STATUS_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(routine_runner_module, "_RESUME_BATTERY_THRESHOLD", 80)

    async def exercise() -> None:
        client = _ScriptedStatusClient([
            {"state": 18, "in_cleaning": 3, "battery": 55},  # segment cleaning
            {"state": 26, "in_cleaning": 3, "battery": 30},  # going to wash mop
            {"state": 6, "in_cleaning": 3, "battery": 14},   # returning home (low battery)
            {"state": 8, "in_cleaning": 3, "battery": 14},   # charging mid-clean, too low
            {"state": 8, "in_cleaning": 3, "battery": 50},   # still too low
            {"state": 8, "in_cleaning": 3, "battery": 80},   # threshold reached → resume sent
            {"state": 18, "in_cleaning": 3, "battery": 80},  # cleaning resumes
            {"state": 18, "in_cleaning": 3, "battery": 60},
            {"state": 6, "in_cleaning": 3, "battery": 40},   # returning home
            {"state": 8, "in_cleaning": 0, "battery": 40},   # step complete
        ])
        await client.wait_for_step_complete()
        assert len(client.sent_commands) == 1
        assert client.sent_commands[0] == (RoborockCommand.RESUME_SEGMENT_CLEAN, [])

    asyncio.run(exercise())


def test_wait_for_step_complete_resume_zoned_clean(monkeypatch) -> None:
    """Resume uses correct command for zone cleaning."""
    monkeypatch.setattr(routine_runner_module, "_STEP_START_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(routine_runner_module, "_STATUS_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(routine_runner_module, "_RESUME_BATTERY_THRESHOLD", 80)

    async def exercise() -> None:
        client = _ScriptedStatusClient([
            {"state": 18, "in_cleaning": 2, "battery": 50},  # zone cleaning
            {"state": 8, "in_cleaning": 2, "battery": 80},   # charging mid-clean → resume
            {"state": 18, "in_cleaning": 2, "battery": 80},  # resumes
            {"state": 8, "in_cleaning": 0, "battery": 60},   # step complete
        ])
        await client.wait_for_step_complete()
        assert len(client.sent_commands) == 1
        assert client.sent_commands[0] == (RoborockCommand.RESUME_ZONED_CLEAN, [])

    asyncio.run(exercise())


def test_wait_for_step_complete_no_resume_when_battery_low(monkeypatch) -> None:
    """No resume sent while battery is below threshold."""
    monkeypatch.setattr(routine_runner_module, "_STEP_START_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(routine_runner_module, "_STATUS_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(routine_runner_module, "_STEP_COMPLETE_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(routine_runner_module, "_RESUME_BATTERY_THRESHOLD", 80)

    async def exercise() -> None:
        client = _ScriptedStatusClient([
            {"state": 18, "in_cleaning": 3, "battery": 50},  # cleaning
            {"state": 8, "in_cleaning": 3, "battery": 14},   # charging, below threshold
            {"state": 8, "in_cleaning": 3, "battery": 50},   # still below
            {"state": 8, "in_cleaning": 3, "battery": 79},   # still below
        ])
        with pytest.raises(routine_runner_module.RoutineExecutionError, match="Timed out"):
            await client.wait_for_step_complete()
        assert len(client.sent_commands) == 0

    asyncio.run(exercise())


def test_wait_for_step_complete_recovers_from_transient_timeout(monkeypatch) -> None:
    """Transient get_status timeouts mid-cleaning must not abort the step."""
    monkeypatch.setattr(routine_runner_module, "_STEP_START_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(routine_runner_module, "_STATUS_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(routine_runner_module, "_GET_STATUS_RETRY_BACKOFF", 0.0)

    async def exercise() -> None:
        client = _ScriptedStatusClient([
            {"state": 18, "in_cleaning": 3},  # segment cleaning
            "timeout",                         # transient blip (count=1)
            "timeout",                         # another blip   (count=2)
            {"state": 18, "in_cleaning": 3},  # recovered → counter resets
            "timeout",                         # later blip (count=1)
            "timeout",                         #            (count=2)
            "timeout",                         #            (count=3)
            "timeout",                         #            (count=4, still under default 5)
            {"state": 6, "in_cleaning": 3},   # returning home → counter resets
            {"state": 8, "in_cleaning": 0},   # step complete
        ])
        await client.wait_for_step_complete()

    asyncio.run(exercise())


def test_wait_for_step_complete_gives_up_after_too_many_timeouts(monkeypatch) -> None:
    """When _GET_STATUS_RETRY_LIMIT consecutive timeouts occur, error bubbles up."""
    monkeypatch.setattr(routine_runner_module, "_STEP_START_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(routine_runner_module, "_STATUS_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(routine_runner_module, "_GET_STATUS_RETRY_BACKOFF", 0.0)
    monkeypatch.setattr(routine_runner_module, "_GET_STATUS_RETRY_LIMIT", 3)

    async def exercise() -> None:
        client = _ScriptedStatusClient([
            {"state": 18, "in_cleaning": 3},  # cleaning starts
            "timeout",
            "timeout",
            "timeout",                         # 3rd consecutive — must bubble up
        ])
        with pytest.raises(routine_runner_module.RoutineExecutionError, match="timed out after"):
            await client.wait_for_step_complete()

    asyncio.run(exercise())


# ---------------------------------------------------------------------------
# scene-completion webhook tests
# ---------------------------------------------------------------------------


def _drain_webhook_tasks() -> None:
    """Yield control a few times so fire-and-forget webhook tasks can run to completion."""
    # three turns is more than enough — the test POST helper doesn't await I/O.


async def _run_on_scene_done(runner: RoutineRunner, *, device_id: str, scene_id: int, scene_name: str, outcome: str) -> None:
    """Seed an active routine, then drive _on_scene_done with a task in the requested outcome."""
    from roborock_local_server.bundled_backend.shared.routine_runner import _ActiveRoutine

    loop = asyncio.get_running_loop()

    if outcome == "success":
        async def _body() -> None:
            return None
    elif outcome == "cancelled":
        async def _body() -> None:
            await asyncio.sleep(3600)
    elif outcome == "exception":
        async def _body() -> None:
            raise RuntimeError("boom")
    else:
        raise ValueError(outcome)

    task = loop.create_task(_body())
    runner._tasks_by_device[device_id] = _ActiveRoutine(
        task=task,
        scene_id=scene_id,
        scene_name=scene_name,
    )
    if outcome == "cancelled":
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, RuntimeError):
        pass
    runner._on_scene_done(device_id, task)
    for _ in range(5):
        await asyncio.sleep(0)


def test_scene_completion_webhook_posts_on_success(tmp_path: Path, monkeypatch) -> None:
    async def exercise() -> None:
        runner = RoutineRunner(
            _test_context(tmp_path, scene_completion_webhook_url="http://ha.example/api/webhook/x")
        )
        captured: list[dict] = []

        class _FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class _FakeSession:
            def __init__(self, *args, **kwargs) -> None:
                _ = args, kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def post(self, url, *, json):
                captured.append({"url": url, "json": json})
                return _FakeResponse()

        monkeypatch.setattr(routine_runner_module.aiohttp, "ClientSession", _FakeSession)

        await _run_on_scene_done(
            runner,
            device_id="device-1",
            scene_id=4491073,
            scene_name="Downstairs Vac+Mop",
            outcome="success",
        )

        assert len(captured) == 1
        assert captured[0]["url"] == "http://ha.example/api/webhook/x"
        payload = captured[0]["json"]
        assert payload["event"] == "scene_completed"
        assert payload["sceneId"] == 4491073
        assert payload["sceneName"] == "Downstairs Vac+Mop"
        assert payload["deviceId"] == "device-1"
        assert isinstance(payload["completedAt"], str)
        assert payload["completedAt"].endswith("+00:00")

    asyncio.run(exercise())


def test_scene_completion_webhook_skips_on_cancellation(tmp_path: Path, monkeypatch) -> None:
    async def exercise() -> None:
        runner = RoutineRunner(
            _test_context(tmp_path, scene_completion_webhook_url="http://ha.example/api/webhook/x")
        )
        captured: list[dict] = []

        class _ShouldNotCall:
            def __init__(self, *args, **kwargs) -> None:
                captured.append({"called": True})

        monkeypatch.setattr(routine_runner_module.aiohttp, "ClientSession", _ShouldNotCall)

        await _run_on_scene_done(
            runner,
            device_id="device-1",
            scene_id=4491073,
            scene_name="Upstairs Vac+Mop",
            outcome="cancelled",
        )

        assert captured == []

    asyncio.run(exercise())


def test_scene_completion_webhook_skips_on_exception(tmp_path: Path, monkeypatch) -> None:
    async def exercise() -> None:
        runner = RoutineRunner(
            _test_context(tmp_path, scene_completion_webhook_url="http://ha.example/api/webhook/x")
        )
        captured: list[dict] = []

        class _ShouldNotCall:
            def __init__(self, *args, **kwargs) -> None:
                captured.append({"called": True})

        monkeypatch.setattr(routine_runner_module.aiohttp, "ClientSession", _ShouldNotCall)

        await _run_on_scene_done(
            runner,
            device_id="device-1",
            scene_id=4491073,
            scene_name="Living Room Vac+Mop",
            outcome="exception",
        )

        assert captured == []

    asyncio.run(exercise())


def test_scene_completion_webhook_skips_when_url_empty(tmp_path: Path, monkeypatch) -> None:
    async def exercise() -> None:
        runner = RoutineRunner(_test_context(tmp_path, scene_completion_webhook_url=""))
        captured: list[dict] = []

        class _ShouldNotCall:
            def __init__(self, *args, **kwargs) -> None:
                captured.append({"called": True})

        monkeypatch.setattr(routine_runner_module.aiohttp, "ClientSession", _ShouldNotCall)

        await _run_on_scene_done(
            runner,
            device_id="device-1",
            scene_id=4491073,
            scene_name="Bathroom Up Vac+Mop",
            outcome="success",
        )

        assert captured == []

    asyncio.run(exercise())


def test_wait_for_step_complete_resume_only_sent_once(monkeypatch) -> None:
    """Resume command is only sent once even if robot returns to dock again."""
    monkeypatch.setattr(routine_runner_module, "_STEP_START_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(routine_runner_module, "_STATUS_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(routine_runner_module, "_RESUME_BATTERY_THRESHOLD", 80)

    async def exercise() -> None:
        client = _ScriptedStatusClient([
            {"state": 18, "in_cleaning": 3, "battery": 95},  # cleaning
            {"state": 8, "in_cleaning": 3, "battery": 80},   # charging → resume sent
            {"state": 18, "in_cleaning": 3, "battery": 80},  # cleaning resumes
            {"state": 8, "in_cleaning": 3, "battery": 80},   # back to charging again → second resume sent
            {"state": 18, "in_cleaning": 3, "battery": 80},  # cleaning resumes again
            {"state": 8, "in_cleaning": 0, "battery": 60},   # step complete
        ])
        await client.wait_for_step_complete()
        assert len(client.sent_commands) == 2

    asyncio.run(exercise())


