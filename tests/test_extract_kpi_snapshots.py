"""Tests for loader.extract_kpi_snapshots.

extract_kpi_snapshots reuses (never reimplements) the KPI functions in
~/Developer/_ops/scripts/generate_status.py (SPEC-agent-ops-warehouse.md Section
3.1: "generate_status.py の関数群を流用"). All fixtures (CAREER-KPI
files, published/ dirs, STATUS.md, and the fake generate_status.py
stubs used to exercise failure paths) are synthetic and created under
tmp_path -- no real career-KPI content is read by tests (Section 2).
"""
from __future__ import annotations

from datetime import date

from loader.extract_kpi_snapshots import extract_kpi_snapshots

FAKE_MODULE_OK = '''
def compute_kpi_c_progress(kpi_path):
    return (2, 3)


def compute_kpi_r_streak(published_dir, today=None):
    return 5


def compute_recent_two_week_count(published_dir, today=None):
    return 2


def compute_evidence_ship_count(published_dir, today=None):
    return 1


def compute_total_ship_count(text, this_month):
    return 9


def find_career_kpi_file():
    return None
'''

FAKE_MODULE_RAISING = '''
def compute_kpi_c_progress(kpi_path):
    raise RuntimeError("boom")


def compute_kpi_r_streak(published_dir, today=None):
    return 5


def compute_recent_two_week_count(published_dir, today=None):
    return 2


def compute_evidence_ship_count(published_dir, today=None):
    return 1


def compute_total_ship_count(text, this_month):
    return 9


def find_career_kpi_file():
    return None
'''


def _write_fake_ops(tmp_path, source: str):
    ops_dir = tmp_path / "fake_ops"
    ops_dir.mkdir()
    (ops_dir / "generate_status.py").write_text(source, encoding="utf-8")
    return ops_dir


def test_extract_kpi_snapshots_normal_case(tmp_path):
    ops_dir = _write_fake_ops(tmp_path, FAKE_MODULE_OK)
    kpi_path = tmp_path / "CAREER-KPI.md"
    kpi_path.write_text("## 2. x\n- [x] G1-3 a\n", encoding="utf-8")
    published_dir = tmp_path / "published"
    published_dir.mkdir()
    status_md = tmp_path / "STATUS.md"
    status_md.write_text("<!-- SHIP_COUNT: 3 (2026-07) -->\n", encoding="utf-8")

    result = extract_kpi_snapshots(
        kpi_path=kpi_path,
        published_dir=published_dir,
        status_md_path=status_md,
        ops_dir=ops_dir,
        today=date(2026, 7, 29),
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert set(row) == {
        "snapshot_date",
        "kpi_c_achieved",
        "kpi_c_total",
        "streak_weeks",
        "recent_two_week_pubs",
        "evidence_ships",
        "monthly_ships",
    }
    assert row["snapshot_date"] == "2026-07-29"
    assert row["kpi_c_achieved"] == 2
    assert row["kpi_c_total"] == 3
    assert row["streak_weeks"] == 5
    assert row["recent_two_week_pubs"] == 2
    assert row["evidence_ships"] == 1
    assert row["monthly_ships"] == 9
    assert "fail" not in result.note.lower()


def test_extract_kpi_snapshots_missing_ops_dir_returns_nulls(tmp_path):
    missing_ops_dir = tmp_path / "no-such-ops-dir"

    result = extract_kpi_snapshots(
        kpi_path=tmp_path / "CAREER-KPI.md",
        published_dir=tmp_path / "published",
        status_md_path=tmp_path / "STATUS.md",
        ops_dir=missing_ops_dir,
        today=date(2026, 7, 29),
    )

    row = result.rows[0]
    assert row["kpi_c_achieved"] is None
    assert row["kpi_c_total"] is None
    assert row["streak_weeks"] is None
    assert row["recent_two_week_pubs"] is None
    assert row["evidence_ships"] is None
    assert row["monthly_ships"] is None
    assert "not importable" in result.note


def test_extract_kpi_snapshots_function_raises_is_caught_per_field(tmp_path):
    ops_dir = _write_fake_ops(tmp_path, FAKE_MODULE_RAISING)
    published_dir = tmp_path / "published"
    published_dir.mkdir()
    status_md = tmp_path / "STATUS.md"
    status_md.write_text("<!-- SHIP_COUNT: 9 (2026-07) -->\n", encoding="utf-8")

    result = extract_kpi_snapshots(
        kpi_path=tmp_path / "CAREER-KPI.md",
        published_dir=published_dir,
        status_md_path=status_md,
        ops_dir=ops_dir,
        today=date(2026, 7, 29),
    )

    row = result.rows[0]
    assert row["kpi_c_achieved"] is None
    assert row["kpi_c_total"] is None
    # Other fields still computed -- one function raising must not take
    # down the whole snapshot row.
    assert row["streak_weeks"] == 5
    assert row["monthly_ships"] == 9
    assert "compute_kpi_c_progress" in result.note


def test_extract_kpi_snapshots_missing_inputs_recorded_in_note(tmp_path):
    ops_dir = _write_fake_ops(tmp_path, FAKE_MODULE_OK)

    result = extract_kpi_snapshots(ops_dir=ops_dir, today=date(2026, 7, 29))

    row = result.rows[0]
    assert row["kpi_c_achieved"] is None
    assert row["streak_weeks"] is None
    assert row["monthly_ships"] is None
    assert "kpi_path" in result.note
    assert "published_dir" in result.note
    assert "status_md_path" in result.note
