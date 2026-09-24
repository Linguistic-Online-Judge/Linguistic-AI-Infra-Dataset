"""Recognize the historical role-only v3 before applying the shared v4 migration.

Both branches published version 3: one added only roles, the other roles and auth.
Complete only the known role-only shape, within the caller's migration transaction.
No credential is inferred from an existing subject or public handle.
"""

from .auth_store import AUTH_TABLES_V3

_AUTH_COLUMNS = {
    "auth_credentials": {"user_id", "email", "password_hash", "version"},
    "auth_sessions": {"token_hash", "user_id", "expires_at"},
    "auth_action_tokens": {"token_hash", "purpose", "email", "user_id", "expires_at"},
    "auth_rate_limits": {"bucket", "attempts", "expires_at"},
}


def complete_legacy_auth_schema(cursor, versions: tuple[int, ...], *, postgres: bool) -> None:
    if 3 not in versions:
        return
    if postgres:
        cursor.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema()"
        )
        columns: dict[str, set[str]] = {}
        for table, column in cursor.fetchall():
            columns.setdefault(table, set()).add(column)
        cursor.execute(
            "SELECT is_nullable, column_default, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'users' AND column_name = 'role'"
        )
        role = cursor.fetchone()
        role_valid = role is not None and tuple(role) == ("NO", "'user'::text", "text")
        cursor.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'users'::regclass AND contype = 'c'"
        )
        checks = {"".join(row[0].lower().split()) for row in cursor.fetchall()}
        role_valid = role_valid and "check((role=any(array['user'::text,'admin'::text])))" in checks
    else:
        role = None
        cursor.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
        tables = dict(cursor.fetchall())
        columns = {}
        known_tables = (
            *_AUTH_COLUMNS, "users", "challenge_admin_state", "challenge_admin_revisions",
        )
        for table in known_tables:
            if table in tables:
                # These names are internal constants, never user input.
                cursor.execute(f"PRAGMA table_info({table})")
                rows = cursor.fetchall()
                columns[table] = {row[1] for row in rows}
                if table == "users":
                    role = next((row for row in rows if row[1] == "role"), None)
        role_valid = role is not None and (role[2].upper(), role[3], role[4]) == (
            "TEXT", 1, "'user'",
        )
        definition = "".join(tables.get("users", "").lower().split())
        role_valid = role_valid and "check(rolein('user','admin'))" in definition
    if not role_valid:
        raise RuntimeError("unsupported legacy authentication schema: users.role")
    cursor.execute("SELECT 1 FROM users WHERE role NOT IN ('user', 'admin') OR role IS NULL")
    if cursor.fetchone() is not None:
        raise RuntimeError("unsupported legacy authentication schema: invalid user role")
    present = set(columns) & _AUTH_COLUMNS.keys()
    if present:
        if present != _AUTH_COLUMNS.keys() or any(
            columns[table] != expected for table, expected in _AUTH_COLUMNS.items()
        ):
            raise RuntimeError("incomplete authentication schema; migration refused")
        return
    if versions != (1, 2, 3) or any(table.startswith("challenge_admin_") for table in columns):
        raise RuntimeError("missing authentication schema; migration refused")
    for statement in AUTH_TABLES_V3.split(";"):
        if statement.strip():
            cursor.execute(statement)
