"""Import a MyAnimeList list export (Profile → Export → animelist_*.xml.gz)."""

import gzip
import xml.etree.ElementTree as ET

STATUS_MAP = {
    "watching": "watching", "1": "watching",
    "completed": "completed", "2": "completed",
    "on-hold": "on_hold", "3": "on_hold",
    "dropped": "dropped", "4": "dropped",
    "plan to watch": "plan", "6": "plan",
}


def _int(text, default=0):
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


def parse(data: bytes):
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    root = ET.fromstring(data)
    out = []
    for node in root.iter("anime"):
        mal_id = _int(node.findtext("series_animedb_id"))
        if not mal_id:
            continue
        score = _int(node.findtext("my_score"))
        out.append({
            "mal_id": mal_id,
            "title": (node.findtext("series_title") or f"#{mal_id}").strip(),
            "type": node.findtext("series_type"),
            "episodes": _int(node.findtext("series_episodes")) or None,
            "status": STATUS_MAP.get((node.findtext("my_status") or "").strip().lower(), "plan"),
            "score": score if 1 <= score <= 10 else None,
            "episodes_watched": _int(node.findtext("my_watched_episodes")),
        })
    return out


def apply(c, user_id, items, overwrite):
    added = skipped = 0
    for it in items:
        c.execute(
            "INSERT OR IGNORE INTO anime (mal_id, title, type, episodes) VALUES (?, ?, ?, ?)",
            (it["mal_id"], it["title"], it["type"], it["episodes"]),
        )
        conflict = (
            "DO UPDATE SET status = excluded.status, score = excluded.score, "
            "episodes_watched = excluded.episodes_watched, updated_at = excluded.updated_at"
            if overwrite else "DO NOTHING"
        )
        cur = c.execute(
            f"""INSERT INTO entries (user_id, mal_id, status, score, episodes_watched)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, mal_id) {conflict}""",
            (user_id, it["mal_id"], it["status"], it["score"], it["episodes_watched"]),
        )
        if cur.rowcount:
            added += 1
        else:
            skipped += 1
    return added, skipped
