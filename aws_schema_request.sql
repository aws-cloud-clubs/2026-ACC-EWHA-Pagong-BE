-- AWS DB schema changes required by main.py.
-- This keeps the current API logic unchanged and adds the columns it expects.

BEGIN;

ALTER TYPE access_action ADD VALUE IF NOT EXISTS 'INTERNAL_FILE_VIEW';
ALTER TYPE access_action ADD VALUE IF NOT EXISTS 'INTERNAL_FILE_DOWNLOAD';
ALTER TYPE access_action ADD VALUE IF NOT EXISTS 'SHARE_LINK_VIEW';
ALTER TYPE access_action ADD VALUE IF NOT EXISTS 'SHARE_LINK_DOWNLOAD';
ALTER TYPE access_result ADD VALUE IF NOT EXISTS 'DENIED';

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS password VARCHAR(255);

UPDATE users
SET password = password_hash
WHERE password IS NULL
  AND EXISTS (
      SELECT 1
      FROM information_schema.columns
      WHERE table_name = 'users'
        AND column_name = 'password_hash'
  );

ALTER TABLE users
    ALTER COLUMN password SET NOT NULL;

ALTER TABLE share_links
    ADD COLUMN IF NOT EXISTS client_name VARCHAR(200),
    ADD COLUMN IF NOT EXISTS assigned_staff_user_id BIGINT,
    ADD COLUMN IF NOT EXISTS status VARCHAR(10),
    ADD COLUMN IF NOT EXISTS note TEXT,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;

UPDATE share_links
SET client_name = ''
WHERE client_name IS NULL;

UPDATE share_links
SET status = CASE WHEN is_active THEN 'ACTIVE' ELSE 'REVOKED' END
WHERE status IS NULL
  AND EXISTS (
      SELECT 1
      FROM information_schema.columns
      WHERE table_name = 'share_links'
        AND column_name = 'is_active'
  );

UPDATE share_links
SET status = 'ACTIVE'
WHERE status IS NULL;

UPDATE share_links
SET updated_at = created_at
WHERE updated_at IS NULL;

ALTER TABLE share_links
    ALTER COLUMN client_name SET NOT NULL,
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN updated_at SET NOT NULL;

ALTER TABLE share_links
    ADD CONSTRAINT fk_share_links_assigned_staff_user_id_users
    FOREIGN KEY (assigned_staff_user_id) REFERENCES users(id);

ALTER TABLE access_logs
    ADD COLUMN IF NOT EXISTS actor_user_id BIGINT,
    ADD COLUMN IF NOT EXISTS actor_type VARCHAR(20),
    ADD COLUMN IF NOT EXISTS message VARCHAR(500);

UPDATE access_logs
SET actor_user_id = user_id
WHERE actor_user_id IS NULL
  AND EXISTS (
      SELECT 1
      FROM information_schema.columns
      WHERE table_name = 'access_logs'
        AND column_name = 'user_id'
  );

UPDATE access_logs
SET actor_type = CASE
    WHEN access_type::text IN ('INTERNAL_USER', 'CUSTOMER') THEN access_type::text
    WHEN access_type::text = 'INTERNAL' THEN 'INTERNAL_USER'
    WHEN access_type::text = 'EXTERNAL_LINK' THEN 'CUSTOMER'
    WHEN user_id IS NULL THEN 'CUSTOMER'
    ELSE 'INTERNAL_USER'
END
WHERE actor_type IS NULL
  AND EXISTS (
      SELECT 1
      FROM information_schema.columns
      WHERE table_name = 'access_logs'
        AND column_name = 'access_type'
  );

UPDATE access_logs
SET actor_type = 'INTERNAL_USER'
WHERE actor_type IS NULL;

UPDATE access_logs
SET message = failure_reason
WHERE message IS NULL
  AND EXISTS (
      SELECT 1
      FROM information_schema.columns
      WHERE table_name = 'access_logs'
        AND column_name = 'failure_reason'
  );

ALTER TABLE access_logs
    ALTER COLUMN actor_type SET NOT NULL;

ALTER TABLE access_logs
    ADD CONSTRAINT fk_access_logs_actor_user_id_users
    FOREIGN KEY (actor_user_id) REFERENCES users(id);

COMMIT;
