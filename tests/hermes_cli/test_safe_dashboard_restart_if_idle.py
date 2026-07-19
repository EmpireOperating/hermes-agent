from scripts.safe_dashboard_restart_if_idle import should_restart_dashboard


def test_refuses_when_miniapp_job_running():
    decision = should_restart_dashboard(
        dashboard_memory_bytes=3_500_000_000,
        dashboard_tasks=300,
        active_miniapp_jobs={"running": 1},
        active_kanban={},
    )

    assert decision.restart is False
    assert decision.active_work is True
    assert "Mini App jobs active" in decision.reason


def test_refuses_when_kanban_running():
    decision = should_restart_dashboard(
        dashboard_memory_bytes=3_500_000_000,
        dashboard_tasks=300,
        active_miniapp_jobs={},
        active_kanban={"running": 1},
    )

    assert decision.restart is False
    assert decision.active_work is True
    assert "Orca/Kanban work active" in decision.reason


def test_restarts_when_dashboard_bloated_and_idle():
    decision = should_restart_dashboard(
        dashboard_memory_bytes=3_500_000_000,
        dashboard_tasks=300,
        active_miniapp_jobs={},
        active_kanban={},
    )

    assert decision.restart is True
    assert decision.dashboard_bloated is True
    assert decision.active_work is False


def test_does_not_restart_when_below_threshold():
    decision = should_restart_dashboard(
        dashboard_memory_bytes=500_000_000,
        dashboard_tasks=20,
        active_miniapp_jobs={},
        active_kanban={},
    )

    assert decision.restart is False
    assert decision.dashboard_bloated is False
    assert "below threshold" in decision.reason


def test_can_ignore_idle_kanban_backlog_but_not_running():
    ready_only = should_restart_dashboard(
        dashboard_memory_bytes=3_500_000_000,
        dashboard_tasks=300,
        active_miniapp_jobs={},
        active_kanban={"ready": 2, "todo": 5},
        allow_kanban_idle_backlog=True,
    )
    running = should_restart_dashboard(
        dashboard_memory_bytes=3_500_000_000,
        dashboard_tasks=300,
        active_miniapp_jobs={},
        active_kanban={"ready": 2, "running": 1},
        allow_kanban_idle_backlog=True,
    )

    assert ready_only.restart is True
    assert running.restart is False
