"""Extract the 投稿ログ (post log) table from X-STRATEGY.md for raw_x_posts.

Table shape (note-articles/X-STRATEGY.md "## 投稿ログ" section):
    | 投稿日 | 形態 | 対象/テーマ | URL |
The header row is located by content (not by the preceding "##" heading
text), the same robust-to-reordering pattern used by
extract_metrics._find_header_row, so this extractor keeps working if the
heading wording changes as long as the table itself still has a "投稿日"
/ "URL" header.

post_type is normalized from the bold-marked leading label in the "形態"
column (e.g. "**フロー(引用リポスト)** — 休眠明け1本目") to one of
flow / stock_single / stock_thread. A label that mentions neither
"フロー" nor "ストック" is undeterminable and normalizes to "flow"
(SPEC-agent-ops-warehouse.md Section 3.2 default).

Rows whose 投稿日/形態 cell is the literal placeholder "未記録" (not yet
logged) are skipped explicitly and reported back to the caller, the same
auditable pattern as extract_git.py / extract_articles.py.

char_count is always NULL: the post log records metadata about posts,
not their body text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

NULL_ROW_MARKER = "未記録"
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


@dataclass
class XPostExtractionResult:
    rows: list[dict] = field(default_factory=list)
    skipped_rows: list[str] = field(default_factory=list)


def extract_x_posts(x_strategy_path) -> XPostExtractionResult:
    """Extract post-log rows (raw_x_posts schema) from an X-STRATEGY.md file.

    Rows with an unparseable date or a "未記録" placeholder are skipped
    explicitly and recorded in XPostExtractionResult.skipped_rows.
    """
    x_strategy_path = Path(x_strategy_path)
    rows: list[dict] = []
    skipped: list[str] = []

    if not x_strategy_path.exists():
        return XPostExtractionResult(rows=rows, skipped_rows=skipped)

    lines = x_strategy_path.read_text(encoding="utf-8").splitlines()
    header_idx = _find_header_row(lines)
    if header_idx is None:
        return XPostExtractionResult(rows=rows, skipped_rows=skipped)

    idx = header_idx + 1
    if idx < len(lines) and _is_separator_row(lines[idx]):
        idx += 1

    while idx < len(lines) and lines[idx].strip().startswith("|"):
        line = lines[idx]
        idx += 1
        cells = _split_row(line)
        if len(cells) < 4:
            continue

        raw_date, raw_type, raw_theme, raw_url = cells[0], cells[1], cells[2], cells[3]
        if NULL_ROW_MARKER in raw_date or NULL_ROW_MARKER in raw_type:
            skipped.append(line.strip())
            continue

        posted_at = _parse_posted_at(raw_date)
        if posted_at is None:
            skipped.append(line.strip())
            continue

        rows.append(
            {
                "posted_at": posted_at,
                "post_type": _normalize_post_type(raw_type),
                "theme": raw_theme.strip(),
                "url": raw_url.strip(),
                "char_count": None,
            }
        )

    return XPostExtractionResult(rows=rows, skipped_rows=skipped)


def _find_header_row(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        cells = [_clean_cell(c) for c in _split_row(line)]
        if cells and cells[0] == "投稿日" and "URL" in cells:
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


def _parse_posted_at(raw_date: str) -> str | None:
    cleaned = _clean_cell(raw_date)
    if not _DATE_RE.match(cleaned):
        return None
    return f"{cleaned}T00:00:00Z"


def _normalize_post_type(raw_type: str) -> str:
    cleaned = raw_type.strip().replace("**", "")
    # Only the label portion before an em-dash comment is meaningful;
    # everything after "—" is free-text commentary (e.g. "休眠明け1本目").
    label = cleaned.split("—")[0].strip()
    if "ストック" in label:
        return "stock_thread" if "スレッド" in label else "stock_single"
    # "フロー" and any undeterminable label both normalize to "flow"
    # (SPEC Section 3.2: "判別不能は\"flow\"").
    return "flow"
