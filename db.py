"""SQLite-бекенд приложения экспертной валидации."""
from __future__ import annotations
import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager

# Путь к БД: если задан DATA_DIR (в Railway это смонтированный Volume) — используем его.
# Иначе data/ внутри приложения.
_DEFAULT_DATA_DIR = Path(__file__).parent / "data"
DATA_DIR = Path(os.environ.get("DATA_DIR", str(_DEFAULT_DATA_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "annotations.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS experts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    full_name     TEXT,
    email         TEXT,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER DEFAULT 0,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS articles (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id     INTEGER UNIQUE,          -- id из исходного JSON Label Studio
    inner_id       INTEGER,
    code           TEXT NOT NULL,           -- ТК РК, УК РК, ГК РК, НК РК
    article_number INTEGER,
    title          TEXT,
    text           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    article_db_id INTEGER NOT NULL REFERENCES articles(id),
    entity_id     TEXT NOT NULL,
    start         INTEGER NOT NULL,
    end           INTEGER NOT NULL,
    text          TEXT,
    group_name    TEXT NOT NULL,
    label         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_article ON entities(article_db_id);
CREATE INDEX IF NOT EXISTS idx_entities_entity_id ON entities(entity_id);

CREATE TABLE IF NOT EXISTS relations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    article_db_id   INTEGER NOT NULL REFERENCES articles(id),
    rel_id          TEXT NOT NULL,
    from_entity_id  TEXT NOT NULL,
    to_entity_id    TEXT NOT NULL,
    from_text       TEXT,
    to_text         TEXT,
    from_label      TEXT,
    to_label        TEXT,
    relation_type   TEXT NOT NULL,
    confidence      REAL,
    trigger_phrase  TEXT,
    algorithm       TEXT,
    source          TEXT DEFAULT 'auto',
    UNIQUE(article_db_id, rel_id)
);
CREATE INDEX IF NOT EXISTS idx_relations_article ON relations(article_db_id);

CREATE TABLE IF NOT EXISTS votes (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    expert_id                INTEGER NOT NULL REFERENCES experts(id),
    relation_id              INTEGER NOT NULL REFERENCES relations(id),
    verdict                  TEXT CHECK(verdict IN ('approve','reject','modify')) NOT NULL,
    correction_relation_type TEXT,
    correction_from_id       TEXT,
    correction_to_id         TEXT,
    comment                  TEXT,
    created_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(expert_id, relation_id)
);
CREATE INDEX IF NOT EXISTS idx_votes_relation ON votes(relation_id);
CREATE INDEX IF NOT EXISTS idx_votes_expert ON votes(expert_id);

CREATE TABLE IF NOT EXISTS expert_additions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    expert_id      INTEGER NOT NULL REFERENCES experts(id),
    article_db_id  INTEGER NOT NULL REFERENCES articles(id),
    from_entity_id TEXT NOT NULL,
    to_entity_id   TEXT NOT NULL,
    relation_type  TEXT NOT NULL,
    comment        TEXT,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS progress (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    expert_id     INTEGER NOT NULL REFERENCES experts(id),
    article_db_id INTEGER NOT NULL REFERENCES articles(id),
    completed     INTEGER DEFAULT 0,
    completed_at  TIMESTAMP,
    UNIQUE(expert_id, article_db_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    sid        TEXT PRIMARY KEY,
    expert_id  INTEGER NOT NULL REFERENCES experts(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def cursor():
    conn = get_conn()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def init_db():
    with cursor() as cur:
        cur.executescript(SCHEMA)


if __name__ == "__main__":
    init_db()
    print(f"DB initialized at {DB_PATH}")
