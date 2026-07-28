"""Extract LESSON-*.md filenames for the raw_lessons table.

Filename convention (governed by CLAUDE.md naming rules):
    LESSON-{YYYYMMDD}-{seq}_{title}.md
Files that do not match this convention (e.g. LESSON_TEMPLATE.md) are
skipped explicitly, and the skipped filenames are reported back to the
caller, the same auditable pattern as extract_git.py / extract_articles.py.

Only the filename is parsed. LESSON body text is intentionally not
parsed in v1 (SPEC-agent-ops-warehouse.md Section 11: LESSON body
wording is inconsistent, so `graduated_to` is left NULL rather than
guessed from free text).

Both the active set (files directly under the given directory) and the
archived set (files under an `archive/` subdirectory) are scanned, per
SPEC Section 3.1: "active+archive両方を分母に含める" -- 30-day forced
graduation would otherwise keep shrinking the active-only denominator.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

FILENAME_PATTERN = re.compile(r"^LESSON-(\d{8})-(\d{3})_(.+)\.md$")


@dataclass
class LessonExtractionResult:
    rows: list[dict] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)


def extract_lessons(lessons_dir) -> "LessonExtractionResult":
    """Extract lesson rows (raw_lessons schema) from a lessons/ dir.

    Files whose name does not match FILENAME_PATTERN are skipped
    explicitly and recorded in LessonExtractionResult.skipped_files.
    """
    lessons_dir = Path(lessons_dir)
    rows: list[dict] = []
    skipped: list[str] = []

    if not lessons_dir.exists():
        return LessonExtractionResult(rows=rows, skipped_files=skipped)

    _scan_directory(lessons_dir, status="active", rows=rows, skipped=skipped)

    archive_dir = lessons_dir / "archive"
    if archive_dir.exists():
        _scan_directory(archive_dir, status="archived", rows=rows, skipped=skipped)

    return LessonExtractionResult(rows=rows, skipped_files=skipped)


def _scan_directory(directory: Path, status: str, rows: list[dict], skipped: list[str]) -> None:
    for path in sorted(directory.glob("*.md")):
        match = FILENAME_PATTERN.match(path.name)
        if not match:
            skipped.append(path.name)
            continue

        raw_date, seq, title = match.groups()
        created_date = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        lesson_id = f"LESSON-{raw_date}-{seq}"

        rows.append(
            {
                "lesson_id": lesson_id,
                "created_date": created_date,
                "seq": int(seq),
                "title": title,
                "status": status,
                # v1 does not parse LESSON body text (see module docstring),
                # so graduation destination is always unknown at load time.
                "graduated_to": None,
            }
        )
