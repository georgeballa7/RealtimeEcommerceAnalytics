"""Offline migration discovery/validation tests; no database or AWS required."""
from pathlib import Path

import pytest

from ecommerce.infrastructure.postgres.migrations import discover_migrations


def test_discovers_two_sequential_migrations(tmp_path: Path):
    (tmp_path / '001_base.sql').write_text('CREATE TABLE first (id int);', encoding='utf-8')
    (tmp_path / '002_ledger.sql').write_text('CREATE TABLE second (id int);', encoding='utf-8')
    results = discover_migrations(tmp_path)
    assert [m.version for m in results] == ['001', '002']
    assert len(results[0].checksum) == 64


def test_rejects_gaps(tmp_path: Path):
    (tmp_path / '001_base.sql').write_text('SELECT 1;', encoding='utf-8')
    (tmp_path / '003_gap.sql').write_text('SELECT 1;', encoding='utf-8')
    with pytest.raises(ValueError, match='continuous'):
        discover_migrations(tmp_path)


def test_rejects_duplicate_versions(tmp_path: Path):
    (tmp_path / '001_a.sql').write_text('SELECT 1;', encoding='utf-8')
    (tmp_path / '001_b.sql').write_text('SELECT 2;', encoding='utf-8')
    with pytest.raises(ValueError, match='Duplicate'):
        discover_migrations(tmp_path)


def test_rejects_unexpected_sql_names(tmp_path: Path):
    (tmp_path / 'invalid_name.sql').write_text('SELECT 1;', encoding='utf-8')
    with pytest.raises(ValueError, match='Unexpected'):
        discover_migrations(tmp_path)


def test_rejects_empty_sql(tmp_path: Path):
    (tmp_path / '001_base.sql').write_text(' ', encoding='utf-8')
    with pytest.raises(ValueError, match='Empty'):
        discover_migrations(tmp_path)
