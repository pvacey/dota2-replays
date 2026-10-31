"""Export the data the replays app reads from a parsed replay.

Given a replay, writes the biggest fight's playback (``fight.json``), one file
per fight plus an ``index.json`` (``--fights-data``), and the map and fight-crop
images the app serves (``--public-figures``). Fights with a playback get their
heroes' readings from a second pass over the replay.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import shutil
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from types import ModuleType

import gem
from gem.analysis.fight_timeline import (
    FightTimeline,
    ModifierWindow,
    build_fight_timeline,
)
from gem.analysis.smoke import SmokeAnalysis
from gem.catalog.abilities import ABILITIES, ability_display
from gem.catalog.items import ITEMS, item_display
from gem.catalog.map import MAP_XMAX, MAP_XMIN, MAP_YMAX, MAP_YMIN
from gem.constants import hero_display
from gem.extractors.fights import Fight
from gem.results.models import ParsedMatch, ParsedPlayer

ROOT = Path(__file__).resolve().parent.parent
SOURCE_MAP = ROOT / "data" / "maps" / "Game_map_7.41.jpg"
COOKBOOK = ROOT / "pipeline" / "cookbook"

#: The overlay's coordinate square, and the map image's size in pixels.
SIZE = 1000
IMAGE_PX = 1200
#: World units around the fight's centroid whose hero paths frame the fight crop.
FIGHT_RADIUS = 2600
#: A sampled jump longer than this (world units) is a teleport or respawn, not a path.
PATH_JUMP = 900
#: Playback sample tolerances: a sample is dropped only when the straight line
#: between its neighbours puts the hero within this of it (map-square units for
#: the position, whole points for HP and mana), so the playback draws what the
#: replay says to well under a pixel.
POSITION_TOLERANCE = 0.1
POINTS_TOLERANCE = 0.49
TICKS_PER_SECOND = 30
TEAMS = {2: "radiant", 3: "dire"}
#: The playback runs on this long after the last death, for an instant buyback
#: and the last death's gold.
PLAYBACK_TAIL_TICKS = 2 * TICKS_PER_SECOND
#: A smoked hero counts as seen when the enemy saw it within this long of the break.
SEEN_AFTER_BREAK_S = 1.0
#: Hidden spans shorter than this (fog flickering at the edge of vision) are left
#: out, so the badges don't flicker.
MIN_HIDDEN_S = 0.5
#: A smoke that led into the fight starts the playback when it came at most this
#: many game seconds before the fight window, so the playback shows the smoked walk-in.
SMOKE_LEAD_S = 60.0
#: Buffs the playback draws as a ring around the hero (the site's choice of what
#: to show; every modifier is in the timeline).
BUFF_RINGS = {
    "modifier_black_king_bar_immune": "bkb",
    "modifier_minotaur_horn_immune": "bkb",
    "modifier_ghost_state": "ghost",
    "modifier_item_blade_mail_reflect": "blade_mail",
    "modifier_item_lotus_orb_active": "lotus",
    "modifier_item_satanic_unholy": "satanic",
    "modifier_item_aeon_disk_buff": "aeon_disk",
}
#: How many damage sources a death's recap lists (its total covers them all).
RECAP_ROWS = 6
#: Names for modifiers that don't name their ability.
MODIFIER_NAMES = {
    "modifier_stunned": "Stun",
    "modifier_bashed": "Bash",
    "modifier_knockback": "Knockback",
    "modifier_ancientapparition_coldfeet_freeze": "Cold Feet",
}
#: Name fragments of disables that carry no stun time: hexes, silences, disarms,
#: roots and the like (a modifier with a stun time is a disable anyway).
DISABLE_FRAGMENTS = (
    "voodoo",
    "sheepstick",
    "hex",
    "silence",
    "disarm",
    "root",
    "taunt",
    "fear",
    "sleep",
    "cyclone",
    "stunned",
    "bashed",
    "knockback",
)
#: A hero's own modifier shorter than this (a dash, a cast's short state) is left
#: out of the feed: the cast row already says it.
MIN_SELF_BUFF_S = 1.5
#: Words dropped from a modifier name that names no ability or item.
GENERIC_WORDS = {"modifier", "item", "debuff", "buff", "active", "effect", "super", "slow"}
#: Name fragments of modifiers the feed leaves out: internal timers, cooldowns and
#: bookkeeping, not effects a viewer would read as a buff or a debuff.
NOISE_FRAGMENTS = (
    "_timer",
    "_internal_cd",
    "cooldown",
    "teleporting",
    "regeneration",
    "phase_boots",
    "respawn",
    "thinker",
    "_counter",
    "_charge",
)


def _project(x: float, y: float) -> list[float]:
    fx = (x - MAP_XMIN) / (MAP_XMAX - MAP_XMIN)
    fy = 1.0 - (y - MAP_YMIN) / (MAP_YMAX - MAP_YMIN)
    return [round(fx * SIZE, 1), round(fy * SIZE, 1)]


def write_map_image(
    path: Path, box: list[float] | None = None, *, px: int = IMAGE_PX, webp: bool = False
) -> None:
    """The map square (the report maps' window), or a ``[x, y, size]`` crop of it, as a JPEG (or WebP)."""
    from PIL import Image

    x, y, size = box or [0.0, 0.0, float(SIZE)]
    with Image.open(SOURCE_MAP) as image:
        side = image.height
        left = (image.width - side) // 2
        scale = side / SIZE
        region = (
            round(left + x * scale),
            round(y * scale),
            round(left + (x + size) * scale),
            round((y + size) * scale),
        )
        square = image.convert("RGB").crop(region)
        square = square.resize((px, px), Image.Resampling.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    if webp:
        square.save(path, "WEBP", quality=80, method=6)
    else:
        square.save(path, "JPEG", quality=85, optimize=True, progressive=True)


def load_match(replay: Path) -> ParsedMatch:
    """Parse the replay (kept separate so tests can supply a match)."""
    return gem.parse(replay)


#: One hero reading: replay tick, world x and y, HP, max HP, mana, max mana and
#: life state (0 alive, 1 dying, 2 dead).
HeroState = tuple[int, float, float, int, int, float, float, int]


def load_hero_states(
    replay: Path, windows: Iterable[tuple[int, int]]
) -> dict[int, list[HeroState]]:
    """Every hero's state at the end of each replay packet in the windows, by player ID.

    A second parse, stopped after the last window. The replay sends entity updates once a
    packet (every other tick); reading at the packet's end means the state
    includes everything the packet's combat-log entries describe. Heroes are
    resolved the way ``PlayerExtractor`` resolves them, so illusions are skipped.
    Kept separate so tests can supply states.
    """
    from gem.extractors.players import PlayerExtractor, _build_hero_snapshot
    from gem.parser import ReplayParser

    spans = sorted(windows)
    parser = ReplayParser(replay)
    # Only for its hero resolution: one sample at the first tick, no minute marks.
    players = PlayerExtractor(sample_interval=sys.maxsize, minute_snapshots=False)
    players.attach(parser)
    states: dict[int, dict[int, HeroState]] = defaultdict(dict)

    def read(tick: int) -> None:
        if not any(start <= tick <= end for start, end in spans):
            return
        for player_id, entity in players._select_heroes().items():
            snap = _build_hero_snapshot(entity, tick, player_id)
            if snap.x is None or snap.y is None:
                continue
            # A tick's later packet overwrites its earlier one.
            states[player_id][tick] = (
                tick,
                snap.x,
                snap.y,
                snap.hp,
                snap.max_hp,
                snap.mana,
                snap.max_mana,
                snap.life_state,
            )

    # The completed-packet boundary: every delta and combat-log entry of the
    # packet has been applied (the parser's internal hook for a stable view).
    parser._on_packet_end(read)
    parser.stop_after_tick(max((end for _, end in spans), default=0))
    parser.parse()
    return {
        player_id: [by_tick[t] for t in sorted(by_tick)] for player_id, by_tick in states.items()
    }


def _unit_name(npc_name: str) -> str:
    """A readable name for a hero, or a tidied NPC name for anything else."""
    if npc_name.startswith("npc_dota_hero_"):
        return hero_display(npc_name)
    name = npc_name.removeprefix("npc_dota_")
    name = name.replace("goodguys", "radiant").replace("badguys", "dire")
    return name.replace("_", " ").strip().capitalize() or "Unknown"


#: Fights with this many deaths or more get a playback (hero states and the
#: map) in the fights recipe; every fight gets its breakdown (the timeline).
PLAYBACK_MIN_DEATHS = 0


def _cookbook(name: str) -> ModuleType:  # a recipe, so a figure shows what it computes
    spec = importlib.util.spec_from_file_location(f"cookbook_{name}", COOKBOOK / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_LANES = {"top": "top", "mid": "middle", "bot": "bottom"}


def _objective_name(what: str) -> str:
    """A building or Roshan, for a reader: "Radiant tier 1 bottom tower", "Dire top ranged barracks"."""
    if what == "roshan":
        return "Roshan"
    side, _, rest = what.partition("_")
    team = {"goodguys": "Radiant", "badguys": "Dire"}.get(side, side.capitalize())
    if tower := re.fullmatch(r"tower(\d)(?:_(\w+))?", rest):
        lane = _LANES.get(tower.group(2) or "", "")
        return f"{team} tier {tower.group(1)}{f' {lane}' if lane else ''} tower"
    if rax := re.fullmatch(r"(melee|range)_rax_(\w+)", rest):
        kind = "melee" if rax.group(1) == "melee" else "ranged"
        return f"{team} {_LANES.get(rax.group(2), rax.group(2))} {kind} barracks"
    return f"{team} {'Ancient' if rest == 'fort' else rest.replace('_', ' ')}"


def fight_box(match: ParsedMatch, fight: Fight, states: dict[int, list[HeroState]]) -> list[float]:
    """The map crop for a fight's playback: the heroes' readings near the fight, and its deaths' centre."""
    start, end = playback_window(match, fight)
    readings = [
        (x, y) for runs in states.values() for tick, x, y, *_ in runs if start <= tick <= end
    ]
    if fight.centroid_x is not None and fight.centroid_y is not None:
        centre: Point = (fight.centroid_x, fight.centroid_y)
    elif readings:
        centre = (
            sum(p[0] for p in readings) / len(readings),
            sum(p[1] for p in readings) / len(readings),
        )
    else:
        return [0.0, 0.0, float(SIZE)]
    return _crop([p for p in readings if math.dist(p, centre) <= FIGHT_RADIUS] + [centre])


def fights_recipe(
    match: ParsedMatch, files: dict[int, str], boxes: dict[int, list[float]]
) -> list[dict]:
    """Every fight for the fights recipe's figure, from the recipe itself (``fight_table``).

    ``files`` names each fight's data file by fight number (``fights/7.json``, or
    ``fight.json`` for Figure 3's); ``boxes`` holds the map crop of each fight
    with a playback.
    """
    table = _cookbook("fights").fight_table(match)
    by_number = dict(enumerate(sorted(match.fights, key=lambda f: f.start_tick), start=1))
    fights = []
    for row in table.itertuples(index=False):
        fight = by_number[row.fight]
        at = (
            _project(fight.centroid_x, fight.centroid_y)
            if fight.centroid_x is not None and fight.centroid_y is not None
            else None
        )
        entry: dict = {
            "n": row.fight,
            "file": files[row.fight],
            "start": row.start,
            "start_s": round(row.start_s, 2),
            "duration": round(row.duration_s, 1),
            "deaths": row.deaths,
            "kills": [row.radiant_kills, row.dire_kills],
            "more": {"radiant": "r", "dire": "d"}.get(row.more_kills, "x"),
            "at": at,
            "gold": [int(row.radiant_gold), int(row.dire_gold)],
            "next": (
                [
                    _objective_name(row.next),
                    {"radiant": "r", "dire": "d"}.get(row.next_for, ""),
                    int(row.next_after_s),
                ]
                if isinstance(row.next, str)
                else None
            ),
        }
        if row.fight in boxes:
            entry["box"] = boxes[row.fight]
        fights.append(entry)
    return fights


def _clock(match: ParsedMatch, tick: int) -> str:
    clock = match.game_clock
    return clock.format_tick(tick) if clock is not None else f"tick {tick}"


Point = tuple[float, float]


def _crop(points: list[Point]) -> list[float]:
    """A square ``[x, y, size]`` in map-square units around the points, kept on the map."""
    projected = [_project(*point) for point in points]
    x0, x1 = min(p[0] for p in projected), max(p[0] for p in projected)
    y0, y1 = min(p[1] for p in projected), max(p[1] for p in projected)
    size = min(float(SIZE), max(x1 - x0, y1 - y0, 160.0) + 80.0)
    left = min(max((x0 + x1 - size) / 2, 0.0), SIZE - size)
    top = min(max((y0 + y1 - size) / 2, 0.0), SIZE - size)
    return [round(left, 1), round(top, 1), round(size, 1)]


def biggest_fight(match: ParsedMatch) -> Fight | None:
    """The fight with the most deaths (the earliest of equals), or ``None``."""
    return max(match.fights, key=lambda f: (f.deaths, -f.start_tick), default=None)


def _game_seconds(match: ParsedMatch, tick: int) -> float:
    """The game clock at ``tick`` in seconds (pause-aware); raw ticks without a clock."""
    clock = match.game_clock
    now = clock.game_time_at(tick) if clock is not None else None
    return round(now if now is not None else tick / TICKS_PER_SECOND, 1)


def _clock_base(match: ParsedMatch, start: int, end: int) -> float:
    """The playback clock's reading at ``start``: ``floor(base + t)`` is gem's clock.

    gem shows OpenDota's whole seconds (``game_seconds_at``), which round the raw
    seconds and the game start separately, so a second doesn't turn over where the
    exact game time crosses a whole number. The offset between the two is fixed
    for a match: this finds it from every tick in the window, checks one offset
    fits them all, and adds half a tick so rounding ``t`` to the hundredth can't
    tip a second.
    """
    clock = match.game_clock
    exact = clock.game_time_at(start) if clock is not None else None
    if clock is None or exact is None:
        return _game_seconds(match, start)
    low, high = -math.inf, math.inf
    for tick in range(start, end + 1):
        now, shown = clock.game_time_at(tick), clock.game_seconds_at(tick)
        if now is None or shown is None:
            continue
        low, high = max(low, shown - now), min(high, shown + 1 - now)
    half_tick = 0.5 / TICKS_PER_SECOND
    if not low + half_tick + 0.005 < high:
        raise ValueError(f"no single clock offset fits ticks {start}-{end}")
    return round(exact + low + half_tick, 4)


def _seconds_since(match: ParsedMatch, start_tick: int, places: int = 1) -> Callable[[int], float]:
    """In-game seconds since ``start_tick``, rounded to ``places`` decimals.

    Pause-aware, like the clock times the narration shows; raw replay ticks only
    when the replay has no game clock. Two places keep every tick distinct (a
    tick is 1/30 s).
    """
    clock = match.game_clock
    start_s = clock.game_time_at(start_tick) if clock is not None else None

    def since(tick: int) -> float:
        now_s = clock.game_time_at(tick) if clock is not None else None
        if now_s is not None and start_s is not None:
            return round(now_s - start_s, places)
        return round((tick - start_tick) / TICKS_PER_SECOND, places)

    return since


def _ability_name(name: str) -> str:
    """The display name of an ability or item (``Avalanche``, ``Black King Bar``)."""
    return item_display(name) if name.startswith("item_") else ability_display(name)


def _source_name(source: str) -> str:
    """A damage source's display name; ``"Attack"`` for a right-click."""
    return _ability_name(source) if source not in ("", "dota_unknown") else "Attack"


def _modifier_name(modifier: str) -> str:
    """A display name for a modifier, from the ability or item its name starts with.

    ``modifier_lion_voodoo`` is Hex, ``modifier_item_shivas_guard_blast`` Shiva's
    Guard: trailing words are dropped until the rest names an item or an ability.
    """
    if modifier in MODIFIER_NAMES:
        return MODIFIER_NAMES[modifier]
    words = modifier.removeprefix("modifier_").removeprefix("item_").split("_")
    for n in range(len(words), 0, -1):
        name = "_".join(words[:n])
        if name in ITEMS:
            return item_display(name)
        if name in ABILITIES:
            return ABILITIES[name]
    # No ability or item by that name: tidy the words (dropping a hero prefix).
    kept = [w for w in words if w not in GENERIC_WORDS] or words
    return ability_display("_".join(kept))


def _applied_seconds(window: ModifierWindow) -> float:
    """The duration a modifier was applied with: its stun time, or the log's duration.

    ``0.0`` when the log gives none: a missing duration, or its ``-1`` sentinel.
    """
    if window.stun_s > 0:
        return window.stun_s
    return window.duration_s if window.duration_s is not None and window.duration_s > 0 else 0.0


def _modifier_kind(window: ModifierWindow, team_of: dict[str, int]) -> str | None:
    """``disable``, ``debuff`` or ``buff`` for the feed, or ``None`` to leave it out."""
    name = window.modifier
    if window.aura or window.start_tick is None or any(f in name for f in NOISE_FRAGMENTS):
        return None
    stun = window.stun_s > 0
    enemy = team_of.get(window.source_hero or "") != team_of.get(window.target)
    # A name fragment marks a disable only on an enemy (Eul's on yourself is a save).
    if stun or (enemy and window.source_hero and any(f in name for f in DISABLE_FRAGMENTS)):
        return "disable"  # kept even without a duration: the feed shows how long it lasted
    if _applied_seconds(window) <= 0:
        return None  # passives, auras and -1 ("no duration") the log gives no time for
    if window.source_hero is None:
        return None
    if window.source_hero == window.target and _applied_seconds(window) < MIN_SELF_BUFF_S:
        return None  # a dash or a cast's own short state: the cast row says it
    return "buff" if team_of.get(window.source_hero) == team_of.get(window.target) else "debuff"


def _hero_runs(
    states: list[HeroState], start: int, end: int, since: Callable[[int], float]
) -> list[list[list[float]]]:
    """The hero's samples in [start, end] as runs split at teleports, deaths and respawns.

    Each sample is ``[t, x, y, hp, max_hp, mana, max_mana]``: seconds since the
    window's start, the map-square position, and the hero's HP and mana at the
    same tick. Only living heroes have samples. A lone jump longer than ``PATH_JUMP``
    is a teleport (a Blink, a respawn), so the run breaks there; a run of long
    jumps is fast movement (a dash) and stays one path. Samples the client's
    straight-line interpolation already reproduces are dropped (``_thin``).
    """
    alive: list[list[HeroState]] = [[]]
    for state in states:
        if not start <= state[0] <= end:
            continue
        # Dying or dead: the playback hides a hero from its death, and the next
        # sample is a respawn, a buyback or an Aegis.
        if state[7] != 0:
            if alive[-1]:
                alive.append([])
            continue
        alive[-1].append(state)
    runs: list[list[HeroState]] = []
    for stretch in alive:
        jumps = [
            math.dist(a[1:3], b[1:3]) > PATH_JUMP
            for a, b in zip(stretch, stretch[1:], strict=False)
        ]
        run: list[HeroState] = []
        for i, state in enumerate(stretch):
            # jumps[i - 1] is the step into this sample; a teleport is one alone.
            if (
                i
                and jumps[i - 1]
                and not (i >= 2 and jumps[i - 2])
                and not (i < len(jumps) and jumps[i])
            ):
                runs.append(run)
                run = []
            run.append(state)
        if run:
            runs.append(run)
    return [
        _thin(
            [
                [since(tick), *_project(x, y), hp, top, round(mana), round(top_mana)]
                for tick, x, y, hp, top, mana, top_mana, _ in run
            ]
        )
        for run in runs
    ]


def _thin(samples: list[list[float]]) -> list[list[float]]:
    """Drop the samples that straight lines between the kept ones reproduce.

    Greedy: from each kept sample, reach as far as a straight line still passes
    within the tolerances of every sample it skips. Both ends of a flat stretch
    stay, so a step in HP stays a one-packet step.
    """
    tolerances = (
        0.0,
        POSITION_TOLERANCE,
        POSITION_TOLERANCE,
        POINTS_TOLERANCE,
        POINTS_TOLERANCE,
        POINTS_TOLERANCE,
        POINTS_TOLERANCE,
    )

    def reproduces(a: list[float], b: list[float], skipped: list[list[float]]) -> bool:
        for sample in skipped:
            # Two samples at one time (a pause): the line holds its start, as drawn.
            k = 0.0 if b[0] == a[0] else (sample[0] - a[0]) / (b[0] - a[0])
            for j in range(1, len(sample)):
                if abs(a[j] + (b[j] - a[j]) * k - sample[j]) > tolerances[j]:
                    return False
        return True

    if len(samples) <= 2:
        return samples
    kept = [samples[0]]
    anchor = 0
    for i in range(2, len(samples)):
        if not reproduces(samples[anchor], samples[i], samples[anchor + 1 : i]):
            anchor = i - 1
            kept.append(samples[anchor])
    kept.append(samples[-1])
    return kept


def playback_window(
    match: ParsedMatch, fight: Fight, smokes: list[SmokeAnalysis] | None = None
) -> tuple[int, int]:
    """The playback's first and last tick: the fight, from the smoke that led into it."""
    if smokes is None:
        smokes = gem.build_smoke_analysis(match)
    start = min(
        [fight.start_tick]
        + [
            smoke.activation_tick
            for smoke in smokes
            if smoke.first_fight is fight
            # Game time, not ticks, so a pause between the two doesn't count.
            and _game_seconds(match, fight.start_tick) - _game_seconds(match, smoke.activation_tick)
            <= SMOKE_LEAD_S
        ]
    )
    return start, (fight.last_death_tick or fight.end_tick) + PLAYBACK_TAIL_TICKS


def fight_playback(
    match: ParsedMatch,
    states: dict[int, list[HeroState]] | None = None,
    fight: Fight | None = None,
) -> dict | None:
    """A fight's playback (the biggest by default): hero state and the fight's timeline.

    Heroes are referred to by their index in ``heroes``. Every ``t`` is seconds
    since the playback's ``start`` (the fight window's start, or the smoke that
    led into the fight when it came shortly before), to the hundredth so every
    tick is distinct. ``states`` are the heroes' readings by player ID
    (``load_hero_states``). An empty dict (or ``None``) gives a breakdown with
    no hero states (no map).
    """
    fight = fight or biggest_fight(match)
    if fight is None:
        return None
    smokes = gem.build_smoke_analysis(match)
    start, end = playback_window(match, fight, smokes)
    if states is None:
        states = {}
    since = _seconds_since(match, start, places=2)
    timeline: FightTimeline = build_fight_timeline(match, start, end)
    players = sorted(match.players, key=lambda p: p.player_id)
    index = {player.hero_name: i for i, player in enumerate(players)}

    def hero(name: str | None) -> int | None:
        return index.get(name) if name else None

    casts = []
    for cast in timeline.casts:
        row: dict = {
            "t": since(cast.tick),
            "by": index[cast.caster],
            "what": _ability_name(cast.ability),
            "hits": [
                [index[hit.hero], hit.damage, hit.damage_type, round(hit.stun_s, 1)]
                for hit in cast.hits
            ],
        }
        if cast.is_item:
            row["item"] = cast.ability.removeprefix("item_")
        if cast.target is not None and cast.target != cast.caster:
            if cast.target_is_hero:
                row["target"] = index[cast.target]
            else:
                row["unit"] = _unit_name(cast.target)
        if cast.self_effect is not None or cast.target == cast.caster:
            row["self"] = True
        casts.append(row)

    team_of = {i: TEAMS.get(player.team, "unknown") for i, player in enumerate(players)}
    hero_team = {player.hero_name: player.team for player in players}
    hidden = _hidden(match, players, start, end, since)
    deaths = []
    for death in timeline.deaths:
        rewards = timeline.rewards_at(death.tick)
        # The log can't say which of several same-tick deaths a bounty paid for, so
        # the shared rewards go on the tick's first death only, and every death on
        # the tick lists the tick's victims.
        first_on_tick = rewards is None or rewards.victims[0] == death.victim
        entry: dict = {
            "t": since(death.tick),
            "victim": index[death.victim],
            "killer": hero(death.killer_hero),
            "killer_name": _unit_name(death.killer),
            "aegis": death.reincarnated,
            "gold_lost": death.gold_lost,
            "gold": [[index[h], g] for h, g in rewards.gold.items() if h in index]
            if rewards and first_on_tick
            else [],
            "xp": [[index[h], x] for h, x in rewards.xp.items() if h in index]
            if rewards and first_on_tick
            else [],
            # The largest sources (the recap's rows), and the total over all of them.
            "recent": [
                [
                    _unit_name(taken.attacker_hero or taken.attacker),
                    _source_name(taken.source),
                    taken.damage_type,
                    taken.damage,
                ]
                for taken in death.recent_damage[:RECAP_ROWS]
            ],
            "recent_total": sum(taken.damage for taken in death.recent_damage),
        }
        if rewards and len(rewards.victims) > 1:
            entry["tick_victims"] = [index[v] for v in rewards.victims if v in index]
        deaths.append(entry)
    return {
        "start": _clock(match, start),
        # The clock at the start, in seconds: the playback shows floor(start_s + t),
        # which is gem's clock at every tick (``start`` is whole seconds).
        "start_s": _clock_base(match, start, end),
        "duration": since(end),
        "heroes": [
            {
                "hero": hero_display(player.hero_name),
                "name": player.player_name,
                "icon": player.hero_name.removeprefix("npc_dota_hero_"),
                "team": team_of[i],
                "runs": _hero_runs(states.get(player.player_id, []), start, end, since),
            }
            for i, player in enumerate(players)
        ],
        "casts": casts,
        # [t, attacker, target, damage, type, by the hero itself (not a summon or
        # illusion), source ("Attack" for a right-click)]
        "damage": [
            [
                since(burst.start_tick),
                index[burst.attacker_hero],
                index[burst.target],
                burst.damage,
                burst.damage_type,
                int(burst.attacker == burst.attacker_hero and not burst.attacker_is_illusion),
                _source_name(burst.source),
            ]
            for burst in timeline.damage
            if burst.attacker_hero in index and burst.target in index
        ],
        # [t, until, target, source hero or null, name, kind, ring, seconds]:
        # disables, debuffs and buffs on heroes. ``until`` is the removal (the
        # playback's end when it outlasted it); ``ring`` names a buff drawn as a
        # ring; ``seconds`` is the duration it was applied with (the stun time,
        # or the log's duration), which a death or dispel can cut short.
        "modifiers": [
            [
                since(window.start_tick),
                since(window.end_tick) if window.end_tick is not None else since(end),
                index[window.target],
                hero(window.source_hero),
                _modifier_name(window.modifier),
                kind,
                BUFF_RINGS.get(window.modifier),
                round(_applied_seconds(window), 1),
            ]
            for window in timeline.modifiers
            if window.start_tick is not None
            and (kind := _modifier_kind(window, hero_team)) is not None
        ],
        "deaths": deaths,
        # [t, hero, cost]
        "buybacks": [[since(b.tick), index[b.hero], b.cost] for b in timeline.buybacks],
        # [team, t of the smoke's use or null before the window, members]; each
        # member [hero, smoked from, smoke broke or null, seen by the enemy within
        # a second of the break or null], clipped to the window.
        "smokes": _smokes(smokes, start, end, index, since, hidden),
        # [hero, from, to]: while the enemy team couldn't see the hero.
        "hidden": hidden,
    }


def _smokes(
    smokes: list,
    start: int,
    end: int,
    index: dict[str, int],
    since: Callable[[int], float],
    hidden: list[list[float]],
) -> list:
    """The smokes on any hero during [start, end], with each member's smoked span.

    A member counts as seen by the enemy when its hidden span ends within a second
    of the smoke breaking (the replay's visibility, not the smoke's own record).
    """

    def seen(hero: int, broke: float | None) -> float | None:
        if broke is None:
            return None
        ends = [to for h, _, to in hidden if h == hero and 0 <= to - broke <= SEEN_AFTER_BREAK_S]
        return min(ends, default=None)

    out = []
    for smoke in smokes:
        members = [
            [
                index[member.hero_name],
                since(max(member.applied_tick, start)),
                # A break after the playback's end is "still smoked at the end".
                broke := since(member.removed_tick)
                if member.removed_tick is not None and member.removed_tick <= end
                else None,
                seen(index[member.hero_name], broke),
            ]
            for member in smoke.members
            if member.hero_name in index
            and member.applied_tick <= end
            and (member.removed_tick is None or member.removed_tick >= start)
        ]
        if members:
            used = since(smoke.activation_tick) if smoke.activation_tick >= start else None
            out.append([TEAMS.get(smoke.team, "unknown"), used, members])
    return out


def _hidden(
    match: ParsedMatch,
    players: list[ParsedPlayer],
    start: int,
    end: int,
    since: Callable[[int], float],
) -> list[list[float]]:
    """Spans of [start, end] when the enemy team couldn't see each hero.

    From the replay's per-team hero visibility (``match.hero_visibility_events``);
    only ``hidden`` counts, not ``unknown``.
    """
    out: list[list[float]] = []
    for i, player in enumerate(players):
        # What the enemy team saw: Dire's view of a Radiant hero, and the reverse.
        seen_by_dire = player.team == 2
        # By tick only: the sort is stable, so same-tick transitions keep the
        # extractor's order (a terminal state, then the replacement's).
        states = sorted(
            (
                (e.tick, str(e.dire_state if seen_by_dire else e.radiant_state))
                for e in match.hero_visibility_events
                if e.player_id == player.player_id
            ),
            key=lambda state: state[0],
        )
        before = [seen for tick, seen in states if tick <= start]
        hidden_since = start if before and before[-1] == "hidden" else None
        for tick, seen in states:
            if not start < tick <= end:
                continue
            if seen == "hidden" and hidden_since is None:
                hidden_since = tick
            elif seen != "hidden" and hidden_since is not None:
                if since(tick) - since(hidden_since) >= MIN_HIDDEN_S:
                    out.append([i, since(hidden_since), since(tick)])
                hidden_since = None
        if hidden_since is not None:
            out.append([i, since(hidden_since), since(end)])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("replay", type=Path)
    parser.add_argument("--fight-data", type=Path, required=True)
    parser.add_argument("--fights-data", type=Path, required=True)
    parser.add_argument("--public-figures", type=Path, required=True)
    args = parser.parse_args(argv)

    match = load_match(args.replay)
    written: list[Path] = []
    # Figure 3's fight, and every fight big enough for a playback in the fights
    # recipe: one more pass over the replay reads every hero in all their windows.
    home_fight = biggest_fight(match)
    in_order = sorted(match.fights, key=lambda f: f.start_tick)
    played = [f for f in in_order if f.deaths >= PLAYBACK_MIN_DEATHS or f is home_fight]
    windows = [playback_window(match, f) for f in played]
    states = load_hero_states(args.replay, windows) if windows else {}
    for start, end in windows:
        missing = [
            p.hero_name
            for p in match.players
            if not any(start <= reading[0] <= end for reading in states.get(p.player_id, []))
        ]
        if missing:
            raise SystemExit(f"no hero readings in the playback window for {', '.join(missing)}")
    playback = fight_playback(match, states, home_fight) if home_fight else None
    args.fight_data.parent.mkdir(parents=True, exist_ok=True)
    # Compact: the playback is loaded by the browser, so every byte counts.
    args.fight_data.write_text(json.dumps(playback, separators=(",", ":")) + "\n")
    written.append(args.fight_data)
    # The fights recipe: one file per fight (Figure 3's is fight.json). A fight
    # with a playback gets the hero states and a map crop; the rest get an
    # empty dict, so their breakdown has no map (never the once-a-second samples).
    shutil.rmtree(args.fights_data, ignore_errors=True)
    args.fights_data.mkdir(parents=True)
    crops = args.public_figures / "fights"
    shutil.rmtree(crops, ignore_errors=True)
    files: dict[int, str] = {}
    boxes: dict[int, list[float]] = {}
    for number, each in enumerate(in_order, start=1):
        has_playback = any(each is f for f in played)
        if has_playback:
            boxes[number] = fight_box(match, each, states)
            write_map_image(crops / f"{number}.webp", boxes[number], px=800, webp=True)
        if each is home_fight:
            files[number] = "fight.json"
            continue
        data_file = fight_playback(match, states if has_playback else {}, each)
        # Named from src/data, as the index and fight.json are.
        files[number] = f"fights/{number}.json"
        (args.fights_data / f"{number}.json").write_text(
            json.dumps(data_file, separators=(",", ":")) + "\n"
        )
    # The timeline spans the match, as the wards figure's does.
    end_tick = match.post_game_tick or match.game_end_tick or 0
    index = {
        "start_s": -90.0,
        "end_s": round(_game_seconds(match, end_tick), 1),
        "fights": fights_recipe(match, files, boxes),
    }
    (args.fights_data / "index.json").write_text(json.dumps(index, separators=(",", ":")) + "\n")
    written.append(args.fights_data)

    write_map_image(args.public_figures / "map.webp", px=1000, webp=True)
    written.append(args.public_figures)
    print("Wrote " + ", ".join(str(path) for path in written))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
