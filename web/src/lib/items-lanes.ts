/**
 * The items recipe's view: every player's purchases as icons on a shared game
 * clock. One lane per player, stacked vertically (Radiant first), so several
 * players' build order reads at a glance. The browser mounts it; the server
 * writes the data (pipeline/export_match.py --items-data).
 */

export interface ItemLanesPlayer {
  slot: number;
  team: string;
  name: string;
  hero: string;
  icon: string;
  /** [game seconds, icon key, readable name, stacked purchases] in time order. */
  items: [number, string, string, number][];
}

export interface ItemLanesData {
  start_s: number;
  end_s: number;
  players: ItemLanesPlayer[];
}

/** The clock's reading for a purchase (negative is before the horn). */
export function itemClock(s: number): string {
  const sign = s < 0 ? "−" : "";
  const whole = Math.abs(Math.round(s));
  return `${sign}${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

const ICON_W = 22;
const COUNT_W = 18;
const ROW_H = 20;
const GAP = 3;
const STEP = 300; // ruler tick every 5 game minutes
const RULER_H = 28; // the sticky ruler's viewport height, for tooltip flipping

type ItemRow = ItemLanesPlayer["items"][number];

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string | null,
  ...children: Array<Node | string | null>
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  for (const child of children) if (child !== null) node.append(child);
  return node;
}

function titleOf(item: ItemRow): string {
  const [t, , display, count] = item;
  const stacked = count > 1 ? ` ×${count}` : "";
  return `${display}${stacked} · ${itemClock(t)}`;
}

/**
 * Several purchases logged at the same second — a combine chain (its
 * components come through the log together with the finished item) or a
 * ward stack-up — are one event on the chart: keep the final item of the run.
 * The pre-horn run is the starting build, not a combine, so it stays whole.
 */
function collapseRuns(items: ItemRow[]): ItemRow[] {
  const out: ItemRow[] = [];
  for (const item of items) {
    const prev = out[out.length - 1];
    if (prev && prev[0] === item[0] && item[0] > 0) out[out.length - 1] = item;
    else out.push(item);
  }
  return out;
}

/**
 * Icons at their exact clock's position — gaps between icons are the gaps
 * between purchases, never stretched to make room. Purchases that would
 * collide stack on further lines inside the lane; a run bought at the same
 * instant (starting items) stays on one line at that instant.
 */
function laneIcons(lane: HTMLElement, items: ItemRow[], width: number, span: number, start: number): void {
  const xOf = (t: number) => (t >= start ? ((t - start) / span) * width : 0);
  lane.replaceChildren();
  // Per sub-row's right edge, so a later purchase goes back to its own spot.
  const rowEnds: number[] = [];
  let prevT: number | null = null;
  let prevRow = 0;
  let rows = 1;
  for (const item of items) {
    const count = item[3];
    const needed = ICON_W + (count > 1 ? COUNT_W : 0);
    const at = xOf(item[0]);
    let row: number;
    let x = at;
    if (item[0] === prevT) {
      row = prevRow;
      x = Math.max(at, rowEnds[row] + GAP);
    } else {
      row = rowEnds.findIndex((end) => end + GAP <= at);
      if (row === -1) row = rowEnds.length;
    }
    while (rowEnds.length <= row) rowEnds.push(-Infinity);
    // Kept inside the lane: a purchase in the final seconds would otherwise
    // push its icon past the right edge (and the page into a sideways scroll).
    x = Math.min(x, Math.max(0, width - needed));
    rowEnds[row] = x + needed;
    prevT = item[0];
    prevRow = row;
    rows = Math.max(rows, row + 1);
    const wrap = el("span", "lane-item");
    wrap.style.left = `${x}px`;
    wrap.style.top = `${row * ROW_H}px`;
    wrap.dataset.title = titleOf(item);
    const img = el("img", "lane-item-icon", null);
    img.src = `/data/icons/items/${item[1]}.png`;
    img.alt = item[2];
    img.width = 22;
    img.height = 16;
    img.loading = "lazy";
    img.addEventListener("error", () => {
      const dot = el("span", "lane-item-dot");
      img.replaceWith(dot);
    });
    wrap.append(img);
    if (count > 1) wrap.append(el("i", "lane-item-count", `×${count}`));
    lane.append(wrap);
  }
  lane.style.height = `${rows * ROW_H + 4}px`;
}

export function mountItems(host: HTMLElement, data: ItemLanesData): void {
  const times = data.players.flatMap((player) => player.items.map(([t]) => t));
  const start = Math.min(data.start_s, ...(times.length ? times : []));
  const end = Math.max(data.end_s, ...(times.length ? times : []));
  const span = Math.max(end - start, 1);

  const head = el("div", "fights-head");
  head.innerHTML = `<h2>Item timelines</h2>`;

  const rulerTrack = el("div", "items-ruler-track");
  const ruler = el("div", "items-ruler", el("div", "items-ruler-gutter"), rulerTrack);
  const grid = el("div", "items-grid");
  const lanes = el("div", "items-lanes", grid);
  const tip = el("div", "lane-tip");
  tip.hidden = true;
  const frame = el("div", "items-frame", ruler, lanes, tip);
  frame.style.setProperty("--horn-x", `${start < 0 ? ((-start / span) * 100).toFixed(3) : 0}%`);
  const view = el("div", "items-view", head, frame);
  host.replaceChildren(view);

  const drawn: Array<[ItemRow[], HTMLElement]> = [];
  let trackWidth = 0;

  function drawRuler(): void {
    rulerTrack.replaceChildren();
    grid.replaceChildren();
    for (let t = Math.ceil(start / STEP) * STEP; t <= end; t += STEP) {
      const x = ((t - start) / span) * trackWidth;
      const tick = el("i", "items-tick");
      tick.style.left = `${x}px`;
      const label = el("span", "items-tick-label");
      label.style.left = `${x}px`;
      label.textContent = itemClock(t);
      rulerTrack.append(tick, label);
      const line = el("i", "items-grid-line");
      line.style.left = `${x}px`;
      grid.append(line);
    }
    if (start < 0 && end >= 0) {
      const horn = el("i", "items-grid-horn");
      horn.style.left = `${((0 - start) / span) * trackWidth}px`;
      grid.append(horn);
    }
  }

  function layout(): void {
    const width = rulerTrack.clientWidth;
    if (!width || width === trackWidth) return;
    trackWidth = width;
    tip.hidden = true;
    drawRuler();
    for (const [items, lane] of drawn) laneIcons(lane, items, trackWidth, span, start);
  }

  // The hover tooltip: item name (and stack) with its purchase time, above the
  // icon — or below it when the sticky ruler would cover it.
  frame.addEventListener("pointerover", (e) => {
    const item = (e.target as Element).closest<HTMLElement>("[data-title]");
    if (!item || !frame.contains(item)) return;
    tip.textContent = item.dataset.title ?? "";
    tip.hidden = false;
    const frameRect = frame.getBoundingClientRect();
    const r = item.getBoundingClientRect();
    const tw = tip.offsetWidth;
    let left = r.left - frameRect.left + r.width / 2 - tw / 2;
    left = Math.max(0, Math.min(left, frame.clientWidth - tw));
    let top = r.top - tip.offsetHeight - 8;
    if (top < RULER_H) top = r.bottom + 8;
    tip.style.left = `${left}px`;
    tip.style.top = `${top - frameRect.top}px`;
  });
  frame.addEventListener("pointerout", (e) => {
    const from = (e.target as Element).closest("[data-title]");
    const to =
      e.relatedTarget instanceof Element ? e.relatedTarget.closest("[data-title]") : null;
    if (from && from !== to) tip.hidden = true;
  });

  const byTeam = new Map<string, ItemLanesPlayer[]>();
  for (const player of data.players) {
    const rows = byTeam.get(player.team) ?? [];
    rows.push(player);
    byTeam.set(player.team, rows);
  }

  for (const [team, label] of [["radiant", "Radiant"], ["dire", "Dire"]] as const) {
    const players = byTeam.get(team) ?? [];
    if (!players.length) continue;
    lanes.append(el("div", `items-team items-team--${team}`, label));
    for (const player of players) {
      const labelCell = el("div", "items-label");
      const heroImg = el("img", `lane-hero lane-hero--${team}`);
      heroImg.src = `/data/icons/heroes/${player.icon}.png`;
      heroImg.alt = "";
      heroImg.width = 26;
      heroImg.height = 26;
      heroImg.loading = "lazy";
      heroImg.addEventListener("error", () => (heroImg.style.visibility = "hidden"));
      labelCell.append(
        heroImg,
        el("span", "lane-name", player.name),
        el("span", "lane-hero-name", player.hero),
      );
      const lane = el("div", "items-lane");
      lanes.append(el("div", "items-row", labelCell, lane));
      drawn.push([collapseRuns(player.items), lane]);
    }
  }

  new ResizeObserver(() => layout()).observe(rulerTrack);
  layout();
}
