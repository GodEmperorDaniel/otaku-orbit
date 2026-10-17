import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.environ.get("ANIMECLUB_DB", str(Path(__file__).resolve().parent.parent / "data" / "animeclub.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    color         TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS anime (
    mal_id        INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    title_english TEXT,
    image_url     TEXT,
    synopsis      TEXT,
    type          TEXT,
    episodes      INTEGER,
    status        TEXT,
    year          INTEGER,
    season        TEXT,
    mal_score     REAL,
    mal_scored_by INTEGER,
    mal_rank      INTEGER,
    genres        TEXT,
    studios       TEXT,
    url           TEXT,
    source        TEXT,
    fetched_at    TEXT,
    links         TEXT,
    links_at      TEXT
);
CREATE TABLE IF NOT EXISTS entries (
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mal_id           INTEGER NOT NULL REFERENCES anime(mal_id),
    status           TEXT NOT NULL DEFAULT 'plan',
    score            REAL,
    episodes_watched INTEGER NOT NULL DEFAULT 0,
    notes            TEXT,
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, mal_id)
);
CREATE INDEX IF NOT EXISTS entries_mal ON entries(mal_id);
CREATE TABLE IF NOT EXISTS picks (
    id         INTEGER PRIMARY KEY,
    mal_id     INTEGER NOT NULL REFERENCES anime(mal_id),
    start_date TEXT NOT NULL,
    end_date   TEXT NOT NULL,
    picked_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    note       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _connect():
    c = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


@contextmanager
def conn():
    c = _connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        # Migrations for databases created by older versions.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(anime)")}
        if "source" not in cols:
            c.execute("ALTER TABLE anime ADD COLUMN source TEXT")
            c.execute("UPDATE anime SET source = 'jikan' WHERE fetched_at IS NOT NULL")
        for col in ("links", "links_at"):
            if col not in cols:
                c.execute(f"ALTER TABLE anime ADD COLUMN {col} TEXT")


def get_setting(key, default=None):
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with conn() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _setting_or_generate(key, env, factory):
    if os.environ.get(env):
        return os.environ[env]
    value = get_setting(key)
    if not value:
        value = factory()
        set_setting(key, value)
    return value


def secret_key():
    return _setting_or_generate("secret_key", "SECRET_KEY", lambda: secrets.token_hex(32))


def invite_code():
    return _setting_or_generate("invite_code", "INVITE_CODE", lambda: secrets.token_urlsafe(8))


def users(c):
    return c.execute("SELECT * FROM users ORDER BY id").fetchall()


def set_links(c, mal_id, links):
    """Streaming platforms for a show: a JSON list of {site, url, color, icon, note}."""
    c.execute("UPDATE anime SET links = ?, links_at = datetime('now') WHERE mal_id = ?", (json.dumps(links), mal_id))


def upsert_anime(c, a):
    c.execute(
        """
        INSERT INTO anime (mal_id, title, title_english, image_url, synopsis, type, episodes, status, year,
                           season, mal_score, mal_scored_by, mal_rank, genres, studios, url, source, fetched_at)
        VALUES (:mal_id, :title, :title_english, :image_url, :synopsis, :type, :episodes, :status, :year,
                :season, :mal_score, :mal_scored_by, :mal_rank, :genres, :studios, :url, :source, datetime('now'))
        ON CONFLICT(mal_id) DO UPDATE SET
            title = excluded.title, title_english = excluded.title_english, image_url = excluded.image_url,
            synopsis = excluded.synopsis, type = excluded.type, episodes = excluded.episodes,
            status = excluded.status, year = excluded.year, season = excluded.season,
            -- Only MAL-backed sources carry MAL scores; a fallback source must not erase them.
            mal_score = CASE WHEN excluded.source IN ('mal', 'jikan') THEN excluded.mal_score ELSE anime.mal_score END,
            mal_scored_by = CASE WHEN excluded.source IN ('mal', 'jikan') THEN excluded.mal_scored_by ELSE anime.mal_scored_by END,
            mal_rank = CASE WHEN excluded.source IN ('mal', 'jikan') THEN excluded.mal_rank ELSE anime.mal_rank END,
            genres = excluded.genres, studios = excluded.studios, url = excluded.url,
            source = excluded.source, fetched_at = excluded.fetched_at
        """,
        a,
    )
