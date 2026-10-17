"""Score aggregation and taste comparison between members."""

import json
import math
from collections import defaultdict


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def stdev(xs):
    xs = list(xs)
    if len(xs) < 2:
        return None
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def pearson(xs, ys):
    if len(xs) < 3:
        return None
    mx, my = mean(xs), mean(ys)
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sx * sy)


def load_scores(c, mal_ids=None):
    """{user_id: {mal_id: score}} for scored entries, optionally limited to mal_ids."""
    out = defaultdict(dict)
    for r in c.execute("SELECT user_id, mal_id, score FROM entries WHERE score IS NOT NULL"):
        if mal_ids is None or r["mal_id"] in mal_ids:
            out[r["user_id"]][r["mal_id"]] = r["score"]
    return out


def pair(a, b):
    shared = sorted(a.keys() & b.keys())
    xs, ys = [a[k] for k in shared], [b[k] for k in shared]
    return {
        "n": len(shared),
        "mad": mean(abs(x - y) for x, y in zip(xs, ys)),
        "bias": mean(x - y for x, y in zip(xs, ys)),
        "r": pearson(xs, ys),
        "shared": shared,
    }


def score_dots(scored):
    """Positions on a 1–10 strip for [(user, score)], stacking identical scores."""
    dots, stacks = [], defaultdict(int)
    for u, s in sorted(scored, key=lambda t: t[1]):
        key = round(s * 2) / 2
        dots.append({
            "left": (s - 1) / 9 * 100,
            "stack": stacks[key],
            "color": u["color"],
            "name": u["display_name"],
            "initial": u["display_name"][:1].upper(),
            "score": s,
        })
        stacks[key] += 1
    return dots, max(stacks.values(), default=1)


def overview(c, users, mal_ids):
    scores = load_scores(c, mal_ids)
    meta = {r["mal_id"]: r for r in c.execute(
        "SELECT mal_id, title, title_english, image_url, mal_score, genres FROM anime")}
    by_id = {u["id"]: u for u in users}

    matrix = {a["id"]: {b["id"]: pair(scores[a["id"]], scores[b["id"]]) for b in users} for a in users}

    # Per-anime aggregates (only where at least two members scored it).
    raters = defaultdict(dict)
    for uid, s in scores.items():
        for mid, sc in s.items():
            raters[mid][uid] = sc
    titles = []
    for mid, rs in raters.items():
        if len(rs) < 2 or mid not in meta:
            continue
        vals = list(rs.values())
        titles.append({
            "anime": meta[mid],
            "mean": mean(vals),
            "sd": stdev(vals),
            "low": min(vals),
            "high": max(vals),
            "scores": rs,
            "dots": score_dots([(by_id[uid], sc) for uid, sc in rs.items() if uid in by_id]),
        })
    k = min(6, len(titles) // 2)  # keep the two lists from overlapping when there are few titles
    divisive = sorted(titles, key=lambda t: -t["sd"])[:k]
    consensus = sorted(titles, key=lambda t: (t["sd"], -t["mean"]))[:k]

    members = []
    for u in users:
        s = scores[u["id"]]
        vs_mal = [sc - meta[mid]["mal_score"] for mid, sc in s.items() if meta.get(mid) and meta[mid]["mal_score"]]
        vs_club = []
        for mid, sc in s.items():
            others = [v for uid, v in raters[mid].items() if uid != u["id"]]
            if others:
                vs_club.append(sc - mean(others))
        candidates = [(matrix[u["id"]][o["id"]], o) for o in users if o["id"] != u["id"]]
        candidates = [(p, o) for p, o in candidates if p["n"] >= 3] or [(p, o) for p, o in candidates if p["n"]]
        twin = min(candidates, key=lambda t: t[0]["mad"], default=None)
        nemesis = max(candidates, key=lambda t: t[0]["mad"], default=None)
        members.append({
            "user": u,
            "n": len(s),
            "mean": mean(s.values()),
            "sd": stdev(s.values()),
            "vs_mal": mean(vs_mal),
            "vs_club": mean(vs_club),
            "twin": twin[1] if twin else None,
            "twin_mad": twin[0]["mad"] if twin else None,
            "nemesis": nemesis[1] if nemesis and len(candidates) > 1 else None,
            "nemesis_mad": nemesis[0]["mad"] if nemesis else None,
        })

    # Genre leanings: each member's genre average relative to their own overall average.
    user_mean = {uid: mean(s.values()) for uid, s in scores.items() if s}
    per_genre = defaultdict(lambda: defaultdict(list))
    for uid, s in scores.items():
        for mid, sc in s.items():
            for g in json.loads((meta.get(mid) or {"genres": None})["genres"] or "[]"):
                per_genre[g][uid].append(sc)
    genres = []
    for g, by_user in per_genre.items():
        cells = {uid: {"delta": mean(v) - user_mean[uid], "avg": mean(v), "n": len(v)}
                 for uid, v in by_user.items() if len(v) >= 2}
        if len(cells) >= 2:
            deltas = [x["delta"] for x in cells.values()]
            genres.append({"name": g, "cells": cells, "total": sum(len(v) for v in by_user.values()),
                           "spread": max(deltas) - min(deltas)})
    genres = sorted(sorted(genres, key=lambda g: -g["total"])[:14], key=lambda g: -g["spread"])

    pairs = []
    for i, a in enumerate(users):
        for b in users[i + 1:]:
            p = matrix[a["id"]][b["id"]]
            if p["n"]:
                pairs.append({"a": a, "b": b, **p})
    pairs.sort(key=lambda p: p["mad"])

    return {
        "matrix": matrix,
        "members": members,
        "divisive": divisive,
        "consensus": consensus,
        "genres": genres,
        "pairs": pairs,
        "rated_titles": len(titles),
    }
