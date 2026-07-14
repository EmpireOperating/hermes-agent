"""Behavior contracts for canonical, project-scoped Kanban missions."""

from __future__ import annotations

import os
import subprocess
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
        assert not missions.project_blocker(
            conn, mission["id"], task_id=child["task_id"]
        )
        root_events = kb.list_events(conn, mission["root_task_id"])
        assert [event.kind for event in root_events].count("blocked") == 1


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

        with pytest.raises(PermissionError, match="Orca"):
            missions.sign_mission_completion(
                conn,
                mission["id"],
                signer_profile="worker",
                summary="done",
                evidence={"tests": "pass"},
            )
        assert missions.sign_mission_completion(
            conn,
            mission["id"],
            signer_profile="orca",
            summary="all acceptance criteria passed",
            evidence={"tests": ["focused: pass"]},
        )
        assert not missions.sign_mission_completion(
            conn,
            mission["id"],
            signer_profile="orca",
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
