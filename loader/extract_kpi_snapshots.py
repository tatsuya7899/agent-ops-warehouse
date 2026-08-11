"""Extract a single KPI snapshot row (raw_kpi_snapshots) by reusing --
never reimplementing -- the compute_* functions already implemented in
~/Developer/_ops/generate_status.py (SPEC-agent-ops-warehouse.md Section
3.1: "generate_status.py の関数群を流用").

generate_status.py is loaded from an explicit file path via
importlib.util (not by mutating sys.path and doing a plain `import`), so
tests can point ops_dir at a synthetic stub module to exercise the
"module not importable" / "a compute_* function raises" failure paths
without ever touching the real script, and without one test's stub
polluting sys.modules for the next (SPEC Section 2: fixtures are
synthetic data only).

Every value this extractor cannot compute -- missing ops module, missing
input path, or an exception raised by a reused function -- becomes NULL
in the row, and the reason is appended to KpiSnapshotResult.note for the
raw_load_runs ledger. One function failing must not sink the whole
snapshot row; each field is computed independently.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

DEFAULT_OPS_DIR = Path.home() / "Developer" / "_ops"

SCHEMA_FIELDS = (
    "kpi_c_achieved",
    "kpi_c_total",
    "streak_weeks",
    "recent_two_week_pubs",
    "evidence_ships",
    "monthly_ships",
)


@dataclass
class KpiSnapshotResult:
    rows: list[dict] = field(default_factory=list)
    note: str = ""


def extract_kpi_snapshots(
    kpi_path=None,
    published_dir=None,
    status_md_path=None,
    ops_dir=None,
    today: date | None = None,
) -> KpiSnapshotResult:
    """Build one raw_kpi_snapshots row for `today` (defaults to date.today()).

    kpi_path / published_dir / status_md_path may be left None; when the
    reused generate_status module exposes find_career_kpi_file() /
    DEVELOPER_DIR / STATUS_PATH, those resolve real-world defaults
    (mirroring generate_status.py's own conventions). Any input that
    stays unresolved, and any compute_* call that raises, is recorded in
    `note` instead of raising.
    """
    # Naive local date.today() is deliberate here, not an oversight: this
    # mirrors generate_status.py's own convention (every compute_*
    # function below defaults `today` the same naive way), and this
    # loader is a manually-triggered local CLI tool (SPEC Section 4:
    # "ロードは手動トリガー"), so "today" means the operator's wall clock,
    # not UTC.
    today = today or date.today()  # noqa: DTZ011
    row = {"snapshot_date": today.isoformat(), **dict.fromkeys(SCHEMA_FIELDS)}
    reasons: list[str] = []

    gs = _load_generate_status_module(ops_dir)
    if gs is None:
        reasons.append(f"generate_status.py not importable from {ops_dir or DEFAULT_OPS_DIR}")
        return KpiSnapshotResult(rows=[row], note="; ".join(reasons))

    if kpi_path is None:
        kpi_path = _safe_call(gs, "find_career_kpi_file")
    if published_dir is None:
        developer_dir = getattr(gs, "DEVELOPER_DIR", None)
        published_dir = developer_dir / "note-articles" / "published" if developer_dir else None
    if status_md_path is None:
        status_md_path = getattr(gs, "STATUS_PATH", None)

    _fill_kpi_c(gs, kpi_path, row, reasons)
    _fill_published_dir_fields(gs, published_dir, today, row, reasons)
    _fill_monthly_ships(gs, status_md_path, today, row, reasons)

    note = "; ".join(reasons) if reasons else "all KPI fields computed via generate_status.py"
    return KpiSnapshotResult(rows=[row], note=note)


def _fill_kpi_c(gs, kpi_path, row: dict, reasons: list[str]) -> None:
    if kpi_path is None:
        reasons.append("kpi_path not provided (no CAREER-KPI file found)")
        return
    try:
        progress = gs.compute_kpi_c_progress(Path(kpi_path))
    except Exception as exc:  # noqa: BLE001 -- one function's failure must not sink the row
        reasons.append(f"compute_kpi_c_progress raised {exc!r}")
        return
    if progress is None:
        reasons.append("compute_kpi_c_progress returned None (file missing/unreadable)")
        return
    row["kpi_c_achieved"], row["kpi_c_total"] = progress


def _fill_published_dir_fields(gs, published_dir, today, row: dict, reasons: list[str]) -> None:
    if published_dir is None:
        reasons.append("published_dir not provided")
        return
    for row_field, func_name in (
        ("streak_weeks", "compute_kpi_r_streak"),
        ("recent_two_week_pubs", "compute_recent_two_week_count"),
        ("evidence_ships", "compute_evidence_ship_count"),
    ):
        try:
            row[row_field] = getattr(gs, func_name)(Path(published_dir), today=today)
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"{func_name} raised {exc!r}")


def _fill_monthly_ships(gs, status_md_path, today, row: dict, reasons: list[str]) -> None:
    if status_md_path is None:
        reasons.append("status_md_path not provided")
        return
    status_path = Path(status_md_path)
    if not status_path.exists():
        reasons.append(f"status_md_path does not exist: {status_path}")
        return
    try:
        text = status_path.read_text(encoding="utf-8")
        row["monthly_ships"] = gs.compute_total_ship_count(text, today.strftime("%Y-%m"))
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"parse_ship_count raised {exc!r}")


def _load_generate_status_module(ops_dir=None):
    ops_dir = Path(ops_dir) if ops_dir else DEFAULT_OPS_DIR
    module_path = ops_dir / "generate_status.py"
    if not module_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "agent_ops_warehouse_generate_status", module_path
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 -- an unloadable module must degrade to None, not raise
        return None
    return module


def _safe_call(module, func_name: str):
    func = getattr(module, func_name, None)
    if func is None:
        return None
    try:
        return func()
    except Exception:  # noqa: BLE001 -- default-resolution helper must degrade to None, not raise
        return None
