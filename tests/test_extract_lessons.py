"""Tests for loader.extract_lessons.

All fixtures are synthetic LESSON-*.md files created under tmp_path -- no
real _ops/lessons/ content is read by tests (SPEC-agent-ops-warehouse.md
Section 2: fixtures are synthetic data only).
"""
from __future__ import annotations

from loader.extract_lessons import extract_lessons


def _write(path, name: str, content: str = "# Lesson\n\nbody\n") -> None:
    (path / name).write_text(content, encoding="utf-8")


def test_extract_lessons_active_case(tmp_path):
    _write(tmp_path, "LESSON-20260704-001_OGメタデータの三重罠.md")

    result = extract_lessons(tmp_path)

    assert len(result.rows) == 1
    row = result.rows[0]
    assert set(row) == {
        "lesson_id",
        "created_date",
        "seq",
        "title",
        "status",
        "graduated_to",
    }
    assert row["lesson_id"] == "LESSON-20260704-001"
    assert row["created_date"] == "2026-07-04"
    assert row["seq"] == 1
    assert row["title"] == "OGメタデータの三重罠"
    assert row["status"] == "active"
    assert row["graduated_to"] is None
    assert result.skipped_files == []


def test_extract_lessons_archive_subdir_marks_status_archived(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    _write(archive_dir, "LESSON-20260601-002_旧い学び.md")

    result = extract_lessons(tmp_path)

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["lesson_id"] == "LESSON-20260601-002"
    assert row["status"] == "archived"
    assert row["seq"] == 2


def test_extract_lessons_mixed_active_and_archived(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    _write(tmp_path, "LESSON-20260704-001_現行の学び.md")
    _write(archive_dir, "LESSON-20260601-001_卒業済みの学び.md")

    result = extract_lessons(tmp_path)

    statuses = {row["lesson_id"]: row["status"] for row in result.rows}
    assert statuses == {
        "LESSON-20260704-001": "active",
        "LESSON-20260601-001": "archived",
    }


def test_extract_lessons_skips_non_conforming_filename(tmp_path):
    _write(tmp_path, "LESSON_TEMPLATE.md")
    _write(tmp_path, "LESSON-20260704-001_通常の学び.md")

    result = extract_lessons(tmp_path)

    assert len(result.rows) == 1
    assert result.rows[0]["lesson_id"] == "LESSON-20260704-001"
    assert result.skipped_files == ["LESSON_TEMPLATE.md"]


def test_extract_lessons_missing_directory_returns_empty(tmp_path):
    missing_dir = tmp_path / "does-not-exist"

    result = extract_lessons(missing_dir)

    assert result.rows == []
    assert result.skipped_files == []
