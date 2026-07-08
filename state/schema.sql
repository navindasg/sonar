-- Sonar live-state schema (SQLite).
--
-- This is the fast, disposable layer that workers write and the harness reads.
-- Durable/semantic memory lives in the Obsidian vault (via rag/), NOT here.
-- The DB file itself lives OUTSIDE the vault and is gitignored (*.sqlite*, state/*.db).
--
-- WAL mode is enabled in code (see db.py: PRAGMA journal_mode=WAL), not here,
-- because journal_mode is a per-connection/per-database runtime pragma.
--
-- All statements are idempotent (IF NOT EXISTS) so init can run repeatedly.

-- A generated daily brief: one row per assembled brief.
CREATE TABLE IF NOT EXISTS briefs (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,   -- ISO-8601 UTC timestamp of assembly
    window      TEXT NOT NULL,   -- 'morning' | 'any'
    title       TEXT NOT NULL,   -- human-readable heading
    body_md     TEXT NOT NULL,   -- the composed markdown body
    note_path   TEXT             -- absolute path to the vault note (NULL on dry-run)
);

CREATE INDEX IF NOT EXISTS idx_briefs_created_at ON briefs (created_at);

-- Audit trail of worker executions (success or failure), for observability.
CREATE TABLE IF NOT EXISTS worker_runs (
    id           INTEGER PRIMARY KEY,
    worker       TEXT NOT NULL,   -- e.g. 'brief-builder'
    started_at   TEXT NOT NULL,   -- ISO-8601 UTC
    finished_at  TEXT,            -- ISO-8601 UTC, NULL while running
    status       TEXT NOT NULL,   -- 'running' | 'ok' | 'error'
    detail       TEXT             -- free-form: brief id, error summary, etc.
);

CREATE INDEX IF NOT EXISTS idx_worker_runs_worker ON worker_runs (worker, started_at);

-- The assistant's OWN captured to-dos — its disposable working memory, distinct
-- from the user's durable Obsidian checkboxes (those live in the vault and are
-- read via the todo_list tool, never copied here). The harness writes these via
-- todo_add and reads them via state_read(kind='todos').
CREATE TABLE IF NOT EXISTS todos (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,                  -- ISO-8601 UTC of capture
    text        TEXT NOT NULL,                  -- the task, short imperative
    status      TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'done'
    due         TEXT,                           -- optional ISO date (YYYY-MM-DD)
    done_at     TEXT                            -- ISO-8601 UTC when done, else NULL
);

CREATE INDEX IF NOT EXISTS idx_todos_status ON todos (status, created_at);
