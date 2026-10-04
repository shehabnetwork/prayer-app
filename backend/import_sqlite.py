"""Read-only SQLite snapshot import into an empty initialized PostgreSQL database."""
import argparse
from contextlib import closing
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import unicodedata

import psycopg
from psycopg import sql


TABLES = {
    "users": ("id", "alias", "alias_key", "password_hash", "created_at", "city_id", "can_supervise", "display_name"),
    "sessions": ("token_hash", "user_id", "expires_at"),
    "days": ("user_id", "date", "prayers", "sunnah", "dhuhr_before_blocks", "version", "updated_at"),
    "supervisor_children": ("supervisor_id", "child_id", "created_at"),
    "supervisor_invites": ("id", "supervisor_id", "token_hash", "created_at", "revoked_at"),
    "supervisor_invite_acceptances": ("invite_id", "child_id", "request_key", "accepted_at"),
}
TABLES.update({
    "groups": ("id", "name", "admin_id", "members_can_view_records", "created_at"),
    "group_members": ("group_id", "user_id", "created_at"),
    "group_invites": ("id", "group_id", "token_hash", "created_at", "revoked_at", "share_token"),
    "group_invite_acceptances": ("invite_id", "user_id", "request_key", "accepted_at"),
})
OPTIONAL_TABLES = set(TABLES) - {"users", "sessions", "days"}
INTEGER_COLUMNS = {"id", "user_id", "supervisor_id", "child_id", "invite_id", "version", "can_supervise", "group_id", "admin_id", "members_can_view_records"}
TIME_COLUMNS = {"created_at", "expires_at", "updated_at", "revoked_at", "accepted_at"}


class ImportValidationError(ValueError):
    """Source or destination cannot safely be imported."""


def _validate_day(row):
    try:
        if datetime.date.fromisoformat(row["date"]).isoformat() != row["date"]:
            raise ValueError()
        prayers, sunnah, blocks = (json.loads(row[key]) for key in
                                   ("prayers", "sunnah", "dhuhr_before_blocks"))
        if not isinstance(prayers, dict) or set(prayers) != {"fajr", "dhuhr", "asr", "maghrib", "isha"}:
            raise ValueError()
        if any(type(value) is not int or value not in (0, 1, 2) for value in prayers.values()):
            raise ValueError()
        keys = {"fajr_before", "dhuhr_before", "dhuhr_after", "maghrib_after", "isha_after", "witr"}
        if not isinstance(sunnah, dict) or set(sunnah) != keys or type(sunnah["witr"]) is not bool:
            raise ValueError()
        for key in keys - {"witr"}:
            if type(sunnah[key]) is not int or sunnah[key] not in ((0, 2, 4) if key == "dhuhr_before" else (0, 2)):
                raise ValueError()
        if not isinstance(blocks, list) or len(blocks) != 2 or any(type(value) is not bool for value in blocks):
            raise ValueError()
        if sunnah["dhuhr_before"] != 2 * sum(blocks):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ImportValidationError("Invalid date or day JSON in source") from None


def read_snapshot(source):
    """Back up a read-only connection to memory for one consistent source view."""
    path = Path(source).expanduser().resolve(strict=True)
    snapshot = sqlite3.connect(":memory:")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as original:
            original.backup(snapshot)
        snapshot.row_factory = sqlite3.Row
        if [row[0] for row in snapshot.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ImportValidationError("Source failed SQLite integrity check")
        if snapshot.execute("PRAGMA foreign_key_check").fetchone():
            raise ImportValidationError("Source has broken foreign keys")
        present = {row[0] for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if present - set(TABLES) - {"sqlite_sequence", "app_migrations"}:
            raise ImportValidationError("Source contains unsupported tables")
        data = {}
        for table, columns in TABLES.items():
            if table not in present:
                if table not in OPTIONAL_TABLES:
                    raise ImportValidationError(f"Missing source table: {table}")
                data[table] = []
                continue
            actual = {row["name"] for row in snapshot.execute(f'PRAGMA table_info("{table}")')}
            allowed_missing = {"city_id", "can_supervise", "display_name"} if table == "users" else ({"share_token"} if table == "group_invites" else set())
            if table == "group_invites":
                durable = snapshot.execute("SELECT 1 FROM app_migrations WHERE name='groups_v2'").fetchone() if "app_migrations" in present else None
                if bool(durable) != ("share_token" in actual):
                    raise ImportValidationError("Incomplete durable group link migration")
            if actual - set(columns) or set(columns) - actual - allowed_missing:
                raise ImportValidationError(f"Unsupported source schema: {table}")
            rows = []
            for raw in snapshot.execute(f'SELECT * FROM "{table}"'):
                row = dict(raw)
                if table == "users":
                    row.setdefault("city_id", None)
                    row.setdefault("can_supervise", 0)
                    row.setdefault("display_name", row["alias"])
                if table == "group_invites":
                    row.setdefault("share_token", None)
                for column in columns:
                    value = row[column]
                    if value is None and column in {"city_id", "revoked_at", "share_token"}:
                        continue
                    if column in INTEGER_COLUMNS:
                        valid = type(value) is int and -9223372036854775808 <= value <= 9223372036854775807
                        if column == "version":
                            valid = valid and value >= 0
                        if column in {"can_supervise", "members_can_view_records"}:
                            valid = valid and value in (0, 1)
                    elif column in TIME_COLUMNS:
                        valid = type(value) in (int, float) and math.isfinite(value)
                    else:
                        valid = type(value) is str and "\x00" not in value
                    if not valid:
                        raise ImportValidationError(f"Invalid source value: {table}.{column}")
                if table == "users":
                    name = row["display_name"]
                    if (not 1 <= len(name) <= 80 or name != name.strip()
                            or any(unicodedata.category(c).startswith("C") for c in name)):
                        raise ImportValidationError("Invalid source value: users.display_name")
                if table == "group_invites" and row["share_token"] is not None:
                    if len(row["share_token"]) < 40 or hashlib.sha256(row["share_token"].encode()).hexdigest() != row["token_hash"]:
                        raise ImportValidationError("Invalid durable group invite token")
                if table == "days":
                    _validate_day(row)
                rows.append(tuple(row[column] for column in columns))
            data[table] = rows
        # Validate references even when a legacy schema omitted FK declarations.
        users = {row[0] for row in data["users"]}
        invites = {row[0] for row in data["supervisor_invites"]}
        for table in TABLES:
            for values in data[table]:
                row = dict(zip(TABLES[table], values))
                for column in ("user_id", "supervisor_id", "child_id", "admin_id"):
                    if column in row and row[column] not in users:
                        raise ImportValidationError(f"Broken source user reference: {table}")
                if table == "supervisor_invite_acceptances" and row["invite_id"] not in invites:
                    raise ImportValidationError("Broken source invite reference")
                if table == "supervisor_children" and row["supervisor_id"] == row["child_id"]:
                    raise ImportValidationError("Source contains a self-supervision relationship")
        required = {"groups", "group_members", "group_invites", "group_invite_acceptances", "app_migrations"}
        if required & present:
            if not required <= present or not snapshot.execute("SELECT 1 FROM app_migrations WHERE name='groups_v1'").fetchone():
                raise ImportValidationError("Incomplete group source migration")
        else:
            # Transform legacy rows in memory once; retain inert rows for audit compatibility.
            owners = sorted({r[0] for r in data["supervisor_children"]} | {r[1] for r in data["supervisor_invites"]})
            user_map = {r[0]: r for r in data["users"]}
            for uid in owners:
                user = user_map[uid]
                data["groups"].append((uid, f"مجموعة {user[1]}", uid, 0, user[4]))
                data["group_members"].append((uid, uid, user[4]))
            data["group_members"].extend(data["supervisor_children"])
            data["group_invites"] = [(*row, None) for row in data["supervisor_invites"]]
            data["group_invite_acceptances"] = list(data["supervisor_invite_acceptances"])
        groups = {r[0]: r for r in data["groups"]}
        members = {(r[0], r[1]) for r in data["group_members"]}
        group_invites = {r[0] for r in data["group_invites"]}
        for gid, group in groups.items():
            if (gid, group[2]) not in members:
                raise ImportValidationError("Group admin must be a member")
        for table in ("group_members", "group_invites"):
            column = TABLES[table].index("group_id")
            if any(row[column] not in groups for row in data[table]):
                raise ImportValidationError("Broken source group reference")
        if any(row[0] not in group_invites for row in data["group_invite_acceptances"]):
            raise ImportValidationError("Broken group invite reference")
        return data
    finally:
        snapshot.close()


def import_sqlite(source, target, *, dry_run=False, schema=None):
    """Import all rows atomically; dry runs perform insertion and verification then roll back."""
    if not target or not target.startswith(("postgresql://", "postgres://")):
        raise ImportValidationError("PRAYER_DATABASE_URL must be a PostgreSQL URL")
    schema = schema if schema is not None else os.environ.get("PRAYER_DATABASE_SCHEMA", "public")
    data = read_snapshot(source)
    with psycopg.connect(target) as conn:
        conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("LOCK TABLE {} IN EXCLUSIVE MODE").format(
            sql.SQL(", ").join(sql.Identifier(table) for table in TABLES)))
        for table, columns in TABLES.items():
            if conn.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(table))).fetchone():
                raise ImportValidationError(f"Target table is not empty: {table}")
        for table, columns in TABLES.items():
            statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, columns)),
                sql.SQL(",").join(sql.Placeholder() for _ in columns))
            with conn.cursor() as cursor:
                cursor.executemany(statement, data[table])
            actual = conn.execute(sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier(table))).fetchall()
            if sorted(actual) != sorted(data[table]):
                raise ImportValidationError(f"Imported content differs: {table}")
        for table in ("users", "supervisor_invites", "groups", "group_invites"):
            sequence = conn.execute("SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=pg_get_serial_sequence(%s,'id')::regclass", (table,)).fetchone()
            if not sequence:
                raise ImportValidationError(f"Missing target identity sequence: {table}")
            next_id = max(0, max((row[0] for row in data[table]), default=0)) + 1
            if next_id > 9223372036854775807:
                raise ImportValidationError(f"Source exhausted identity range: {table}")
            if not dry_run:
                conn.execute(sql.SQL("ALTER SEQUENCE {} RESTART WITH {}").format(
                    sql.Identifier(*sequence), sql.Literal(next_id)))
        if dry_run:
            conn.rollback()
    return {table: len(rows) for table, rows in data.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="SQLite source file (opened read-only)")
    parser.add_argument("--dry-run", action="store_true", help="Validate import and roll back all target writes")
    args = parser.parse_args(argv)
    try:
        counts = import_sqlite(args.source, os.environ.get("PRAYER_DATABASE_URL"),
                               dry_run=args.dry_run, schema=os.environ.get("PRAYER_DATABASE_SCHEMA"))
    except (ImportValidationError, OSError, sqlite3.Error, psycopg.Error):
        # Connection errors may contain credentials or private row values.
        print("Import failed; no rows committed. Check source integrity, empty initialized target, and database configuration.", file=sys.stderr)
        return 1
    print(json.dumps({"dry_run": args.dry_run, "counts": counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
