import datetime as dt
import hmac
import json
import logging
import math
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, browse, db, mal_import, sources, stats

logging.basicConfig(level=logging.INFO)

HERE = Path(__file__).resolve().parent
SITE_NAME = os.environ.get("SITE_NAME", "Anime Club")
PICK_DAYS = int(os.environ.get("PICK_DAYS", "14"))
STATUSES = {
    "watching": "Watching",
    "completed": "Completed",
    "on_hold": "On hold",
    "dropped": "Dropped",
    "plan": "Plan to watch",
}
PALETTE = ["#f472b6", "#60a5fa", "#34d399", "#fbbf24", "#a78bfa",
           "#f87171", "#22d3ee", "#fb923c", "#a3e635", "#e879f9"]
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,24}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

db.init()


@asynccontextmanager
async def lifespan(app):
    sources.start_refresher()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=db.secret_key(),
    max_age=60 * 60 * 24 * 90,
    same_site="lax",
    https_only=os.environ.get("HTTPS_ONLY") == "1",
)
if os.environ.get("HTTPS_ONLY") == "1":
    @app.middleware("http")
    async def upgrade_to_https(request: Request, call_next):
        # The session cookie is Secure, so a visit over plain http could never stay logged in.
        # Only act when a proxy says the visitor used http; direct LAN requests carry no such header.
        if request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "http":
            return RedirectResponse(str(request.url.replace(scheme="https", port=None)), status_code=308)
        return await call_next(request)

app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


# ---------- template helpers ----------

def fmt_score(v):
    if v is None:
        return "–"
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def fmt_num(v, digits=1):
    return "–" if v is None else f"{v:.{digits}f}"


def fmt_signed(v, digits=1):
    return "–" if v is None else f"{v:+.{digits}f}"


def tint(delta, scale=2.0):
    """Diverging background: positive → --pos, negative → --neg."""
    if delta is None:
        return ""
    t = max(-1.0, min(1.0, delta / scale))
    var = "--pos" if t >= 0 else "--neg"
    return f"background: color-mix(in oklab, var({var}) {abs(t) * 70:.0f}%, transparent)"


def agree(mad):
    """Sequential background for an average score difference (0 = identical taste)."""
    if mad is None:
        return ""
    t = max(0.0, min(1.0, 1 - mad / 3))
    return f"background: color-mix(in oklab, var(--accent) {t * 75:.0f}%, var(--surface-2))"


def title_of(a):
    return a["title_english"] or a["title"]


templates.env.filters.update(score=fmt_score, num=fmt_num, signed=fmt_signed,
                             genres=lambda s: json.loads(s or "[]"), fromjson=lambda s: json.loads(s or "[]"))
templates.env.globals.update(STATUSES=STATUSES, SITE_NAME=SITE_NAME, tint=tint, agree=agree,
                             title_of=title_of, PALETTE=PALETTE, SOURCE_LABELS=sources.LABELS,
                             ago=lambda t: dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "never")


# ---------- auth plumbing ----------

class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
def _to_login(request, exc):
    return RedirectResponse(f"/login?next={quote(request.url.path)}", 303)


def current_user(request):
    uid = request.session.get("uid")
    if not uid:
        return None
    with db.conn() as c:
        return c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def require_user(request):
    u = current_user(request)
    if u is None:
        raise LoginRequired()
    return u


def require_admin(request):
    u = require_user(request)
    if not u["is_admin"]:
        raise HTTPException(403, "Admins only")
    return u


def viewer(request):
    """Logged-in user, or None for guests when read-only guest access is enabled."""
    u = current_user(request)
    if u is None and db.get_setting("guest_view") != "1":
        raise LoginRequired()
    return u


def flash(request, msg, kind="ok"):
    request.session["flash"] = request.session.get("flash", []) + [{"msg": msg, "kind": kind}]


def render(request, name, user, status_code=200, **ctx):
    flashes = request.session.pop("flash", [])
    return templates.TemplateResponse(request, name, {"user": user, "flashes": flashes, **ctx},
                                      status_code=status_code)


def redirect(url):
    return RedirectResponse(url, 303)


def safe_next(url, default="/"):
    return url if url and url.startswith("/") and not url.startswith("//") else default


def parse_date(s):
    try:
        return dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


@app.exception_handler(HTTPException)
def _http_error(request, exc):
    try:
        user = current_user(request)
    except Exception:
        user = None
    return render(request, "error.html", user, status_code=exc.status_code, code=exc.status_code, detail=exc.detail)


# ---------- shared view builders ----------

PICK_SQL = """
    SELECT p.*, a.title, a.title_english, a.image_url, a.episodes, a.mal_score, a.type, a.year, a.genres,
           a.synopsis, a.links, a.links_at, u.display_name AS picker_name, u.color AS picker_color
    FROM picks p JOIN anime a ON a.mal_id = p.mal_id LEFT JOIN users u ON u.id = p.picked_by
"""


def score_view(c, mal_id, mal_score, users, me):
    entries = {r["user_id"]: r for r in c.execute("SELECT * FROM entries WHERE mal_id = ?", (mal_id,))}
    members = [{"user": u, "entry": entries.get(u["id"])} for u in users]
    scored = [(m["user"], m["entry"]["score"]) for m in members if m["entry"] and m["entry"]["score"] is not None]
    vals = [s for _, s in scored]
    dots, rows = stats.score_dots(scored)
    avg = stats.mean(vals)
    for m in members:
        e = m["entry"]
        m["delta"] = e["score"] - avg if e and e["score"] is not None and len(vals) > 1 else None
    return {
        "members": members,
        "mean": avg,
        "sd": stats.stdev(vals),
        "n": len(vals),
        "dots": dots,
        "rows": rows,
        "mal_score": mal_score,
        "mine": entries.get(me["id"]) if me else None,
    }


def pick_view(c, p, users, me):
    return {**dict(p), **score_view(c, p["mal_id"], p["mal_score"], users, me)}


def next_pick_dates(c):
    today = dt.date.today()
    last = c.execute("SELECT MAX(end_date) AS d FROM picks").fetchone()["d"]
    start = max(today, parse_date(last) or today)
    return start.isoformat(), (start + dt.timedelta(days=PICK_DAYS)).isoformat()


# ---------- pages ----------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    user = viewer(request)
    today = dt.date.today().isoformat()
    with db.conn() as c:
        users = db.users(c)
        started = c.execute(PICK_SQL + " WHERE p.start_date <= ? ORDER BY p.start_date DESC, p.id DESC",
                            (today,)).fetchall()
        upcoming = c.execute(PICK_SQL + " WHERE p.start_date > ? ORDER BY p.start_date", (today,)).fetchall()
        current = pick_view(c, started[0], users, user) if started else None
        if current:
            links = sources.ensure_links(current["mal_id"], current["links_at"])
            if links is not None:
                current["links"] = json.dumps(links)
        past = [pick_view(c, p, users, user) for p in started[1:]]
        recent = c.execute(
            """SELECT e.*, u.display_name, u.color, u.username, a.title, a.title_english, a.image_url, a.episodes
               FROM entries e JOIN users u ON u.id = e.user_id JOIN anime a ON a.mal_id = e.mal_id
               ORDER BY e.updated_at DESC LIMIT 12""").fetchall()
        defaults = next_pick_dates(c)
    return render(request, "dashboard.html", user, current=current, upcoming=upcoming, past=past,
                  users=users, recent=recent, today=today, defaults=defaults)


@app.get("/search", response_class=HTMLResponse)
def search(request: Request):
    user = viewer(request)
    f = browse.Filters.from_query(request.query_params, sources.current_season())
    candidates, error = [], None
    try:
        candidates = sources.browse(f.q, f.mode, f.year, f.season)
    except sources.SourceError as e:
        error = str(e)
    mine = {}
    if user:
        with db.conn() as c:
            mine = {r["mal_id"]: r for r in c.execute("SELECT * FROM entries WHERE user_id = ?", (user["id"],))}
    results, facets = browse.apply(candidates, f, frozenset(mine))
    this_year = dt.date.today().year
    return render(request, "search.html", user, f=f, q=f.q, results=results[:f.page * browse.PER_PAGE],
                  total=len(results), candidates=len(candidates), error=error, mine=mine, facets=facets,
                  modes=browse.MODES, sorts=browse.SORTS, seasons=browse.SEASONS,
                  statuses=browse.STATUS_FILTERS, years=range(this_year + 1, 1959, -1), per_page=browse.PER_PAGE)


@app.get("/anime/{mal_id}", response_class=HTMLResponse)
def anime_page(request: Request, mal_id: int):
    user = viewer(request)
    with db.conn() as c:
        a = c.execute("SELECT * FROM anime WHERE mal_id = ?", (mal_id,)).fetchone()
    # Rows saved before related shows were tracked get them on their first visit.
    if a is None or a["fetched_at"] is None or a["relations"] is None:
        try:
            sources.fetch_and_store(mal_id)
        except sources.NotFound:
            if a is None or a["fetched_at"] is None:
                raise HTTPException(404, "That anime doesn't exist on MyAnimeList.")
        except sources.SourceError as e:
            if a is None:
                raise HTTPException(502, f"Couldn't fetch metadata: {e}")
    with db.conn() as c:
        a = c.execute("SELECT * FROM anime WHERE mal_id = ?", (mal_id,)).fetchone()
    sources.ensure_links(mal_id, a["links_at"])
    with db.conn() as c:
        a = c.execute("SELECT * FROM anime WHERE mal_id = ?", (mal_id,)).fetchone()
        users = db.users(c)
        view = score_view(c, mal_id, a["mal_score"], users, user)
        picks = c.execute(PICK_SQL + " WHERE p.mal_id = ? ORDER BY p.start_date DESC", (mal_id,)).fetchall()
        defaults = next_pick_dates(c)
        related = related_view(c, a, user)
    return render(request, "anime.html", user, a=a, view=view, picks=picks, defaults=defaults, related=related)


SEASON_ORDER = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}


def related_view(c, a, me):
    """Prequels/sequels as a watch order, plus other related shows grouped by relation."""
    rows = {a["mal_id"]: a}

    def load(ids):
        ids = [i for i in ids if i not in rows]
        if ids:
            q = f"SELECT * FROM anime WHERE mal_id IN ({','.join('?' * len(ids))})"
            rows.update((r["mal_id"], r) for r in c.execute(q, ids))

    def rels(row):
        return json.loads(row["relations"] or "[]") if row else []

    direct = rels(a)
    load(r["mal_id"] for r in direct)

    # Walk prequel/sequel links through what's stored. Branches (a movie between two seasons, say) all
    # end up in the order, sorted by when they aired; the refresher fills in shows we haven't fetched yet.
    chain, frontier = {a["mal_id"]}, [a["mal_id"]]
    while frontier and len(chain) < 40:
        load(r["mal_id"] for i in frontier for r in rels(rows.get(i)))
        nxt = []
        for i in frontier:
            for r in rels(rows.get(i)):
                if r["relation"] in ("prequel", "sequel") and r["mal_id"] not in chain and r["mal_id"] in rows:
                    chain.add(r["mal_id"])
                    nxt.append(r["mal_id"])
        frontier = nxt

    mine = {}
    if me:
        ids = list(rows)
        mine = {e["mal_id"]: e for e in c.execute(
            f"SELECT mal_id, status, score FROM entries WHERE user_id = ? AND mal_id IN ({','.join('?' * len(ids))})",
            [me["id"], *ids])}

    def item(mal_id, fallback=None):
        row = rows.get(mal_id)
        src = row or fallback
        return {
            "mal_id": mal_id,
            "title": title_of(row) if row else (src["title"] or f"#{mal_id}"),
            "image_url": src["image_url"],
            "type": row["type"] if row else None,
            "year": row["year"] if row else None,
            "season": row["season"] if row else None,
            "loading": row is not None and row["fetched_at"] is None,  # a stub the refresher will fill in
            "entry": mine.get(mal_id),
        }

    order = sorted((item(i) for i in chain),
                   key=lambda r: (r["year"] is None, r["year"] or 0, SEASON_ORDER.get(r["season"], 4), r["mal_id"]))
    groups = {}
    for r in direct:
        if r["relation"] not in ("prequel", "sequel"):
            groups.setdefault(r["relation"], []).append(item(r["mal_id"], r))
    return {
        "prev": [item(r["mal_id"], r) for r in direct if r["relation"] == "prequel"],
        "next": [item(r["mal_id"], r) for r in direct if r["relation"] == "sequel"],
        "order": order if len(order) > 1 else [],
        "groups": [(label, groups[k]) for k, label in sources.RELATIONS.items() if k in groups],
    }


@app.post("/anime/{mal_id}/entry")
def save_entry(request: Request, mal_id: int, status: str = Form("plan"), score: str = Form(""),
               episodes_watched: str = Form(""), notes: str = Form(""), next: str = Form(""),
               action: str = Form("save")):
    user = require_user(request)
    with db.conn() as c:
        a = c.execute("SELECT * FROM anime WHERE mal_id = ?", (mal_id,)).fetchone()
        if a is None:
            raise HTTPException(404, "Unknown anime")
        if action == "remove":
            c.execute("DELETE FROM entries WHERE user_id = ? AND mal_id = ?", (user["id"], mal_id))
            flash(request, f"Removed {title_of(a)} from your list.")
            return redirect(safe_next(next, f"/anime/{mal_id}"))
        if status not in STATUSES:
            status = "plan"
        try:
            score_val = round(max(1.0, min(10.0, float(score))), 1) if score.strip() else None
        except ValueError:
            score_val = None
        try:
            eps = max(0, int(episodes_watched)) if episodes_watched.strip() else 0
        except ValueError:
            eps = 0
        if a["episodes"]:
            if status == "completed" and eps == 0:
                eps = a["episodes"]
            eps = min(eps, a["episodes"])
        c.execute(
            """INSERT INTO entries (user_id, mal_id, status, score, episodes_watched, notes, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(user_id, mal_id) DO UPDATE SET status = excluded.status, score = excluded.score,
                   episodes_watched = excluded.episodes_watched, notes = excluded.notes,
                   updated_at = excluded.updated_at""",
            (user["id"], mal_id, status, score_val, eps, notes.strip() or None),
        )
    flash(request, f"Saved {title_of(a)}.")
    return redirect(safe_next(next, f"/anime/{mal_id}"))


@app.post("/anime/{mal_id}/refresh")
def refresh_anime(request: Request, mal_id: int):
    require_user(request)
    try:
        a = sources.fetch_and_store(mal_id)
        sources.ensure_links(mal_id, None)
        flash(request, f"Metadata refreshed from {sources.LABELS[a['source']]}.")
    except sources.SourceError as e:
        flash(request, str(e), "error")
    return redirect(f"/anime/{mal_id}")


@app.get("/u/{username}", response_class=HTMLResponse)
def profile(request: Request, username: str, status: str = "all", sort: str = "score"):
    user = viewer(request)
    with db.conn() as c:
        owner = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if owner is None:
            raise HTTPException(404, "No such member")
        rows = c.execute(
            """SELECT e.*, a.title, a.title_english, a.image_url, a.episodes, a.mal_score, a.type, a.year
               FROM entries e JOIN anime a ON a.mal_id = e.mal_id WHERE e.user_id = ?""",
            (owner["id"],)).fetchall()
    counts = {k: 0 for k in STATUSES}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    shown = [r for r in rows if status == "all" or r["status"] == status]
    sorters = {
        "score": lambda r: (r["score"] is None, -(r["score"] or 0), title_of(r).lower()),
        "title": lambda r: title_of(r).lower(),
        "updated": lambda r: r["updated_at"],
        "mal": lambda r: (r["mal_score"] is None, -(r["mal_score"] or 0)),
    }
    shown.sort(key=sorters.get(sort, sorters["score"]), reverse=(sort == "updated"))
    scores = [r["score"] for r in rows if r["score"] is not None]
    dist = [0] * 10
    for s in scores:
        dist[min(10, max(1, math.floor(s + 0.5))) - 1] += 1
    vs_mal = stats.mean(r["score"] - r["mal_score"] for r in rows if r["score"] is not None and r["mal_score"])
    return render(request, "profile.html", user, owner=owner, rows=shown, counts=counts, total=len(rows),
                  status=status, sort=sort, mean=stats.mean(scores), n_scored=len(scores), dist=dist,
                  dist_max=max(dist) or 1, vs_mal=vs_mal)


@app.get("/compare", response_class=HTMLResponse)
def compare(request: Request, scope: str = "club"):
    user = viewer(request)
    today = dt.date.today().isoformat()
    with db.conn() as c:
        users = db.users(c)
        club_ids = {r["mal_id"] for r in c.execute("SELECT mal_id FROM picks WHERE start_date <= ?", (today,))}
        if scope != "all":
            scope = "club"
        ov = stats.overview(c, users, club_ids if scope == "club" else None)
    return render(request, "compare.html", user, ov=ov, users=users, scope=scope, n_picks=len(club_ids))


@app.get("/compare/{a}/{b}", response_class=HTMLResponse)
def compare_pair(request: Request, a: str, b: str):
    user = viewer(request)
    with db.conn() as c:
        ua = c.execute("SELECT * FROM users WHERE username = ?", (a,)).fetchone()
        ub = c.execute("SELECT * FROM users WHERE username = ?", (b,)).fetchone()
        if not ua or not ub:
            raise HTTPException(404, "No such member")
        scores = stats.load_scores(c)
        p = stats.pair(scores[ua["id"]], scores[ub["id"]])
        meta = {r["mal_id"]: r for r in c.execute("SELECT * FROM anime")}
        listed = {uid: {r["mal_id"] for r in c.execute("SELECT mal_id FROM entries WHERE user_id = ?", (uid,))}
                  for uid in (ua["id"], ub["id"])}
        club_ids = {r["mal_id"] for r in c.execute("SELECT mal_id FROM picks")}
    rows = [{"anime": meta[m], "a": scores[ua["id"]][m], "b": scores[ub["id"]][m],
             "diff": scores[ua["id"]][m] - scores[ub["id"]][m], "club": m in club_ids} for m in p["shared"]]
    rows.sort(key=lambda r: (-abs(r["diff"]), title_of(r["anime"])))

    def recs(src, dst):
        out = [(meta[m], s) for m, s in scores[src["id"]].items() if s >= 8 and m not in listed[dst["id"]]]
        return sorted(out, key=lambda t: -t[1])[:12]

    return render(request, "pair.html", user, ua=ua, ub=ub, p=p, rows=rows,
                  recs_for_a=recs(ub, ua), recs_for_b=recs(ua, ub))


# ---------- club picks ----------

@app.post("/picks")
def create_pick(request: Request, mal_id: int = Form(...), start_date: str = Form(...),
                end_date: str = Form(""), note: str = Form("")):
    user = require_user(request)
    start = parse_date(start_date)
    if start is None:
        flash(request, "Please give a valid start date.", "error")
        return redirect(f"/anime/{mal_id}")
    end = parse_date(end_date) or start + dt.timedelta(days=PICK_DAYS)
    if end < start:
        flash(request, "The end date must be after the start date.", "error")
        return redirect(f"/anime/{mal_id}")
    with db.conn() as c:
        a = c.execute("SELECT * FROM anime WHERE mal_id = ?", (mal_id,)).fetchone()
        if a is None:
            raise HTTPException(404, "Unknown anime")
        c.execute("INSERT INTO picks (mal_id, start_date, end_date, picked_by, note) VALUES (?, ?, ?, ?, ?)",
                  (mal_id, start.isoformat(), end.isoformat(), user["id"], note.strip() or None))
    flash(request, f"{title_of(a)} is scheduled for {start:%b %d}.")
    return redirect("/")


@app.post("/picks/{pick_id}/delete")
def delete_pick(request: Request, pick_id: int):
    user = require_user(request)
    with db.conn() as c:
        p = c.execute("SELECT * FROM picks WHERE id = ?", (pick_id,)).fetchone()
        if p is None:
            raise HTTPException(404, "No such pick")
        if not user["is_admin"] and p["picked_by"] != user["id"]:
            raise HTTPException(403, "Only the person who scheduled it (or an admin) can remove a pick.")
        c.execute("DELETE FROM picks WHERE id = ?", (pick_id,))
    flash(request, "Pick removed.")
    return redirect("/")


# ---------- accounts ----------

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    with db.conn() as c:
        has_users = c.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    if not has_users:
        return redirect("/register")
    return render(request, "login.html", None, next=safe_next(next))


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    with db.conn() as c:
        u = c.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if not u or not auth.verify_password(password, u["password_hash"]):
        flash(request, "Wrong username or password.", "error")
        return redirect(f"/login?next={quote(safe_next(next))}")
    request.session["uid"] = u["id"]
    return redirect(safe_next(next))


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request, code: str = ""):
    with db.conn() as c:
        first = c.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
    return render(request, "register.html", current_user(request), first=first, code=code)


@app.post("/register")
def register(request: Request, username: str = Form(...), display_name: str = Form(""),
             password: str = Form(...), code: str = Form("")):
    username = username.strip()
    with db.conn() as c:
        count = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        error = None
        if count and not hmac.compare_digest(code.strip(), db.invite_code()):
            error = "That invite code isn't right — ask your club admin for the current one."
        elif not USERNAME_RE.match(username):
            error = "Usernames are 2–24 letters, numbers, - or _."
        elif len(password) < 8:
            error = "Passwords need at least 8 characters."
        elif c.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            error = "That username is taken."
        if error:
            flash(request, error, "error")
            return redirect(f"/register?code={quote(code.strip())}")
        cur = c.execute(
            "INSERT INTO users (username, display_name, password_hash, color, is_admin) VALUES (?, ?, ?, ?, ?)",
            (username, display_name.strip()[:40] or username, auth.hash_password(password),
             PALETTE[count % len(PALETTE)], 1 if count == 0 else 0))
        request.session["uid"] = cur.lastrowid
    flash(request, "Welcome to the club! Search for something you've watched to get started.")
    return redirect("/")


def readable_color(color):
    """Member colours are drawn on both light and dark backgrounds, so keep them mid-luminance."""
    if not COLOR_RE.match(color or ""):
        return False
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.25 <= 0.2126 * r + 0.7152 * g + 0.0722 * b <= 0.85


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return render(request, "settings.html", require_user(request))


@app.post("/settings")
def save_settings(request: Request, display_name: str = Form(""), color: str = Form(""),
                  current_password: str = Form(""), new_password: str = Form("")):
    user = require_user(request)
    if not readable_color(color):
        flash(request, "That colour is too dark or too light to read in both themes; kept your old one.", "error")
        color = user["color"]
    with db.conn() as c:
        c.execute("UPDATE users SET display_name = ?, color = ? WHERE id = ?",
                  (display_name.strip()[:40] or user["username"], color, user["id"]))
        if new_password:
            if not auth.verify_password(current_password, user["password_hash"]):
                flash(request, "Current password is wrong; password not changed.", "error")
                return redirect("/settings")
            if len(new_password) < 8:
                flash(request, "New password needs at least 8 characters.", "error")
                return redirect("/settings")
            c.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                      (auth.hash_password(new_password), user["id"]))
    flash(request, "Settings saved.")
    return redirect("/settings")


# ---------- import / export ----------

@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    return render(request, "import.html", require_user(request))


@app.post("/import")
async def import_list(request: Request, file: UploadFile = File(...), overwrite: str = Form("")):
    user = require_user(request)
    data = await file.read(50 * 1024 * 1024)
    try:
        items = mal_import.parse(data)
    except Exception:
        flash(request, "That doesn't look like a MyAnimeList export (.xml or .xml.gz).", "error")
        return redirect("/import")
    with db.conn() as c:
        added, skipped = mal_import.apply(c, user["id"], items, overwrite == "on")
    sources.wake_refresher()
    msg = f"Imported {added} entries"
    if skipped:
        msg += f", skipped {skipped} already on your list"
    flash(request, msg + ". Posters and MAL scores fill in over the next few minutes.")
    return redirect(f"/u/{user['username']}")


@app.get("/export.json")
def export(request: Request):
    user = require_user(request)
    with db.conn() as c:
        rows = c.execute(
            """SELECT e.mal_id, a.title, e.status, e.score, e.episodes_watched, e.notes, e.updated_at
               FROM entries e JOIN anime a ON a.mal_id = e.mal_id WHERE e.user_id = ? ORDER BY a.title""",
            (user["id"],)).fetchall()
    return JSONResponse({"user": user["username"], "entries": [dict(r) for r in rows]},
                        headers={"Content-Disposition": f'attachment; filename="{user["username"]}-anime.json"'})


# ---------- admin ----------

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    user = require_admin(request)
    with db.conn() as c:
        users = c.execute(
            """SELECT u.*, COUNT(e.mal_id) AS n FROM users u LEFT JOIN entries e ON e.user_id = u.id
               GROUP BY u.id ORDER BY u.id""").fetchall()
    return render(request, "admin.html", user, users=users, invite=db.invite_code(),
                  invite_from_env=bool(os.environ.get("INVITE_CODE")),
                  guest_view=db.get_setting("guest_view") == "1", sources=sources.status(),
                  base_url=str(request.base_url).rstrip("/"))


@app.post("/admin")
def admin_action(request: Request, action: str = Form(...), user_id: int = Form(0)):
    admin = require_admin(request)
    if action == "new_invite":
        db.set_setting("invite_code", secrets.token_urlsafe(8))
        flash(request, "New invite code generated; the old one no longer works.")
    elif action == "toggle_guest":
        db.set_setting("guest_view", "0" if db.get_setting("guest_view") == "1" else "1")
        flash(request, "Guest access updated.")
    elif action in ("reset_password", "toggle_admin", "delete") and user_id and user_id != admin["id"]:
        with db.conn() as c:
            target = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if target is None:
                raise HTTPException(404, "No such member")
            if action == "reset_password":
                temp = secrets.token_urlsafe(9)
                c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (auth.hash_password(temp), user_id))
                flash(request, f"Temporary password for {target['username']}: {temp}")
            elif action == "toggle_admin":
                c.execute("UPDATE users SET is_admin = 1 - is_admin WHERE id = ?", (user_id,))
                flash(request, f"Updated admin rights for {target['username']}.")
            else:
                c.execute("DELETE FROM users WHERE id = ?", (user_id,))
                flash(request, f"Deleted {target['username']} and their list.")
    return redirect("/admin")
