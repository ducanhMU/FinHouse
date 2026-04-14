-- ============================================================
-- FinHouse — Database Initialization
-- ============================================================

-- Negative-ID sequence for incognito/guest projects
CREATE SEQUENCE IF NOT EXISTS incognito_project_seq
    START WITH -1
    INCREMENT BY -1
    NO MAXVALUE
    NO MINVALUE
    CACHE 1;

-- ── Users ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS "user" (
    user_id       SERIAL       PRIMARY KEY,
    user_name     VARCHAR(128) NOT NULL UNIQUE,
    user_password VARCHAR(256),
    create_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Seed guest user (user_id = 0)
INSERT INTO "user" (user_id, user_name, user_password)
VALUES (0, 'guest', NULL)
ON CONFLICT (user_id) DO NOTHING;

-- Reset sequence so next real user starts at 1
SELECT setval(pg_get_serial_sequence('"user"', 'user_id'),
              GREATEST((SELECT MAX(user_id) FROM "user"), 0));

-- ── Projects ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS project (
    project_id    INTEGER      PRIMARY KEY,
    user_id       INTEGER      NOT NULL REFERENCES "user"(user_id),
    project_title VARCHAR(256) NOT NULL,
    description   TEXT,
    create_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    update_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Seed default inbox project (project_id = 0)
INSERT INTO project (project_id, user_id, project_title, description)
VALUES (0, 0, 'Default', 'Default inbox project')
ON CONFLICT (project_id) DO NOTHING;

-- Sequence for positive project IDs
CREATE SEQUENCE IF NOT EXISTS project_id_seq START WITH 1;

-- ── Chat Sessions ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chat_session (
    session_id    UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    INTEGER      NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
    session_title VARCHAR(512),
    create_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    update_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    model_used    VARCHAR(128) NOT NULL,
    tools_used    TEXT[],
    turn_count    INTEGER      NOT NULL DEFAULT 0,
    summary_count INTEGER      NOT NULL DEFAULT 0
);

-- ── Chat Events (append-only log) ──────────────────────────
CREATE TABLE IF NOT EXISTS chat_event (
    message_id UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID         NOT NULL REFERENCES chat_session(session_id) ON DELETE CASCADE,
    num_order  INTEGER      NOT NULL,
    role       VARCHAR(32)  NOT NULL,
    text       TEXT         NOT NULL,
    event_type VARCHAR(32)  NOT NULL,
    create_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── Files ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS file (
    file_id        UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        INTEGER      NOT NULL REFERENCES "user"(user_id),
    project_id     INTEGER      NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
    session_id     UUID         REFERENCES chat_session(session_id) ON DELETE SET NULL,
    file_hash      VARCHAR(64)  NOT NULL,
    file_name      VARCHAR(512) NOT NULL,
    file_type      VARCHAR(16)  NOT NULL,
    process_status VARCHAR(32)  NOT NULL DEFAULT 'pending',
    process_at     TIMESTAMPTZ,
    file_dir       VARCHAR(1024) NOT NULL
);

-- ── Indexes ─────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_session_project_update
    ON chat_session (project_id, update_at DESC);

CREATE INDEX IF NOT EXISTS idx_session_negative_project
    ON chat_session (project_id) WHERE project_id < 0;

CREATE INDEX IF NOT EXISTS idx_event_session_order
    ON chat_event (session_id, num_order ASC);

CREATE INDEX IF NOT EXISTS idx_event_session_type
    ON chat_event (session_id, event_type);

CREATE INDEX IF NOT EXISTS idx_file_project_status
    ON file (project_id, process_status);

CREATE INDEX IF NOT EXISTS idx_file_hash_project
    ON file (file_hash, project_id);

CREATE INDEX IF NOT EXISTS idx_project_user_update
    ON project (user_id, update_at DESC);
