"""Populate `data/` (first run): catalog JSONs, Steam CDN icons, the 7.41 map.

`data/` is not committed. This script fills it from reproducible sources:

- `data/heroes.json` — copied from the installed `gem` wheel (its own
  catalog JSONs; keep `gem-dota==0.14.0` pinned so the copy is stable);
- `data/icons/heroes/*.png` and `data/icons/items/*.png` — downloaded from
  the Dota 2 CDN (same URL patterns as gem's `scripts/_icons.py`), named
  from the wheel's `heroes.json` / `items.json`;
- `data/maps/Game_map_7.41.jpg` — the gem repo's asset at the pinned tag,
  verified against a known sha256.

Usage:

    uv run scripts/fetch_assets.py           # fill what's missing
    uv run scripts/fetch_assets.py --check   # verify only, download nothing
    uv run scripts/fetch_assets.py --force   # re-download everything
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request
from importlib import resources
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ICON_DIRS = (DATA / "icons" / "heroes", DATA / "icons" / "items")

MAP_URL = "https://raw.githubusercontent.com/whanyu1212/gem-dota/v0.14.0/assets/maps/Game_map_7.41.jpg"
MAP_SHA256 = "9fb56fc131acc161fb904f6c7189c6f37c57705bac752ccf9a47e01b1b079764"

HERO_CDN_URLS = (
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/heroes/{short}_icon.png",
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes/icons/{short}.png",
    "https://cdn.stratz.com/images/dota2/heroes/{short}_icon.png",
)
ITEM_CDN_URLS = (
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/items/{short}.png",
    "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/items/{short}_lg.png",
)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_POOL_DELAY_S = 0.05  # politeness between CDN hits


def _wheel_json(name: str) -> dict:
    return json.loads(resources.files("gem").joinpath(f"data/{name}").read_text(encoding="utf-8"))


def catalog_jsons(*, force: bool) -> None:
    """The wheel's catalog JSONs we read directly (currently just heroes.json)."""
    target = DATA / "heroes.json"
    if target.exists() and not force:
        return
    DATA.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(resources.files("gem").joinpath("data/heroes.json"), target)


def _shorts(json_name: str, *, prefix: str) -> list[str]:
    names = _wheel_json(json_name)
    keys = names if isinstance(names, (list, tuple)) else sorted(names)
    return [name.removeprefix(prefix) for name in keys]


def _fetch(url: str, *, timeout: float = 30.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "replays/0.1 asset fetch"})
    with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as r:
        body = r.read()
    if not body.startswith(_PNG_MAGIC):
        raise ValueError(f"not a PNG: {url}")
    return body


def _download_icons(shorts: list[str], urls: tuple[str, ...], out: Path, *, force: bool) -> tuple[int, int, int]:
    out.mkdir(parents=True, exist_ok=True)
    got = skipped = failed = 0
    for short in shorts:
        target = out / f"{short}.png"
        if target.exists() and target.stat().st_size > 0 and not force:
            skipped += 1
            continue
        body = None
        for pattern in urls:
            try:
                body = _fetch(pattern.format(short=short))
                break
            except Exception:
                time.sleep(_POOL_DELAY_S)
        if body is None:
            failed += 1
            continue
        target.write_bytes(body)
        got += 1
        time.sleep(_POOL_DELAY_S)
    return got, skipped, failed


def icons(*, force: bool) -> bool:
    ok = True
    for json_name, prefix, urls, out in (
        ("heroes.json", "npc_dota_hero_", HERO_CDN_URLS, ICON_DIRS[0]),
        ("items.json", "item_", ITEM_CDN_URLS, ICON_DIRS[1]),
    ):
        got, skipped, failed = _download_icons(_shorts(json_name, prefix=prefix), urls, out, force=force)
        label = "heroes" if json_name == "heroes.json" else "items"
        print(f"icons/{label}: downloaded {got}, skipped {skipped}, failed {failed}")
        # A hero icon is required (every match needs them); item icons are
        # best-effort — the CDN has drifted across patches and the playback
        # shows the item's name when its icon is missing.
        if failed and label == "heroes":
            ok = False
    return ok


def map_image(*, force: bool) -> bool:
    target = DATA / "maps" / "Game_map_7.41.jpg"
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == MAP_SHA256 and not force:
        return True
    if not force and target.exists():
        print("map: present but hash mismatch; use --force to re-download")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(MAP_URL, headers={"User-Agent": "replays/0.1 asset fetch"})
    with urllib.request.urlopen(request, timeout=120, context=ssl.create_default_context()) as r:
        target.write_bytes(r.read())
    whole = hashlib.sha256(target.read_bytes()).hexdigest()
    if whole != MAP_SHA256:
        target.unlink(missing_ok=True)
        print("map: downloaded bytes do not match the pinned sha256")
        return False
    return True


def check() -> int:
    problems: list[str] = []
    if not (DATA / "heroes.json").is_file():
        problems.append("data/heroes.json missing")
    for label, out, json_name, prefix in (("heroes", ICON_DIRS[0], "heroes.json", "npc_dota_hero_"),):
        expected = len(_shorts(json_name, prefix=prefix))
        found = len(list(out.glob("*.png"))) if out.is_dir() else 0
        if found != expected:
            problems.append(f"icons/{label}: {found} of {expected} present")
    if not (DATA / "maps" / "Game_map_7.41.jpg").is_file():
        problems.append("data/maps/Game_map_7.41.jpg missing")
    else:
        whole = hashlib.sha256((DATA / "maps" / "Game_map_7.41.jpg").read_bytes()).hexdigest()
        if whole != MAP_SHA256:
            problems.append("map sha256 mismatch")
    if problems:
        print("missing or stale assets — run: uv run scripts/fetch_assets.py")
        for line in problems:
            print(f"  - {line}")
        return 1
    for label, out, json_name, prefix in (
        ("heroes", ICON_DIRS[0], "heroes.json", "npc_dota_hero_"),
        ("items", ICON_DIRS[1], "items.json", "item_"),
    ):
        expected = len(_shorts(json_name, prefix=prefix))
        found = len(list(out.glob("*.png"))) if out.is_dir() else 0
        if found and found < expected:  # informational: item icons are best-effort
            print(f"(warn) icons/{label}: {expected - found} not on the CDN; the playback shows names for those")
    print("assets ok:", ", ".join(
        f"{label}={len(list(out.glob('*.png')))}" for label, out in (("heroes", ICON_DIRS[0]), ("items", ICON_DIRS[1]))
    ) + ", map ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify, download nothing")
    parser.add_argument("--force", action="store_true", help="re-download even what's present")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    catalog_jsons(force=args.force)
    ok = icons(force=args.force)
    ok = map_image(force=args.force) and ok
    if check():
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
