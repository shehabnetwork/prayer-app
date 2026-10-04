"""Group schema and one-time conversion of inert legacy supervision data."""
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS groups (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL,
 admin_id INTEGER NOT NULL REFERENCES users(id),
 members_can_view_records INTEGER NOT NULL DEFAULT 0 CHECK(members_can_view_records IN (0,1)),
 created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS group_members (
 group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 created_at REAL NOT NULL, PRIMARY KEY(group_id,user_id));
CREATE INDEX IF NOT EXISTS group_member_user ON group_members(user_id);
CREATE TABLE IF NOT EXISTS group_invites (
 id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
 token_hash TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, revoked_at REAL, share_token TEXT);

CREATE TABLE IF NOT EXISTS group_invite_acceptances (
 invite_id INTEGER NOT NULL REFERENCES group_invites(id),
 user_id INTEGER NOT NULL REFERENCES users(id), request_key TEXT NOT NULL,
 accepted_at REAL NOT NULL, PRIMARY KEY(user_id,request_key));
"""


def migrate_legacy(conn):
    owners = conn.execute("SELECT supervisor_id FROM supervisor_children UNION SELECT supervisor_id FROM supervisor_invites ORDER BY supervisor_id").fetchall()
    for owner in owners:
        uid = owner[0]
        user = conn.execute("SELECT alias,created_at FROM users WHERE id=?", (uid,)).fetchone()
        gid = conn.insert_id("INSERT INTO groups(name,admin_id,created_at) VALUES(?,?,?)", (f"مجموعة {user['alias']}", uid, user['created_at']))
        conn.execute("INSERT INTO group_members(group_id,user_id,created_at) VALUES(?,?,?)", (gid, uid, user['created_at']))
        conn.execute("INSERT INTO group_members(group_id,user_id,created_at) SELECT ?,child_id,created_at FROM supervisor_children WHERE supervisor_id=?", (gid, uid))
        conn.execute("INSERT INTO group_invites(id,group_id,token_hash,created_at,revoked_at) SELECT id,?,token_hash,created_at,revoked_at FROM supervisor_invites WHERE supervisor_id=?", (gid, uid))
    conn.execute("INSERT INTO group_invite_acceptances(invite_id,user_id,request_key,accepted_at) SELECT invite_id,child_id,request_key,accepted_at FROM supervisor_invite_acceptances")
