"""Shared plumbing for metadata providers.

Every provider module exposes NAME, LABEL, enabled(), search(q), season(year, season), ranking(kind),
get(mal_id) and health(); providers that know streaming platforms also expose streaming(mal_id).
Anime are returned as dicts shaped like `record()` below, keyed by MyAnimeList ID.
"""

import datetime as dt
import json
import logging
import os
import threading
import time

import httpx

log = logging.getLogger("animeclub.sources")

SFW = os.environ.get("SFW", os.environ.get("JIKAN_SFW", "1")) == "1"
COOLDOWN = 120  # seconds to skip a provider after it fails at the network level


class SourceError(Exception):
    pass


class NotFound(SourceError):
    pass


def record(**fields):
    base = dict.fromkeys(
        ["mal_id", "title", "title_english", "image_url", "synopsis", "type", "episodes", "status", "year",
         "season", "mal_score", "mal_scored_by", "mal_rank", "url", "source",
         # Not stored in the database; only used to sort and filter browse results.
         "popularity", "start_date"])
    base.update(fields)
    base["genres"] = json.dumps(fields.get("genres") or [])
    base["studios"] = json.dumps(fields.get("studios") or [])
    base["url"] = base["url"] or f"https://myanimelist.net/anime/{base['mal_id']}"
    return base


RANKINGS = ("top", "popular", "airing", "upcoming")


def link(site, url, color=None, icon=None, note=None):
    return {"site": site, "url": url, "color": color, "icon": icon, "note": note}


def current_season(today=None):
    today = today or dt.date.today()
    return today.year, ["winter", "spring", "summer", "fall"][(today.month - 1) // 3]


class Client:
    """Rate-limited JSON HTTP client that records its health for the admin page."""

    def __init__(self, label, base_url, min_interval, headers=None, timeout=8, retries=1):
        self.label = label
        self.http = httpx.Client(base_url=base_url, timeout=timeout,
                                 headers={"User-Agent": "animeclub/1.0", **(headers or {})})
        self.min_interval = min_interval
        self.retries = retries
        self._lock = threading.Lock()
        self._last = 0.0
        self.down_until = 0.0
        self.last_ok = None
        self.last_error = None  # (unix time, message)

    def request(self, method, path, **kw):
        if time.monotonic() < self.down_until:
            raise SourceError(f"{self.label} is cooling down after recent failures")
        error = None
        for attempt in range(self.retries + 1):
            with self._lock:
                wait = self._last + self.min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            try:
                r = self.http.request(method, path, **kw)
            except httpx.HTTPError as e:
                error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            else:
                if r.status_code == 404:
                    self.last_ok = time.time()
                    raise NotFound(f"{self.label}: not found")
                if r.status_code == 429 or r.status_code >= 500:
                    error = f"HTTP {r.status_code}"
                elif r.status_code >= 400:
                    # Our request was rejected (bad key, bad query); retrying or cooling down won't help.
                    self.last_error = (time.time(), f"HTTP {r.status_code}: {r.text[:200]}")
                    raise SourceError(f"{self.label} rejected the request (HTTP {r.status_code})")
                else:
                    self.last_ok = time.time()
                    return r.json()
            if attempt < self.retries:
                time.sleep(1.5 * (attempt + 1))
        self.last_error = (time.time(), error)
        self.down_until = time.monotonic() + COOLDOWN
        log.warning("%s failed (%s); skipping it for %ss", self.label, error, COOLDOWN)
        raise SourceError(f"{self.label} is unavailable ({error})")
