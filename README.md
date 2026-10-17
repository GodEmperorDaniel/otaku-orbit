# Anime Club

A small self-hosted anime tracker for a group of friends, similar to MyAnimeList or Kitsu but running on your own server.

- **Personal lists**: watching, completed, on hold, dropped and plan to watch, with episode progress, 1–10 scores (to one decimal place) and notes.
- **Metadata from MyAnimeList's official API**: titles, posters, genres, studios and MAL scores. If MAL is unreachable, the app falls back to [AniList](https://anilist.co) automatically. Data is cached in SQLite and refreshed weekly.
- **Browse and search with filters**: browse any season, top rated, most popular, airing now or upcoming. Filter by type, genres, airing status, minimum MAL score and year range, hide shows already on your list, and sort by popularity, score, date or title.
- **Seasons and related shows**: each show's page has buttons for the previous and next season, the franchise's full watch order (filled in from prequel and sequel links), and its side stories, spin-offs, movies and other related entries.
- **Where to watch**: each show's page links to its streaming platforms (Crunchyroll, Netflix and so on), and so does the club's current pick on the home page.
- **Club picks**: schedule what the club watches together, by default every two weeks. The home page shows the current pick with everyone's progress and scores, what's up next, and a scoreboard of past picks.
- **Taste comparison**: an agreement matrix (average score gap and correlation for each pair of members), each member's lean against MAL and against the club, the most divisive picks and the ones everyone agreed on, genre leanings, and head-to-head pages with recommendations ("Bob rated these 8+ and you haven't seen them").
- **MAL import**: upload your MyAnimeList export (`.xml.gz`) to copy over your existing list.
- **Sharing**: friends join with an invite link. You can also turn on read-only guest access so anyone with the URL can look.

## Run it

```sh
cp .env.example .env   # then add your MAL client ID (see below)
mkdir -p data          # create it yourself so it's owned by you (the container runs as uid 1000)
docker compose up -d --build
```

Before the first start, get a MyAnimeList client ID: go to https://myanimelist.net/apiconfig and create an app (type "web"; the redirect URL isn't used). Then run `cp .env.example .env` and put the ID in `.env`. Docker Compose reads `.env` automatically, and git ignores it. Without one the app still works, using AniList only, but MAL scores will be missing.

Open http://your-server:8000. The first account you register becomes the admin. To invite friends, open **Admin** and send them the invite link.

Everything is stored in `./data/animeclub.db`. To back up, copy that file. sqlite's `.backup` command works while the app is running.

### Without Docker

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Run a single worker, since the app uses SQLite and runs an in-process metadata refresher.

## Configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `SITE_NAME` | `Anime Club` | Shown in the header |
| `MAL_CLIENT_ID` | none | MyAnimeList API client ID (see above) |
| `METADATA_SOURCES` | `mal,anilist` | Sources to try, in order: `mal`, `anilist`, `jikan`. The default is `anilist` when no MAL client ID is set |
| `PICK_DAYS` | `14` | Default length of a club pick |
| `INVITE_CODE` | generated | Fixes the invite code. If unset, the code is generated and can be rotated from Admin |
| `SECRET_KEY` | generated | Session signing key. If unset, it's generated and stored in the database |
| `HTTPS_ONLY` | off | Set to `1` behind HTTPS so session cookies are marked secure |
| `SFW` | `1` | Set to `0` to include adult titles in search |
| `REFRESH_DAYS` | `7` | How often cached MAL metadata and scores are refreshed |
| `JIKAN_URL` | public Jikan | Only used if `jikan` is in `METADATA_SOURCES` |
| `ANIMECLUB_DB` | `data/animeclub.db` | Database path |

## Exposing it to friends

Put it behind a reverse proxy that handles HTTPS and set `HTTPS_ONLY=1`. Caddy, for example:

```
anime.example.com {
    reverse_proxy localhost:8000
}
```

If you'd rather not expose it to the internet, Tailscale is an easy way to share it with just your group.

## Where the anime data comes from

The code in `app/sources/` has one module per provider. Each returns the same record shape, keyed by MyAnimeList ID:

- **`mal`**: MyAnimeList's official API. This is the primary source and the only one besides Jikan that has real MAL scores.
- **`anilist`**: AniList's GraphQL API. It needs no key, and every show maps to its MAL ID. Its scores are AniList's own, so they aren't stored as MAL scores. When a show came from the fallback, any MAL score already saved is kept, and the background refresher upgrades the record from MAL once MAL is back.
- **`jikan`**: an unofficial scraper of the MyAnimeList website. Off by default.

Browse filters are applied by the app itself to the list a source returns: up to 100 search results, or up to 500 shows for a season or ranking. So "Top rated + Romance" means the Romance titles within MAL's top 500.

Streaming links come from AniList (or Jikan, if enabled), because MAL's API doesn't include them. They're looked up when a show's page is first opened, saved, and refreshed weekly.

Each request tries the sources in order. If a source fails at the network level, it's skipped for two minutes so pages stay fast. The **Admin** page shows each source's last success and last error.
