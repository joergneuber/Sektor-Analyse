#!/usr/bin/env python3
"""Create, verify and restore an immutable OHLCV release snapshot bundle.

The database and dynamic universe are packaged as one versioned ZIP asset.
The bundle manifest binds both payloads by SHA-256 and size. SQLite is copied
through SQLite's backup API before packaging and checked with integrity_check.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile

DB_NAME = "historical_ohlcv.sqlite"
UNIVERSE_NAME = "historical_ohlcv_universe.json"
MANIFEST_NAME = "manifest.json"
BUNDLE_VERSION = 1
CHUNK_SIZE = 1024 * 1024
# GitHub requires each Release asset to be strictly smaller than 2 GiB.
# ZIP_STORED is used, so the bundle is approximately the sum of its members.
MAX_RELEASE_ASSET_BYTES = 2 * 1024 * 1024 * 1024 - 1
MAX_DB_BYTES = MAX_RELEASE_ASSET_BYTES
MAX_UNIVERSE_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_COMPRESSION_RATIO = 1000

from ohlcv_ticker import normalize_market_ticker as _normalize_market_ticker


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_universe(path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Universum ist kein gueltiges UTF-8-JSON: {exc}") from exc
    if isinstance(data, list):
        tickers = data
    elif isinstance(data, dict) and data.get("version") == 1 and isinstance(data.get("tickers"), list):
        tickers = data["tickers"]
    else:
        raise ValueError("Universum muss eine Tickerliste oder ein version=1-Objekt mit tickers-Liste sein")
    if not tickers:
        raise ValueError("Universum enthaelt keine Ticker")
    if any(not isinstance(ticker, str) or not ticker.strip() for ticker in tickers):
        raise ValueError("Universum enthaelt einen leeren oder nicht-string Ticker")
    invalid = [ticker for ticker in tickers if _normalize_market_ticker(ticker) != ticker.strip().upper()]
    if invalid:
        raise ValueError(f"Universum enthaelt einen ungueltigen Ticker: {invalid[0]!r}")


def _integrity_check(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"SQLite-Datenbank fehlt oder ist leer: {path}")
    try:
        connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            rows = connection.execute("PRAGMA integrity_check").fetchall()
            if rows != [("ok",)]:
                details = "; ".join(str(row[0]) for row in rows[:20])
                raise ValueError(f"SQLite integrity_check fehlgeschlagen: {details}")
            fk_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
            if fk_rows:
                raise ValueError(f"SQLite foreign_key_check meldet {len(fk_rows)} Fehler")
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"SQLite-Datenbank kann nicht vollstaendig geprueft werden: {exc}") from exc


def verify_files(db_path: Path, universe_path: Path) -> dict:
    _validate_universe(universe_path)
    _integrity_check(db_path)
    return {
        "database_sha256": _sha256(db_path),
        "database_size": db_path.stat().st_size,
        "universe_sha256": _sha256(universe_path),
        "universe_size": universe_path.stat().st_size,
    }


def create_bundle(db_path: Path, universe_path: Path, output_path: Path) -> dict:
    if not db_path.is_file():
        raise ValueError(f"SQLite-Datenbank fehlt: {db_path}")
    if not universe_path.is_file() or universe_path.stat().st_size <= 0:
        raise ValueError(f"Universum fehlt oder ist leer: {universe_path}")
    _validate_universe(universe_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ohlcv-bundle-") as temp_dir:
        temp_root = Path(temp_dir)
        snapshot_db = temp_root / DB_NAME
        source_uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        source = sqlite3.connect(source_uri, uri=True)
        target = sqlite3.connect(snapshot_db)
        try:
            source.backup(target, pages=1000, sleep=0.1)
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"SQLite-Datenbank ist nicht lesbar oder beschaedigt: {exc}") from exc
        finally:
            target.close()
            source.close()
        # The archived snapshot is checked once below by verify_bundle();
        # that check runs integrity_check against the exact database bytes
        # contained in the upload artifact.
        snapshot_universe = temp_root / UNIVERSE_NAME
        shutil.copyfile(universe_path, snapshot_universe)
        _validate_universe(snapshot_universe)
        manifest = {
            "bundle_version": BUNDLE_VERSION,
            "files": {
                DB_NAME: {"size": snapshot_db.stat().st_size, "sha256": _sha256(snapshot_db)},
                UNIVERSE_NAME: {"size": snapshot_universe.stat().st_size, "sha256": _sha256(snapshot_universe)},
            },
        }
        manifest_path = temp_root / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        temporary_output = output_path.with_name(output_path.name + ".tmp")
        try:
            with zipfile.ZipFile(temporary_output, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                archive.write(snapshot_db, DB_NAME)
                archive.write(snapshot_universe, UNIVERSE_NAME)
                archive.write(manifest_path, MANIFEST_NAME)
            # Check the final upload object, not only its uncompressed members.
            if temporary_output.stat().st_size > MAX_RELEASE_ASSET_BYTES:
                raise ValueError(
                    "OHLCV-Bundle ueberschreitet die GitHub-Release-Grenze: "
                    "jede Release-Datei muss kleiner als 2 GiB sein"
                )
            # Validate the exact bytes that will be uploaded before publishing locally.
            verify_bundle(temporary_output)
            os.replace(temporary_output, output_path)
        finally:
            if temporary_output.exists():
                temporary_output.unlink()
    return {"bundle": str(output_path), "sha256": _sha256(output_path), "size": output_path.stat().st_size}


def _safe_members(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    expected = {DB_NAME, UNIVERSE_NAME, MANIFEST_NAME}
    if len(names) != len(set(names)) or set(names) != expected:
        raise ValueError(f"Bundle enthaelt unerwartete oder doppelte Dateien: {names}")
    limits = {DB_NAME: MAX_DB_BYTES, UNIVERSE_NAME: MAX_UNIVERSE_BYTES, MANIFEST_NAME: MAX_MANIFEST_BYTES}
    for info in infos:
        candidate = Path(info.filename)
        if candidate.is_absolute() or ".." in candidate.parts or len(candidate.parts) != 1:
            raise ValueError(f"Unsicherer ZIP-Pfad: {info.filename}")
        if info.file_size < 0 or info.file_size > limits[info.filename]:
            raise ValueError(
                f"ZIP-Eintrag ueberschreitet Groessenlimit: {info.filename} "
                f"({info.file_size} > {limits[info.filename]} Bytes)"
            )
        if info.file_size and info.compress_size == 0:
            raise ValueError(f"Ungueltige ZIP-Kompressionsgroesse: {info.filename}")
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise ValueError(
                f"ZIP-Kompressionsverhaeltnis zu hoch: {info.filename} "
                f"({info.file_size / info.compress_size:.1f}:1)"
            )


def _replace_pair_transactionally(staged_db: Path, staged_universe: Path, db_path: Path, universe_path: Path) -> dict:
    """Replace a DB/universe pair with rollback on either replacement failure."""
    db_path = db_path.resolve()
    universe_path = universe_path.resolve()
    if db_path == universe_path:
        raise ValueError("Datenbank- und Universum-Ziel duerfen nicht identisch sein")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    universe_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ohlcv-pair-db-", dir=db_path.parent) as db_temp:
        with tempfile.TemporaryDirectory(prefix="ohlcv-pair-universe-", dir=universe_path.parent) as universe_temp:
            db_root, universe_root = Path(db_temp), Path(universe_temp)
            staged_db_local = db_root / "new.sqlite"
            staged_universe_local = universe_root / "new.json"
            shutil.copyfile(staged_db, staged_db_local)
            shutil.copyfile(staged_universe, staged_universe_local)
            db_existed, universe_existed = db_path.exists(), universe_path.exists()
            db_backup, universe_backup = db_root / "previous.sqlite", universe_root / "previous.json"
            if db_existed:
                shutil.copy2(db_path, db_backup)
            if universe_existed:
                shutil.copy2(universe_path, universe_backup)
            expected_db_sha = _sha256(staged_db_local)
            expected_universe_sha = _sha256(staged_universe_local)
            try:
                os.replace(staged_db_local, db_path)
                os.replace(staged_universe_local, universe_path)
                installed = verify_files(db_path, universe_path)
                if installed["database_sha256"] != expected_db_sha:
                    raise RuntimeError("Installierte SQLite-Datei weicht vom geprueften Snapshot ab")
                if installed["universe_sha256"] != expected_universe_sha:
                    raise RuntimeError("Installiertes Universum weicht vom geprueften Snapshot ab")
            except Exception as replace_error:
                rollback_errors = []
                for target_path, backup_path, existed in (
                    (db_path, db_backup, db_existed),
                    (universe_path, universe_backup, universe_existed),
                ):
                    try:
                        if existed:
                            os.replace(backup_path, target_path)
                        else:
                            target_path.unlink(missing_ok=True)
                    except Exception as rollback_error:
                        rollback_errors.append(f"{target_path}: {rollback_error}")
                if rollback_errors:
                    raise RuntimeError(
                        "Restore fehlgeschlagen und Rollback unvollstaendig: "
                        + "; ".join(rollback_errors)
                    ) from replace_error
                raise RuntimeError(
                    "Restore fehlgeschlagen; vorheriges DB/Universum-Paar wurde wiederhergestellt"
                ) from replace_error
            return installed


def _extract_and_verify_bundle(bundle_path: Path, target_dir: Path) -> dict:
    if not bundle_path.is_file() or bundle_path.stat().st_size <= 0:
        raise ValueError(f"Bundle fehlt oder ist leer: {bundle_path}")
    if bundle_path.stat().st_size > MAX_RELEASE_ASSET_BYTES:
        raise ValueError(
            "OHLCV-Bundle ueberschreitet die GitHub-Release-Grenze: "
            "jede Release-Datei muss kleiner als 2 GiB sein"
        )
    try:
        with zipfile.ZipFile(bundle_path, "r") as archive:
            _safe_members(archive)
            bad_member = archive.testzip()
            if bad_member:
                raise ValueError(f"ZIP-Pruefsumme fehlerhaft: {bad_member}")
            for name in (DB_NAME, UNIVERSE_NAME, MANIFEST_NAME):
                with archive.open(name, "r") as source, (target_dir / name).open("wb") as target:
                    shutil.copyfileobj(source, target, length=CHUNK_SIZE)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Bundle ist kein gueltiges ZIP: {exc}") from exc

    manifest_path = target_dir / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Bundle-Manifest ist ungueltig: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("bundle_version") != BUNDLE_VERSION or not isinstance(manifest.get("files"), dict):
        raise ValueError("Nicht unterstuetzte oder ungueltige Bundle-Manifest-Version")
    if set(manifest["files"]) != {DB_NAME, UNIVERSE_NAME}:
        raise ValueError("Bundle-Manifest enthaelt unerwartete oder fehlende Datei-Eintraege")
    for name in (DB_NAME, UNIVERSE_NAME):
        metadata = manifest["files"].get(name)
        payload = target_dir / name
        if not isinstance(metadata, dict) or not payload.is_file():
            raise ValueError(f"Bundle-Datei oder Manifest-Eintrag fehlt: {name}")
        if metadata.get("size") != payload.stat().st_size:
            raise ValueError(f"Groesse stimmt nicht mit Manifest ueberein: {name}")
        if metadata.get("sha256") != _sha256(payload):
            raise ValueError(f"SHA-256 stimmt nicht mit Manifest ueberein: {name}")
    _validate_universe(target_dir / UNIVERSE_NAME)
    _integrity_check(target_dir / DB_NAME)
    return {"bundle_sha256": _sha256(bundle_path), "bundle_size": bundle_path.stat().st_size, "status": "ok"}


def verify_bundle(bundle_path: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="ohlcv-verify-") as temp_dir:
        return _extract_and_verify_bundle(bundle_path, Path(temp_dir))


def extract_bundle(bundle_path: Path, db_path: Path, universe_path: Path) -> None:
    """Restore a verified bundle with rollback if either target replacement fails."""
    with tempfile.TemporaryDirectory(prefix="ohlcv-bundle-install-") as temp_dir:
        staging = Path(temp_dir)
        _extract_and_verify_bundle(bundle_path, staging)
        _replace_pair_transactionally(staging / DB_NAME, staging / UNIVERSE_NAME, db_path, universe_path)


def restore_files(source_db: Path, source_universe: Path, db_path: Path, universe_path: Path) -> dict:
    """Validate and transactionally install a legacy DB/universe pair."""
    verify_files(source_db, source_universe)
    with tempfile.TemporaryDirectory(prefix="ohlcv-legacy-stage-") as temp_dir:
        staging = Path(temp_dir)
        staged_db, staged_universe = staging / DB_NAME, staging / UNIVERSE_NAME
        shutil.copyfile(source_db, staged_db)
        shutil.copyfile(source_universe, staged_universe)
        verify_files(staged_db, staged_universe)
        installed = _replace_pair_transactionally(
            staged_db, staged_universe, db_path, universe_path
        )
    return {"status": "restored", **installed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    verify_files_parser = sub.add_parser("verify-files")
    verify_files_parser.add_argument("--db", type=Path, default=Path(DB_NAME))
    verify_files_parser.add_argument("--universe", type=Path, default=Path(UNIVERSE_NAME))
    restore_files_parser = sub.add_parser("restore-files")
    restore_files_parser.add_argument("--source-db", type=Path, required=True)
    restore_files_parser.add_argument("--source-universe", type=Path, required=True)
    restore_files_parser.add_argument("--db", type=Path, default=Path(DB_NAME))
    restore_files_parser.add_argument("--universe", type=Path, default=Path(UNIVERSE_NAME))
    create_parser = sub.add_parser("create")
    create_parser.add_argument("--db", type=Path, default=Path(DB_NAME))
    create_parser.add_argument("--universe", type=Path, default=Path(UNIVERSE_NAME))
    create_parser.add_argument("--output", type=Path, required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--bundle", type=Path, required=True)
    extract_parser = sub.add_parser("extract")
    extract_parser.add_argument("--bundle", type=Path, required=True)
    extract_parser.add_argument("--db", type=Path, default=Path(DB_NAME))
    extract_parser.add_argument("--universe", type=Path, default=Path(UNIVERSE_NAME))
    args = parser.parse_args()
    try:
        if args.command == "verify-files":
            result = verify_files(args.db, args.universe)
        elif args.command == "restore-files":
            result = restore_files(args.source_db, args.source_universe, args.db, args.universe)
        elif args.command == "create":
            result = create_bundle(args.db, args.universe, args.output)
        elif args.command == "verify":
            result = verify_bundle(args.bundle)
        else:
            extract_bundle(args.bundle, args.db, args.universe)
            result = {"status": "restored", "database": str(args.db), "universe": str(args.universe)}
    except Exception as exc:
        print(f"OHLCV_BUNDLE=FAIL | {type(exc).__name__}: {exc}")
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print("OHLCV_BUNDLE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
