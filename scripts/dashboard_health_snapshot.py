#!/usr/bin/env python3
"""Compact health snapshot for the Hermes dashboard/Desktop-parity service.

This script is intentionally read-only. It exists so Mini App operations can
separate dashboard/LSP bloat from Mini App or Orca work before restarting
anything.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

DASHBOARD_SERVICE = "hermes-dashboard-9132.service"
MINIAPP_SERVICE = "hermes-miniapp-v4.service"
GATEWAY_SERVICE = "hermes-gateway.service"
SESSIONS_DB = Path("/home/hermes-agent/workspace/active/hermes_miniapp_v4/sessions.db")
KANBAN_DB = Path(os.environ.get("HERMES_KANBAN_BOARD", "/home/hermes-agent/.hermes/kanban.db"))
SYSTEMCTL_FIELDS = (
    "Id,ActiveState,SubState,ExecMainPID,MemoryCurrent,TasksCurrent,NRestarts,Result"
)


def parse_systemctl_show(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def _run(args: list[str], timeout: int = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, text=True, capture_output=True, timeout=timeout)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip() or default)
    except Exception:
        return default


def collect_service(name: str) -> dict[str, Any]:
    proc = _run([
        "systemctl",
        "--user",
        "show",
        name,
        "--no-pager",
        "-p",
        SYSTEMCTL_FIELDS,
    ])
    parsed = parse_systemctl_show(proc.stdout)
    return {
        "id": parsed.get("Id", name),
        "active_state": parsed.get("ActiveState", "unknown"),
        "sub_state": parsed.get("SubState", "unknown"),
        "pid": _as_int(parsed.get("ExecMainPID")),
        "memory_bytes": _as_int(parsed.get("MemoryCurrent")),
        "memory_mib": round(_as_int(parsed.get("MemoryCurrent")) / 1024 / 1024, 1),
        "tasks": _as_int(parsed.get("TasksCurrent")),
        "n_restarts": _as_int(parsed.get("NRestarts")),
        "result": parsed.get("Result", ""),
        "error": proc.stderr.strip() if proc.returncode else "",
    }


def parse_free_m(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "Mem:" and len(parts) >= 7:
            result.update(
                mem_total_mib=_as_int(parts[1]),
                mem_used_mib=_as_int(parts[2]),
                mem_free_mib=_as_int(parts[3]),
                mem_available_mib=_as_int(parts[6]),
            )
        elif parts[0] == "Swap:" and len(parts) >= 4:
            total = _as_int(parts[1])
            used = _as_int(parts[2])
            result.update(
                swap_total_mib=total,
                swap_used_mib=used,
                swap_free_mib=_as_int(parts[3]),
                swap_used_pct=round((used / total * 100.0) if total else 0.0, 1),
            )
    return result


def collect_memory() -> dict[str, Any]:
    proc = _run(["free", "-m"])
    return parse_free_m(proc.stdout)


def parse_memory_pressure(text: str) -> dict[str, float]:
    result = {"some_avg10": 0.0, "some_avg60": 0.0, "some_avg300": 0.0, "full_avg10": 0.0, "full_avg60": 0.0, "full_avg300": 0.0}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        prefix = parts[0]
        if prefix not in {"some", "full"}:
            continue
        for part in parts[1:]:
            if part.startswith("avg10="):
                result[f"{prefix}_avg10"] = float(part.split("=", 1)[1])
            elif part.startswith("avg60="):
                result[f"{prefix}_avg60"] = float(part.split("=", 1)[1])
            elif part.startswith("avg300="):
                result[f"{prefix}_avg300"] = float(part.split("=", 1)[1])
    return result


def collect_pressure() -> dict[str, float]:
    try:
        return parse_memory_pressure(Path("/proc/pressure/memory").read_text())
    except Exception:
        return parse_memory_pressure("")


def _count_rows(db_path: Path, sql: str) -> dict[str, int]:
    if not db_path.exists():
        return {}
    try:
        con = sqlite3.connect(str(db_path), timeout=2)
        rows = con.execute(sql).fetchall()
        con.close()
    except Exception:
        return {}
    return {str(status): int(count) for status, count in rows}


def collect_miniapp_jobs() -> dict[str, int]:
    return _count_rows(
        SESSIONS_DB,
        """
        SELECT status, COUNT(*)
        FROM chat_jobs
        WHERE status IN ('queued','running','cancelling')
        GROUP BY status
        """,
    )


def collect_kanban_work() -> dict[str, int]:
    return _count_rows(
        KANBAN_DB,
        """
        SELECT status, COUNT(*)
        FROM tasks
        WHERE status IN ('triage','todo','scheduled','ready','running')
        GROUP BY status
        """,
    )


def summarize_pressure(
    *,
    dashboard_memory_bytes: int,
    dashboard_tasks: int,
    mem_available_mib: int,
    swap_free_mib: int,
    psi_some_avg60: float,
    memory_threshold_mib: int = 2500,
    tasks_threshold: int = 200,
) -> dict[str, Any]:
    dashboard_memory_mib = dashboard_memory_bytes / 1024 / 1024
    dashboard_bloated = dashboard_memory_mib >= memory_threshold_mib or dashboard_tasks >= tasks_threshold
    memory_tight = mem_available_mib <= 1536 or psi_some_avg60 > 0.1 or swap_free_mib <= 128
    return {
        "dashboard_bloated": dashboard_bloated,
        "memory_tight": memory_tight,
        "needs_cleanup": dashboard_bloated and memory_tight,
        "dashboard_memory_mib": round(dashboard_memory_mib, 1),
    }


def collect_snapshot() -> dict[str, Any]:
    services = {
        "dashboard": collect_service(DASHBOARD_SERVICE),
        "miniapp": collect_service(MINIAPP_SERVICE),
        "gateway": collect_service(GATEWAY_SERVICE),
    }
    memory = collect_memory()
    pressure = collect_pressure()
    summary = summarize_pressure(
        dashboard_memory_bytes=int(services["dashboard"].get("memory_bytes") or 0),
        dashboard_tasks=int(services["dashboard"].get("tasks") or 0),
        mem_available_mib=int(memory.get("mem_available_mib") or 0),
        swap_free_mib=int(memory.get("swap_free_mib") or 0),
        psi_some_avg60=float(pressure.get("some_avg60") or 0.0),
    )
    return {
        "services": services,
        "memory": memory,
        "pressure": pressure,
        "active_miniapp_jobs": collect_miniapp_jobs(),
        "active_kanban": collect_kanban_work(),
        "summary": summary,
    }


def _counts_text(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


def print_human(snapshot: dict[str, Any]) -> None:
    dash = snapshot["services"]["dashboard"]
    mini = snapshot["services"]["miniapp"]
    gw = snapshot["services"]["gateway"]
    mem = snapshot["memory"]
    psi = snapshot["pressure"]
    print("Dashboard/LSP health")
    print(f"Dashboard: {dash['active_state']}/{dash['sub_state']} pid={dash['pid']} memory={dash['memory_mib']}MiB tasks={dash['tasks']}")
    print(f"Mini App:  {mini['active_state']}/{mini['sub_state']} pid={mini['pid']} memory={mini['memory_mib']}MiB tasks={mini['tasks']}")
    print(f"Gateway:   {gw['active_state']}/{gw['sub_state']} pid={gw['pid']} memory={gw['memory_mib']}MiB tasks={gw['tasks']}")
    print(f"RAM available: {mem.get('mem_available_mib', 0)}MiB")
    print(f"Swap: {mem.get('swap_used_mib', 0)}MiB/{mem.get('swap_total_mib', 0)}MiB ({mem.get('swap_used_pct', 0)}%); free={mem.get('swap_free_mib', 0)}MiB")
    print(f"PSI memory: some avg10={psi.get('some_avg10', 0.0):.2f} avg60={psi.get('some_avg60', 0.0):.2f}; full avg10={psi.get('full_avg10', 0.0):.2f} avg60={psi.get('full_avg60', 0.0):.2f}")
    print(f"Mini App jobs: {_counts_text(snapshot['active_miniapp_jobs'])}")
    print(f"Orca/Kanban: {_counts_text(snapshot['active_kanban'])}")
    print(f"Summary: dashboard_bloated={snapshot['summary']['dashboard_bloated']} needs_cleanup={snapshot['summary']['needs_cleanup']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Hermes dashboard/LSP health snapshot")
    parser.add_argument("--human", action="store_true", help="Print compact human-readable output instead of JSON")
    args = parser.parse_args(argv)
    snapshot = collect_snapshot()
    if args.human:
        print_human(snapshot)
    else:
        print(json.dumps(snapshot, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
