from pathlib import Path

import pytest

import sect.core.db as db


def write_migrations(path: Path, *names: str) -> None:
    for name in names:
        (path / name).write_text("SELECT 1;", encoding="utf-8")


def test_migration_files_accepts_contiguous_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_migrations(tmp_path, "0002_peaks.sql", "0001_base.sql")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", tmp_path)

    assert [name for name, _ in db.migration_files()] == ["0001_base.sql", "0002_peaks.sql"]


def test_migration_files_rejects_duplicate_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_migrations(tmp_path, "0001_base.sql", "0001_other.sql")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", tmp_path)

    with pytest.raises(ValueError, match=r"0001_base\.sql.*0001_other\.sql"):
        db.migration_files()


def test_migration_files_rejects_gaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_migrations(tmp_path, "0001_base.sql", "0003_peaks.sql")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", tmp_path)

    with pytest.raises(ValueError, match=r"0003_peaks\.sql"):
        db.migration_files()
