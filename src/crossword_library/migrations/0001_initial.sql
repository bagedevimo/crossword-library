CREATE TABLE puzzles (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    publisher text NOT NULL,
    source_id text NOT NULL,
    series text NOT NULL CHECK (series IN ('daily', 'mini', 'big')),
    puzzle_date date NOT NULL,
    source_url text NOT NULL,
    title text NOT NULL DEFAULT '',
    author text,
    download_attempts integer NOT NULL DEFAULT 0,
    last_download_attempt_at timestamptz,
    last_download_error text,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (publisher, source_id)
);

CREATE INDEX puzzles_puzzle_date_idx ON puzzles (puzzle_date DESC);
CREATE INDEX puzzles_source_url_idx ON puzzles (source_url);

CREATE TABLE puzzle_files (
    puzzle_id bigint NOT NULL REFERENCES puzzles (id) ON DELETE CASCADE,
    format text NOT NULL CHECK (format IN ('xd', 'puz')),
    content bytea NOT NULL,
    sha256 text NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (puzzle_id, format)
);

CREATE TABLE completions (
    puzzle_id bigint PRIMARY KEY REFERENCES puzzles (id) ON DELETE CASCADE,
    completed boolean NOT NULL,
    completed_at timestamptz,
    source text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sync_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'failed')),
    counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    errors jsonb NOT NULL DEFAULT '[]'::jsonb,
    token_expires_at timestamptz
);
