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
import time
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


def _proc_stat(pid: int) -> tuple[int, str] | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except Exception:
        return None
    # comm is wrapped in parens and may contain spaces. The parent pid is the
    # fourth field after the closing paren: "pid (comm) state ppid ...".
    try:
        after = text.rsplit(")", 1)[1].strip().split()
        return int(after[1]), str(after[0])
    except Exception:
        return None


def collect_descendants(root_pid: int) -> dict[str, Any]:
    if root_pid <= 0:
        return {"total_processes": 0, "total_threads": 0, "by_comm": {}, "by_state": {}, "top": []}
    children: dict[int, list[int]] = {}
    states: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        parsed = _proc_stat(pid)
        if parsed is None:
            continue
        ppid, state = parsed
        children.setdefault(ppid, []).append(pid)
        states[pid] = state

    descendants: list[int] = []
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        descendants.append(pid)
        stack.extend(children.get(pid, []))

    by_comm: dict[str, dict[str, int]] = {}
    by_state: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    now = time.time()
    ticks = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK"))
    boot_time = 0.0
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                boot_time = float(line.split()[1])
                break
    except Exception:
        pass
    for pid in descendants:
        proc_dir = Path(f"/proc/{pid}")
        try:
            comm = (proc_dir / "comm").read_text().strip() or "unknown"
        except Exception:
            comm = "unknown"
        state = states.get(pid, "?")
        try:
            status_lines = (proc_dir / "status").read_text().splitlines()
            threads = next((int(line.split()[1]) for line in status_lines if line.startswith("Threads:")), 1)
        except Exception:
            threads = 1
        etimes = 0
        try:
            parts = (proc_dir / "stat").read_text().rsplit(")", 1)[1].strip().split()
            start_ticks = int(parts[19])
            if boot_time:
                etimes = max(0, int(now - (boot_time + start_ticks / ticks)))
        except Exception:
            pass
        by_comm.setdefault(comm, {"processes": 0, "threads": 0})
        by_comm[comm]["processes"] += 1
        by_comm[comm]["threads"] += threads
        by_state[state] = by_state.get(state, 0) + 1
        rows.append({"pid": pid, "comm": comm, "state": state, "threads": threads, "etimes": etimes})
    rows.sort(key=lambda row: int(row.get("threads") or 0), reverse=True)
    return {
        "total_processes": len(descendants),
        "total_threads": sum(int(row.get("threads") or 0) for row in rows),
        "by_comm": by_comm,
        "by_state": by_state,
        "top": rows[:12],
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
    dashboard_descendants = collect_descendants(int(services["dashboard"].get("pid") or 0))
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
        "dashboard_descendants": dashboard_descendants,
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
    desc = snapshot.get("dashboard_descendants") or {}
    by_comm = desc.get("by_comm") or {}
    if by_comm:
        parts = []
        for name, counts in sorted(by_comm.items(), key=lambda item: int((item[1] or {}).get("threads") or 0), reverse=True)[:5]:
            parts.append(f"{name}={counts.get('processes', 0)}p/{counts.get('threads', 0)}t")
        print(f"Dashboard descendants: {desc.get('total_processes', 0)}p/{desc.get('total_threads', 0)}t; " + ", ".join(parts))
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
