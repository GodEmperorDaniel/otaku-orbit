"""Anime metadata from a chain of providers, cached in SQLite.

METADATA_SOURCES sets the order (default: "mal,anilist" with a MAL client ID, else "anilist").
Each call tries the providers in order and uses the first that answers.
"""

import json
import logging
import os
import threading
import time

from .. import db
from . import anilist, jikan, mal
from .base import RANKINGS, RELATIONS, NotFound, SourceError, current_season

log = logging.getLogger("animeclub.sources")

PROVIDERS = {p.NAME: p for p in (mal, anilist, jikan)}
LABELS = {p.NAME: p.LABEL for p in PROVIDERS.values()}
REFRESH_DAYS = int(os.environ.get("REFRESH_DAYS", "7"))

__all__ = ["NotFound", "SourceError", "LABELS", "RANKINGS", "RELATIONS", "current_season", "active", "browse",
           "fetch_and_store", "ensure_links", "start_refresher", "wake_refresher", "status"]


def _configure():
    default = "mal,anilist" if mal.enabled() else "anilist"
    chosen = []
    for name in os.environ.get("METADATA_SOURCES", default).split(","):
        name = name.strip().lower()
        p = PROVIDERS.get(name)
        if not name:
            continue
        if p is None:
            log.warning("Unknown metadata source %r ignored (choose from %s)", name, ", ".join(PROVIDERS))
        elif not p.enabled():
            log.warning("Metadata source %r is listed but not configured (is MAL_CLIENT_ID set?)", name)
        elif p not in chosen:
            chosen.append(p)
    log.info("Metadata sources: %s", " → ".join(p.LABEL for p in chosen) or "none")
    return chosen


_active = _configure()
_cache = {}
_wake = threading.Event()


def active():
    return list(_active)


def _first(op, *args, providers=None):
    providers = _active if providers is None else providers
    errors, misses = [], 0
    for p in providers:
        try:
            return getattr(p, op)(*args)
        except NotFound:
            misses += 1
        except SourceError as e:
            errors.append(str(e))
    if providers and misses == len(providers):
        raise NotFound("Not found in any metadata source")
    raise SourceError("; ".join(errors) or "No metadata sources are configured")


def _store(items):
    stubs = 0
    with db.conn() as c:
        for a in items:
            db.upsert_anime(c, a)
            if "links" in a:
                db.set_links(c, a["mal_id"], a["links"])
            if "relations" in a:
                stubs += db.set_relations(c, a["mal_id"], a["relations"])
    if stubs:
        wake_refresher()
    return items


def _cached(key, ttl, fetch):
    hit = _cache.get(key)
    if hit and hit[0] > time.time() - ttl:
        return hit[1]
    items = _store(fetch())
    _cache[key] = (time.time(), items)
    return items


def _dedupe(items):
    seen, out = set(), []
    for a in items:
        if a["mal_id"] not in seen:
            seen.add(a["mal_id"])
            out.append(a)
    return out


def browse(q=None, mode="season", year=None, season=None):
    """Candidate list for the browse page, before filtering: search results, a season, or a ranking."""
    if q:
        q = q.strip()
        return _cached("q:" + q.lower(), 3600, lambda: _dedupe(_first("search", q)))
    if mode in RANKINGS:
        return _cached("rank:" + mode, 6 * 3600, lambda: _dedupe(_first("ranking", mode)))
    if not (year and season):
        year, season = current_season()
    if season == "any":
        return _whole_year(year)
    return _cached(f"season:{year}:{season}", 6 * 3600, lambda: _dedupe(_first("season", year, season)))


def _whole_year(year):
    """All four seasons of a year merged, most popular first. Each season is fetched (and cached) on its own."""
    items, errors = [], []
    for season in ("winter", "spring", "summer", "fall"):
        try:
            items += browse(mode="season", year=year, season=season)
        except SourceError as e:
            errors.append(f"{season}: {e}")
    if errors and not items:
        raise SourceError("; ".join(errors))
    if errors:
        log.warning("year %s is missing some seasons: %s", year, "; ".join(errors))
    return sorted(_dedupe(items), key=lambda a: -(a["popularity"] or 0))


def fetch_and_store(mal_id):
    return _store([_first("get", mal_id)])[0]


def ensure_links(mal_id, links_at):
    """Fetch streaming links if we've never looked them up, or the lookup is stale. Never raises."""
    fresh = links_at and links_at > time.strftime("%Y-%m-%d %H:%M:%S",
                                                    time.gmtime(time.time() - REFRESH_DAYS * 86400))
    if fresh:
        return None
    providers = [p for p in _active if hasattr(p, "streaming")]
    if not providers:
        return None
    try:
        links = _first("streaming", mal_id, providers=providers)
    except NotFound:
        links = []
    except SourceError as e:
        log.warning("streaming links for %s unavailable: %s", mal_id, e)
        return None
    with db.conn() as c:
        db.set_links(c, mal_id, links)
    return links


def status():
    """Provider health for the admin page."""
    out = []
    for p in PROVIDERS.values():
        h = p.health()
        out.append({
            "name": p.NAME,
            "label": p.LABEL,
            "enabled": p.enabled(),
            "position": _active.index(p) + 1 if p in _active else None,
            "last_ok": h.last_ok,
            "last_error": h.last_error,
            "cooling": h.down_until > time.monotonic(),
        })
    return sorted(out, key=lambda s: (s["position"] is None, s["position"] or 0))


def wake_refresher():
    _wake.set()


def start_refresher():
    if os.environ.get("DISABLE_REFRESH") == "1":
        return
    threading.Thread(target=_refresh_loop, name="metadata-refresh", daemon=True).start()


def _refresh_loop():
    """Fill in imported stubs, keep MAL scores fresh, and upgrade rows that came from a fallback source."""
    batch = 25
    while True:
        rows = []
        primary = _active[0].NAME if _active else None
        try:
            with db.conn() as c:
                rows = c.execute(
                    """SELECT mal_id FROM anime
                       WHERE fetched_at IS NULL
                          OR fetched_at < datetime('now', ?)
                          OR (source IS NOT ? AND fetched_at < datetime('now', '-6 hours'))
                       ORDER BY fetched_at IS NOT NULL, fetched_at LIMIT ?""",
                    (f"-{REFRESH_DAYS} days", primary, batch),
                ).fetchall()
            for r in rows:
                try:
                    fetch_and_store(r["mal_id"])
                except NotFound:
                    with db.conn() as c:
                        c.execute("UPDATE anime SET fetched_at = datetime('now'), source = ? WHERE mal_id = ?",
                                  (primary, r["mal_id"]))
                except SourceError as e:
                    log.warning("refresh of %s failed: %s", r["mal_id"], e)
                    rows = []  # everything is down; wait before trying again
                    break
                time.sleep(1.5)
        except Exception:
            log.exception("metadata refresher crashed; retrying later")
        if len(rows) < batch:
            _wake.wait(3600)
            _wake.clear()
