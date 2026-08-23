"""TUI profile mutations share the public lifecycle lock."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import tui_gateway.server as srv


@pytest.fixture()
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


def test_configure_waits_for_external_profile_mutation_lock(home):
    from hermes_cli.profiles import profile_mutation_lock

    done = threading.Event()
    response = {}

    def configure():
        response.update(
            srv._methods["profiles.configure"](
                "configure",
                {"name": "default", "ui_meta": {"test-lock": {"ready": True}}},
            )
        )
        done.set()

    with profile_mutation_lock("default"):
        thread = threading.Thread(target=configure)
        thread.start()
        assert not done.wait(0.2)

    assert done.wait(2)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert response["result"]["applied"]["ui_meta"] is True


@pytest.mark.parametrize(
    ("method", "expected_code"),
    [("profiles.create", 4062), ("profiles.configure", 4064)],
)
def test_invalid_name_preserves_structured_error_and_creates_no_lock_artifact(
    home, method, expected_code,
):
    response = srv._methods[method]("invalid", {"name": "../../bad"})

    assert response["error"]["code"] == expected_code
    assert not (home / ".profile-locks").exists()


def test_configure_missing_name_preserves_its_error_response(home):
    response = srv._methods["profiles.configure"]("configure", {})

    assert response["error"] == {"code": 4063, "message": "name required"}


def test_registry_composes_mutation_lock_with_profile_scope(home):
    from hermes_cli.profiles import profile_mutation_lock
    from tui_gateway.method_ctx import HandlerRegistry

    registry = HandlerRegistry()

    @registry.method("profiles.test")
    @registry.profile_scoped
    @registry.profile_mutation_locked
    def handler(_rid, params):
        return params["scoped"]

    def profile_scoped(fn):
        def wrapped(rid, params):
            scoped_params = dict(params)
            scoped_params["scoped"] = True
            return fn(rid, scoped_params)

        return wrapped

    server = SimpleNamespace(_methods={}, _profile_scoped=profile_scoped)
    registry.install(server)

    result = {}
    done = threading.Event()

    def call_handler():
        result["value"] = server._methods["profiles.test"]("test", {"name": "default"})
        done.set()

    with profile_mutation_lock("default"):
        thread = threading.Thread(target=call_handler)
        thread.start()
        assert not done.wait(0.2)

    assert done.wait(2)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result == {"value": True}


def test_tui_profile_identity_is_created_listed_and_guards_metadata(home):
    created = srv._methods["profiles.create"](
        "create",
        {
            "name": "care-instance",
            "no_skills": True,
            "mirror_credentials": False,
        },
    )["result"]

    assert created["instance_written"] is True
    instance_id = created["instance_id"]
    assert len(instance_id) == 32
    assert all(char in "0123456789abcdef" for char in instance_id)

    wrong = srv._methods["profiles.configure"](
        "wrong",
        {
            "name": "care-instance",
            "expected_instance_id": "f" * 32,
            "ui_meta": {"empire_care": {"schema": 1, "display_name": "Wrong"}},
        },
    )["result"]
    correct = srv._methods["profiles.configure"](
        "correct",
        {
            "name": "care-instance",
            "expected_instance_id": instance_id,
            "ui_meta": {"empire_care": {"schema": 1, "display_name": "Correct"}},
        },
    )["result"]
    inventory = srv._methods["profiles.list"](
        "list", {"include_sessions": False},
    )["result"]
    row = next(item for item in inventory["profiles"] if item["name"] == "care-instance")

    assert wrong["applied"]["ui_meta"] is False
    assert correct["applied"]["ui_meta"] is True
    assert row["instance_id"] == instance_id
    assert row["ui_meta"]["empire_care"]["display_name"] == "Correct"
    assert Path(created["path"]).is_dir()
