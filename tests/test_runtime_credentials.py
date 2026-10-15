import json
from pathlib import Path

from roborock_local_server.bundled_backend.shared.runtime_credentials import RuntimeCredentialsStore


def test_ensure_device_merges_split_did_and_duid_records(tmp_path: Path) -> None:
    credentials_path = tmp_path / "runtime_credentials.json"
    credentials_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "devices": [
                    {
                        "did": "1103821560705",
                        "duid": "",
                        "name": "",
                        "model": "",
                        "product_id": "",
                        "localkey": "",
                        "local_key_source": "",
                        "device_mqtt_usr": "mqtt-user-a",
                        "device_mqtt_pass": "",
                        "updated_at": "2026-03-16T00:22:31.225097+00:00",
                        "last_nc_at": "",
                        "last_mqtt_seen_at": "2026-03-16T00:22:31.225063+00:00",
                    },
                    {
                        "did": "",
                        "duid": "cloud-q7-a",
                        "name": "Q7 Upstairs",
                        "model": "roborock.vacuum.sc05",
                        "product_id": "product-q7-a",
                        "localkey": "local-key-a",
                        "local_key_source": "inventory",
                        "device_mqtt_usr": "",
                        "device_mqtt_pass": "",
                        "updated_at": "2026-03-16T00:22:20.199941+00:00",
                        "last_nc_at": "",
                        "last_mqtt_seen_at": "",
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    store = RuntimeCredentialsStore(credentials_path)
    merged = store.ensure_device(
        did="1103821560705",
        duid="cloud-q7-a",
        device_mqtt_usr="mqtt-user-a",
        device_mqtt_pass="mqtt-pass-a",
        assign_localkey=False,
    )

    devices = store.devices()
    assert len(devices) == 1
    assert devices[0]["did"] == "1103821560705"
    assert devices[0]["duid"] == "cloud-q7-a"
    assert devices[0]["name"] == "Q7 Upstairs"
    assert devices[0]["model"] == "roborock.vacuum.sc05"
    assert devices[0]["product_id"] == "product-q7-a"
    assert devices[0]["localkey"] == "local-key-a"
    assert devices[0]["device_mqtt_usr"] == "mqtt-user-a"
    assert devices[0]["device_mqtt_pass"] == "mqtt-pass-a"
    assert merged["duid"] == "cloud-q7-a"


def test_backfill_device_mqtt_passwords_updates_only_known_usernames(tmp_path: Path) -> None:
    credentials_path = tmp_path / "runtime_credentials.json"
    credentials_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "devices": [
                    {
                        "did": "1103821560705",
                        "duid": "cloud-q7-a",
                        "name": "Q7 Upstairs",
                        "model": "roborock.vacuum.sc05",
                        "product_id": "product-q7-a",
                        "localkey": "local-key-a",
                        "local_key_source": "inventory",
                        "device_mqtt_usr": "c25b14ceac358d2a",
                        "device_mqtt_pass": "",
                        "updated_at": "",
                        "last_nc_at": "",
                        "last_mqtt_seen_at": "",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    log_path = tmp_path / "mqtt_server.log"
    log_path.write_text(
        "\n".join(
            [
                "2026-04-17 16:20:32,393 [INFO] [conn 1670 c2b] CONNECT len=82 hex=105000044d51545404c2001e00106130313233393163623566386263393700106332356231346365616333353864326100206666383932326432346139613961663831663138663335646365653961356135",
                "2026-04-17 16:20:33,603 [INFO] [conn 1671 c2b] CONNECT len=82 hex=105000044d51545404c2001e00103664343439636537383337643366666600106464323131333035653264343837336200203762336533656138346663383333613937306464363432363032356162313436",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    store = RuntimeCredentialsStore(credentials_path)
    changed = store.backfill_device_mqtt_passwords(log_path)

    assert changed == 1
    device = store.resolve_device(duid="cloud-q7-a")
    assert device is not None
    assert device["device_mqtt_pass"] == "ff8922d24a9a9af81f18f35dcee9a5a5"


def test_confirm_credentials_failed_publication_preserves_previous_state(tmp_path, monkeypatch):
    import pytest
    from roborock_local_server.bundled_backend.shared import runtime_credentials as module
    from roborock_local_server.bundled_backend.shared.durable_state import PersistenceError
    path = tmp_path / "credentials.json"
    store = RuntimeCredentialsStore(path)
    store.ensure_device(did="known", duid="known-duid", model="test")
    before = store.devices()
    disk_before = path.read_bytes()
    def fail(*args):
        raise PersistenceError("synthetic disk full")
    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_json", fail)
        with pytest.raises(PersistenceError):
            store.confirm_device_mqtt_credentials(did="known", username="confirmed", password="confirmed-secret")
    assert store.devices() == before
    assert path.read_bytes() == disk_before
    confirmed = store.confirm_device_mqtt_credentials(did="known", username="confirmed", password="confirmed-secret")
    assert confirmed["device_mqtt_pass"] == "confirmed-secret"
    assert RuntimeCredentialsStore(path).resolve_device(did="known")["device_mqtt_pass"] == "confirmed-secret"
