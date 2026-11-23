-- Схема создаётся автоматически при ПЕРВОМ старте контейнера db
-- (смонтирована в /docker-entrypoint-initdb.d). Повторно не выполняется, пока жив том pgdata.

CREATE TABLE IF NOT EXISTS users (
    username   TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS groups (
    id         BIGSERIAL PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memberships (
    group_id  BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    username  TEXT   NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, username)
);

CREATE TABLE IF NOT EXISTS messages (
    id         BIGSERIAL PRIMARY KEY,
    group_id   BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    author     TEXT   NOT NULL REFERENCES users(username),
    text       TEXT   NOT NULL CHECK (length(text) BETWEEN 1 AND 2000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- «последние N сообщений группы» — самый частый запрос
CREATE INDEX IF NOT EXISTS idx_messages_group_id_id ON messages (group_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_memberships_username ON memberships (username);
