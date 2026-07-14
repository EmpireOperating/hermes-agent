"""Behavior contracts for terminal-worker supervisor signoff routing."""

import json

from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt


def _isolated_board(tmp_path, monkeypatch):
    db_path = tmp_path / "supervisor-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    return db_path


def _claimed_task(conn, *, title="worker deliverable", assignee="worker"):
    task_id = kb.create_task(conn, title=title, assignee=assignee)
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    return task_id, claimed


def test_terminal_worker_handoff_waits_for_orca_signoff(tmp_path, monkeypatch):
    _isolated_board(tmp_path, monkeypatch)
    monkeypatch.setattr(
        kt,
        "load_config",
        lambda: {"kanban": {"supervisor_profile": "orca"}},
    )
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "orca")

    with kb.connect() as conn:
        task_id, claimed = _claimed_task(conn)

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    worker_out = json.loads(
        kt._handle_complete(
            {
                "summary": "implementation and focused tests are complete",
                "metadata": {
                    "tests_passed": 7,
                    "artifacts": ["/tmp/worker-report.html"],
                },
            }
        )
    )
    assert worker_out["ok"] is True
    assert worker_out["status"] == "review"
    assert worker_out["supervisor_profile"] == "orca"

    with kb.connect() as conn:
        review_task = kb.get_task(conn, task_id)
        assert review_task is not None
        assert review_task.status == "review"
        assert review_task.assignee == "orca"
        assert review_task.current_step_key == kb.SUPERVISOR_SIGNOFF_STEP
        events = kb.list_events(conn, task_id)
        assert "review_requested" in [event.kind for event in events]
        assert "completed" not in [event.kind for event in events]
        submitted_run = kb.latest_run(conn, task_id)
        assert submitted_run.outcome == "submitted_for_review"
        assert submitted_run.metadata["submitted_by"] == "worker"

        supervisor_claim = kb.claim_review_task(conn, task_id)
        assert supervisor_claim is not None

    monkeypatch.setenv(
        "HERMES_KANBAN_RUN_ID", str(supervisor_claim.current_run_id)
    )
    monkeypatch.setenv("HERMES_PROFILE", "orca")
    supervisor_out = json.loads(
        kt._handle_complete(
            {
                "summary": "independently verified implementation and tests",
                "metadata": {"reviewed_tests": 7},
            }
        )
    )
    assert supervisor_out["ok"] is True
    assert supervisor_out["status"] == "done"
    assert supervisor_out["supervisor_signoff"] == "orca"

    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "done"
        completed = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "completed"
        ]
        assert len(completed) == 1
        assert completed[0].payload is not None
        assert completed[0].payload["supervisor_signoff"] == "orca"
        assert completed[0].payload["artifacts"] == ["/tmp/worker-report.html"]


def test_reject_or_qa_followup_uses_existing_parent_gate(tmp_path, monkeypatch):
    _isolated_board(tmp_path, monkeypatch)
    with kb.connect() as conn:
        task_id, claimed = _claimed_task(conn)
        assert kb.submit_task_for_supervisor_review(
            conn,
            task_id,
            supervisor_profile="orca",
            summary="worker handoff",
            expected_run_id=claimed.current_run_id,
        )
        supervisor = kb.claim_review_task(conn, task_id)
        assert supervisor is not None

        qa_id = kb.create_task(conn, title="run browser QA", assignee="qa")
        kb.link_tasks(conn, parent_id=qa_id, child_id=task_id)
        assert kb.block_task(
            conn,
            task_id,
            reason="QA evidence required before approval",
            kind="dependency",
            expected_run_id=supervisor.current_run_id,
        )
        waiting = kb.get_task(conn, task_id)
        assert waiting.status == "todo"
        assert waiting.current_step_key == kb.SUPERVISOR_SIGNOFF_STEP
        assert waiting.assignee == "orca"

        assert kb.complete_task(conn, qa_id, summary="QA passed")
        resumed = kb.get_task(conn, task_id)
        assert resumed.status == "ready"
        assert resumed.current_step_key == kb.SUPERVISOR_SIGNOFF_STEP
        assert resumed.assignee == "orca"


def test_missing_supervisor_profile_fails_without_finalizing(tmp_path, monkeypatch):
    _isolated_board(tmp_path, monkeypatch)
    monkeypatch.setattr(
        kt,
        "load_config",
        lambda: {"kanban": {"supervisor_profile": "missing-reviewer"}},
    )
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: False)

    with kb.connect() as conn:
        task_id, claimed = _claimed_task(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

    output = json.loads(kt._handle_complete({"summary": "worker handoff"}))
    assert "error" in output
    assert "does not resolve to an installed Hermes profile" in output["error"]
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "running"
        assert not [event for event in kb.list_events(conn, task_id) if event.kind == "completed"]


def test_empty_supervisor_config_preserves_direct_completion(tmp_path, monkeypatch):
    _isolated_board(tmp_path, monkeypatch)
    monkeypatch.setattr(
        kt,
        "load_config",
        lambda: {"kanban": {"supervisor_profile": ""}},
    )

    with kb.connect() as conn:
        task_id, claimed = _claimed_task(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

    output = json.loads(kt._handle_complete({"summary": "legacy completion"}))
    assert output["ok"] is True
    assert output["status"] == "done"
    assert output["supervisor_signoff"] is None
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "done"
        events = kb.list_events(conn, task_id)
        assert not [event for event in events if event.kind == "review_requested"]
        assert len([event for event in events if event.kind == "completed"]) == 1


def test_review_spawn_failure_retries_without_losing_review_identity(
    tmp_path, monkeypatch,
):
    _isolated_board(tmp_path, monkeypatch)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    with kb.connect() as conn:
        task_id, claimed = _claimed_task(conn)
        assert kb.submit_task_for_supervisor_review(
            conn,
            task_id,
            supervisor_profile="orca",
            summary="worker handoff",
            expected_run_id=claimed.current_run_id,
        )

        def broken_spawn(*_args, **_kwargs):
            raise RuntimeError("review tooling unavailable")

        result = kb.dispatch_once(
            conn,
            spawn_fn=broken_spawn,
            failure_limit=2,
        )
        retriable = kb.get_task(conn, task_id)
        assert retriable.status == "ready"
        assert retriable.assignee == "orca"
        assert retriable.current_step_key == kb.SUPERVISOR_SIGNOFF_STEP
        assert retriable.consecutive_failures == 1
        assert not result.auto_blocked


def test_supervisor_budget_timeout_retries_without_losing_review_identity(
    tmp_path, monkeypatch,
):
    _isolated_board(tmp_path, monkeypatch)
    with kb.connect() as conn:
        task_id, claimed = _claimed_task(conn)
        assert kb.submit_task_for_supervisor_review(
            conn,
            task_id,
            supervisor_profile="orca",
            summary="worker handoff",
            expected_run_id=claimed.current_run_id,
        )
        supervisor = kb.claim_review_task(conn, task_id)
        assert supervisor is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = 424242, max_runtime_seconds = 1 "
                "WHERE id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid = 424242, started_at = 1, "
                "max_runtime_seconds = 1 WHERE id = ?",
                (supervisor.current_run_id,),
            )

        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        assert kb.enforce_max_runtime(conn, signal_fn=lambda *_args: None) == [task_id]
        retry = kb.get_task(conn, task_id)
        assert retry is not None
        assert retry.status == "ready"
        assert retry.assignee == "orca"
        assert retry.current_step_key == kb.SUPERVISOR_SIGNOFF_STEP
        assert retry.consecutive_failures == 1
        timed_out = [
            e for e in kb.list_events(conn, task_id) if e.kind == "timed_out"
        ]
        assert timed_out[-1].payload["supervisor_retry"] is True


def test_supervisor_crash_retries_without_user_facing_failure(tmp_path, monkeypatch):
    _isolated_board(tmp_path, monkeypatch)
    with kb.connect() as conn:
        task_id, claimed = _claimed_task(conn)
        assert kb.submit_task_for_supervisor_review(
            conn,
            task_id,
            supervisor_profile="orca",
            summary="worker handoff",
            expected_run_id=claimed.current_run_id,
        )
        supervisor = kb.claim_review_task(conn, task_id)
        assert supervisor is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = 424243, started_at = 1 WHERE id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid = 424243, started_at = 1 "
                "WHERE id = ?",
                (supervisor.current_run_id,),
            )

        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        assert kb.detect_crashed_workers(conn) == [task_id]
        retry = kb.get_task(conn, task_id)
        assert retry is not None
        assert retry.status == "ready"
        assert retry.assignee == "orca"
        assert retry.current_step_key == kb.SUPERVISOR_SIGNOFF_STEP
        crashed = [
            e for e in kb.list_events(conn, task_id) if e.kind == "crashed"
        ]
        assert crashed[-1].payload is not None
        assert crashed[-1].payload["supervisor_retry"] is True
        assert not [e for e in kb.list_events(conn, task_id) if e.kind == "gave_up"]


def test_supervisor_context_names_all_supported_outcomes(tmp_path, monkeypatch):
    _isolated_board(tmp_path, monkeypatch)
    with kb.connect() as conn:
        task_id, claimed = _claimed_task(conn)
        assert kb.submit_task_for_supervisor_review(
            conn,
            task_id,
            supervisor_profile="orca",
            summary="worker handoff",
            expected_run_id=claimed.current_run_id,
        )
        context = kb.build_worker_context(conn, task_id)

    assert "Supervisor signoff protocol" in context
    assert "Approve" in context
    assert "Reject or request QA" in context
    assert "kanban_block(kind=\"dependency\")" in context
    assert "needs_input" in context
    assert "capability" in context


def test_non_terminal_worker_completion_bypasses_supervisor_gate(
    tmp_path, monkeypatch,
):
    _isolated_board(tmp_path, monkeypatch)
    monkeypatch.setattr(
        kt, "load_config", lambda: {"kanban": {"supervisor_profile": "orca"}}
    )
    with kb.connect() as conn:
        task_id, claimed = _claimed_task(conn, title="internal implementation")
        child_id = kb.create_task(
            conn, title="internal QA", assignee="qa", parents=[task_id]
        )
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    output = json.loads(kt._handle_complete({"summary": "implementation ready"}))
    assert output["ok"] is True
    assert output["status"] == "done"
    assert output["supervisor_signoff"] is None
    with kb.connect() as conn:
        assert kb.get_task(conn, child_id).status == "ready"
        assert not [
            e for e in kb.list_events(conn, task_id)
            if e.kind == "review_requested"
        ]


def test_supervisor_owned_terminal_task_signs_off_without_recursive_review(
    tmp_path, monkeypatch,
):
    _isolated_board(tmp_path, monkeypatch)
    monkeypatch.setattr(
        kt, "load_config", lambda: {"kanban": {"supervisor_profile": "Orca"}}
    )
    with kb.connect() as conn:
        task_id, claimed = _claimed_task(
            conn, title="root mission", assignee="orca"
        )
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_PROFILE", "ORCA")
    output = json.loads(kt._handle_complete({"summary": "final review passed"}))
    assert output["ok"] is True
    assert output["status"] == "done"
    assert output["supervisor_signoff"] == "orca"
    with kb.connect() as conn:
        events = kb.list_events(conn, task_id)
        assert not [e for e in events if e.kind == "review_requested"]
        completed = [e for e in events if e.kind == "completed"]
        assert completed[-1].payload["supervisor_signoff"] == "orca"


def test_foreign_profile_cannot_complete_supervisor_review(tmp_path, monkeypatch):
    _isolated_board(tmp_path, monkeypatch)
    monkeypatch.setattr(
        kt, "load_config", lambda: {"kanban": {"supervisor_profile": "orca"}}
    )
    with kb.connect() as conn:
        task_id, claimed = _claimed_task(conn)
        assert kb.submit_task_for_supervisor_review(
            conn,
            task_id,
            supervisor_profile="orca",
            summary="worker handoff",
            expected_run_id=claimed.current_run_id,
        )
        supervisor = kb.claim_review_task(conn, task_id)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(supervisor.current_run_id))
    monkeypatch.setenv("HERMES_PROFILE", "foreign-orchestrator")
    output = json.loads(
        kt._handle_complete({"task_id": task_id, "summary": "looks fine"})
    )
    assert "error" in output
    assert "requires active HERMES_PROFILE='orca'" in output["error"]
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "running"
        assert not [
            e for e in kb.list_events(conn, task_id) if e.kind == "completed"
        ]


def test_goal_mode_supervisor_can_block_on_hard_capability(
    tmp_path, monkeypatch,
):
    _isolated_board(tmp_path, monkeypatch)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="root mission", assignee="worker", goal_mode=True
        )
        claimed = kb.claim_task(conn, task_id)
        assert kb.submit_task_for_supervisor_review(
            conn,
            task_id,
            supervisor_profile="orca",
            summary="worker handoff",
            expected_run_id=claimed.current_run_id,
        )
        supervisor = kb.claim_review_task(conn, task_id)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(supervisor.current_run_id))
    monkeypatch.setenv("HERMES_PROFILE", "orca")
    output = json.loads(
        kt._handle_block(
            {"reason": "physical device access required", "kind": "capability"}
        )
    )
    assert output["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.current_step_key == kb.SUPERVISOR_SIGNOFF_STEP


def test_foreign_profile_cannot_emit_supervisor_human_block(
    tmp_path, monkeypatch,
):
    _isolated_board(tmp_path, monkeypatch)
    with kb.connect() as conn:
        task_id, claimed = _claimed_task(conn)
        assert kb.submit_task_for_supervisor_review(
            conn,
            task_id,
            supervisor_profile="orca",
            summary="worker handoff",
            expected_run_id=claimed.current_run_id,
        )
        supervisor = kb.claim_review_task(conn, task_id)
        assert supervisor is not None
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(supervisor.current_run_id))
    monkeypatch.setenv("HERMES_PROFILE", "foreign-orchestrator")

    output = json.loads(
        kt._handle_block(
            {
                "task_id": task_id,
                "reason": "pretend human operation",
                "kind": "capability",
            }
        )
    )
    assert "error" in output
    assert "requires active HERMES_PROFILE='orca'" in output["error"]
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert not [
            e for e in kb.list_events(conn, task_id) if e.kind == "blocked"
        ]


def test_supervisor_failure_breaker_keeps_review_retryable(tmp_path, monkeypatch):
    _isolated_board(tmp_path, monkeypatch)
    with kb.connect() as conn:
        task_id, claimed = _claimed_task(conn)
        assert kb.submit_task_for_supervisor_review(
            conn,
            task_id,
            supervisor_profile="orca",
            summary="worker handoff",
            expected_run_id=claimed.current_run_id,
        )
        kb.claim_review_task(conn, task_id)
        assert kb._record_task_failure(
            conn,
            task_id,
            error="supervisor runtime unavailable",
            outcome="spawn_failed",
            failure_limit=1,
            release_claim=True,
            end_run=True,
        ) is False
        task = kb.get_task(conn, task_id)
        assert task.status == "ready"
        assert task.assignee == "orca"
        assert task.current_step_key == kb.SUPERVISOR_SIGNOFF_STEP
        failures = [
            e for e in kb.list_events(conn, task_id)
            if e.kind == "spawn_failed"
        ]
        assert failures[-1].payload["supervisor_retry"] is True
        assert not [e for e in kb.list_events(conn, task_id) if e.kind == "gave_up"]
