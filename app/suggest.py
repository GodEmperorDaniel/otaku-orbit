"""Random "what should we watch next?" suggestions.

AniList is the only source that can filter by streaming platform, so suggestions always come from it,
whatever METADATA_SOURCES says. AniList no longer reports how many shows match a filter, so we find the
last page of matches by probing several pages per request, then draw random pages below it.

The queue of upcoming suggestions lives in the session; only the current one is ever rendered.
"""

import random
import time
from dataclasses import asdict, dataclass, field

from . import db, sources
from .sources import anilist, mal
from .sources.base import site_name

PER_PAGE = 25
MAX_PAGES = 5000 // PER_PAGE  # AniList refuses to page past its first 5,000 matches
BATCH_PAGES = 3
MIN_BATCH = 10  # draw more pages when local filtering leaves fewer suggestions than this
KEEP_BEHIND = 10  # how many past suggestions "Previous" can step back through
MAX_QUEUE = 200  # suggestions kept for the list view (the queue is stored in the session cookie)

KINDS = {
    "series": ("Series", ["TV", "TV_SHORT", "ONA"]),
    "movie": ("Movies", ["MOVIE"]),
    "extra": ("OVAs & specials", ["OVA", "SPECIAL"]),
}
SKIPS = {"mine": "Skip shows on my list", "club": "Skip shows anyone has started",
         "none": "Don't skip anything"}
# Shown up front; the rest (regional services and ones with a handful of shows) sit under "More platforms".
MAIN_SITES = ["Crunchyroll", "Netflix", "HIDIVE", "Prime Video", "Disney+", "Hulu", "Max", "YouTube", "Tubi",
              "RetroCrush", "Adult Swim"]
FALLBACK_SITES = [(5, "Crunchyroll"), (10, "Netflix"), (20, "HIDIVE"), (21, "Amazon Prime Video"), (7, "Hulu"),
                  (118, "Disney Plus")]
# Episode slider positions; the ends mean "no minimum" and "no maximum".
EP_STOPS = [1, 2, 3, 4, 6, 8, 10, 12, 13, 20, 24, 26, 39, 50, 75, 100, 150, 200, None]
# AniList's genres (its GenreCollection, minus Hentai, which adult filtering already hides).
GENRES = ["Action", "Adventure", "Comedy", "Drama", "Ecchi", "Fantasy", "Horror", "Mahou Shoujo", "Mecha", "Music",
          "Mystery", "Psychological", "Romance", "Sci-Fi", "Slice of Life", "Sports", "Supernatural", "Thriller"]


def _int(v, lo, hi):
    try:
        return min(hi, max(lo, int(v)))
    except (TypeError, ValueError):
        return None


def _float(v, lo, hi):
    try:
        return round(min(hi, max(lo, float(v))), 1)
    except (TypeError, ValueError):
        return None


@dataclass
class Filters:
    kinds: list = field(default_factory=lambda: ["series", "movie"])
    ep_min: int | None = None
    ep_max: int | None = None
    score_min: float | None = 7.0
    score_max: float | None = None
    platforms: list = field(default_factory=list)
    genres: list = field(default_factory=list)
    no_genres: list = field(default_factory=list)
    skip: str = "mine"
    first_only: bool = True
    no_spinoffs: bool = True
    no_recaps: bool = True

    @classmethod
    def from_form(cls, kinds, ep_lo, ep_hi, score_min, score_max, platforms, skip, first_only, no_spinoffs,
                  genres=(), no_genres=(), no_recaps=""):
        """ep_lo and ep_hi are slider positions in EP_STOPS."""
        last = len(EP_STOPS) - 1
        lo, hi = sorted([_int(ep_lo, 0, last) or 0, last if _int(ep_hi, 0, last) is None else _int(ep_hi, 0, last)])
        known = {name for name, _ in sites()}
        f = cls(
            kinds=[k for k in kinds if k in KINDS] or list(KINDS),
            ep_min=EP_STOPS[lo] if lo > 0 else None,
            ep_max=EP_STOPS[hi],
            score_min=_float(score_min, 1, 10),
            score_max=_float(score_max, 1, 10),
            platforms=sorted({p for p in platforms if p in known}),
            genres=[g for g in GENRES if g in genres],
            # A genre ticked both ways counts as wanted.
            no_genres=[g for g in GENRES if g in no_genres and g not in genres],
            skip=skip if skip in SKIPS else "mine",
            first_only=first_only == "1",
            no_spinoffs=no_spinoffs == "1",
            no_recaps=no_recaps == "1",
        )
        if f.score_min and f.score_max and f.score_min > f.score_max:
            f.score_min, f.score_max = f.score_max, f.score_min
        return f

    @classmethod
    def from_dict(cls, d):
        f = cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})
        f.platforms = [p for p in f.platforms if isinstance(p, str)]  # older sessions stored AniList IDs
        f.genres = [g for g in f.genres if g in GENRES]
        f.no_genres = [g for g in f.no_genres if g in GENRES]
        return f

    def ep_positions(self):
        """Slider positions for the current episode limits."""
        lo = max((i for i, s in enumerate(EP_STOPS[:-1]) if self.ep_min and s <= self.ep_min), default=0)
        hi = min((i for i, s in enumerate(EP_STOPS[:-1]) if self.ep_max and s >= self.ep_max),
                 default=len(EP_STOPS) - 1)
        return lo, hi

    def ep_label(self):
        a, b = self.ep_min, self.ep_max
        if not a and not b:
            return "Any length"
        if not a:
            return f"Up to {b} eps"
        if not b:
            return f"{a}+ eps"
        return f"{a} eps" if a == b else f"{a}–{b} eps"

    def platform_ids(self):
        ids = dict(sites())
        return sorted({i for name in self.platforms for i in ids.get(name, [])})

    def formats(self):
        return [fmt for k in self.kinds for fmt in KINDS[k][1]]

    def key(self):
        """What AniList is asked; skipping and the first-season, spin-off and recap checks happen locally."""
        return (tuple(self.kinds), self.ep_min, self.ep_max, self.score_min, self.score_max, tuple(self.platforms),
                tuple(self.genres), tuple(self.no_genres))


# Per filter: how many pages of matches there are and how many shows that is, found by probing.
_ranges = {}
RANGE_TTL = 6 * 3600
PROBES = 10  # pages checked per probing request


def _ask(f, pages, light):
    return anilist.discover(sorted(pages), PER_PAGE, f.formats(), f.platform_ids(), f.ep_min, f.ep_max,
                            f.score_min, f.score_max, f.genres, f.no_genres, light=light)


def _last_page(f):
    """Number of pages of matches (0 if none). Narrows in from both ends, PROBES pages per request,
    so it usually takes two or three requests and is then cached."""
    st = _ranges.get(f.key())
    if st and st["at"] > time.time() - RANGE_TTL:
        return st["pages"]
    lo, hi = 0, MAX_PAGES + 1  # page lo has matches (or lo is 0); page hi doesn't
    candidates = [1, 2, 4, 8, 16, 32, 64, 100, 150, MAX_PAGES]
    count = None
    while hi - lo > 1:
        found = _ask(f, candidates, light=True)
        for n in sorted(found):
            _, has_next, raw = found[n]
            if raw and not has_next:
                lo = hi = n  # the last page
                count = (n - 1) * PER_PAGE + raw
                break
            if raw:
                lo = max(lo, n)
            else:
                hi = min(hi, n)
        if lo == hi:
            break
        step = (hi - lo) / (PROBES + 1)
        candidates = sorted({min(hi - 1, max(lo + 1, round(lo + step * (k + 1)))) for k in range(PROBES)})
    if count is None:
        count = lo * PER_PAGE  # every probed page was full; close enough for a label
    pages = lo
    _ranges[f.key()] = {"at": time.time(), "pages": pages, "count": count}
    return pages


def match_count(f):
    st = _ranges.get(f.key())
    return st["count"] if st else None


def _draw(f, used):
    """Up to BATCH_PAGES random pages of matches not drawn before, fetched in one request."""
    last = _last_page(f)
    choices = [p for p in range(1, last + 1) if p not in used]
    pages = random.sample(choices, min(BATCH_PAGES, len(choices)))
    if not pages:
        return []
    used.extend(pages)
    return [a for items, _, _ in _ask(f, pages, light=False).values() for a in items]


def _skipped(f, user):
    if f.skip == "none" or (f.skip == "mine" and not user):
        return set()
    with db.conn() as c:
        if f.skip == "club":
            # Plan-to-watch doesn't count: someone wanting to see a show is a reason to suggest it.
            rows = c.execute("SELECT DISTINCT mal_id FROM entries WHERE status != 'plan'")
        else:
            rows = c.execute("SELECT mal_id FROM entries WHERE user_id = ?", (user["id"],))
        return {r["mal_id"] for r in rows}


# AniList's score for each suggestion, shown in the list view until the MAL score is fetched.
anilist_scores = {}

# AniList ID -> [(sequel's AniList ID, start)], filled in as sequel chains are followed.
_sequel_cache = {}
SEQUEL_HOPS = 3


def _later_prequels(shows):
    """MAL IDs of shows that have no prequel but aren't where their franchise starts either: following their
    sequels leads to something released earlier. Kizumonogatari (2016) is the start of the Monogatari story,
    and its sequels lead to Bakemonogatari (2009). Follows chains SEQUEL_HOPS steps, one request per step."""
    chains = {a["mal_id"]: (a["start"], [i for i, _ in a["sequels"]], set()) for a in shows if a["sequels"]}
    out = set()
    for _ in range(SEQUEL_HOPS):
        todo = {i for _, frontier, _ in chains.values() for i in frontier if i not in _sequel_cache}
        if todo:
            try:
                _sequel_cache.update(anilist.sequels(todo))
            except sources.SourceError:
                break  # rather show a few later prequels than nothing
        for mal_id, (start, frontier, seen) in list(chains.items()):
            nxt = [(i, st) for f in frontier for i, st in _sequel_cache.get(f, []) if i not in seen]
            seen.update(frontier)
            if any(st < start for _, st in nxt):
                out.add(mal_id)
                del chains[mal_id]
            elif nxt:
                chains[mal_id] = (start, [i for i, _ in nxt], seen)
            else:
                del chains[mal_id]
        if not chains:
            break
    return out


def batch(f, user, exclude, used_pages):
    """A shuffled list of new suggestion IDs, drawing BATCH_PAGES fresh pages at a time (up to three times
    while local filtering leaves fewer than MIN_BATCH). Updates used_pages in place. Raises SourceError if
    AniList is down."""
    skip = _skipped(f, user) | set(exclude)
    ids = []
    for _ in range(3):
        drawn = _draw(f, used_pages)
        sources.remember(drawn)
        anilist_scores.update((a["mal_id"], a["anilist_score"]) for a in drawn)
        kept = [a for a in drawn
                if a["mal_id"] not in skip
                and (a["first_season"] or not f.first_only)
                and not (a["spin_off"] and f.no_spinoffs)
                and not (a["recap"] and f.no_recaps)]
        later = _later_prequels(kept) if f.first_only else set()
        new = [a["mal_id"] for a in kept if a["mal_id"] not in later]
        ids = list(dict.fromkeys(ids + new))
        if len(ids) >= MIN_BATCH or not drawn:
            break
    random.shuffle(ids)
    return ids


def reveal(mal_id):
    """The stored row for a suggestion, upgraded to MyAnimeList data (for its MAL score) when possible."""
    with db.conn() as c:
        a = c.execute("SELECT * FROM anime WHERE mal_id = ?", (mal_id,)).fetchone()
    if a is None or (mal.enabled() and a["source"] != "mal"):
        try:
            sources.fetch_and_store(mal_id)
        except sources.SourceError:
            pass
        with db.conn() as c:
            a = c.execute("SELECT * FROM anime WHERE mal_id = ?", (mal_id,)).fetchone()
    if a is not None and sources.ensure_links(mal_id, a["links_at"]) is not None:
        with db.conn() as c:
            a = c.execute("SELECT * FROM anime WHERE mal_id = ?", (mal_id,)).fetchone()
    return a


_sites = (0.0, None)


def sites():
    """Streaming platforms to filter by, as [(name, [AniList IDs])] with names merged (see SITE_NAMES),
    main ones first in MAIN_SITES order. Cached for a day."""
    global _sites
    at, cached = _sites
    if cached is None or at < time.time() - 86400:
        try:
            raw = anilist.streaming_sites()
            at = time.time()
        except sources.SourceError:
            raw, at = FALLBACK_SITES, time.time() - 86400 + 300  # try again in five minutes
        merged = {}
        for site_id, name in raw:
            merged.setdefault(site_name(name), []).append(site_id)
        rank = {name: i for i, name in enumerate(MAIN_SITES)}
        cached = sorted(merged.items(), key=lambda s: (rank.get(s[0], len(rank)), s[0].lower()))
        _sites = (at, cached)
    return cached


def split_sites():
    """(main platforms, the rest) for the filter form."""
    all_sites = sites()
    return [s for s in all_sites if s[0] in MAIN_SITES], [s for s in all_sites if s[0] not in MAIN_SITES]


def rows(ids):
    """Stored rows for a list of suggestions, in order, without fetching anything."""
    with db.conn() as c:
        found = {r["mal_id"]: r for r in c.execute(
            f"SELECT * FROM anime WHERE mal_id IN ({','.join('?' * len(ids))})", ids)}
    return [found[i] for i in ids if i in found]


def state_dict(f, queue, index, pages):
    return {"f": asdict(f), "q": queue, "i": index, "pages": pages}
