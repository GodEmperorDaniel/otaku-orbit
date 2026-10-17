"""Jikan (https://jikan.moe), an unofficial API that scrapes MyAnimeList. Opt-in via METADATA_SOURCES."""

import os

from .base import SFW, Client, link, record, relation

NAME = "jikan"
LABEL = "Jikan"
TOP_FILTERS = {"top": None, "popular": "bypopularity", "airing": "airing", "upcoming": "upcoming"}

_client = Client(LABEL, os.environ.get("JIKAN_URL", "https://api.jikan.moe/v4"), 0.5, timeout=15)


def enabled():
    return True


def _names(d, key):
    return [x["name"] for x in d.get(key) or [] if x.get("name")]


def normalize(d):
    images = d.get("images") or {}
    webp, jpg = images.get("webp") or {}, images.get("jpg") or {}
    aired_from = ((d.get("aired") or {}).get("prop") or {}).get("from") or {}
    return record(
        mal_id=d["mal_id"],
        title=d.get("title") or d.get("title_english") or f"#{d['mal_id']}",
        title_english=d.get("title_english"),
        image_url=webp.get("large_image_url") or jpg.get("large_image_url") or jpg.get("image_url"),
        synopsis=d.get("synopsis"),
        type=d.get("type"),
        episodes=d.get("episodes"),
        status=d.get("status"),
        year=d.get("year") or aired_from.get("year"),
        season=d.get("season"),
        mal_score=d.get("score"),
        mal_scored_by=d.get("scored_by"),
        mal_rank=d.get("rank"),
        genres=_names(d, "genres") + _names(d, "themes") + _names(d, "demographics"),
        studios=_names(d, "studios"),
        url=d.get("url"),
        source=NAME,
        popularity=d.get("members") or 0,
        start_date=((d.get("aired") or {}).get("from") or "")[:10] or None,
    )


def _params(**extra):
    extra = {k: v for k, v in extra.items() if v is not None}
    return {**extra, "sfw": "true"} if SFW else extra


def _list(path, **params):
    seen, items = set(), []
    for d in _client.request("GET", path, params=_params(**params))["data"]:
        if d["mal_id"] not in seen:  # Jikan sometimes repeats entries
            seen.add(d["mal_id"])
            items.append(normalize(d))
    return items


def search(q):
    return _list("/anime", q=q, limit=25, order_by="members", sort="desc")


def season(year, season):
    return _list(f"/seasons/{year}/{season}", limit=25)


def ranking(kind):
    return _list("/top/anime", filter=TOP_FILTERS[kind], limit=25)


def get(mal_id):
    d = _client.request("GET", f"/anime/{mal_id}/full")["data"]
    a = normalize(d)
    a["relations"] = [
        relation(e["mal_id"], e.get("name"), None, (r.get("relation") or "").lower().replace(" ", "_").replace("-", "_"))
        for r in d.get("relations") or [] for e in r.get("entry") or [] if e.get("type") == "anime"
    ]
    return a


def streaming(mal_id):
    data = _client.request("GET", f"/anime/{mal_id}/streaming")["data"]
    return [link(x["name"], x["url"]) for x in data if x.get("url")]


def health():
    return _client
