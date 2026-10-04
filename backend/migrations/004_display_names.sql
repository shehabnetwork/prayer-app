ALTER TABLE users ADD COLUMN display_name TEXT;
UPDATE users SET display_name=alias;
ALTER TABLE users ALTER COLUMN display_name SET NOT NULL;
