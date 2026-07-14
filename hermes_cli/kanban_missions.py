"""Canonical Kanban runtime for chat-originated, project-scoped missions.

Missions add immutable receipts and orchestration metadata to the existing
Kanban database. They do not own a queue, dispatcher, or parallel task model:
every unit of work remains an ordinary ``tasks`` row.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db


VALID_MISSION_ROLES = {"root", "code", "integration", "supervisor"}


def _required(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        raise ValueError(detail)
    return (result.stdout or "").strip()


def _canonical_repo(repo_root: str) -> Path:
    requested = Path(_required(repo_root, "repo_root")).expanduser()
    if not requested.is_absolute():
        raise ValueError("repo_root must be absolute")
    canonical = Path(_git(requested, "rev-parse", "--show-toplevel")).resolve()
    if canonical != requested.resolve():
        raise ValueError("repo_root must name the selected repository root exactly")
    return canonical


def _canonical_project(project_id: str, repo: Path) -> str:
    """Resolve the selected first-class Project and bind it to ``repo``."""
    from hermes_cli import projects_db

    selected = _required(project_id, "project_id")
    with projects_db.connect_closing() as project_conn:
        project = projects_db.get_project(project_conn, selected)
    if project is None or project.archived:
        raise ValueError("project_id must identify an active selected project")
    project_paths = {
        Path(folder.path).expanduser().resolve()
        for folder in project.folders
        if folder.path
    }
    if project.primary_path:
        project_paths.add(Path(project.primary_path).expanduser().resolve())
    if repo not in project_paths:
        raise ValueError("repo_root must belong to the selected project")
    return project.id


def _commit(repo: Path, ref: str) -> str:
    candidate = _required(ref, "git reference")
    if candidate.startswith("-") or "\x00" in candidate:
        raise ValueError("git reference must not be an option or contain NUL")
    return _git(repo, "rev-parse", "--verify", f"{candidate}^{{commit}}")


def _is_ancestor(repo: Path, older: str, newer: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", older, newer],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0


def _dirty_snapshot(repo: Path) -> dict[str, Any]:
    porcelain = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    entries = [line for line in porcelain.splitlines() if line]
    return {
        "dirty": bool(entries),
        "entries": entries,
        "digest": hashlib.sha256(porcelain.encode("utf-8")).hexdigest(),
        "captured_at": int(time.time()),
    }


def _mission_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "root_task_id": row["root_task_id"],
        "idempotency_key": row["idempotency_key"],
        "source": {
            "platform": row["source_platform"],
            "chat_id": row["source_chat_id"],
            "thread_id": row["source_thread_id"],
            "session_id": row["source_session_id"],
        },
        "project_id": row["project_id"],
        "objective": row["objective"],
        "acceptance_criteria": _loads(row["acceptance_criteria"], []),
        "constraints": _loads(row["constraints_json"], []),
        "non_goals": _loads(row["non_goals_json"], []),
        "provenance": _loads(row["provenance_json"], {}),
        "repo_root": row["repo_root"],
        "base_commit": row["base_commit"],
        "source_branch": row["source_branch"],
        "dirty_state_snapshot": _loads(row["dirty_snapshot_json"], {}),
        "supervisor_profile": row["supervisor_profile"],
        "status": row["status"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def get_mission(conn: sqlite3.Connection, mission_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM kanban_missions WHERE id = ?", (mission_id,)
    ).fetchone()
    return _mission_dict(row) if row else None


def create_mission_receipt(
    conn: sqlite3.Connection,
    *,
    idempotency_key: str,
    source_platform: str,
    source_chat_id: str,
    source_session_id: str,
    project_id: str,
    objective: str,
    acceptance_criteria: Iterable[str],
    repo_root: str,
    base_commit: str,
    source_branch: str,
    constraints: Iterable[str] = (),
    non_goals: Iterable[str] = (),
    current_chat_provenance: Optional[dict[str, Any]] = None,
    source_thread_id: str = "",
    supervisor_profile: str = "orca",
    notifier_profile: Optional[str] = None,
) -> tuple[dict[str, Any], bool]:
    """Persist an immutable, source-bound mission receipt idempotently.

    No cwd, current session, or global recall fallback exists: every source and
    project field is required and the selected repo is resolved explicitly.
    """
    key = _required(idempotency_key, "idempotency_key")
    platform = _required(source_platform, "source_platform")
    chat_id = _required(source_chat_id, "source_chat_id")
    session_id = _required(source_session_id, "source_session_id")
    objective_text = _required(objective, "objective")
    branch = _required(source_branch, "source_branch")
    supervisor = kanban_db._canonical_assignee(
        _required(supervisor_profile, "supervisor_profile")
    )
    if supervisor != "orca":
        raise ValueError("mission supervisor_profile must be canonical Orca")
    criteria = [str(item).strip() for item in acceptance_criteria if str(item).strip()]
    if not criteria:
        raise ValueError("acceptance_criteria must contain at least one item")
    provenance = dict(current_chat_provenance or {})
    if provenance.get("session_id") not in (None, session_id):
        raise ValueError("current_chat_provenance.session_id must match source_session_id")
    provenance["session_id"] = session_id
    provenance["chat_id"] = chat_id
    provenance["thread_id"] = source_thread_id or ""

    repo = _canonical_repo(repo_root)
    selected_project = _canonical_project(project_id, repo)

    existing = conn.execute(
        "SELECT * FROM kanban_missions WHERE idempotency_key = ?", (key,)
    ).fetchone()
    if existing:
        binding = (
            existing["source_platform"], existing["source_chat_id"],
            existing["source_thread_id"], existing["source_session_id"],
            existing["project_id"],
        )
        requested = (platform, chat_id, source_thread_id or "", session_id, selected_project)
        if binding != requested:
            raise ValueError("idempotency_key is already bound to another source or project")
        return _mission_dict(existing), True

    immutable_base = _commit(repo, base_commit)
    branch_tip = _commit(repo, f"refs/heads/{branch}")
    if not _is_ancestor(repo, immutable_base, branch_tip):
        raise ValueError("base_commit must be an ancestor of source_branch")
    dirty = _dirty_snapshot(repo)
    mission_id = f"m_{secrets.token_hex(6)}"
    now = int(time.time())
    root_task_id = kanban_db.create_task(
        conn,
        title=f"Mission: {objective_text[:100]}",
        body=(
            f"Planning/supervision root for mission {mission_id}.\n\n"
            f"Objective: {objective_text}\n\nAcceptance criteria:\n- "
            + "\n- ".join(criteria)
        ),
        assignee=supervisor,
        created_by=supervisor,
        workspace_kind="scratch",
        priority=100,
        idempotency_key=f"mission-root:{key}",
        initial_status="running",
        session_id=session_id,
    )
    try:
        with kanban_db.write_txn(conn):
            conn.execute(
                """
                INSERT INTO kanban_missions (
                    id, root_task_id, idempotency_key, source_platform,
                    source_chat_id, source_thread_id, source_session_id, project_id,
                    objective, acceptance_criteria, constraints_json, non_goals_json,
                    provenance_json, repo_root, base_commit, source_branch,
                    dirty_snapshot_json, supervisor_profile, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    mission_id, root_task_id, key, platform, chat_id,
                    source_thread_id or "", session_id, selected_project,
                    objective_text, _json(criteria), _json(list(constraints)),
                    _json(list(non_goals)), _json(provenance), str(repo),
                    immutable_base, branch, _json(dirty), supervisor, now,
                ),
            )
            conn.execute(
                "INSERT INTO kanban_mission_tasks "
                "(mission_id, task_id, role, immutable_base_commit, created_at) "
                "VALUES (?, ?, 'root', ?, ?)",
                (mission_id, root_task_id, immutable_base, now),
            )
            # Keep the planning root scratch-only. Generic create_task turns a
            # project-linked scratch task into a code worktree, so attach the
            # already-validated project after creating the root card.
            conn.execute(
                "UPDATE tasks SET project_id = ? WHERE id = ?",
                (selected_project, root_task_id),
            )
            kanban_db._append_event(
                conn, root_task_id, "mission_receipt_created",
                {"mission_id": mission_id, "project_id": selected_project},
            )
    except sqlite3.IntegrityError:
        # Concurrent duplicate dispatches can both pass the optimistic lookup.
        # The UNIQUE receipt key is the final arbiter; collapse onto its winner
        # and remove only the losing call's orphan root task.
        winner = conn.execute(
            "SELECT * FROM kanban_missions WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if winner is None:
            raise
        requested = (platform, chat_id, source_thread_id or "", session_id, selected_project)
        binding = (
            winner["source_platform"], winner["source_chat_id"],
            winner["source_thread_id"], winner["source_session_id"],
            winner["project_id"],
        )
        if winner["root_task_id"] != root_task_id:
            kanban_db.delete_task(conn, root_task_id)
        if binding != requested:
            raise ValueError("idempotency_key is already bound to another source or project")
        kanban_db.add_notify_sub(
            conn,
            task_id=winner["root_task_id"],
            platform=platform,
            chat_id=chat_id,
            thread_id=source_thread_id or "",
            notifier_profile=notifier_profile,
        )
        return _mission_dict(winner), True
    kanban_db.add_notify_sub(
        conn,
        task_id=root_task_id,
        platform=platform,
        chat_id=chat_id,
        thread_id=source_thread_id or "",
        notifier_profile=notifier_profile,
    )
    receipt = get_mission(conn, mission_id)
    assert receipt is not None
    return receipt, False


def _mission_task_row(conn: sqlite3.Connection, mission_id: str, task_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM kanban_mission_tasks WHERE mission_id = ? AND task_id = ?",
        (mission_id, task_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"task {task_id} does not belong to mission {mission_id}")
    return row


def create_mission_child(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    title: str,
    body: str,
    assignee: str,
    role: str = "code",
    parent_task_ids: Iterable[str] = (),
    priority: int = 0,
) -> dict[str, Any]:
    """Create a code/integration card with a distinct immutable-base worktree."""
    mission = get_mission(conn, mission_id)
    if mission is None:
        raise ValueError(f"unknown mission {mission_id}")
    if mission["status"] != "active":
        raise ValueError("mission is not active")
    if role not in {"code", "integration"}:
        raise ValueError("mission child role must be 'code' or 'integration'")
    parent_ids = tuple(dict.fromkeys(str(p) for p in parent_task_ids if p))
    for parent_id in parent_ids:
        parent_row = _mission_task_row(conn, mission_id, parent_id)
        if parent_row["role"] == "root":
            raise ValueError("mission root cannot be a code dependency")

    repo = Path(mission["repo_root"])
    declared: list[str] = []
    if role == "integration":
        for parent_id in parent_ids:
            handoff = conn.execute(
                "SELECT commit_sha FROM kanban_mission_handoffs WHERE mission_id = ? AND task_id = ?",
                (mission_id, parent_id),
            ).fetchone()
            if handoff is None:
                raise ValueError(f"parent {parent_id} has no declared committed handoff")
            declared.append(handoff["commit_sha"])

    task_id = kanban_db.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        created_by=mission["supervisor_profile"],
        workspace_kind="worktree",
        workspace_path=str(repo),
        tenant=None,
        priority=priority,
        parents=parent_ids,
        session_id=mission["source"]["session_id"],
        project_id=mission["project_id"],
    )
    branch = f"mission/{mission_id}/{task_id}"
    target = repo / ".worktrees" / task_id
    allocated = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _git(
            repo, "worktree", "add", "-b", branch, str(target),
            mission["base_commit"],
        )
        allocated = True
        for parent_commit in declared:
            # Preserve each declared handoff SHA in integration ancestry. A
            # cherry-pick would copy the patch under a new SHA and make the
            # durable handoff impossible to verify from the resulting branch.
            _git(target, "merge", "--no-edit", "--no-ff", parent_commit)
    except Exception:
        if allocated:
            _git(target, "merge", "--abort", check=False)
            _git(repo, "worktree", "remove", "--force", str(target), check=False)
            _git(repo, "branch", "-D", branch, check=False)
        kanban_db.delete_task(conn, task_id)
        raise
    kanban_db.set_workspace_path(conn, task_id, target)
    kanban_db.set_branch_name(conn, task_id, branch)
    now = int(time.time())
    with kanban_db.write_txn(conn):
        conn.execute(
            "INSERT INTO kanban_mission_tasks "
            "(mission_id, task_id, role, immutable_base_commit, "
            "declared_parent_commits, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (mission_id, task_id, role, mission["base_commit"], _json(declared), now),
        )
        kanban_db._append_event(
            conn, task_id, "mission_worktree_allocated",
            {
                "mission_id": mission_id,
                "role": role,
                "base_commit": mission["base_commit"],
                "branch": branch,
                "declared_parent_commits": declared,
            },
        )
    task = kanban_db.get_task(conn, task_id)
    assert task is not None
    return {
        "task_id": task_id,
        "role": role,
        "workspace_path": task.workspace_path,
        "branch_name": task.branch_name,
        "base_commit": mission["base_commit"],
        "declared_parent_commits": declared,
    }


def record_worker_handoff(
    conn: sqlite3.Connection,
    mission_id: str,
    task_id: str,
    *,
    commit_sha: str,
    branch_name: str,
    evidence: dict[str, Any],
    submitted_by: str,
) -> dict[str, Any]:
    membership = _mission_task_row(conn, mission_id, task_id)
    if membership["role"] not in {"code", "integration"}:
        raise ValueError("only code-changing mission tasks can submit commit handoffs")
    task = kanban_db.get_task(conn, task_id)
    if task is None or not task.workspace_path:
        raise ValueError("mission task has no allocated worktree")
    if not evidence:
        raise ValueError("handoff evidence is required")
    branch = _required(branch_name, "branch_name")
    if branch != task.branch_name:
        raise ValueError("handoff branch does not match the allocated task branch")
    worktree = Path(task.workspace_path)
    commit = _commit(worktree, commit_sha)
    actual_branch = _git(worktree, "branch", "--show-current")
    if actual_branch != branch:
        raise ValueError("allocated worktree is not on the declared branch")
    if not _is_ancestor(worktree, membership["immutable_base_commit"], commit):
        raise ValueError("handoff commit is not descended from the immutable mission base")
    if not _is_ancestor(worktree, commit, branch):
        raise ValueError("handoff commit is not contained in the declared branch")
    existing = conn.execute(
        "SELECT * FROM kanban_mission_handoffs WHERE task_id = ?", (task_id,)
    ).fetchone()
    if existing:
        if existing["commit_sha"] != commit or existing["branch_name"] != branch:
            raise ValueError("task already has a different durable handoff")
        return {
            "task_id": task_id,
            "commit_sha": existing["commit_sha"],
            "branch_name": existing["branch_name"],
            "evidence": _loads(existing["evidence_json"], {}),
        }
    now = int(time.time())
    with kanban_db.write_txn(conn):
        inserted = conn.execute(
            "INSERT OR IGNORE INTO kanban_mission_handoffs "
            "(mission_id, task_id, commit_sha, branch_name, evidence_json, submitted_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mission_id, task_id, commit, branch, _json(evidence), submitted_by, now),
        )
        if inserted.rowcount == 1:
            kanban_db._append_event(
                conn, task_id, "mission_handoff_recorded",
                {"mission_id": mission_id, "commit_sha": commit, "branch": branch},
            )
    durable = conn.execute(
        "SELECT commit_sha, branch_name FROM kanban_mission_handoffs WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if durable is None or durable["commit_sha"] != commit or durable["branch_name"] != branch:
        raise ValueError("task already has a different durable handoff")
    return {
        "task_id": task_id,
        "commit_sha": commit,
        "branch_name": branch,
        "evidence": evidence,
    }


def steer_mission(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    task_id: str,
    source_platform: str,
    source_chat_id: str,
    source_thread_id: str,
    source_session_id: str,
    instruction: str,
) -> dict[str, Any]:
    mission = get_mission(conn, mission_id)
    if mission is None:
        raise ValueError(f"unknown mission {mission_id}")
    source = {
        "platform": _required(source_platform, "source_platform"),
        "chat_id": _required(source_chat_id, "source_chat_id"),
        "thread_id": str(source_thread_id or ""),
        "session_id": _required(source_session_id, "source_session_id"),
    }
    if source != mission["source"]:
        raise PermissionError("steering is scoped to the mission source chat and session")
    session_id = source["session_id"]
    _mission_task_row(conn, mission_id, task_id)
    text = _required(instruction, "instruction")
    now = int(time.time())
    with kanban_db.write_txn(conn):
        conn.execute(
            "INSERT INTO kanban_mission_steering "
            "(mission_id, task_id, source_session_id, instruction, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (mission_id, task_id, session_id, text, now),
        )
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (task_id, f"mission:{session_id}", text, now),
        )
        kanban_db._append_event(
            conn, task_id, "mission_steered", {"mission_id": mission_id},
        )
    return {"mission_id": mission_id, "task_id": task_id, "instruction": text}


def project_blocker(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    task_id: str,
) -> bool:
    """Project one precise blocker event onto the subscribed mission root."""
    mission = get_mission(conn, mission_id)
    if mission is None:
        raise ValueError(f"unknown mission {mission_id}")
    _mission_task_row(conn, mission_id, task_id)
    task = kanban_db.get_task(conn, task_id)
    if task is None or task.status != "blocked":
        raise ValueError("only an actually blocked mission task can be projected")
    blocked_event = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    canonical_payload = _loads(blocked_event["payload"], {}) if blocked_event else {}
    reason_text = _required(canonical_payload.get("reason"), "canonical blocker reason")
    fingerprint = hashlib.sha256(f"{task_id}\0{reason_text}".encode()).hexdigest()
    payload = {
        "mission_id": mission_id,
        "source_task_id": task_id,
        "reason": reason_text,
        "kind": canonical_payload.get("kind"),
    }
    now = int(time.time())
    try:
        with kanban_db.write_txn(conn):
            conn.execute(
                "INSERT INTO kanban_mission_projections "
                "(mission_id, task_id, kind, fingerprint, payload_json, created_at) "
                "VALUES (?, ?, 'blocker', ?, ?, ?)",
                (mission_id, task_id, fingerprint, _json(payload), now),
            )
            kanban_db._append_event(
                conn, mission["root_task_id"], "blocked", payload,
            )
    except sqlite3.IntegrityError:
        return False
    return True


def mission_status(conn: sqlite3.Connection, mission_id: str) -> dict[str, Any]:
    mission = get_mission(conn, mission_id)
    if mission is None:
        raise ValueError(f"unknown mission {mission_id}")
    rows = conn.execute(
        """
        SELECT mt.task_id, mt.role, mt.immutable_base_commit,
               mt.declared_parent_commits, t.title, t.status, t.assignee,
               t.branch_name, t.workspace_path,
               h.commit_sha AS handoff_commit, h.evidence_json
          FROM kanban_mission_tasks mt
          JOIN tasks t ON t.id = mt.task_id
          LEFT JOIN kanban_mission_handoffs h ON h.task_id = mt.task_id
         WHERE mt.mission_id = ?
         ORDER BY mt.created_at, mt.task_id
        """,
        (mission_id,),
    ).fetchall()
    tasks = [
        {
            "task_id": row["task_id"],
            "role": row["role"],
            "title": row["title"],
            "status": row["status"],
            "assignee": row["assignee"],
            "branch_name": row["branch_name"],
            "workspace_path": row["workspace_path"],
            "base_commit": row["immutable_base_commit"],
            "declared_parent_commits": _loads(row["declared_parent_commits"], []),
            "handoff_commit": row["handoff_commit"],
            "evidence": _loads(row["evidence_json"], None),
        }
        for row in rows
    ]
    blockers = conn.execute(
        "SELECT payload_json FROM kanban_mission_projections "
        "WHERE mission_id = ? AND kind = 'blocker' ORDER BY id",
        (mission_id,),
    ).fetchall()
    return {
        "mission": mission,
        "tasks": tasks,
        "blockers": [_loads(row["payload_json"], {}) for row in blockers],
    }


def sign_mission_completion(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    signer_profile: str,
    summary: str,
    evidence: dict[str, Any],
) -> bool:
    """Emit the sole final completion projection after Orca signoff."""
    mission = get_mission(conn, mission_id)
    if mission is None:
        raise ValueError(f"unknown mission {mission_id}")
    signer = kanban_db._canonical_assignee(_required(signer_profile, "signer_profile"))
    if signer != mission["supervisor_profile"] or signer != "orca":
        raise PermissionError("mission completion requires canonical Orca signoff")
    if mission["status"] == "completed":
        return False
    summary_text = _required(summary, "summary")
    if not evidence:
        raise ValueError("completion evidence is required")
    pending = conn.execute(
        """
        SELECT mt.task_id, mt.role, t.status,
               CASE WHEN h.task_id IS NULL THEN 0 ELSE 1 END AS has_handoff
          FROM kanban_mission_tasks mt
          JOIN tasks t ON t.id = mt.task_id
          LEFT JOIN kanban_mission_handoffs h ON h.task_id = mt.task_id
         WHERE mt.mission_id = ? AND mt.role IN ('code', 'integration')
           AND (t.status != 'done' OR h.task_id IS NULL)
        """,
        (mission_id,),
    ).fetchall()
    if pending:
        details = ", ".join(
            f"{row['task_id']}({row['status']},handoff={bool(row['has_handoff'])})"
            for row in pending
        )
        raise ValueError(f"mission has incomplete deliverables: {details}")
    metadata = {
        "mission_id": mission_id,
        "signed_by": signer,
        "evidence": evidence,
    }
    completed = kanban_db.complete_task(
        conn,
        mission["root_task_id"],
        result=summary_text,
        summary=summary_text,
        metadata=metadata,
        signoff_profile=signer,
    )
    if not completed:
        return False
    now = int(time.time())
    fingerprint = hashlib.sha256(f"{mission_id}\0completion".encode()).hexdigest()
    with kanban_db.write_txn(conn):
        conn.execute(
            "UPDATE kanban_missions SET status = 'completed', completed_at = ? WHERE id = ?",
            (now, mission_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO kanban_mission_projections "
            "(mission_id, task_id, kind, fingerprint, payload_json, created_at) "
            "VALUES (?, ?, 'completion', ?, ?, ?)",
            (
                mission_id, mission["root_task_id"], fingerprint,
                _json({"summary": summary_text, **metadata}), now,
            ),
        )
    return True
