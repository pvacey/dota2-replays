#!/usr/bin/env python3
"""replays backend: static site + per-match data, match list (ClickHouse +
already-parsed matches), scoreboard (OpenDota) and the single-worker parse
queue. Stdlib only."""

from __future__ import annotations

import bz2
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent  # the replays project
DIST = ROOT / "web" / "dist"
DATA = ROOT / "data"
HEROES_JSON = DATA / "heroes.json"
ITEMS_JSON = DATA / "items.json"
HERO_ICONS = DATA / "icons" / "heroes"
ITEM_ICONS = DATA / "icons" / "items"
EXPORT_PY = ROOT / "pipeline" / "export_match.py"
GEM_PY = sys.executable
STORE = ROOT / "var" / "store"
MATCHES = STORE / "matches"
JOBS = ROOT / "var" / "jobs"
REPLAYS = ROOT / "var" / "replays"

CH_URL = os.environ.get(
    "CH_URL", "http://clickhouse-service.cluster-services.svc.cluster.local:8123/")
OD_URL = "https://api.opendota.com/api/matches/"
USER_AGENT = "dota2-replays/1.0"
SQL = (
    "SELECT match_id, MAX(timestamp) AS latest_timestamp FROM dota_events "
    "WHERE match_id != 0 GROUP BY match_id ORDER BY latest_timestamp DESC "
    "LIMIT 25 FORMAT JSONEachRow"
)
CHUNK = 65536
MATCH_TTL_S = 30 * 86400
PURGE_INTERVAL_S = 3600


def now() -> str:
    return time.strftime("%H:%M:%S")


def line(msg: str) -> str:
    return f"[{now()}] {msg}"


def load_heroes() -> tuple[dict[int, str], dict[int, str]]:
    """heroes.json: {npc_dota_hero_x: {id, localized_name, ...}} -> id->name, id->icon."""
    try:
        raw = json.loads(HEROES_JSON.read_text())
    except Exception:
        return {}, {}
    by_id: dict[int, str] = {}
    icons: dict[int, str] = {}
    for key, entry in raw.items():
        hid = entry.get("id")
        if hid is None:
            continue
        by_id[hid] = entry.get("localized_name")
        if key.startswith("npc_dota_hero_"):
            icons[hid] = key[len("npc_dota_hero_"):]
    return by_id, icons


HERO_NAME, HERO_ICON = load_heroes()


def load_item_names() -> dict[str, str]:
    """items.json: {short: {id, dname, ...}} -> short -> display name."""
    try:
        raw = json.loads(ITEMS_JSON.read_text())
    except Exception:
        return {}
    return {
        key: str(entry.get("dname") or key)
        for key, entry in raw.items()
        if isinstance(entry, dict)
    }


ITEM_DNAME = load_item_names()


def load_item_ids() -> dict[int, str]:
    """The same catalog inverted: OpenDota's per-slot item ids -> icon key."""
    try:
        raw = json.loads(ITEMS_JSON.read_text())
    except Exception:
        return {}
    return {
        int(entry["id"]): key
        for key, entry in raw.items()
        if isinstance(entry, dict) and isinstance(entry.get("id"), int)
    }


ITEM_BY_ID = load_item_ids()


# ---------------------------------------------------------------- job store

def save_job(job: dict) -> None:
    JOBS.mkdir(parents=True, exist_ok=True)
    path = JOBS / f"{job['match_id']}.json"
    tmp = JOBS / f"{job['match_id']}.json.tmp"
    tmp.write_text(json.dumps(job, indent=1) + "\n")
    os.replace(tmp, path)


def load_job(match_id: int) -> dict | None:
    path = JOBS / f"{match_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def is_parsed(match_id: int) -> bool:
    return (MATCHES / str(match_id) / "fights" / "index.json").is_file()


def parsed_match_ids() -> list[int]:
    """Match ids with exported data under var/store/matches."""
    if not MATCHES.is_dir():
        return []
    out = []
    for entry in MATCHES.iterdir():
        if entry.is_dir() and entry.name.isdigit() \
                and (entry / "fights" / "index.json").is_file():
            out.append(int(entry.name))
    return out


def match_timestamp(match_id: int) -> int | None:
    """Best-effort match start (epoch s) from the cached scoreboard."""
    path = MATCHES / str(match_id) / "scoreboard.json"
    if path.is_file():
        try:
            start = json.loads(path.read_text()).get("start_time")
            if isinstance(start, (int, float)) and start > 0:
                return int(start)
        except Exception:
            pass
    return None


def match_age(match_id: int) -> float | None:
    """Seconds since the match happened, or None when unknown."""
    ts = match_timestamp(match_id)
    if ts is None:
        try:
            ts = (MATCHES / str(match_id)).stat().st_mtime  # created locally
        except OSError:
            return None
    return max(0.0, time.time() - ts)


def purge_old_matches() -> None:
    """Drop var data (store, job, replay) for matches older than 30 days."""
    for entry in [] if not MATCHES.is_dir() else list(MATCHES.iterdir()):
        if not entry.is_dir() or not entry.name.isdigit():
            continue
        age = match_age(int(entry.name))
        if age is not None and age <= MATCH_TTL_S:
            continue
        try:
            shutil.rmtree(entry)
            print(line(f"purged match {entry.name} data"), flush=True)
        except Exception as e:
            print(line(f"purge {entry.name}: {e}"), flush=True)
        job = JOBS / f"{entry.name}.json"
        try:
            job.unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            print(line(f"purge job {entry.name}: {e}"), flush=True)
        for suffix in (".dem", ".dem.bz2"):
            replay = REPLAYS / f"{entry.name}{suffix}"
            try:
                replay.unlink()
            except FileNotFoundError:
                pass
            except Exception as e:
                print(line(f"purge replay {entry.name}: {e}"), flush=True)
    # orphaned job files / replay leftovers
    if JOBS.is_dir():
        todo = list(JOBS.glob("*.json.tmp"))
        todo += [p for p in JOBS.glob("*.json")
                 if not (MATCHES / p.stem).is_dir()
                 and time.time() - p.stat().st_mtime > MATCH_TTL_S]
        for path in todo:
            try:
                path.unlink()
            except Exception:
                pass
    if REPLAYS.is_dir():
        for replay in REPLAYS.iterdir():
            try:
                if time.time() - replay.stat().st_mtime > MATCH_TTL_S:
                    replay.unlink()
            except Exception:
                pass


def purger() -> None:
    while True:
        try:
            purge_old_matches()
        except Exception as e:  # purger never dies
            print(line(f"purge loop: {e}"), flush=True)
        time.sleep(PURGE_INTERVAL_S)


# ------------------------------------------------------------ parse queue

_lock = threading.Condition()
_queue: list[dict] = []          # {"match_id": int, "replay_url": str}
_active: set[int] = set()        # queued or running ids


def opendota_match(match_id: int) -> tuple[dict | None, str | None]:
    """GET OpenDota /matches/{id}. Returns (data, error)."""
    req = urllib.request.Request(f"{OD_URL}{match_id}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return None, f"opendota http {resp.status}"
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"opendota http {e.code}"
    except Exception as e:
        return None, f"opendota error: {e}"


def finish_error(match_id: int, msg: str) -> None:
    job = load_job(match_id)
    if job is None:
        job = {"match_id": match_id, "log": []}
    job.update(stage="error", pct=None, done=False, error=msg)
    job.setdefault("log", []).append(line(msg))
    save_job(job)


def run_job(match_id: int, replay_url: str) -> None:
    job = load_job(match_id)
    if job is None:
        job = {"match_id": match_id, "stage": "queued", "pct": None,
               "log": [], "done": False, "error": None}
    match_dir = MATCHES / str(match_id)
    match_dir.mkdir(parents=True, exist_ok=True)
    bz2_path = REPLAYS / f"{match_id}.dem.bz2"
    dem_path = REPLAYS / f"{match_id}.dem"
    try:
        # 1) download
        job.update(stage="downloading", pct=None, error=None)
        job["log"].append(line(f"downloading replay {replay_url}"))
        save_job(job)
        REPLAYS.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(replay_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp, open(bz2_path, "wb") as out:
            total = resp.headers.get("Content-Length")
            total = int(total) if total and total.isdigit() else None
            got = 0
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                if total:
                    job["pct"] = min(100, int(got * 100 / total))
                    save_job(job)
        job["pct"] = 100
        job["log"].append(line(f"downloaded {got} bytes"))
        save_job(job)

        # 2) decompress — the replay CDN serves bz2 or zstd depending on vintage;
        #    sniff the magic instead of trusting the ".dem.bz2" suffix.
        job.update(stage="decompressing", pct=None)
        job["log"].append(line("decompressing replay"))
        save_job(job)
        with open(bz2_path, "rb") as probe:
            magic = probe.read(4)
        with open(bz2_path, "rb") as src, open(dem_path, "wb") as dst:
            if magic[:3] == b"BZh":
                dec = bz2.open(src, "rb")
            elif magic == b"\x28\xb5\x2f\xfd":
                import zstandard

                dec = zstandard.ZstdDecompressor().stream_reader(src)
            else:
                raise ValueError(f"unknown replay compression (magic {magic.hex()})")
            while True:
                block = dec.read(1 << 20)
                if not block:
                    break
                dst.write(block)
        os.remove(bz2_path)

        # 3) export
        job.update(stage="exporting", pct=None)
        job["log"].append(line("exporting match data"))
        save_job(job)
        cmd = [
            GEM_PY, str(EXPORT_PY), str(dem_path),
            # fight.json is the home fight's playback: index entries reference
            # it verbatim as "fight.json", so it must sit next to fights/ (as
            # web/src/data/fight.json does in the static site).
            "--fight-data", str(match_dir / "fight.json"),
            "--fights-data", str(match_dir / "fights"),
            "--public-figures", str(match_dir / "figures"),
            # The per-player item purchase lanes (the items tab's data).
            "--items-data", str(match_dir / "items.json"),
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        for out_line in proc.stdout:
            job["log"].append(line(out_line.rstrip("\n")))
            save_job(job)
        rc = proc.wait()
        if rc != 0:  # keep .dem for retry
            finish_error(match_id, job["log"][-1] if job["log"] else f"export exited {rc}")
            return
        # success: drop the replay
        if dem_path.exists():
            os.remove(dem_path)
        job.update(stage="done", pct=None, done=True, error=None)
        job["log"].append(line("parse complete"))
        save_job(job)
    except Exception as e:
        finish_error(match_id, str(e) or e.__class__.__name__)


def worker() -> None:
    while True:
        with _lock:
            while not _queue:
                _lock.wait()
            item = _queue.pop(0)
        try:
            run_job(item["match_id"], item["replay_url"])
        except Exception as e:  # worker never dies
            finish_error(item["match_id"], str(e) or e.__class__.__name__)
        finally:
            with _lock:
                _active.discard(item["match_id"])
                _lock.notify_all()


def enqueue(match_id: int, replay_url: str) -> None:
    with _lock:
        _queue.append({"match_id": match_id, "replay_url": replay_url})
        _lock.notify_all()


# ---------------------------------------------------------------- handlers

#: Bumped when the mapped board's shape changes; older caches refetch.
SCOREBOARD_V = 2


def _item_slot(p: dict, key: str) -> list[str] | None:
    """One inventory slot: [icon key, display name], or None when empty/unknown."""
    short = ITEM_BY_ID.get(p.get(key) or 0)
    if short is None:
        return None
    return [short, ITEM_DNAME.get(short, short)]


def map_scoreboard(raw: dict) -> dict:
    radiant, dire = [], []
    for p in raw.get("players") or []:
        hid = p.get("hero_id")
        entry = {
            "player": p.get("personaname") or "anonymous",
            "hero": HERO_NAME.get(hid),
            "hero_icon": HERO_ICON.get(hid),
            "kills": p.get("kills"),
            "deaths": p.get("deaths"),
            "assists": p.get("assists"),
            "net_worth": p.get("net_worth"),
            "total_gold": p.get("total_gold"),
            "last_hits": p.get("last_hits"),
            "denies": p.get("denies"),
            "level": p.get("level"),
            "gpm": p.get("gold_per_min"),
            "xpm": p.get("xp_per_min"),
            "hero_damage": p.get("hero_damage"),
            # Final build: six inventory slots, three backpack, the neutral
            # item(s); each [icon key, display name] or None for an empty slot.
            "items": [_item_slot(p, f"item_{i}") for i in range(6)],
            "backpack": [_item_slot(p, f"backpack_{i}") for i in range(3)],
            "neutral": _item_slot(p, "item_neutral"),
            "neutral2": _item_slot(p, "item_neutral2"),
        }
        (radiant if p.get("isRadiant") else dire).append(entry)
    return {
        "_v": SCOREBOARD_V,
        "start_time": raw.get("start_time"),
        "duration_s": raw.get("duration"),
        "radiant_win": raw.get("radiant_win"),
        "radiant_score": raw.get("radiant_score"),
        "dire_score": raw.get("dire_score"),
        "turbo": raw.get("game_mode") == 23,
        "radiant": radiant,
        "dire": dire,
    }


def get_scoreboard(match_id: int) -> tuple[dict | None, str | None]:
    """Cached mapped scoreboard from store/matches/{id}/scoreboard.json.

    Caches older than SCOREBOARD_V refetch (they lack the newer fields); if
    OpenDota can't be reached, the stale board still serves — the card then
    shows fewer stats instead of nothing.
    """
    cache = MATCHES / str(match_id) / "scoreboard.json"
    stale: dict | None = None
    if cache.is_file():
        try:
            cached = json.loads(cache.read_text())
            if cached.get("_v", 0) >= SCOREBOARD_V:
                return cached, None
            stale = cached
        except Exception:
            pass
    data, err = opendota_match(match_id)
    if err:
        return (stale, None) if stale is not None else (None, err)
    board = map_scoreboard(data)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.parent / "scoreboard.json.tmp"
        tmp.write_text(json.dumps(board, indent=1) + "\n")
        os.replace(tmp, cache)
    except Exception:
        pass
    return board, None


def items_from_opendota(raw: dict) -> dict | None:
    """OpenDota's purchase logs, in the items recipe's lane schema.

    OpenDota's ``purchase_log`` exists only for matches whose replay *they*
    parsed, so this is a fallback for matches on disk without the (replay-
    accurate) items.json the exporter writes.
    """
    players = []
    any_rows = False
    for p in raw.get("players") or []:
        rows = []
        for e in p.get("purchase_log") or []:
            key = str(e.get("key") or "").removeprefix("item_")
            if not key or not isinstance(e.get("time"), (int, float)):
                continue
            if rows and rows[-1][1] == key:
                rows[-1][3] += 1
                continue
            rows.append([e["time"], key, ITEM_DNAME.get(key, key), 1])
        if rows:
            any_rows = True
        players.append({
            "team": "radiant" if p.get("isRadiant") else "dire",
            "name": p.get("personaname") or "anonymous",
            "hero": HERO_NAME.get(p.get("hero_id")),
            "icon": HERO_ICON.get(p.get("hero_id")),
            "items": rows,
        })
    if not any_rows:
        return None
    return {
        "start_s": -90.0,
        "end_s": round(float(raw.get("duration") or 0), 1),
        "players": players,
    }


def items_fallback(match_id: int) -> dict | None:
    """items.json for a match without one, from OpenDota (cached on disk)."""
    data, err = opendota_match(match_id)
    if data is None or err:
        return None
    lanes = items_from_opendota(data)
    if lanes is None:
        return None
    path = MATCHES / str(match_id) / "items.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("items.json.tmp")
        tmp.write_text(json.dumps(lanes, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    except Exception:
        pass
    return lanes


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- plumbing -------------------------------------------------
    def send_json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not getattr(self, "_head", False):
            self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        if path.is_dir():
            path = path / "index.html"
        if not path.is_file():
            self.send_error_html()
            return
        ctype = self.guess_type(str(path))
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if ctype.startswith("text/html") or ctype == "application/json":
            # The proxy in front caches aggressively per URL; pages must never
            # go stale (their hashed assets change together with them), and a
            # cached 404 must never survive a parse that fills the JSON in.
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not getattr(self, "_head", False):
            self.wfile.write(data)

    def send_error_html(self) -> None:
        body = b"not found\n"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        # The proxy in front caches per URL: keep misses re-checkable, or a
        # parse's newly written file stays 404 behind them.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not getattr(self, "_head", False):
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self._head = True
        try:
            self.do_GET()
        finally:
            self._head = False

    @staticmethod
    def guess_type(path: str) -> str:
        base = os.path.basename(path)
        if "." not in base:  # extension-less page -> HTML (serve.py behavior)
            return "text/html"
        ctype = mimetypes.guess_type(path)[0]
        return ctype or "application/octet-stream"

    @staticmethod
    def safe(root: Path, rel: str) -> Path | None:
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            return None
        return root.joinpath(*parts)

    # ---- routes ---------------------------------------------------
    def do_GET(self) -> None:
        try:
            path = unquote(urlparse(self.path).path)
            if path.startswith("/api/"):
                self.api_get(path)
            elif path.startswith("/data/"):
                self.data_get(path)
            else:
                self.static_get(path)
        except Exception as e:  # never raise past the handler
            try:
                self.send_json(500, {"error": str(e) or e.__class__.__name__})
            except Exception:
                pass

    def do_POST(self) -> None:
        try:
            path = unquote(urlparse(self.path).path)
            if path.startswith("/api/parse/"):
                self.api_post_parse(path)
            else:
                self.send_json(404, {"error": "not found"})
        except Exception as e:
            try:
                self.send_json(500, {"error": str(e) or e.__class__.__name__})
            except Exception:
                pass

    def static_get(self, path: str) -> None:
        target = self.safe(DIST, path.lstrip("/"))
        if target is None:
            self.send_error_html()
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.exists() and "." not in os.path.basename(target.name):
            if target.stem == "app":
                target = target.with_name("index.html")  # /app alias for the home page
            else:
                target = target.with_name(target.name + ".html")  # symlink-less fallback
        self.send_file(target)

    def data_get(self, path: str) -> None:
        for prefix, root in (("/data/icons/heroes/", HERO_ICONS),
                             ("/data/icons/items/", ITEM_ICONS)):
            if path.startswith(prefix):
                target = self.safe(root, path[len(prefix):])
                if target is not None and target.is_file() and target.suffix == ".png":
                    self.send_file(target)
                else:
                    self.send_error_html()
                return
        if path.startswith("/data/matches/"):
            target = self.safe(MATCHES, path[len("/data/matches/"):])
            if target is not None and target.is_file():
                self.send_file(target)
                return
            # items.json missing for a match: OpenDota's purchase log is the
            # fallback source (it may also cover matches we never parsed).
            if (target is not None and target.name == "items.json"
                    and target.parent.name.isdigit()):
                lanes = items_fallback(int(target.parent.name))
                if lanes is not None:
                    self.send_json(200, lanes)
                    return
            self.send_error_html()
            return
        self.send_error_html()

    def api_get(self, path: str) -> None:
        parts = [p for p in path.split("/") if p]
        if parts == ["api", "matches"]:
            self.api_matches()
        elif len(parts) == 3 and parts[:2] == ["api", "matches"] and parts[2].isdigit():
            self.api_match(int(parts[2]))
        elif len(parts) == 4 and parts[:2] == ["api", "parse"] and parts[2].isdigit() \
                and parts[3] == "status":
            self.api_status(int(parts[2]))
        else:
            self.send_json(404, {"error": "not found"})

    def api_matches(self) -> None:
        req = urllib.request.Request(
            CH_URL, data=SQL.encode("utf-8"),
            headers={"Content-Type": "text/plain", "User-Agent": USER_AGENT},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
        except Exception as e:
            self.send_json(503, {"error": str(e) or e.__class__.__name__})
            return
        try:
            matches = [json.loads(l) for l in raw.splitlines() if l.strip()]
        except Exception as e:
            self.send_json(503, {"error": f"bad clickhouse response: {e}"})
            return
        # Unparsed ClickHouse rows + anything already parsed locally, whether
        # or not ClickHouse still has it.
        seen = {int(m["match_id"]) for m in matches if str(m.get("match_id", "")).isdigit()}
        fmt = "%Y-%m-%d %H:%M:%S"
        for mid in parsed_match_ids():
            if mid in seen:
                continue
            ts = match_timestamp(mid)
            matches.append({
                "match_id": mid,
                "latest_timestamp": time.strftime(fmt, time.gmtime(ts)) if ts else "",
            })
        matches.sort(key=lambda m: str(m.get("latest_timestamp", "")), reverse=True)
        self.send_json(200, {"matches": matches})

    def api_match(self, match_id: int) -> None:
        job = load_job(match_id)
        board, board_err = get_scoreboard(match_id)
        body = {
            "match_id": match_id,
            "parsed": is_parsed(match_id),
            "parse_error": (job or {}).get("error"),
            "scoreboard": board,
        }
        if board is None:
            body["scoreboard_error"] = board_err
        self.send_json(200, body)

    def api_status(self, match_id: int) -> None:
        job = load_job(match_id)
        if job is None:
            self.send_json(404, {"error": "no job"})
            return
        self.send_json(200, {
            "match_id": job.get("match_id", match_id),
            "stage": job.get("stage"),
            "pct": job.get("pct"),
            "log": job.get("log", []),
            "done": job.get("done", False),
            "error": job.get("error"),
        })

    def api_post_parse(self, path: str) -> None:
        parts = [p for p in path.split("/") if p]
        if len(parts) != 3 or parts[1] != "parse" or not parts[2].isdigit():
            self.send_json(404, {"error": "not found"})
            return
        match_id = int(parts[2])

        with _lock:
            if is_parsed(match_id):
                self.send_json(409, {"status": "already_parsed"})
                return
            if match_id in _active:
                self.send_json(409, {"status": "already_running"})
                return
            _active.add(match_id)  # reserve while we talk to OpenDota

        try:
            data, err = opendota_match(match_id)
            if err:
                with _lock:
                    _active.discard(match_id)
                self.send_json(502, {"status": "opendota_error"})
                return
            replay_url = (data or {}).get("replay_url")
            if not replay_url:
                with _lock:
                    _active.discard(match_id)
                self.send_json(404, {"status": "no_replay"})
                return
        except Exception:
            with _lock:
                _active.discard(match_id)
            self.send_json(502, {"status": "opendota_error"})
            return

        with _lock:
            if is_parsed(match_id):  # re-check after the network call
                _active.discard(match_id)
                self.send_json(409, {"status": "already_parsed"})
                return
        job = {"match_id": match_id, "stage": "queued", "pct": None,
               "log": [line("queued")], "done": False, "error": None}
        save_job(job)
        enqueue(match_id, replay_url)
        self.send_json(202, {"status": "started"})


def main() -> None:
    STORE.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=purger, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"replays backend on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
