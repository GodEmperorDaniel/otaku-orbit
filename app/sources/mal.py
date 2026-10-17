"""The official MyAnimeList API v2 (https://myanimelist.net/apiconfig/references/api/v2).

Public data only needs a client ID, sent as the X-MAL-CLIENT-ID header.
"""

import os
import re

from .base import SFW, Client, SourceError, record

NAME = "mal"
LABEL = "MyAnimeList"
CLIENT_ID = os.environ.get("MAL_CLIENT_ID", "").strip()
FIELDS = ("id,title,main_picture,alternative_titles,start_date,synopsis,mean,rank,num_scoring_users,"
          "media_type,status,num_episodes,start_season,genres,studios,num_list_users")
MEDIA_TYPES = {"tv": "TV", "movie": "Movie", "ova": "OVA", "ona": "ONA", "special": "Special",
               "tv_special": "TV Special", "music": "Music", "cm": "CM", "pv": "PV"}
STATUSES = {"finished_airing": "Finished Airing", "currently_airing": "Currently Airing",
            "not_yet_aired": "Not yet aired"}
RANKING_TYPES = {"top": "all", "popular": "bypopularity", "airing": "airing", "upcoming": "upcoming"}

_client = Client(LABEL, "https://api.myanimelist.net/v2", 0.5, headers={"X-MAL-CLIENT-ID": CLIENT_ID})


def enabled():
    return bool(CLIENT_ID)


def _params(**extra):
    params = {"fields": FIELDS, **extra}
    if not SFW:
        params["nsfw"] = "true"
    return params


def normalize(n):
    pic = n.get("main_picture") or {}
    season = n.get("start_season") or {}
    start = n.get("start_date") or ""
    return record(
        mal_id=n["id"],
        title=n.get("title") or f"#{n['id']}",
        title_english=(n.get("alternative_titles") or {}).get("en") or None,
        image_url=pic.get("large") or pic.get("medium"),
        synopsis=n.get("synopsis") or None,
        type=MEDIA_TYPES.get(n.get("media_type"), (n.get("media_type") or "").upper() or None),
        episodes=n.get("num_episodes") or None,
        status=STATUSES.get(n.get("status"), n.get("status")),
        year=season.get("year") or (int(start[:4]) if start[:4].isdigit() else None),
        season=season.get("season"),
        mal_score=n.get("mean"),
        mal_scored_by=n.get("num_scoring_users"),
        mal_rank=n.get("rank"),
        genres=[g["name"] for g in n.get("genres") or []],
        studios=[s["name"] for s in n.get("studios") or []],
        source=NAME,
        popularity=n.get("num_list_users") or 0,
        start_date=start or None,
    )


def _squash(text):
    return re.sub(r"[^0-9a-z]", "", (text or "").lower())


def _list(path, **params):
    return [normalize(d["node"]) for d in _client.request("GET", path, params=_params(**params))["data"]]


def search(q):
    if len(q) < 3:
        raise SourceError(f"{LABEL} needs at least 3 characters to search")  # the chain falls through
    items = _list("/anime", q=q[:64], limit=100)
    # MAL's own ordering is loose: put titles containing the query first, most popular first within each group.
    key = _squash(q)
    items.sort(key=lambda a: (key not in _squash(a["title"]) + "|" + _squash(a["title_english"]), -a["popularity"]))
    return items


def season(year, season):
    return _list(f"/anime/season/{year}/{season}", sort="anime_num_list_users", limit=500)


def ranking(kind):
    return _list("/anime/ranking", ranking_type=RANKING_TYPES[kind], limit=500)


def get(mal_id):
    return normalize(_client.request("GET", f"/anime/{mal_id}", params=_params()))


def health():
    return _client
