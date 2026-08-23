"""Cross-process-safe serialization for profile lifecycle mutations."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import profiles


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles.Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_invalid_name_never_creates_an_escaped_lock_file(profile_env):
    escaped = profile_env.parent / "escaped.lock"

    with pytest.raises(ValueError):
        with profiles.profile_mutation_lock("../../escaped"):
            pass

    assert not escaped.exists()
    assert not (profile_env / ".profile-locks").exists()


def test_symlinked_lock_file_is_rejected_without_clobbering_target(
    profile_env, tmp_path,
):
    if os.name == "nt":
        pytest.skip("POSIX symlink/no-follow contract")
    root = profile_env / ".profile-locks"
    root.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_bytes(b"UNCHANGED-SENTINEL")
    (root / "worker.lock").symlink_to(victim)

    with pytest.raises(ValueError, match="unsafe profile lock"):
        with profiles.profile_mutation_lock("worker"):
            pass

    assert victim.read_bytes() == b"UNCHANGED-SENTINEL"


def test_symlinked_lock_root_is_rejected_without_creating_lock(
    profile_env, tmp_path,
):
    if os.name == "nt":
        pytest.skip("POSIX symlink/no-follow contract")
    target = tmp_path / "redirected-locks"
    target.mkdir()
    (profile_env / ".profile-locks").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="unsafe profile lock"):
        with profiles.profile_mutation_lock("worker"):
            pass

    assert list(target.iterdir()) == []


def test_existing_regular_lock_contents_are_not_clobbered(profile_env):
    root = profile_env / ".profile-locks"
    root.mkdir(mode=0o700)
    lock_file = root / "worker.lock"
    lock_file.write_bytes(b"UNCHANGED-LOCK")
    if os.name != "nt":
        lock_file.chmod(0o600)

    with profiles.profile_mutation_lock("worker"):
        pass

    assert lock_file.read_bytes() == b"UNCHANGED-LOCK"


def test_lock_root_and_file_are_owner_only(profile_env):
    with profiles.profile_mutation_lock("worker"):
        lock_root = profile_env / ".profile-locks"
        lock_file = lock_root / "worker.lock"
        assert lock_file.read_bytes() == b"\0"
        assert stat.S_IMODE(lock_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600


def test_same_name_blocks_another_thread(profile_env):
    entered = threading.Event()

    def worker():
        with profiles.profile_mutation_lock("worker"):
            entered.set()

    with profiles.profile_mutation_lock("worker"):
        thread = threading.Thread(target=worker)
        thread.start()
        assert not entered.wait(0.2)

    assert entered.wait(2)
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_same_name_blocks_another_process(profile_env):
    script = """\
from hermes_cli.profiles import profile_mutation_lock
print('ready', flush=True)
with profile_mutation_lock('worker'):
    print('entered', flush=True)
"""
    env = os.environ.copy()
    env["HERMES_HOME"] = str(profile_env)
    repo_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (repo_root, env.get("PYTHONPATH")) if part
    )
    with profiles.profile_mutation_lock("worker"):
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=str(profile_env.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "ready"
            time.sleep(0.2)
            assert process.poll() is None

        finally:
            if process.poll() is not None:
                process.communicate()
    try:
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == 0, stderr
    assert stdout.strip() == "entered"


def test_different_names_are_independent(profile_env):
    entered = threading.Event()

    def worker():
        with profiles.profile_mutation_lock("beta"):
            entered.set()

    with profiles.profile_mutation_lock("alpha"):
        thread = threading.Thread(target=worker)
        thread.start()
        assert entered.wait(2)
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_same_thread_reentrant_lock(profile_env):
    with profiles.profile_mutation_lock("Worker"):
        with profiles.profile_mutation_lock("worker"):
            assert (profile_env / ".profile-locks" / "worker.lock").exists()


def test_opposite_two_name_orders_cannot_deadlock(profile_env):
    ready = threading.Barrier(2)
    completed = [threading.Event(), threading.Event()]

    def worker(index, names):
        ready.wait()
        with profiles.profile_mutation_lock(*names):
            completed[index].set()

    threads = [
        threading.Thread(target=worker, args=(0, ("alpha", "beta"))),
        threading.Thread(target=worker, args=(1, ("beta", "alpha"))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(event.is_set() for event in completed)
    assert not any(thread.is_alive() for thread in threads)


def test_lifecycle_operations_wait_for_external_lock_and_leave_valid_state(profile_env):
    def assert_waits(names, operation):
        finished = threading.Event()

        def worker():
            operation()
            finished.set()

        with profiles.profile_mutation_lock(*names):
            thread = threading.Thread(target=worker)
            thread.start()
            assert not finished.wait(0.2)
        assert finished.wait(5)
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert_waits(("created",), lambda: profiles.create_profile("created", no_alias=True))
    assert profiles.get_profile_dir("created").is_dir()

    profiles.create_profile("deleted", no_alias=True)
    assert_waits(("deleted",), lambda: profiles.delete_profile("deleted", yes=True))
    assert not profiles.get_profile_dir("deleted").exists()

    profiles.create_profile("oldname", no_alias=True)
    assert_waits(
        ("oldname", "newname"),
        lambda: profiles.rename_profile("oldname", "newname"),
    )
    assert not profiles.get_profile_dir("oldname").exists()
    assert profiles.get_profile_dir("newname").is_dir()


def test_create_profile_can_return_and_persist_unique_lifecycle_identity(profile_env):
    first_path, first_id = profiles.create_profile(
        "care-instance-one", no_alias=True, no_skills=True, return_instance=True,
    )
    second_path, second_id = profiles.create_profile(
        "care-instance-two", no_alias=True, no_skills=True, return_instance=True,
    )

    import yaml

    assert len(first_id) == 32
    assert all(char in "0123456789abcdef" for char in first_id)
    assert first_id != second_id
    assert yaml.safe_load((first_path / "profile.yaml").read_text())["instance_id"] == first_id
    assert yaml.safe_load((second_path / "profile.yaml").read_text())["instance_id"] == second_id
    assert isinstance(profiles.create_profile("care-path-only", no_alias=True, no_skills=True), Path)


def test_default_profile_display_rename_keeps_its_existing_name_rules(profile_env):
    assert profiles.rename_profile("default", "My Workspace") == profile_env
