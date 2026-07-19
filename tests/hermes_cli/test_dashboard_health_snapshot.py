from scripts.dashboard_health_snapshot import (
    parse_free_m,
    parse_memory_pressure,
    parse_systemctl_show,
    summarize_pressure,
)


def test_parse_systemctl_show_basic_fields():
    text = "ExecMainPID=123\nMemoryCurrent=2500000000\nTasksCurrent=222\n"

    parsed = parse_systemctl_show(text)

    assert parsed["ExecMainPID"] == "123"
    assert parsed["MemoryCurrent"] == "2500000000"
    assert parsed["TasksCurrent"] == "222"


def test_parse_free_m_extracts_memory_and_swap():
    text = """
               total        used        free      shared  buff/cache   available
Mem:            7838        6400         201         532        2081        1438
Swap:           4095        4072          23
"""

    parsed = parse_free_m(text)

    assert parsed["mem_available_mib"] == 1438
    assert parsed["swap_total_mib"] == 4095
    assert parsed["swap_used_mib"] == 4072
    assert parsed["swap_free_mib"] == 23
    assert parsed["swap_used_pct"] == 99.4


def test_parse_memory_pressure_extracts_avg_values():
    parsed = parse_memory_pressure(
        "some avg10=0.00 avg60=0.17 avg300=0.28 total=131163241\n"
        "full avg10=0.00 avg60=0.17 avg300=0.27 total=113643809\n"
    )

    assert parsed["some_avg10"] == 0.0
    assert parsed["some_avg60"] == 0.17
    assert parsed["full_avg300"] == 0.27


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


def test_summarize_pressure_ignores_healthy_dashboard():
    summary = summarize_pressure(
        dashboard_memory_bytes=300_000_000,
        dashboard_tasks=10,
        mem_available_mib=4000,
        swap_free_mib=2000,
        psi_some_avg60=0.0,
    )

    assert summary["dashboard_bloated"] is False
    assert summary["needs_cleanup"] is False
