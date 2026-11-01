# replays

Self-hosted viewer for Dota 2 team fights. Parses official replays with
[gem](https://pypi.org/project/gem-dota/), stores per-match fight data, and
serves an interactive fight player: pick a match, watch the map playback with
casts, damage, disables, deaths and smokes on every replay packet of the fight.

- **Backend** — `backend/server.py`, stdlib-only HTTP server. Serves the app
  at `/`, the `/api` and `/data` endpoints, and manages a single-worker parse
  queue (download → decompress → export).
- **Exporter** — `pipeline/export_match.py`. Reads a `.dem` file and writes
  `fight.json` (the biggest fight's playback), `fights/index.json` +
  `fights/N.json` (every fight), and `figures/map.webp` +
  `figures/fights/N.webp` (map crops).
- **Frontend** — `web/`, an Astro app built with bun; the server serves
  `web/dist` as static files.
- **Data** — `data/` is generated, not committed. Hero/item icons and the 7.41
  map image are fetched on first run (see `scripts/fetch_assets.py`; gem's
  catalog JSONs come from the pinned wheel). `var/` (gitignored) holds
  runtime data: parsed matches in `var/store/matches/`, job state in
  `var/jobs/`, downloaded replays in `var/replays/`.

## Run

The Docker image builds the Astro site (bun), installs the Python deps (uv)
and bakes `data/` (icons, map, catalog) at build time:

```bash
docker build -t replays .
docker run --rm -p 8000:8000 -v replays-var:/app/var replays
```

Mount a volume at `/app/var` to keep parsed matches, job state and downloaded
replays across restarts. The container needs network only for the app's own
services: ClickHouse (match list), OpenDota (scoreboards, replay URLs) and the
replay CDN (parse jobs).

In a paseo workspace, `paseo script start replays` still runs the backend
(paseo.json passes `$PASEO_PORT` on as `PORT`).

### Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `PORT` | `8000` | Port the backend binds (`0.0.0.0`). |
| `CH_URL` | `http://clickhouse-service.cluster-services.svc.cluster.local:8123/` | ClickHouse HTTP endpoint the match list queries. |

`OD_URL` (OpenDota API base for scoreboards and replay URLs) is a constant in
`backend/server.py`.

Kubernetes: set `containerPort` from `PORT` and probe `/` (the home page,
200). `/api/matches` answers 503 while `CH_URL` is unreachable, which the UI
treats as "no match list".

## Development

Python 3.11 via [uv](https://docs.astral.sh/uv/). Paths for local data are
relative to the repo root.

```bash
uv sync                      # python deps + .venv
uv run scripts/fetch_assets.py   # populate data/ (icons, map, catalog json)
cd web && bun install        # js deps
cd web && bun run check      # astro typecheck
cd web && bun run build      # build dist for the backend to serve
uv run python backend/server.py

# export one match by hand:
uv run python pipeline/export_match.py <replay.dem> \
  --fight-data var/store/matches/<id>/fight.json \
  --fights-data var/store/matches/<id>/fights \
  --public-figures var/store/matches/<id>/figures
```

## Config

- Match list comes from ClickHouse (`CH_URL` env, default
  `http://clickhouse-service.cluster-services.svc.cluster.local:8123/`).
- Scoreboards come from OpenDota (cached per match in the store).
- Parse jobs: `POST /api/parse/{id}`; the single worker processes one match at
  a time; `.dem` files are deleted after a successful export.
