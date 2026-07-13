-- AEO Monitor: схема БД (совместима с существующими aeo.mentions/aeo.responses)
CREATE SCHEMA IF NOT EXISTS aeo;

CREATE TABLE IF NOT EXISTS aeo.responses (
    week_start    date        NOT NULL,
    query_id      text        NOT NULL,
    provider      text        NOT NULL,
    query_text    text        NOT NULL,
    response_text text,
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week_start, query_id, provider)
);

CREATE TABLE IF NOT EXISTS aeo.mentions (
    week_start     date    NOT NULL,
    query_id       text    NOT NULL,
    provider       text    NOT NULL,
    brand          text    NOT NULL,
    is_ours        boolean NOT NULL DEFAULT false,
    mentioned      boolean NOT NULL DEFAULT false,
    first_position int,
    mention_count  int     NOT NULL DEFAULT 0,
    PRIMARY KEY (week_start, query_id, provider, brand)
);

-- НОВОЕ (этап 1): какие URL цитируют AI — карта следа
CREATE TABLE IF NOT EXISTS aeo.citations (
    week_start  date    NOT NULL,
    query_id    text    NOT NULL,
    provider    text    NOT NULL,
    url         text    NOT NULL,
    domain      text    NOT NULL,
    position    int,
    is_ours     boolean NOT NULL DEFAULT false,
    source_type text    NOT NULL DEFAULT 'third_party',  -- own_site|social|marketplace|third_party
    title       text,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week_start, query_id, provider, url)
);

CREATE INDEX IF NOT EXISTS idx_citations_week   ON aeo.citations (week_start);
CREATE INDEX IF NOT EXISTS idx_citations_domain ON aeo.citations (domain);
CREATE INDEX IF NOT EXISTS idx_mentions_week    ON aeo.mentions (week_start);
