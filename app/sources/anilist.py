"""AniList's GraphQL API (https://docs.anilist.co). No key needed.

AniList stores each show's MyAnimeList ID, so results line up with the rest of the app.
Its scores are AniList's own, so they are not stored as MAL scores.
It's also where streaming links come from: MAL's API doesn't provide them.
"""

import html
import re

from .base import SFW, Client, NotFound, link, record, relation, unique_links

NAME = "anilist"
LABEL = "AniList"
FORMATS = {"TV": "TV", "TV_SHORT": "TV Short", "MOVIE": "Movie", "SPECIAL": "Special", "OVA": "OVA",
           "ONA": "ONA", "MUSIC": "Music"}
STATUSES = {"FINISHED": "Finished Airing", "RELEASING": "Currently Airing", "NOT_YET_RELEASED": "Not yet aired",
            "CANCELLED": "Cancelled", "HIATUS": "On hiatus"}
RANKINGS = {
    "top": ("", "SCORE_DESC"),
    "popular": ("", "POPULARITY_DESC"),
    "airing": ("status: RELEASING,", "POPULARITY_DESC"),
    "upcoming": ("status: NOT_YET_RELEASED,", "POPULARITY_DESC"),
}

MEDIA = """
    idMal title { romaji english } coverImage { extraLarge large } description(asHtml: false)
    format episodes status season seasonYear startDate { year month day } genres siteUrl popularity
    studios(isMain: true) { nodes { name } }
"""
LINKS = "externalLinks { site url type language color icon notes isDisabled }"
RELATIONS = "relations { edges { relationType node { idMal type title { romaji english } coverImage { large } } } }"
RELATION_TYPES = {"PREQUEL": "prequel", "SEQUEL": "sequel", "PARENT": "parent_story", "SIDE_STORY": "side_story",
                  "SPIN_OFF": "spin_off", "ALTERNATIVE": "alternative_version", "SUMMARY": "summary",
                  "COMPILATION": "summary", "CONTAINS": "full_story", "CHARACTER": "character", "OTHER": "other"}

_client = Client(LABEL, "https://graphql.anilist.co", 1.0)


def enabled():
    return True


def _query(query, **variables):
    data = _client.request("POST", "", json={"query": query, "variables": variables})
    return data["data"]


def _clean(text):
    if not text:
        return None
    text = re.sub(r"<br\s*/?>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip() or None


def _date(d):
    d = d or {}
    parts = [f"{d['year']:04d}" if d.get("year") else None,
             f"{d['month']:02d}" if d.get("month") else None,
             f"{d['day']:02d}" if d.get("day") else None]
    out = []
    for p in parts:
        if p is None:
            break
        out.append(p)
    return "-".join(out) or None


def _links(m):
    out = []
    for x in m.get("externalLinks") or []:
        if x.get("type") == "STREAMING" and not x.get("isDisabled") and x.get("url"):
            note = x.get("notes") or (x.get("language") if x.get("language") not in (None, "English") else None)
            out.append(link(x["site"], x["url"], x.get("color"), x.get("icon"), note))
    return unique_links(out)


def normalize(m):
    title = m.get("title") or {}
    cover = m.get("coverImage") or {}
    a = record(
        mal_id=m["idMal"],
        title=title.get("romaji") or title.get("english") or f"#{m['idMal']}",
        title_english=title.get("english"),
        image_url=cover.get("extraLarge") or cover.get("large"),
        synopsis=_clean(m.get("description")),
        type=FORMATS.get(m.get("format"), m.get("format")),
        episodes=m.get("episodes"),
        status=STATUSES.get(m.get("status"), m.get("status")),
        year=m.get("seasonYear") or (m.get("startDate") or {}).get("year"),
        season=(m.get("season") or "").lower() or None,
        genres=m.get("genres") or [],
        studios=[s["name"] for s in (m.get("studios") or {}).get("nodes") or []],
        source=NAME,
        popularity=m.get("popularity") or 0,
        start_date=_date(m.get("startDate")),
    )
    if "externalLinks" in m:
        a["links"] = _links(m)
    if "relations" in m:
        a["relations"] = [
            relation(e["node"]["idMal"], (e["node"].get("title") or {}).get("romaji"),
                     (e["node"].get("coverImage") or {}).get("large"), RELATION_TYPES[e["relationType"]])
            for e in (m["relations"] or {}).get("edges") or []
            # Manga adaptations and shows MAL doesn't have can't be linked to.
            if e["node"].get("type") == "ANIME" and e["node"].get("idMal") and e.get("relationType") in RELATION_TYPES
        ]
    return a


def _page(declarations, filters, variables):
    decl = ", ".join(["$adult: Boolean"] + ([declarations] if declarations else []))
    query = f"""query ({decl}) {{
        Page(perPage: 50) {{ media(type: ANIME, isAdult: $adult, {filters}) {{ {MEDIA} }} }}
    }}"""
    media = _query(query, adult=False if SFW else None, **variables)["Page"]["media"]
    return [normalize(m) for m in media if m.get("idMal")]  # skip shows MAL doesn't have


def search(q):
    return _page("$q: String", "search: $q, sort: [SEARCH_MATCH, POPULARITY_DESC]", {"q": q})


def season(year, season):
    return _page("$season: MediaSeason, $year: Int", "season: $season, seasonYear: $year, sort: POPULARITY_DESC",
                 {"season": season.upper(), "year": year})


def ranking(kind):
    status, sort = RANKINGS[kind]
    return _page("", f"{status} sort: {sort}", {})


def get(mal_id):
    data = _query(f"query ($id: Int) {{ Media(idMal: $id, type: ANIME) {{ {MEDIA} {LINKS} {RELATIONS} }} }}", id=mal_id)
    if not data.get("Media"):
        raise NotFound(f"{LABEL}: not found")
    return normalize(data["Media"])


def streaming(mal_id):
    data = _query(f"query ($id: Int) {{ Media(idMal: $id, type: ANIME) {{ {LINKS} }} }}", id=mal_id)
    if not data.get("Media"):
        raise NotFound(f"{LABEL}: not found")
    return _links(data["Media"])


def discover(pages, per_page, formats, platforms=(), ep_min=None, ep_max=None, score_min=None, score_max=None,
             genres=(), no_genres=(), light=False):
    """Several pages of released shows matching the random-picker filters, most popular first, in one request.

    Returns {page: (records, has_next, raw_count)}. With light=True, records are just MAL IDs (for finding
    where the matches end); otherwise each record gets "first_season": False when it has a prequel, and
    "spin_off": True when it links to a parent anime (spin-offs and side stories do; main series don't),
    "recap": True for a movie, OVA or special that retells an earlier series (see _is_recap), and
    "anilist_score" on a 1–10 scale. Shows must have every genre in genres and none in no_genres.
    AniList won't page past its first 5,000 matches, so the pool is the 5,000 most popular."""
    decl = ["$perPage: Int", "$formats: [MediaFormat]"]
    filters = ["format_in: $formats", "status_not: NOT_YET_RELEASED", "sort: POPULARITY_DESC"]
    variables = {"perPage": per_page, "formats": list(formats)}
    # AniList's comparisons are exclusive and its scores run 0–100.
    for name, gql, kind, value in [
        ("platforms", "licensedById_in", "[Int]", list(platforms) or None),
        ("epMin", "episodes_greater", "Int", ep_min - 1 if ep_min else None),
        ("epMax", "episodes_lesser", "Int", ep_max + 1 if ep_max else None),
        ("scoreMin", "averageScore_greater", "Int", round(score_min * 10) - 1 if score_min else None),
        ("scoreMax", "averageScore_lesser", "Int", round(score_max * 10) + 1 if score_max else None),
        ("genres", "genre_in", "[String]", list(genres) or None),
        ("noGenres", "genre_not_in", "[String]", list(no_genres) or None),
    ]:
        if value is not None:
            decl.append(f"${name}: {kind}")
            filters.append(f"{gql}: ${name}")
            variables[name] = value
    fields = "idMal" if light else (f"{MEDIA} {LINKS} averageScore "
                                    "relations { edges { relationType node { type format startDate { year month } } } }")
    blocks = "\n".join(
        f"p{n}: Page(page: {n}, perPage: $perPage) {{ pageInfo {{ hasNextPage }} "
        f"media(type: ANIME, isAdult: $adult, {', '.join(filters)}) {{ {fields} }} }}"
        for n in pages)
    data = _query(f"query ($adult: Boolean, {', '.join(decl)}) {{ {blocks} }}", adult=False if SFW else None, **variables)
    out = {}
    for n in pages:
        page = data[f"p{n}"]
        items = []
        for m in page["media"]:
            if not m.get("idMal"):
                continue
            if light:
                items.append(m["idMal"])
                continue
            edges = [e for e in (m.pop("relations") or {}).get("edges") or []
                     if (e.get("node") or {}).get("type") == "ANIME"]
            a = normalize(m)
            related = {e.get("relationType") for e in edges}
            a["first_season"] = "PREQUEL" not in related
            a["spin_off"] = "PARENT" in related
            a["recap"] = _is_recap(m, edges)
            a["anilist_score"] = m["averageScore"] / 10 if m.get("averageScore") else None
            items.append(a)
        out[n] = (items, page["pageInfo"]["hasNextPage"], len(page["media"]))
    return out


def _is_recap(m, edges):
    """Recap and compilation movies (and OVAs and specials) are linked to the series they retell as an
    "alternative version", and come out after it. Original films with a later TV adaptation (Little Witch
    Academia, Death Billiards) and TV remakes (Brotherhood, Fruits Basket 2019) don't count."""
    if m.get("format") not in ("MOVIE", "OVA", "SPECIAL"):
        return False

    def start(x):
        d = x.get("startDate") or {}
        return (d.get("year") or 9999, d.get("month") or 13)

    return any(e.get("relationType") == "ALTERNATIVE" and e["node"].get("format") in ("TV", "TV_SHORT", "ONA")
               and start(e["node"]) < start(m) for e in edges)


def streaming_sites():
    """Streaming platforms AniList knows, as [(id, name)]."""
    data = _query("query { ExternalLinkSourceCollection(mediaType: ANIME, type: STREAMING) { id site isDisabled } }")
    return sorted(((s["id"], s["site"]) for s in data["ExternalLinkSourceCollection"] if not s["isDisabled"]),
                  key=lambda s: s[1].lower())


def health():
    return _client
