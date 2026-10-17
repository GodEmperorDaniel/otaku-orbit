"""Filtering, sorting and facet counts for the browse/search page.

Sources hand us a candidate list (up to 100 search results, or up to 500 for a season or ranking)
and everything here runs locally, so filters behave the same whichever source answered.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlencode

PER_PAGE = 36
SEASONS = ["winter", "spring", "summer", "fall"]
MODES = {
    "season": "Season",
    "top": "Top rated",
    "popular": "Most popular",
    "airing": "Airing now",
    "upcoming": "Upcoming",
}
STATUS_FILTERS = {
    "airing": ("Airing", {"Currently Airing"}),
    "finished": ("Finished", {"Finished Airing"}),
    "upcoming": ("Not yet aired", {"Not yet aired"}),
}
SORTS = {
    "default": "Default order",
    "popularity": "Popularity",
    "score": "MAL score",
    "newest": "Newest first",
    "oldest": "Oldest first",
    "title": "Title A–Z",
}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class Filters:
    q: str = ""
    mode: str = "season"
    year: int | None = None
    season: str | None = None
    types: list = field(default_factory=list)
    genres: list = field(default_factory=list)
    status: str = ""
    min_score: float | None = None
    year_from: int | None = None
    year_to: int | None = None
    sort: str = "default"
    hide_mine: bool = False
    continuing: bool = False
    page: int = 1
    open: bool = False  # whether the filter panel is expanded

    @classmethod
    def from_query(cls, params, default_season):
        f = cls(
            q=(params.get("q") or "").strip(),
            mode=params.get("mode") if params.get("mode") in MODES else "season",
            year=_int(params.get("year")),
            season=params.get("season") if params.get("season") in SEASONS + ["any"] else None,
            types=[t for t in params.getlist("type") if t],
            genres=[g for g in params.getlist("genre") if g],
            status=params.get("status") if params.get("status") in STATUS_FILTERS else "",
            min_score=_float(params.get("min_score")),
            year_from=_int(params.get("year_from")),
            year_to=_int(params.get("year_to")),
            sort=params.get("sort") if params.get("sort") in SORTS else "default",
            hide_mine=params.get("hide_mine") == "1",
            continuing=params.get("continuing") == "1",
            page=max(1, _int(params.get("page")) or 1),
            open=params.get("open") == "1",
        )
        if f.mode == "season":
            f.year = f.year or default_season[0]
            f.season = f.season or (default_season[1] if f.year == default_season[0] else "any")
        return f

    @property
    def active_count(self):
        return sum(bool(x) for x in (self.types, self.genres, self.status, self.min_score is not None,
                                     self.year_from, self.year_to, self.hide_mine))

    def query(self, **overrides):
        """URL query string for these filters, with some values replaced (None drops a key)."""
        values = {
            "q": self.q or None,
            "mode": None if self.q else self.mode,
            "year": self.year if not self.q and self.mode == "season" else None,
            "season": self.season if not self.q and self.mode == "season" else None,
            "type": self.types,
            "genre": self.genres,
            "status": self.status or None,
            "min_score": self.min_score,
            "year_from": self.year_from,
            "year_to": self.year_to,
            "sort": None if self.sort == "default" else self.sort,
            "hide_mine": "1" if self.hide_mine else None,
            "continuing": "1" if self.continuing else None,
            "page": None,
        }
        values.update(overrides)
        pairs = []
        for k, v in values.items():
            for item in (v if isinstance(v, list) else [v]):
                if item not in (None, ""):
                    pairs.append((k, item))
        return urlencode(pairs)


def _genres(a):
    return json.loads(a["genres"] or "[]")


def apply(items, f, mine=frozenset()):
    """Returns (filtered and sorted items, facets) — facets count what's left in the candidate list."""
    if f.mode == "season" and not f.q and not f.continuing:
        # MAL lists shows still running from earlier seasons; keep only premieres by default.
        if f.season == "any":
            items = [a for a in items if not a["year"] or a["year"] == f.year]
        else:
            items = [a for a in items if not a["year"] or (a["year"], a["season"]) == (f.year, f.season)
                     or a["season"] is None]

    def keep(a, skip=None):
        if skip != "type" and f.types and a["type"] not in f.types:
            return False
        if f.genres and not set(f.genres) <= set(_genres(a)):
            return False
        if f.status and a["status"] not in STATUS_FILTERS[f.status][1]:
            return False
        if f.min_score is not None and (a["mal_score"] or 0) < f.min_score:
            return False
        if f.year_from and (not a["year"] or a["year"] < f.year_from):
            return False
        if f.year_to and (not a["year"] or a["year"] > f.year_to):
            return False
        if f.hide_mine and a["mal_id"] in mine:
            return False
        return True

    out = [a for a in items if keep(a)]
    # Each count is what you'd get by also ticking that option. Types combine with OR, so their counts
    # ignore the type filter; genres combine with AND, so their counts come from the current results.
    type_counts = Counter(a["type"] for a in items if a["type"] and keep(a, skip="type"))
    genre_counts = Counter(g for a in out for g in _genres(a))

    if f.sort == "popularity":
        out.sort(key=lambda a: -(a["popularity"] or 0))
    elif f.sort == "score":
        out.sort(key=lambda a: (a["mal_score"] is None, -(a["mal_score"] or 0)))
    elif f.sort in ("newest", "oldest"):
        dated = [a for a in out if a["start_date"] or a["year"]]
        dated.sort(key=lambda a: a["start_date"] or str(a["year"]), reverse=f.sort == "newest")
        out = dated + [a for a in out if not (a["start_date"] or a["year"])]
    elif f.sort == "title":
        out.sort(key=lambda a: (a["title_english"] or a["title"]).lower())

    facets = {
        "types": sorted(type_counts.items(), key=lambda t: -t[1]),
        "genres": sorted(genre_counts.items(), key=lambda t: (-t[1], t[0])),
    }
    return out, facets
