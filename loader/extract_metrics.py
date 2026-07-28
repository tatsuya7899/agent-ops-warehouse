"""Extract the Section 1 monthly summary table from METRICS.md.

METRICS.md (SPEC-agent-ops-warehouse.md Section 3.1) is a hand-edited,
append-only Markdown log. Its "Section 1" table has 10 human-readable
columns; the raw_metrics_monthly schema (Section 3.2) only needs 9 of
them (the "profile clicks" column is intentionally not modeled -- it is
unobtainable without a paid X subscription per the source doc itself).

Column matching is by header name, not position, so the extractor keeps
working if columns are reordered as long as the header text is
unchanged. Values are cleaned in three ways before being cast to a raw
row:
  1. bold markers (`**value**`) are stripped
  2. "no data" markers ("—" / "取得不可" / "未計測" / "") become NULL
  3. composite values ("0(累計76人)") take the leading number only
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Header name -> raw_metrics_monthly column. Any header cell not listed
# here (e.g. "プロフィールクリック") is read but intentionally dropped.
COLUMN_TO_FIELD = {
    "月": "month",
    "note公開数": "note_articles",
    "note総view": "note_views",
    "スキ計": "note_likes",
    "コメント計": "note_comments",
    "X投稿数": "x_posts",
    "Xインプレ計": "x_impressions",
    "フォロワー増": "x_followers_total",
    "一行所見": "note_text",
}

SCHEMA_FIELDS = (
    "month",
    "note_articles",
    "note_views",
    "note_likes",
    "note_comments",
    "x_posts",
    "x_impressions",
    "x_followers_total",
    "note_text",
)

# String fields are kept verbatim (after bold-marker stripping); all
# other schema fields are numeric.
STRING_FIELDS = {"month", "note_text"}

NULL_MARKERS = {"—", "取得不可", "未計測", ""}
_LEADING_NUMBER_RE = re.compile(r"-?\d+")


@dataclass
class MetricsExtractionResult:
    rows: list[dict] = field(default_factory=list)


def extract_metrics(metrics_path) -> MetricsExtractionResult:
    """Extract monthly rows (raw_metrics_monthly schema) from METRICS.md.

    Returns an empty result (never raises) if the file is missing or the
    Section 1 summary table cannot be located -- the file is a manually
    edited log and its shape may not yet contain a matching table.
    """
    metrics_path = Path(metrics_path)
    if not metrics_path.exists():
        return MetricsExtractionResult(rows=[])

    lines = metrics_path.read_text(encoding="utf-8").splitlines()
    header_idx = _find_header_row(lines)
    if header_idx is None:
        return MetricsExtractionResult(rows=[])

    header_cells = [_clean_cell(c) for c in _split_row(lines[header_idx])]
    field_by_index = [COLUMN_TO_FIELD.get(name) for name in header_cells]

    idx = header_idx + 1
    if idx < len(lines) and _is_separator_row(lines[idx]):
        idx += 1

    rows: list[dict] = []
    while idx < len(lines) and lines[idx].strip().startswith("|"):
        cells = _split_row(lines[idx])
        row = {name: None for name in SCHEMA_FIELDS}
        for field_name, raw_cell in zip(field_by_index, cells):
            if field_name is None:
                continue
            row[field_name] = _parse_value(field_name, raw_cell)
        rows.append(row)
        idx += 1

    return MetricsExtractionResult(rows=rows)


def _find_header_row(lines: list[str]) -> int | None:
    """Locate the Section 1 monthly summary table by header content.

    Matching by content (not by preceding heading text) makes this
    robust to synthetic fixtures/future heading wording changes, as long
    as the table itself still has a "月" / "note公開数" header.
    """
    for i, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        cells = [_clean_cell(c) for c in _split_row(line)]
        if cells and cells[0] == "月" and "note公開数" in cells:
            return i
    return None


def _split_row(line: str) -> list[str]:
    parts = line.strip().split("|")
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


def _clean_cell(cell: str) -> str:
    return cell.strip().strip("*").strip()


def _is_separator_row(line: str) -> bool:
    cells = _split_row(line)
    if not cells:
        return False
    return all(re.match(r"^[\-: ]+$", c) for c in cells)


_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _parse_value(field_name: str, raw_cell: str):
    cleaned = _clean_cell(raw_cell)
    if field_name == "month":
        # BQ DATE columns need a full date; the table stores "YYYY-MM"
        # so the first day of the month is appended (schema note: "First
        # day of month"). Anything not matching the expected shape is
        # passed through verbatim rather than guessed at.
        return f"{cleaned}-01" if _MONTH_RE.match(cleaned) else cleaned
    if field_name in STRING_FIELDS:
        return cleaned
    if cleaned in NULL_MARKERS:
        return None
    match = _LEADING_NUMBER_RE.match(cleaned)
    if not match:
        return None
    return int(match.group(0))
