# Dashboard/LSP Memory Stability Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Stop the Hermes dashboard/Desktop-parity backend from ballooning memory/tasks and causing Mini App operational alerts or child-spawn failures.

**Architecture:** Add observability first, then a safe idle-only containment path, then fix lifecycle leaks in the dashboard/TUI gateway session + LSP/helper management. Keep Mini App and gateway restarts separate; dashboard cleanup must never interrupt active Mini App chat jobs or Orca/Kanban workers.

**Tech Stack:** Python, Hermes dashboard/TUI gateway, systemd user services, SQLite live Mini App DB, Hermes Kanban DB, pytest via `scripts/run_tests.sh`.

---

## Current Evidence

Live incident evidence from 2026-07-19:

- `hermes-dashboard-9132.service` grew to roughly `3.8GiB` memory and `336` tasks.
- Restarting only `hermes-dashboard-9132.service` dropped it to roughly `159MiB` and `4` tasks.
- Mini App was small during incidents, typically `~100-300MiB`, and restarting Mini App did not reclaim the large dashboard/LSP footprint.
- Major swap holder was `Hermes dashboard + LSP`, around `~2GiB` swap at peak.
- Alert was operationally relevant once RAM available dropped near `~1.1-1.4GiB`, swap was full, and PSI had nonzero recent averages.
- Source paths of interest:
  - Dashboard service unit: `/home/openclaw/.config/systemd/user/hermes-dashboard-9132.service`
  - Hermes repo: `/home/hermes-agent/.hermes/hermes-agent`
  - Dashboard server: `hermes_cli/web_server.py`
  - TUI/Desktop gateway: `tui_gateway/server.py`
  - Config defaults: `hermes_cli/config.py`
  - Gateway cap code: `gateway/run.py`
  - Active Mini App DB: `/home/hermes-agent/workspace/active/hermes_miniapp_v4/sessions.db`
  - Kanban DB: `/home/hermes-agent/.hermes/kanban.db`

## Safety Invariants

- Never restart Mini App unless Josh explicitly asks or an idle watcher proves no active chat jobs.
- Dashboard restarts are safer than Mini App restarts, but still should avoid active Desktop-parity sessions when possible.
- Any automated cleanup must first check:
  - Mini App `chat_jobs.status IN ('queued','running','cancelling')` is empty.
  - Kanban `tasks.status IN ('triage','todo','scheduled','ready','running')` is empty or below an explicitly allowed threshold.
  - Dashboard has no running session/child turn if we add a reliable dashboard status endpoint.
- Do not clear zram as a primary fix. Treat zram reset as cosmetic/high-risk unless RAM and PSI are healthy and Josh explicitly asks.

---

## Task 1: Add a dashboard health snapshot script

**Objective:** Create a repeatable local command that captures dashboard memory/task/session/LSP state without restarting anything.

**Files:**
- Create: `scripts/dashboard_health_snapshot.py`
- Test: `tests/hermes_cli/test_dashboard_health_snapshot.py`

**Step 1: Write failing tests**

Add tests that exercise pure parsing helpers only; do not require real systemd in unit tests.

Expected helpers:

```python
from scripts.dashboard_health_snapshot import parse_systemctl_show, summarize_pressure


def test_parse_systemctl_show_basic_fields():
    text = "ExecMainPID=123\nMemoryCurrent=2500000000\nTasksCurrent=222\n"
    parsed = parse_systemctl_show(text)
    assert parsed["ExecMainPID"] == "123"
    assert parsed["MemoryCurrent"] == "2500000000"
    assert parsed["TasksCurrent"] == "222"


def test_summarize_pressure_marks_dashboard_bloat():
    summary = summarize_pressure(
        dashboard_memory_bytes=3_000_000_000,
        dashboard_tasks=220,
        mem_available_mib=1400,
        swap_free_mib=25,
        psi_some_avg60=0.2,
    )
    assert summary["dashboard_bloated"] is True
    assert summary["needs_cleanup"] is True
```

**Step 2: Run tests to verify failure**

Run:

```bash
cd /home/hermes-agent/.hermes/hermes-agent
scripts/run_tests.sh tests/hermes_cli/test_dashboard_health_snapshot.py -q
```

Expected: FAIL because the script/helper does not exist.

**Step 3: Implement script**

`scripts/dashboard_health_snapshot.py` should:

- Run `systemctl --user show hermes-dashboard-9132.service hermes-miniapp-v4.service hermes-gateway.service` with fields `Id,ActiveState,SubState,ExecMainPID,MemoryCurrent,TasksCurrent,NRestarts,Result`.
- Read `free -m` and `/proc/pressure/memory`.
- Query active Mini App jobs from `/home/hermes-agent/workspace/active/hermes_miniapp_v4/sessions.db`.
- Query active Kanban work from `/home/hermes-agent/.hermes/kanban.db`.
- Print compact JSON by default; support `--human` for a short table.
- Never print secrets or full env.

**Step 4: Run tests to verify pass**

Run:

```bash
cd /home/hermes-agent/.hermes/hermes-agent
scripts/run_tests.sh tests/hermes_cli/test_dashboard_health_snapshot.py -q
```

Expected: PASS.

**Step 5: Manual smoke**

Run:

```bash
cd /home/hermes-agent/.hermes/hermes-agent
python scripts/dashboard_health_snapshot.py --human
python scripts/dashboard_health_snapshot.py | python -m json.tool >/tmp/dashboard-health.json
```

Expected: readable short output and valid JSON.

---

## Task 2: Add an idle-only dashboard cleanup script

**Objective:** Provide a safe operational cleanup script that restarts only dashboard/LSP when bloated and idle.

**Files:**
- Create: `scripts/safe_dashboard_restart_if_idle.py`
- Test: `tests/hermes_cli/test_safe_dashboard_restart_if_idle.py`

**Step 1: Write failing tests**

Test pure decision logic:

```python
from scripts.safe_dashboard_restart_if_idle import should_restart_dashboard


def test_refuses_when_miniapp_job_running():
    decision = should_restart_dashboard(
        dashboard_memory_bytes=3_500_000_000,
        dashboard_tasks=300,
        active_miniapp_jobs={"running": 1},
        active_kanban={}),
    assert decision.restart is False
    assert "Mini App jobs active" in decision.reason


def test_restarts_when_dashboard_bloated_and_idle():
    decision = should_restart_dashboard(
        dashboard_memory_bytes=3_500_000_000,
        dashboard_tasks=300,
        active_miniapp_jobs={},
        active_kanban={},
    )
    assert decision.restart is True
```

**Step 2: Run tests to verify failure**

```bash
cd /home/hermes-agent/.hermes/hermes-agent
scripts/run_tests.sh tests/hermes_cli/test_safe_dashboard_restart_if_idle.py -q
```

Expected: FAIL.

**Step 3: Implement script**

Behavior:

- Default mode is dry-run; `--apply` performs restart.
- Thresholds:
  - `--memory-mib 2500`
  - `--tasks 200`
  - restart if either threshold is exceeded.
- Refuse restart if Mini App jobs active.
- Refuse restart if Kanban running/ready/todo active unless `--allow-kanban-idle-backlog` is set. For now, default should be conservative.
- Restart command: `systemctl --user restart hermes-dashboard-9132.service`.
- After restart, verify `ActiveState=active`, `SubState=running`, PID changed, and memory/tasks reduced.
- Output one compact message, e.g.:

```text
✅ Restarted Hermes dashboard/LSP: memory 3604MiB→180MiB, tasks 336→4. Mini App/gateway untouched.
```

or:

```text
No action: dashboard 900MiB/80 tasks below threshold.
```

**Step 4: Run tests**

```bash
cd /home/hermes-agent/.hermes/hermes-agent
scripts/run_tests.sh tests/hermes_cli/test_safe_dashboard_restart_if_idle.py -q
```

Expected: PASS.

**Step 5: Manual dry-run**

```bash
cd /home/hermes-agent/.hermes/hermes-agent
python scripts/safe_dashboard_restart_if_idle.py
```

Expected: no restart unless `--apply` is passed.

---

## Task 3: Wire a temporary cron watchdog for dashboard bloat

**Objective:** Stop manual babysitting while the durable code fix is underway.

**Files:**
- Create: `~/.hermes/scripts/dashboard_bloat_watchdog.py` or reuse `scripts/safe_dashboard_restart_if_idle.py` via cron `script`.
- Modify: Hermes cron job config through `hermes cron` / `cronjob` tool.

**Step 1: Create script-only cron**

Use `no_agent=True`. Schedule every 10-15 minutes initially.

Output rules:

- Empty stdout when healthy/no action.
- Send a compact message only when it actually restarts dashboard or refuses due to active work while bloat is severe.

**Step 2: Validate silent healthy run**

Run script directly on current healthy-ish dashboard state.

Expected: empty stdout or one-line dry-run reason depending mode.

**Step 3: Validate refusal path**

Mock or add a test for active Mini App job/active Kanban state.

Expected: no restart.

**Step 4: Validate applied restart path only when idle**

Only run `--apply` manually after verifying idle. Confirm dashboard PID changes and Mini App PID does not.

---

## Task 4: Add dashboard runtime telemetry endpoint

**Objective:** Make the dashboard tell us its own session/LSP/child state instead of inferring from process counts.

**Files:**
- Modify: `tui_gateway/server.py`
- Modify: `hermes_cli/web_server.py` if routing is needed there.
- Test: `tests/tui_gateway/test_protocol.py` or `tests/hermes_cli/test_web_server.py`

**Step 1: Add JSON-RPC method or HTTP route**

Add a small status payload containing:

```json
{
  "sessions_total": 0,
  "sessions_running": 0,
  "sessions_detached": 0,
  "sessions_evictable": 0,
  "active_child_runs": 0,
  "child_mirrors": 0,
  "max_live_sessions": 16
}
```

Do not include prompts, system prompts, messages, secrets, env, or tokens.

**Step 2: Test status shape**

Add tests proving the payload has counts only and no transcript fields.

**Step 3: Wire cleanup script to this endpoint**

Use it to avoid dashboard restarts during active dashboard child runs.

---

## Task 5: Tighten detached session eviction

**Objective:** Ensure stale Desktop/dashboard sessions are evicted before they accumulate for hours.

**Files:**
- Modify: `tui_gateway/server.py:780-875`
- Modify: `hermes_cli/config.py:912-920`
- Test: `tests/tui_gateway/test_protocol.py`

**Step 1: Add tests around `_enforce_session_cap()`**

Existing relevant tests include:

- `test_enforce_session_cap_evicts_oldest_detached_only`
- `test_enforce_session_cap_disabled_is_noop`

Add a test that simulates many detached sessions and verifies cap enforcement closes enough sessions after disconnect/session completion.

**Step 2: Consider lowering default cap for dashboard/Desktop-parity service**

Current default in `hermes_cli/config.py`:

```python
"max_live_sessions": 16
```

Plan candidate:

- Keep global default `16` if upstream compatibility matters.
- Set local service config/drop-in for this host to `max_live_sessions: 6-8`, or add dashboard-specific config if supported.

**Step 3: Ensure cap enforcement runs after disconnect and turn completion**

Search for `_schedule_session_cap_enforcement()` call sites and add missing call after dashboard session detach/close paths if needed.

**Step 4: Verify**

```bash
cd /home/hermes-agent/.hermes/hermes-agent
scripts/run_tests.sh tests/tui_gateway/test_protocol.py -q
```

Expected: PASS.

---

## Task 6: Stop giant `session.info` payload logging

**Objective:** Avoid huge dashboard logs and possible retained payload strings containing full skills/tool/system prompt data.

**Files:**
- Modify: `tui_gateway/server.py`
- Possibly modify: `hermes_logging.py`
- Test: `tests/tui_gateway/test_protocol.py`

**Step 1: Locate `session.info` emission**

Search:

```bash
cd /home/hermes-agent/.hermes/hermes-agent
python - <<'PY'
from pathlib import Path
for p in Path('.').rglob('*.py'):
    if any(part in {'.git','venv','.venv','node_modules','.worktrees'} for part in p.parts):
        continue
    text = p.read_text(errors='ignore')
    if 'session.info' in text:
        print(p)
PY
```

**Step 2: Add test that logs are summarized/redacted**

The event sent to clients may still need rich fields, but logs should not print the full event body. Add a logger helper that summarizes high-volume event payloads.

Expected log shape:

```text
session.info session_id=... model=... provider=... tools_count=... skills_count=... system_prompt_chars=...
```

not the entire system prompt/tools/skills payload.

**Step 3: Verify no behavior regression**

Existing clients should still receive necessary `session.info` data over WebSocket.

---

## Task 7: Audit and cap LSP helper lifecycle

**Objective:** Prevent TypeScript/Pyright LSP helpers from multiplying or surviving after their owning dashboard session is detached/evicted.

**Files:**
- Search first. Likely areas:
  - `agent/lsp/*`
  - `tui_gateway/server.py`
  - `hermes_cli/web_server.py`
- Test:
  - `tests/agent/lsp/test_shell_linter_lsp_skip.py`
  - add focused lifecycle tests if a manager exists.

**Step 1: Find LSP manager**

Search:

```bash
cd /home/hermes-agent/.hermes/hermes-agent
python - <<'PY'
from pathlib import Path
for p in Path('.').rglob('*.py'):
    if any(part in {'.git','venv','.venv','node_modules','.worktrees'} for part in p.parts):
        continue
    text = p.read_text(errors='ignore').lower()
    if 'pyright' in text or 'tsserver' in text or 'lsp' in text:
        print(p)
PY
```

**Step 2: Add owner tracking**

If LSP processes are launched per session/worktree, tag them with owner session/worktree and last-used timestamp.

**Step 3: Add idle reap**

Reap LSP helpers when:

- owner session closes/evicts, or
- idle longer than threshold, e.g. 15-30 minutes, or
- dashboard process exceeds task cap.

**Step 4: Verify process cleanup**

Use tests for manager state and manual smoke with `pgrep -af 'pyright|tsserver'` before/after dashboard session close.

---

## Task 8: Add local systemd safety guardrails

**Objective:** Prevent dashboard bloat from starving the machine while code fixes roll out.

**Files:**
- Create user drop-in:
  - `/home/openclaw/.config/systemd/user/hermes-dashboard-9132.service.d/20-memory-guard.conf`

**Step 1: Add soft guard first**

Candidate drop-in:

```ini
[Service]
MemoryHigh=2500M
MemoryMax=4000M
TasksMax=450
```

Do not set `MemoryMax` too low; if exceeded, systemd may kill the dashboard mid-chat. The idle watchdog should be the main cleanup path.

**Step 2: Reload only systemd manager**

```bash
systemctl --user daemon-reload
systemctl --user show hermes-dashboard-9132.service -p MemoryHigh -p MemoryMax -p TasksMax --no-pager
```

**Step 3: Restart dashboard only during idle window**

Use the safe dashboard restart script, not a blind restart.

---

## Task 9: End-to-end validation under load

**Objective:** Prove the fix survives realistic Mini App/Desktop-parity usage.

**Files:**
- Create: `tests/hermes_cli/test_dashboard_memory_regression_smoke.py` if feasible, or document manual smoke if memory assertions are too environment-specific.

**Manual smoke sequence:**

1. Record baseline:
   ```bash
   python scripts/dashboard_health_snapshot.py --human
   ```
2. Run 5-10 Mini App Desktop-parity chat turns in the Lance chat or a test chat.
3. Record post-run snapshot.
4. Verify:
   - dashboard tasks do not grow unbounded;
   - detached sessions are evicted;
   - LSP process count stabilizes;
   - Mini App chat jobs complete;
   - no child spawn cap errors.
5. Trigger `python scripts/safe_dashboard_restart_if_idle.py` in dry-run mode.
6. If idle and over threshold, run `--apply` and verify Mini App/gateway PIDs unchanged.

---

## Task 10: Rollout and monitoring

**Objective:** Deploy without breaking live operations.

**Step 1: Commit code changes in small commits**

Suggested sequence:

```bash
git add scripts/dashboard_health_snapshot.py tests/hermes_cli/test_dashboard_health_snapshot.py
git commit -m "ops: add dashboard health snapshot"

git add scripts/safe_dashboard_restart_if_idle.py tests/hermes_cli/test_safe_dashboard_restart_if_idle.py
git commit -m "ops: add idle-safe dashboard cleanup"

git add tui_gateway/server.py hermes_cli/config.py tests/tui_gateway/test_protocol.py
git commit -m "fix: prune stale dashboard sessions"
```

**Step 2: Run focused gates**

```bash
cd /home/hermes-agent/.hermes/hermes-agent
scripts/run_tests.sh tests/hermes_cli/test_dashboard_health_snapshot.py tests/hermes_cli/test_safe_dashboard_restart_if_idle.py -q
scripts/run_tests.sh tests/tui_gateway/test_protocol.py -q
scripts/run_tests.sh tests/hermes_cli/test_web_server.py -q
git diff --check
```

**Step 3: Live rollout boundary**

- Do not restart Mini App.
- Restart dashboard only after confirming no active Mini App jobs and no running Kanban tasks, or use idle-safe dashboard cleanup script.
- Verify:
  ```bash
  systemctl --user show hermes-dashboard-9132.service --no-pager -p ActiveState -p SubState -p ExecMainPID -p MemoryCurrent -p TasksCurrent
  curl -fsS --max-time 5 http://127.0.0.1:8787/health
  python scripts/dashboard_health_snapshot.py --human
  ```

**Step 4: Keep the temporary cron until code fix is proven**

After 24-48 hours without bloat alerts, either loosen or remove the dashboard cleanup cron.

---

## Open Questions Before Implementation

1. Is `hermes-dashboard-9132.service` intended to be long-lived production Desktop-parity backend, or should Mini App eventually own a separate canonical dashboard service profile?
2. Should dashboard cleanup be automatic, or should it alert Josh first and wait for explicit approval?
3. What is the acceptable active Desktop-parity session cap for this host: `6`, `8`, or `16`?
4. Should LSP be enabled for Mini App Desktop-parity chats by default, or disabled unless the current task needs code editing?

## Recommended Immediate Path

- Implement Tasks 1-3 first as operations containment.
- Then implement Tasks 4-7 as durable root-cause fixes.
- Add Task 8 systemd guardrails only after the idle-safe cleanup script exists.
- Do not continue relying on Mini App restarts for this class of issue.
