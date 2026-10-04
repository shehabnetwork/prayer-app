ALTER TABLE group_invites ADD COLUMN share_token TEXT;
DROP INDEX group_active_invite;
CREATE UNIQUE INDEX group_active_legacy_invite ON group_invites(group_id) WHERE revoked_at IS NULL AND share_token IS NULL;
CREATE UNIQUE INDEX group_active_share_invite ON group_invites(group_id) WHERE revoked_at IS NULL AND share_token IS NOT NULL;
INSERT INTO app_migrations VALUES('groups_v2');
