#!/usr/bin/env python3
"""Idle-safe cleanup for Hermes dashboard/LSP bloat.

Default mode is dry-run. Pass --apply to restart only the dashboard service,
and only when live Mini App and Orca/Kanban work are idle.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dashboard_health_snapshot import DASHBOARD_SERVICE, collect_snapshot, print_human


@dataclass(frozen=True)
class RestartDecision:
    restart: bool
    reason: str
    dashboard_bloated: bool
    active_work: bool


def _total(counts: dict[str, int]) -> int:
    return sum(int(v) for v in counts.values())


def should_restart_dashboard(
    *,
    dashboard_memory_bytes: int,
    dashboard_tasks: int,
    active_miniapp_jobs: dict[str, int],
    active_kanban: dict[str, int],
    memory_threshold_mib: int = 2500,
    tasks_threshold: int = 200,
    allow_kanban_idle_backlog: bool = False,
) -> RestartDecision:
    memory_mib = dashboard_memory_bytes / 1024 / 1024
    memory_bloated = memory_mib >= memory_threshold_mib
    tasks_bloated = dashboard_tasks >= tasks_threshold
    dashboard_bloated = memory_bloated or tasks_bloated
    if not dashboard_bloated:
        return RestartDecision(
            restart=False,
            reason=f"dashboard below threshold: {memory_mib:.0f}MiB/{dashboard_tasks} tasks",
            dashboard_bloated=False,
            active_work=False,
        )

    if _total(active_miniapp_jobs):
        return RestartDecision(
            restart=False,
            reason=f"Mini App jobs active: {active_miniapp_jobs}",
            dashboard_bloated=True,
            active_work=True,
        )

    kanban_counts = dict(active_kanban)
    if allow_kanban_idle_backlog:
        kanban_counts.pop("todo", None)
        kanban_counts.pop("ready", None)
        kanban_counts.pop("triage", None)
        kanban_counts.pop("scheduled", None)
    if _total(kanban_counts):
        return RestartDecision(
            restart=False,
            reason=f"Orca/Kanban work active: {active_kanban}",
            dashboard_bloated=True,
            active_work=True,
        )

    reasons = []
    if memory_bloated:
        reasons.append(f"memory {memory_mib:.0f}MiB >= {memory_threshold_mib}MiB")
    if tasks_bloated:
        reasons.append(f"tasks {dashboard_tasks} >= {tasks_threshold}")
    return RestartDecision(
        restart=True,
        reason="dashboard bloated and idle: " + "; ".join(reasons),
        dashboard_bloated=True,
        active_work=False,
    )


def decision_from_snapshot(
    snapshot: dict[str, Any],
    *,
    memory_threshold_mib: int,
    tasks_threshold: int,
    allow_kanban_idle_backlog: bool,
) -> RestartDecision:
    dashboard = snapshot["services"]["dashboard"]
    return should_restart_dashboard(
        dashboard_memory_bytes=int(dashboard.get("memory_bytes") or 0),
        dashboard_tasks=int(dashboard.get("tasks") or 0),
        active_miniapp_jobs=snapshot.get("active_miniapp_jobs") or {},
        active_kanban=snapshot.get("active_kanban") or {},
        memory_threshold_mib=memory_threshold_mib,
        tasks_threshold=tasks_threshold,
        allow_kanban_idle_backlog=allow_kanban_idle_backlog,
    )


def restart_dashboard() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", "restart", DASHBOARD_SERVICE],
        check=False,
        text=True,
        capture_output=True,
        timeout=60,
    )


def format_restart_message(before: dict[str, Any], after: dict[str, Any]) -> str:
    b = before["services"]["dashboard"]
    a = after["services"]["dashboard"]
    return (
        "✅ Restarted Hermes dashboard/LSP: "
        f"memory {b['memory_mib']:.0f}MiB→{a['memory_mib']:.0f}MiB, "
        f"tasks {b['tasks']}→{a['tasks']}. Mini App/gateway untouched."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restart Hermes dashboard only if bloated and idle")
    parser.add_argument("--apply", action="store_true", help="Actually restart dashboard; default is dry-run")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument("--memory-mib", type=int, default=2500)
    parser.add_argument("--tasks", type=int, default=200)
    parser.add_argument(
        "--allow-kanban-idle-backlog",
        action="store_true",
        help="Ignore non-running Kanban backlog statuses when deciding whether dashboard cleanup is safe",
    )
    args = parser.parse_args(argv)

    before = collect_snapshot()
    decision = decision_from_snapshot(
        before,
        memory_threshold_mib=args.memory_mib,
        tasks_threshold=args.tasks,
        allow_kanban_idle_backlog=args.allow_kanban_idle_backlog,
    )

    payload: dict[str, Any] = {"decision": asdict(decision), "applied": False}
    if not decision.restart:
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(f"No action: {decision.reason}")
        return 0

    if not args.apply:
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(f"Would restart dashboard: {decision.reason}")
        return 0

    proc = restart_dashboard()
    payload["restart_returncode"] = proc.returncode
    if proc.returncode != 0:
        payload["stderr"] = proc.stderr.strip()
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(f"Failed to restart dashboard: {proc.stderr.strip() or proc.returncode}")
        return 1

    time.sleep(8)
    after = collect_snapshot()
    payload["applied"] = True
    payload["after"] = after
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(format_restart_message(before, after))
        if after["services"]["dashboard"].get("active_state") != "active":
            print_human(after)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
