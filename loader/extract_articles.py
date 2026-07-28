"""Extract published note-articles for the raw_articles table.

Filename convention (governed by CLAUDE.md naming rules):
    YYYYMMDD_slug.md
Files that do not match this convention are skipped explicitly, and the
skipped filenames are reported back to the caller (not silently dropped),
so the exclusion is auditable the same way as the git repo allowlist in
extract_git.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

FILENAME_PATTERN = re.compile(r"^(\d{8})_(.+)\.md$")
# Gate IDs used throughout CAREER-KPI docs look like G1-3, G2-1, G2-3, G2-4:
# first digit is 1 or 2, second digit 0-9. We take the first occurrence in
# the article body, whether or not it is preceded by a "gate:" label.
GATE_PATTERN = re.compile(r"G[12]-\d")
TITLE_PATTERN = re.compile(r"^#\s+(.+)$", re.MULTILINE)


@dataclass
class ArticleExtractionResult:
    rows: list[dict] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)


def extract_articles(published_dir) -> ArticleExtractionResult:
    """Extract article rows (raw_articles schema) from a published/ dir.

    Files whose name does not match FILENAME_PATTERN are skipped
    explicitly and recorded in ArticleExtractionResult.skipped_files.
    """
    published_dir = Path(published_dir)
    rows: list[dict] = []
    skipped: list[str] = []

    if not published_dir.exists():
        return ArticleExtractionResult(rows=rows, skipped_files=skipped)

    for path in sorted(published_dir.glob("*.md")):
        match = FILENAME_PATTERN.match(path.name)
        if not match:
            skipped.append(path.name)
            continue

        raw_date, slug = match.groups()
        published_date = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        text = path.read_text(encoding="utf-8")

        title_match = TITLE_PATTERN.search(text)
        title = title_match.group(1).strip() if title_match else slug

        gate_match = GATE_PATTERN.search(text)
        gate_id = gate_match.group(0) if gate_match else None

        rows.append(
            {
                "filename": path.name,
                "published_date": published_date,
                "title": title,
                "gate_id": gate_id,
                "char_count": len(text),
            }
        )

    return ArticleExtractionResult(rows=rows, skipped_files=skipped)
