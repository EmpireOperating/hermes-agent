from __future__ import annotations

import importlib
import json
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


@pytest.fixture()
def server(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    with patch.dict(
        "sys.modules",
        {"hermes_cli.env_loader": MagicMock(), "hermes_cli.banner": MagicMock()},
    ):
        mod = importlib.import_module("tui_gateway.server")
    methods = dict(mod._methods)
    yield mod
    mod._methods.clear()
    mod._methods.update(methods)
    mod._sessions.clear()
    mod._pending.clear()
    mod._answers.clear()
    mod._db = None


def _create(server, name: str, provider: str) -> dict:
    response = server._methods["profiles.create"](
        name,
        {
            "name": name,
            "description": f"test {name}",
            "no_skills": True,
            "model": "test-model",
            "provider": provider,
            "mirror_credentials": False,
            "share_auth": True,
            "soul": "test soul",
        },
    )
    assert "error" not in response
    return response["result"]


def _write_pool(home: Path, provider: str, *, last_status: str | None = None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {
                    provider: [
                        {
                            "id": f"{provider}-test",
                            "access_token": "test-token",
                            "auth_type": "oauth",
                            "label": "test",
                            "priority": 0,
                            "last_status": last_status,
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )


def test_profiles_create_without_mirroring_leaves_no_credential_files(server):
    result = _create(server, "care-no-credential-files", "openai-codex")
    profile = Path(result["path"])

    assert not (profile / ".env").exists()
    assert not (profile / "auth.json").exists()
    assert result["mirrored"]["env"] is False
    assert result["mirrored"]["env_absent"] is True
    assert result["mirrored"]["auth"] == "shared"


def test_profiles_create_without_mirroring_does_not_delete_clone_credentials(server):
    from hermes_cli.profiles import create_profile

    source = create_profile("source-with-env", no_skills=True)
    assert isinstance(source, Path)
    (source / ".env").write_text("TEST_ONLY_KEY=value\n", encoding="utf-8")
    response = server._methods["profiles.create"](
        "care-cloned-env",
        {
            "name": "care-cloned-env",
            "clone_from": "source-with-env",
            "model": "test-model",
            "provider": "openai-codex",
            "mirror_credentials": False,
            "share_auth": True,
            "soul": "test soul",
        },
    )

    assert "error" not in response
    result = response["result"]
    profile = Path(result["path"])
    assert (profile / ".env").read_text(encoding="utf-8") == "TEST_ONLY_KEY=value\n"
    assert result["mirrored"]["env_absent"] is False


def test_named_operator_local_credential_does_not_attest_for_new_profile(server):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    root = Path(os.environ["HERMES_HOME"])
    operator = root / "profiles" / "operator-blue"
    _write_pool(operator, "openai-codex")
    token = set_hermes_home_override(str(operator))
    try:
        result = _create(server, "care-local-only-auth", "openai-codex")
    finally:
        reset_hermes_home_override(token)

    assert result["mirrored"]["auth_verified"] is False


def test_new_profile_uses_global_pool_not_dead_named_operator_shadow(server):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    root = Path(os.environ["HERMES_HOME"])
    _write_pool(root, "openai-codex")
    operator = root / "profiles" / "operator-blue"
    _write_pool(operator, "openai-codex", last_status="dead")
    token = set_hermes_home_override(str(operator))
    try:
        result = _create(server, "care-global-auth", "openai-codex")
    finally:
        reset_hermes_home_override(token)

    assert result["mirrored"]["auth_verified"] is True


def test_profiles_create_persists_unique_instance_identity_in_inventory(server):
    first = _create(server, "care-instance-one", "provider-without-auth")
    second = _create(server, "care-instance-two", "provider-without-auth")

    assert first["instance_written"] is True
    assert second["instance_written"] is True
    assert len(first["instance_id"]) == 32
    assert first["instance_id"] != second["instance_id"]

    inventory = server._methods["profiles.list"]("list", {"include_sessions": False})["result"]
    rows = {row["name"]: row for row in inventory["profiles"]}
    assert rows["care-instance-one"]["instance_id"] == first["instance_id"]
    assert rows["care-instance-two"]["instance_id"] == second["instance_id"]


def test_profiles_configure_binds_ui_metadata_to_expected_instance(server):
    created = _create(server, "care-configure-instance", "provider-without-auth")

    wrong = server._methods["profiles.configure"](
        "wrong",
        {
            "name": "care-configure-instance",
            "expected_instance_id": "ffffffffffffffffffffffffffffffff",
            "ui_meta": {"empire_care": {"schema": 1, "display_name": "Wrong"}},
        },
    )["result"]
    assert wrong["applied"]["ui_meta"] is False

    correct = server._methods["profiles.configure"](
        "correct",
        {
            "name": "care-configure-instance",
            "expected_instance_id": created["instance_id"],
            "ui_meta": {"empire_care": {"schema": 1, "display_name": "Correct"}},
        },
    )["result"]
    assert correct["applied"]["ui_meta"] is True

    inventory = server._methods["profiles.list"]("list", {"include_sessions": False})["result"]
    row = next(item for item in inventory["profiles"] if item["name"] == "care-configure-instance")
    assert row["instance_id"] == created["instance_id"]
    assert row["ui_meta"]["empire_care"]["display_name"] == "Correct"


def test_profile_mutation_lock_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from hermes_cli.profiles import profile_mutation_lock

    escaped = tmp_path / "escaped.lock"
    with pytest.raises(ValueError):
        with profile_mutation_lock("../../escaped"):
            pass
    assert not escaped.exists()


def test_profile_mutation_lock_serializes_delete_for_same_name(server):
    _create(server, "care-locked", "provider-without-auth")
    from hermes_cli.profiles import delete_profile, profile_mutation_lock

    finished = threading.Event()

    def delete() -> None:
        delete_profile("care-locked", yes=True)
        finished.set()

    with profile_mutation_lock("care-locked"):
        worker = threading.Thread(target=delete)
        worker.start()
        assert not finished.wait(0.1)

    worker.join(timeout=5)
    assert finished.is_set()


def test_configure_delete_recreate_race_cannot_tag_replacement(server):
    created = _create(server, "care-race", "provider-without-auth")
    from hermes_cli import profiles as profiles_mod
    from utils import atomic_yaml_write as real_atomic_yaml_write

    entered = threading.Event()
    release = threading.Event()
    replacement_done = threading.Event()
    configured = {}

    def blocking_atomic(path, data, **kwargs):
        if Path(path).parent.name == "care-race" and "ui_meta" in data and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return real_atomic_yaml_write(path, data, **kwargs)

    def configure() -> None:
        configured.update(server._methods["profiles.configure"](
            "configure",
            {
                "name": "care-race",
                "expected_instance_id": created["instance_id"],
                "ui_meta": {"empire_care": {"schema": 1, "display_name": "Stale"}},
            },
        )["result"])

    replacement = {}

    def replace() -> None:
        profiles_mod.delete_profile("care-race", yes=True)
        created_profile = profiles_mod.create_profile(
            "care-race", no_skills=True, return_instance=True,
        )
        assert isinstance(created_profile, tuple)
        path, instance_id = created_profile
        replacement.update(path=path, instance_id=instance_id)
        replacement_done.set()

    with patch("utils.atomic_yaml_write", side_effect=blocking_atomic):
        configure_thread = threading.Thread(target=configure)
        configure_thread.start()
        assert entered.wait(5)
        replace_thread = threading.Thread(target=replace)
        replace_thread.start()
        assert not replacement_done.wait(0.1)
        release.set()
        configure_thread.join(timeout=5)
        replace_thread.join(timeout=5)

    assert configured["applied"]["ui_meta"] is True
    assert replacement_done.is_set()
    assert replacement["instance_id"] != created["instance_id"]
    metadata = yaml.safe_load((replacement["path"] / "profile.yaml").read_text())
    assert metadata["instance_id"] == replacement["instance_id"]
    assert "ui_meta" not in metadata


def test_profiles_create_transaction_cannot_write_into_replacement(server):
    from hermes_cli import profiles as profiles_mod

    entered = threading.Event()
    release = threading.Event()
    replacement_done = threading.Event()
    original_result = {}
    replacement = {}

    def paused_seed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {}

    def create_original() -> None:
        original_result.update(_create(server, "care-create-race", "provider-without-auth"))

    def replace() -> None:
        profiles_mod.delete_profile("care-create-race", yes=True)
        created = profiles_mod.create_profile(
            "care-create-race", no_skills=True, return_instance=True,
        )
        assert isinstance(created, tuple)
        path, instance_id = created
        replacement.update(path=path, instance_id=instance_id)
        replacement_done.set()

    with patch.object(profiles_mod, "seed_profile_skills", side_effect=paused_seed):
        create_thread = threading.Thread(target=create_original)
        create_thread.start()
        assert entered.wait(5)
        replace_thread = threading.Thread(target=replace)
        replace_thread.start()
        assert not replacement_done.wait(0.1)
        release.set()
        create_thread.join(timeout=5)
        replace_thread.join(timeout=5)

    assert replacement_done.is_set()
    assert replacement["instance_id"] != original_result["instance_id"]
    assert (replacement["path"] / "SOUL.md").read_text() != "test soul"


def test_profiles_create_reports_shared_auth_unverified_without_global_credential(server):
    result = _create(server, "care-no-auth", "provider-without-auth")

    assert result["mirrored"]["auth"] == "shared"
    assert result["mirrored"]["auth_verified"] is False


def test_profiles_create_rejects_dead_shared_credential(server):
    auth_path = Path(os.environ["HERMES_HOME"]) / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "dead-test",
                            "access_token": "test-token",
                            "auth_type": "oauth",
                            "label": "test",
                            "priority": 0,
                            "last_status": "dead",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = _create(server, "care-dead-auth", "openai-codex")

    assert result["mirrored"]["auth_verified"] is False


def test_profiles_create_verifies_shared_global_credential(server):
    auth_path = Path(os.environ["HERMES_HOME"]) / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "shared-test",
                            "access_token": "test-token",
                            "auth_type": "oauth",
                            "label": "test",
                            "priority": 0,
                            "status": "active",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = _create(server, "care-shared-auth", "openai-codex")

    assert result["mirrored"]["auth"] == "shared"
    assert result["mirrored"]["auth_verified"] is True
