"""Behavior contracts for canonical, project-scoped Kanban missions."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_missions as missions
from hermes_cli import projects_db


@pytest.fixture
def mission_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return tmp_path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(path: Path, marker: str = "base") -> tuple[Path, str]:
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(path, "config", "user.email", "mission@example.com")
    _git(path, "config", "user.name", "Mission Test")
    (path / "README.md").write_text(f"{marker}\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "base")
    return path, _git(path, "rev-parse", "HEAD")


def _receipt(
    conn,
    repo: Path,
    base: str,
    *,
    key: str,
    session: str,
    project: str,
    supervisor: str = "orca",
):
    with projects_db.connect_closing() as project_conn:
        existing = projects_db.get_project(project_conn, project)
        if existing is None:
            project = projects_db.create_project(
                project_conn,
                name=project,
                slug=project,
                primary_path=str(repo),
            )
        else:
            project = existing.id
    return missions.create_mission_receipt(
        conn,
        idempotency_key=key,
        source_platform="telegram",
        source_chat_id=f"chat-{session}",
        source_thread_id="topic-1",
        source_session_id=session,
        project_id=project,
        objective=f"ship {project}",
        acceptance_criteria=["tests pass", "signed completion"],
        constraints=["no deploy"],
        non_goals=["unrelated refactors"],
        current_chat_provenance={"message_id": f"msg-{session}"},
        repo_root=str(repo),
        base_commit=base,
        source_branch="main",
        supervisor_profile=supervisor,
    )


def _commit_child(allocation: dict, filename: str, text: str) -> str:
    worktree = Path(allocation["workspace_path"])
    (worktree / filename).write_text(text, encoding="utf-8")
    _git(worktree, "add", filename)
    _git(worktree, "commit", "-m", f"add {filename}")
    return _git(worktree, "rev-parse", "HEAD")


def _handoff_and_complete(conn, mission_id: str, allocation: dict, commit: str):
    missions.record_worker_handoff(
        conn,
        mission_id,
        allocation["task_id"],
        commit_sha=commit,
        branch_name=allocation["branch_name"],
        evidence={"tests": ["focused: pass"]},
        submitted_by="worker",
    )
    assert kb.complete_task(
        conn,
        allocation["task_id"],
        summary="committed handoff",
        metadata={"commit_sha": commit},
    )


def _claim_root(conn, mission: dict):
    claimed = kb.claim_task(conn, mission["root_task_id"], claimer="orca-test")
    assert claimed is not None
    assert claimed.current_run_id is not None
    return claimed


def test_duplicate_dispatch_is_idempotent_and_source_binding_is_immutable(mission_env):
    repo, base = _repo(mission_env / "repo-a")
    with kb.connect() as conn:
        first, duplicate_first = _receipt(
            conn, repo, base, key="dispatch-1", session="s1", project="p1"
        )
        second, duplicate_second = _receipt(
            conn, repo, base, key="dispatch-1", session="s1", project="p1"
        )
        assert duplicate_first is False
        assert duplicate_second is True
        assert first["id"] == second["id"]
        assert conn.execute("SELECT COUNT(*) FROM kanban_missions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

        with pytest.raises(ValueError, match="another source or project"):
            _receipt(
                conn, repo, base, key="dispatch-1", session="other", project="p1"
            )

        with pytest.raises(ValueError, match="canonical Orca"):
            _receipt(
                conn,
                repo,
                base,
                key="dispatch-worker",
                session="s1",
                project="p1",
                supervisor="worker",
            )

        other_repo, other_base = _repo(mission_env / "repo-b")
        with pytest.raises(ValueError, match="belong to the selected project"):
            _receipt(
                conn,
                other_repo,
                other_base,
                key="dispatch-cross-project",
                session="s1",
                project="p1",
            )


def test_forced_concurrent_duplicate_dispatch_collapses_to_one_receipt(
    mission_env, monkeypatch,
):
    repo, base = _repo(mission_env / "repo-concurrent")
    with projects_db.connect_closing() as project_conn:
        project_id = projects_db.create_project(
            project_conn,
            name="Concurrent Project",
            slug="concurrent-project",
            primary_path=str(repo),
        )
    barrier = threading.Barrier(2)
    original_create = kb.create_task

    def synchronized_create(*args, **kwargs):
        barrier.wait(timeout=10)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(kb, "create_task", synchronized_create)

    def dispatch():
        with kb.connect() as conn:
            return missions.create_mission_receipt(
                conn,
                idempotency_key="forced-duplicate",
                source_platform="telegram",
                source_chat_id="chat-concurrent",
                source_thread_id="topic-1",
                source_session_id="session-concurrent",
                project_id=project_id,
                objective="concurrent mission",
                acceptance_criteria=["exactly one receipt"],
                constraints=["no deploy"],
                non_goals=["duplicates"],
                current_chat_provenance={"message_id": "msg-concurrent"},
                repo_root=str(repo),
                base_commit=base,
                source_branch="main",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(dispatch), pool.submit(dispatch)]
        results = [future.result() for future in futures]
    assert len({receipt["id"] for receipt, _duplicate in results}) == 1
    assert sorted(duplicate for _receipt, duplicate in results) == [False, True]
    with kb.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM kanban_missions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_two_repos_and_sessions_stay_strictly_scoped(mission_env, monkeypatch):
    repo_a, base_a = _repo(mission_env / "repo-a", "a")
    repo_b, base_b = _repo(mission_env / "repo-b", "b")
    monkeypatch.setenv("HERMES_SESSION_ID", "ambient-session-must-not-leak")
    monkeypatch.chdir(repo_b)

    with kb.connect() as conn:
        mission_a, _ = _receipt(
            conn, repo_a, base_a, key="a", session="source-a", project="project-a"
        )
        mission_b, _ = _receipt(
            conn, repo_b, base_b, key="b", session="source-b", project="project-b"
        )
        assert mission_a["repo_root"] == str(repo_a.resolve())
        assert mission_b["repo_root"] == str(repo_b.resolve())
        assert mission_a["source"]["session_id"] == "source-a"
        assert mission_b["source"]["session_id"] == "source-b"
        assert "ambient-session-must-not-leak" not in str(mission_a)
        assert "ambient-session-must-not-leak" not in str(mission_b)
        assert mission_a["project_id"] != mission_b["project_id"]

        # Control-room summaries stay explicitly scoped under concurrent
        # missions; neither ambient cwd nor the other source session leaks in.
        listed_a = missions.list_missions(
            conn,
            project_id=mission_a["project_id"],
            source_session_id="source-a",
        )
        listed_b = missions.list_missions(
            conn,
            project_id=mission_b["project_id"],
            source_session_id="source-b",
        )
        assert [item["id"] for item in listed_a] == [mission_a["id"]]
        assert [item["id"] for item in listed_b] == [mission_b["id"]]
        assert listed_a[0]["repo_root"] == str(repo_a.resolve())
        assert listed_b[0]["repo_root"] == str(repo_b.resolve())
        assert "runs" not in listed_a[0]

        root_a = kb.get_task(conn, mission_a["root_task_id"])
        assert root_a is not None
        assert root_a.project_id == mission_a["project_id"]
        assert root_a.workspace_kind == "scratch"

        with pytest.raises(PermissionError, match="source chat and session"):
            missions.steer_mission(
                conn,
                mission_a["id"],
                task_id=mission_a["root_task_id"],
                source_platform="telegram",
                source_chat_id="chat-source-b",
                source_thread_id="topic-1",
                source_session_id="source-b",
                instruction="cross-session steering",
            )

        with pytest.raises(PermissionError, match="source chat and session"):
            missions.steer_mission(
                conn,
                mission_a["id"],
                task_id=mission_a["root_task_id"],
                source_platform="telegram",
                source_chat_id="another-chat",
                source_thread_id="topic-1",
                source_session_id="source-a",
                instruction="cross-chat steering",
            )


def test_parallel_children_are_distinct_worktrees_from_one_immutable_base(mission_env):
    repo, base = _repo(mission_env / "repo")
    with kb.connect() as conn:
        mission, _ = _receipt(
            conn, repo, base, key="parallel", session="s", project="p"
        )
        left = missions.create_mission_child(
            conn, mission["id"], title="left", body="left", assignee="worker-a"
        )
        right = missions.create_mission_child(
            conn, mission["id"], title="right", body="right", assignee="worker-b"
        )

        assert left["workspace_path"] != right["workspace_path"]
        assert left["branch_name"] != right["branch_name"]
        assert _git(Path(left["workspace_path"]), "rev-parse", "HEAD") == base
        assert _git(Path(right["workspace_path"]), "rev-parse", "HEAD") == base
        assert _git(Path(left["workspace_path"]), "status", "--porcelain") == ""
        assert _git(Path(right["workspace_path"]), "status", "--porcelain") == ""


def test_concurrent_children_allocate_distinct_worktrees(mission_env):
    repo, base = _repo(mission_env / "repo-concurrent-worktrees")
    with kb.connect() as conn:
        mission, _ = _receipt(
            conn, repo, base, key="concurrent-worktrees", session="s", project="p"
        )
    barrier = threading.Barrier(2)

    def allocate(name: str):
        barrier.wait(timeout=10)
        with kb.connect() as conn:
            return missions.create_mission_child(
                conn,
                mission["id"],
                title=name,
                body=f"caller body {name}",
                assignee=f"worker-{name}",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(allocate, "left"), pool.submit(allocate, "right")]
        allocations = [future.result() for future in futures]
    assert len({item["workspace_path"] for item in allocations}) == 2
    assert len({item["branch_name"] for item in allocations}) == 2
    for item in allocations:
        assert _git(Path(item["workspace_path"]), "rev-parse", "HEAD") == base


def test_worker_context_projects_receipt_and_handoff_contract(mission_env, monkeypatch):
    repo, base = _repo(mission_env / "repo-context")
    (repo / "dirty-sentinel.txt").write_text("not committed\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_SESSION_ID", "ambient-must-not-win")
    with kb.connect() as conn:
        mission, _ = _receipt(
            conn, repo, base, key="context", session="source-context", project="p"
        )
        child = missions.create_mission_child(
            conn,
            mission["id"],
            title="context child",
            body="caller-authored body without canonical receipt details",
            assignee="worker",
        )
        context = kb.build_worker_context(conn, child["task_id"])
        root_context = kb.build_worker_context(conn, mission["root_task_id"])
    assert "## Immutable mission receipt" in context
    assert "ship p" in context
    assert "tests pass" in context
    assert "no deploy" in context
    assert "unrelated refactors" in context
    assert "chat-source-context" in context
    assert "msg-source-context" in context
    assert str(repo.resolve()) in context
    assert base in context
    assert "dirty-sentinel.txt" in context
    assert "## Durable mission handoff contract" in context
    assert "ambient-must-not-win" not in context
    assert "## Immutable mission receipt" in root_context
    assert "only this canonical Orca run may finalize" in root_context


def test_integration_worktree_receives_only_declared_parent_commits(mission_env):
    repo, base = _repo(mission_env / "repo")
    with kb.connect() as conn:
        mission, _ = _receipt(
            conn, repo, base, key="integration", session="s", project="p"
        )
        one = missions.create_mission_child(
            conn, mission["id"], title="one", body="one", assignee="worker-a"
        )
        two = missions.create_mission_child(
            conn, mission["id"], title="two", body="two", assignee="worker-b"
        )
        commit_one = _commit_child(one, "one.txt", "one\n")
        commit_two = _commit_child(two, "two.txt", "two\n")
        _handoff_and_complete(conn, mission["id"], one, commit_one)
        _handoff_and_complete(conn, mission["id"], two, commit_two)

        integration = missions.create_mission_child(
            conn,
            mission["id"],
            title="integrate",
            body="integrate declared commits",
            assignee="integrator",
            role="integration",
            parent_task_ids=[one["task_id"], two["task_id"]],
        )
        worktree = Path(integration["workspace_path"])
        assert integration["declared_parent_commits"] == [commit_one, commit_two]
        assert (worktree / "one.txt").read_text(encoding="utf-8") == "one\n"
        assert (worktree / "two.txt").read_text(encoding="utf-8") == "two\n"
        assert _git(worktree, "merge-base", "--is-ancestor", base, "HEAD") == ""
        assert _git(worktree, "merge-base", "--is-ancestor", commit_one, "HEAD") == ""
        assert _git(worktree, "merge-base", "--is-ancestor", commit_two, "HEAD") == ""


def test_internal_retries_are_suppressed_and_projection_is_exactly_once(mission_env):
    repo, base = _repo(mission_env / "repo")
    with kb.connect() as conn:
        mission, _ = _receipt(
            conn, repo, base, key="projection", session="s", project="p"
        )
        child = missions.create_mission_child(
            conn, mission["id"], title="code", body="code", assignee="worker"
        )
        now = 1
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
                "VALUES (?, 'worker', 'crashed', ?, ?, 'crashed')",
                (child["task_id"], now, now),
            )
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
                "VALUES (?, 'worker', 'timed_out', ?, ?, 'timed_out')",
                (child["task_id"], now, now),
            )
        status = missions.mission_status(conn, mission["id"])
        assert "runs" not in status
        assert status["blockers"] == []

        assert kb.block_task(conn, child["task_id"], reason="Need API contract")
        assert missions.project_blocker(
            conn, mission["id"], task_id=child["task_id"]
        )
        projected_blocker = [
            event for event in kb.list_events(conn, mission["root_task_id"])
            if event.kind == "blocked"
            and (event.payload or {}).get("mission_projection") is True
        ]
        assert len(projected_blocker) == 1
        assert not missions.project_blocker(
            conn, mission["id"], task_id=child["task_id"]
        )
        root_events = kb.list_events(conn, mission["root_task_id"])
        assert [event.kind for event in root_events].count("blocked") == 1

        assert kb.unblock_task(conn, child["task_id"])
        assert kb.block_task(conn, child["task_id"], reason="Different wording")
        assert not missions.project_blocker(
            conn, mission["id"], task_id=child["task_id"]
        )


def test_root_generic_completion_and_retry_events_are_not_projectable(
    mission_env, monkeypatch,
):
    from gateway.kanban_watchers import _projectable_notification_events

    repo, base = _repo(mission_env / "repo-root-events")
    with kb.connect() as conn:
        mission, _ = _receipt(
            conn, repo, base, key="root-events", session="s", project="p"
        )
        root = _claim_root(conn, mission)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?",
                (999999, root.id),
            )
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: ("unknown", None))
        assert kb.detect_crashed_workers(conn) == [root.id]
        crashed = [
            event for event in kb.list_events(conn, root.id)
            if event.kind in {"crashed", "timed_out", "completed"}
        ]
        assert _projectable_notification_events(conn, root.id, crashed) == []

        # The internal crash is retryable but remains invisible to the source.
        root = _claim_root(conn, mission)
        assert not kb.complete_task(conn, root.id, summary="generic bypass")
        generic = [
            kb.Event(1, root.id, "crashed", {"error": "boom"}, 1),
            kb.Event(2, root.id, "timed_out", {"limit_seconds": 1}, 2),
            kb.Event(3, root.id, "completed", {"summary": "generic"}, 3),
        ]
        assert _projectable_notification_events(conn, root.id, generic) == []
        explicit = kb.Event(
            4, root.id, "completed", {"mission_projection": True}, 4
        )
        assert _projectable_notification_events(conn, root.id, [explicit]) == [explicit]


def test_orca_only_signed_completion_projects_once(mission_env):
    repo, base = _repo(mission_env / "repo")
    with kb.connect() as conn:
        mission, _ = _receipt(
            conn, repo, base, key="completion", session="s", project="p"
        )
        child = missions.create_mission_child(
            conn, mission["id"], title="code", body="code", assignee="worker"
        )
        commit = _commit_child(child, "done.txt", "done\n")
        _handoff_and_complete(conn, mission["id"], child, commit)
        root = _claim_root(conn, mission)

        with pytest.raises(PermissionError, match="Orca"):
            missions.sign_mission_completion(
                conn,
                mission["id"],
                signer_run_id=999999,
                summary="done",
                evidence={"tests": "pass"},
            )
        assert missions.sign_mission_completion(
            conn,
            mission["id"],
            signer_run_id=root.current_run_id,
            summary="all acceptance criteria passed",
            evidence={"tests": ["focused: pass"]},
        )
        assert not missions.sign_mission_completion(
            conn,
            mission["id"],
            signer_run_id=root.current_run_id,
            summary="duplicate",
            evidence={"tests": ["focused: pass"]},
        )
        rows = conn.execute(
            "SELECT kind FROM kanban_mission_projections WHERE mission_id = ?",
            (mission["id"],),
        ).fetchall()
        assert [row["kind"] for row in rows].count("completion") == 1
        completed = [
            event for event in kb.list_events(conn, mission["root_task_id"])
            if event.kind == "completed"
        ]
        assert len(completed) == 1
        assert completed[0].payload is not None
        assert completed[0].payload["supervisor_signoff"] == "orca"
        assert completed[0].payload["mission_projection"] is True


def test_root_worker_tool_binds_completion_to_dispatcher_run(
    mission_env, monkeypatch,
):
    from tools import kanban_tools

    repo, base = _repo(mission_env / "repo-tool-signoff")
    with kb.connect() as conn:
        mission, _ = _receipt(
            conn, repo, base, key="tool-signoff", session="s", project="p"
        )
        child = missions.create_mission_child(
            conn,
            mission["id"],
            title="implementation",
            body="finish",
            assignee="worker",
        )
        commit = _commit_child(child, "done.txt", "done\n")
        _handoff_and_complete(conn, mission["id"], child, commit)
        root = _claim_root(conn, mission)

    monkeypatch.setenv("HERMES_KANBAN_TASK", root.id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(root.current_run_id))
    result = json.loads(kanban_tools._handle_complete({
        "summary": "canonical worker signoff",
        "metadata": {"tests": ["mission suite: pass"]},
    }))
    assert result["ok"] is True
    assert result["mission_id"] == mission["id"]
    with kb.connect() as conn:
        status = missions.mission_status(conn, mission["id"])
        assert status["mission"]["status"] == "completed"
        assert status["completion"]["signer_run_id"] == root.current_run_id


def test_completion_repairs_legacy_root_done_before_projection(mission_env):
    repo, base = _repo(mission_env / "repo-repair")
    with kb.connect() as conn:
        mission, _ = _receipt(
            conn, repo, base, key="repair", session="s", project="p"
        )
        root = _claim_root(conn, mission)
        run_id = root.current_run_id
        assert run_id is not None

        # Reproduce the pre-fix crash window: generic root completion committed,
        # but the mission receipt and explicit source projection did not.
        now = 1_700_000_000
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', result = ?, completed_at = ? "
                "WHERE id = ?",
                ("generic root result", now, root.id),
            )
            kb._end_run(
                conn, root.id, outcome="completed", status="done",
                summary="generic root result",
            )
            kb._append_event(
                conn, root.id, "completed", {"summary": "generic root result"},
                run_id=run_id,
            )

        durable_mission = missions.get_mission(conn, mission["id"])
        assert durable_mission is not None
        assert durable_mission["status"] == "active"
        assert missions.mission_status(conn, mission["id"])["completion"] is None
        assert missions.sign_mission_completion(
            conn,
            mission["id"],
            signer_run_id=run_id,
            summary="repaired signed result",
            evidence={"recovery": "verified"},
        )
        repaired = missions.mission_status(conn, mission["id"])
        assert repaired["mission"]["status"] == "completed"
        assert repaired["completion"]["summary"] == "repaired signed result"
        projected = [
            event for event in kb.list_events(conn, root.id)
            if event.kind == "completed"
            and (event.payload or {}).get("mission_projection") is True
        ]
        assert len(projected) == 1
