"""Tests for integrity-checked and paired OHLCV release bundles."""
import json
import sqlite3
from pathlib import Path
import zipfile

import pytest

from ohlcv_release_bundle import create_bundle, extract_bundle, verify_bundle, verify_files


def _db(path: Path, value="AAA"):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE bars (ticker TEXT NOT NULL, close REAL NOT NULL)")
        connection.execute("INSERT INTO bars VALUES (?, 1.0)", (value,))


def _universe(path: Path, ticker="AAA"):
    path.write_text(json.dumps({"version": 1, "tickers": [ticker]}), encoding="utf-8")


def test_create_verify_and_extract_bundle_round_trip(tmp_path):
    source_db, source_universe = tmp_path / "source.sqlite", tmp_path / "source.json"
    _db(source_db, "NEW")
    _universe(source_universe, "NEW")
    bundle = tmp_path / "snapshot.zip"
    create_bundle(source_db, source_universe, bundle)
    assert verify_bundle(bundle)["status"] == "ok"
    target_db, target_universe = tmp_path / "restored.sqlite", tmp_path / "restored.json"
    extract_bundle(bundle, target_db, target_universe)
    with sqlite3.connect(target_db) as connection:
        assert connection.execute("SELECT ticker FROM bars").fetchone()[0] == "NEW"
    assert json.loads(target_universe.read_text())["tickers"] == ["NEW"]


def test_create_rejects_corrupt_sqlite_before_bundle_creation(tmp_path):
    db, universe, bundle = tmp_path / "bad.sqlite", tmp_path / "universe.json", tmp_path / "out.zip"
    db.write_bytes(b"not sqlite")
    _universe(universe)
    with pytest.raises(ValueError, match="SQLite-Datenbank"):
        create_bundle(db, universe, bundle)
    assert not bundle.exists()


def test_verify_rejects_payload_tampering_even_if_zip_is_valid(tmp_path):
    db, universe, bundle = tmp_path / "db.sqlite", tmp_path / "universe.json", tmp_path / "out.zip"
    _db(db)
    _universe(universe)
    create_bundle(db, universe, bundle)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(bundle) as source, zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_STORED) as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "historical_ohlcv_universe.json":
                data += b" "
            target.writestr(name, data)
    with pytest.raises(ValueError, match="Groesse|SHA-256"):
        verify_bundle(tampered)


def test_verify_files_accepts_legacy_pair_only_when_database_is_sound(tmp_path):
    db, universe = tmp_path / "db.sqlite", tmp_path / "universe.json"
    _db(db)
    _universe(universe)
    result = verify_files(db, universe)
    assert len(result["database_sha256"]) == 64
    assert len(result["universe_sha256"]) == 64


def test_verify_files_rejects_invalid_universe(tmp_path):
    db, universe = tmp_path / "db.sqlite", tmp_path / "universe.json"
    _db(db)
    universe.write_text('{"version":1,"tickers":"not-a-list"}', encoding="utf-8")
    with pytest.raises(ValueError, match="Universum"):
        verify_files(db, universe)


def test_bundle_rejects_duplicate_or_unexpected_archive_members(tmp_path):
    bundle = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("../escape", "x")
    with pytest.raises(ValueError, match="unerwartete oder doppelte"):
        verify_bundle(bundle)


def test_bundle_backup_includes_committed_wal_pages(tmp_path):
    db = tmp_path / "wal.sqlite"
    universe = tmp_path / "universe.json"
    bundle = tmp_path / "wal-bundle.zip"
    connection = sqlite3.connect(db)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE bars (ticker TEXT NOT NULL)")
    connection.execute("INSERT INTO bars VALUES ('WAL.COMMITTED')")
    connection.commit()
    try:
        assert Path(str(db) + "-wal").exists()
        _universe(universe, "WAL.COMMITTED")
        create_bundle(db, universe, bundle)
        restored_db, restored_universe = tmp_path / "wal-restored.sqlite", tmp_path / "wal-restored.json"
        extract_bundle(bundle, restored_db, restored_universe)
        with sqlite3.connect(restored_db) as restored:
            assert restored.execute("SELECT ticker FROM bars").fetchone()[0] == "WAL.COMMITTED"
    finally:
        connection.close()


def test_verify_files_accepts_legacy_universe_list_format(tmp_path):
    db, universe = tmp_path / "db.sqlite", tmp_path / "universe.json"
    _db(db)
    universe.write_text('["AAA", "BBB"]', encoding="utf-8")
    result = verify_files(db, universe)
    assert len(result["universe_sha256"]) == 64


def test_create_rejects_missing_universe_or_database(tmp_path):
    db, universe, bundle = tmp_path / "missing.sqlite", tmp_path / "missing.json", tmp_path / "out.zip"
    with pytest.raises(ValueError, match="SQLite-Datenbank fehlt"):
        create_bundle(db, universe, bundle)
    db.write_bytes(b"x")
    with pytest.raises(ValueError, match="Universum fehlt"):
        create_bundle(db, universe, bundle)
    assert not bundle.exists()


def test_extract_rolls_back_both_targets_if_second_replace_fails(tmp_path, monkeypatch):
    import ohlcv_release_bundle as bundle_module

    source_db, source_universe = tmp_path / "source.sqlite", tmp_path / "source.json"
    _db(source_db, "NEW")
    _universe(source_universe, "NEW")
    bundle = tmp_path / "snapshot.zip"
    create_bundle(source_db, source_universe, bundle)

    target_db, target_universe = tmp_path / "target.sqlite", tmp_path / "target.json"
    _db(target_db, "OLD")
    target_universe.write_text('{"version":1,"tickers":["OLD.DE"]}', encoding="utf-8")
    original_replace = bundle_module.os.replace
    calls = {"count": 0}

    def fail_second_replace(src, dst):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated second target replace failure")
        return original_replace(src, dst)

    monkeypatch.setattr(bundle_module.os, "replace", fail_second_replace)
    with pytest.raises(RuntimeError, match="vorheriges DB/Universum-Paar"):
        extract_bundle(bundle, target_db, target_universe)

    with sqlite3.connect(target_db) as connection:
        assert connection.execute("SELECT ticker FROM bars").fetchone()[0] == "OLD"
    assert json.loads(target_universe.read_text())["tickers"] == ["OLD.DE"]


def test_extract_rolls_back_when_target_did_not_exist(tmp_path, monkeypatch):
    import ohlcv_release_bundle as bundle_module

    source_db, source_universe = tmp_path / "source.sqlite", tmp_path / "source.json"
    _db(source_db, "NEW")
    _universe(source_universe, "NEW")
    bundle = tmp_path / "snapshot.zip"
    create_bundle(source_db, source_universe, bundle)
    target_db, target_universe = tmp_path / "target.sqlite", tmp_path / "target.json"
    original_replace = bundle_module.os.replace
    calls = {"count": 0}

    def fail_second_replace(src, dst):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated second target replace failure")
        return original_replace(src, dst)

    monkeypatch.setattr(bundle_module.os, "replace", fail_second_replace)
    with pytest.raises(RuntimeError, match="vorheriges DB/Universum-Paar"):
        extract_bundle(bundle, target_db, target_universe)
    assert not target_db.exists()
    assert not target_universe.exists()


def test_verify_files_rejects_empty_universe(tmp_path):
    db, universe = tmp_path / "db.sqlite", tmp_path / "universe.json"
    _db(db)
    universe.write_text('{"version":1,"tickers":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="keine Ticker"):
        verify_files(db, universe)



def test_restore_files_transactionally_installs_verified_legacy_pair(tmp_path):
    from ohlcv_release_bundle import restore_files

    source_db, source_universe = tmp_path / "legacy.sqlite", tmp_path / "legacy.json"
    _db(source_db, "LEGACY")
    _universe(source_universe, "LEGACY")
    target_db, target_universe = tmp_path / "target.sqlite", tmp_path / "target.json"
    _db(target_db, "OLD")
    _universe(target_universe, "OLD")
    result = restore_files(source_db, source_universe, target_db, target_universe)
    assert result["status"] == "restored"
    with sqlite3.connect(target_db) as connection:
        assert connection.execute("SELECT ticker FROM bars").fetchone()[0] == "LEGACY"
    assert json.loads(target_universe.read_text())["tickers"] == ["LEGACY"]


def test_restore_files_rolls_back_both_targets_if_second_replace_fails(tmp_path, monkeypatch):
    import ohlcv_release_bundle as bundle_module

    source_db, source_universe = tmp_path / "legacy.sqlite", tmp_path / "legacy.json"
    _db(source_db, "LEGACY")
    _universe(source_universe, "LEGACY")
    target_db, target_universe = tmp_path / "target.sqlite", tmp_path / "target.json"
    _db(target_db, "OLD")
    _universe(target_universe, "OLD")
    original_replace = bundle_module.os.replace
    calls = {"count": 0}

    def fail_second_replace(src, dst):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated second legacy target replace failure")
        return original_replace(src, dst)

    monkeypatch.setattr(bundle_module.os, "replace", fail_second_replace)
    with pytest.raises(RuntimeError, match="vorheriges DB/Universum-Paar"):
        bundle_module.restore_files(source_db, source_universe, target_db, target_universe)
    with sqlite3.connect(target_db) as connection:
        assert connection.execute("SELECT ticker FROM bars").fetchone()[0] == "OLD"
    assert json.loads(target_universe.read_text())["tickers"] == ["OLD"]


def test_bundle_rejects_member_above_configured_size_limit(tmp_path, monkeypatch):
    import ohlcv_release_bundle as bundle_module

    monkeypatch.setattr(bundle_module, "MAX_DB_BYTES", 32)
    bundle = tmp_path / "oversized.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(bundle_module.DB_NAME, b"x" * 33)
        archive.writestr(bundle_module.UNIVERSE_NAME, '{"version":1,"tickers":["AAA"]}')
        archive.writestr(bundle_module.MANIFEST_NAME, '{}')
    with pytest.raises(ValueError, match="Groessenlimit"):
        verify_bundle(bundle)


def test_bundle_rejects_extreme_compression_ratio(tmp_path):
    import ohlcv_release_bundle as bundle_module

    bundle = tmp_path / "compression-bomb.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(bundle_module.DB_NAME, b"0" * 2_000_000)
        archive.writestr(bundle_module.UNIVERSE_NAME, '{"version":1,"tickers":["AAA"]}')
        archive.writestr(bundle_module.MANIFEST_NAME, '{}')
    with pytest.raises(ValueError, match="Kompressionsverhaeltnis"):
        verify_bundle(bundle)



def test_restore_files_rolls_back_when_installed_pair_verification_fails(tmp_path, monkeypatch):
    import ohlcv_release_bundle as bundle_module

    source_db, source_universe = tmp_path / "legacy.sqlite", tmp_path / "legacy.json"
    _db(source_db, "LEGACY")
    _universe(source_universe, "LEGACY")
    target_db, target_universe = tmp_path / "target.sqlite", tmp_path / "target.json"
    _db(target_db, "OLD")
    _universe(target_universe, "OLD")
    original_verify = bundle_module.verify_files

    def fail_installed_verification(db_path, universe_path):
        if Path(db_path).resolve() == target_db.resolve():
            raise ValueError("simulated installed-pair verification failure")
        return original_verify(db_path, universe_path)

    monkeypatch.setattr(bundle_module, "verify_files", fail_installed_verification)
    with pytest.raises(RuntimeError, match="vorheriges DB/Universum-Paar"):
        bundle_module.restore_files(source_db, source_universe, target_db, target_universe)
    with sqlite3.connect(target_db) as connection:
        assert connection.execute("SELECT ticker FROM bars").fetchone()[0] == "OLD"
    assert json.loads(target_universe.read_text())["tickers"] == ["OLD"]


def test_create_rejects_final_bundle_at_configured_release_asset_limit(tmp_path, monkeypatch):
    import ohlcv_release_bundle as bundle_module

    db, universe, bundle = tmp_path / "db.sqlite", tmp_path / "universe.json", tmp_path / "out.zip"
    _db(db)
    _universe(universe)
    monkeypatch.setattr(bundle_module, "MAX_RELEASE_ASSET_BYTES", 100)
    monkeypatch.setattr(bundle_module, "MAX_DB_BYTES", 1024 * 1024)
    with pytest.raises(ValueError, match="kleiner als 2 GiB"):
        create_bundle(db, universe, bundle)
    assert not bundle.exists()


def test_verify_rejects_existing_bundle_over_release_asset_limit(tmp_path, monkeypatch):
    import ohlcv_release_bundle as bundle_module

    db, universe, bundle = tmp_path / "db.sqlite", tmp_path / "universe.json", tmp_path / "out.zip"
    _db(db)
    _universe(universe)
    create_bundle(db, universe, bundle)
    monkeypatch.setattr(bundle_module, "MAX_RELEASE_ASSET_BYTES", bundle.stat().st_size - 1)
    with pytest.raises(ValueError, match="kleiner als 2 GiB"):
        verify_bundle(bundle)


def test_bundle_validator_rejects_ticker_runtime_loader_would_reject(tmp_path):
    from historical_ohlcv import _load_dynamic_universe

    db, universe = tmp_path / "db.sqlite", tmp_path / "universe.json"
    _db(db)
    universe.write_text('{"version":1,"tickers":["BAD!"]}', encoding="utf-8")

    with pytest.raises(ValueError, match="ungueltigen Ticker"):
        verify_files(db, universe)
    with pytest.raises(RuntimeError, match="ungueltigen Ticker"):
        _load_dynamic_universe(universe)


def test_bundle_validator_accepts_runtime_normalized_symbols(tmp_path):
    db, universe = tmp_path / "db.sqlite", tmp_path / "universe.json"
    _db(db)
    universe.write_text('{"version":1,"tickers":["BRK-B", "^GSPC", "EURUSD=X"]}', encoding="utf-8")
    assert verify_files(db, universe)["universe_size"] == universe.stat().st_size
