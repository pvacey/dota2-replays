/**
 * The fights recipe's figure in the browser. The page already shows every
 * fight on the map (src/figures/fights.ts); this adds a size filter, a timeline
 * of each fight's kills to pick a stretch on (drag; a click goes back to the
 * whole match), a list, and each fight's card. Picking a fight loads its data
 * and opens it in Figure 3's player below: the feed, the death recaps and the
 * damage timeline for every fight, and the map playback for the bigger ones.
 */
import { createPlayer, type View } from "./fight-player";
import { damageStrip, fightLegend, playbackControls } from "./fight-markup";
import type { FightData } from "./fight-playback";
import { card, fightDots, fightsIn, pickShown, score, type FightEntry, type FightsData } from "./fights-data";
import { attachRangePicker, placeRange, type TimeRange } from "./range-picker";
import { clock } from "./time";

const SVG = "http://www.w3.org/2000/svg";
/** A drag on the timeline shorter than this is a click. */
const MIN_RANGE_S = 20;
const SIZES: [number, string][] = [
  [1, "All fights"],
  [2, "2+ deaths"],
  [3, "3+"],
  [5, "5+"],
];

function el<K extends keyof HTMLElementTagNameMap>(tag: K, attrs: Record<string, string> = {}, ...children: (Node | string)[]) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  node.append(...children);
  return node;
}
function svgEl(tag: string, attrs: Record<string, string | number>, parent: Element) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  parent.append(node);
  return node;
}

export interface MountOpts {
  /** Resolve a fight's data by its `file` field (e.g. "fights/1.json"). */
  loadFight: (file: string) => Promise<{ default: FightData }>;
  /** Icon URLs by hero/item name; null → the player falls back to /data/icons/{heroes|items}/{name}.png. */
  icons?: View["icons"] | null;
}

export function mountFightsFigure(figure: HTMLElement, data: FightsData, opts?: MountOpts): void {
  if (!opts?.loadFight) throw new Error("mountFightsFigure: opts.loadFight is required to load a fight's data.");
  const loadFight = opts.loadFight;
  const assets = figure.dataset.assets ?? "";
  const $ = <T extends Element>(selector: string) => figure.querySelector<T>(selector)!;
  let minDeaths = 1;
  let range: TimeRange | null = null;
  // Open on the fight a link names (#fight-6, from the objectives recipe), or the
  // biggest: Figure 3's, on the home page.
  const linked = /^#fight-(\d+)$/.exec(location.hash);
  const named = linked ? data.fights.find((f) => f.n === Number(linked[1])) : undefined;
  let selected: number | null = named?.n ?? [...data.fights].sort((a, b) => b.deaths - a.deaths)[0]?.n ?? null;
  let player: ReturnType<typeof createPlayer> | undefined;
  let loading = 0;

  // --- Size filter and the range pill ---
  const controls = $<HTMLElement>("[data-controls]");
  const sizeButtons = SIZES.map(([n, label]) => {
    const button = el("button", { type: "button", class: "chip", "aria-pressed": String(n === minDeaths) }, label);
    button.addEventListener("click", () => {
      minDeaths = n;
      for (const [k, b] of sizeButtons.entries()) b.setAttribute("aria-pressed", String(SIZES[k][0] === n));
      render();
    });
    return button;
  });
  const pill = el("button", { type: "button", class: "chip range-pill", "aria-pressed": "true", hidden: "" });
  pill.addEventListener("click", () => {
    range = null;
    render();
  });
  controls.append(el("span", { class: "chip-label" }, "Show"), ...sizeButtons, pill);

  // --- The card beside the map ---
  // Optional: a page may omit the side panel entirely. Built once; render()
  // rewrites its lines in place.
  const side = figure.querySelector<HTMLElement>("[data-side]");
  const sideTitle = el("p", { class: "fights-card-title", hidden: "" });
  const sideKills = el("p", { class: "fights-card-kills", hidden: "" });
  const sideGold = el("p", { class: "fights-card-line", hidden: "" });
  const sideNext = el("p", { class: "fights-card-line", hidden: "" });
  const sideBelow = el("p", { class: "fights-card-line", hidden: "" });
  const sideEmpty = el("p", { class: "fights-card-line" }, "No fights here. Pick another stretch, or a smaller size.");
  side?.append(sideTitle, sideKills, sideGold, sideNext, sideBelow, sideEmpty);

  // --- Picking a fight: on the map, in the list, or on the timeline ---
  const dots = $<SVGGElement>("[data-fights]");
  const pick = (n: number) => {
    if (n === selected) return;
    selected = n;
    // Keep the address naming the open fight, so it can be shared.
    history.replaceState(null, "", `#fight-${n}`);
    render();
    openPanel();
  };
  const pickFrom = (event: Event) => {
    const target = (event.target as Element).closest("[data-fight]");
    if (target) pick(Number(target.getAttribute("data-fight")));
  };
  dots.addEventListener("click", pickFrom);
  dots.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      pickFrom(event);
    }
  });

  // --- The timeline: each fight's kills, Radiant above and Dire below ---
  const strip = $<HTMLElement>("[data-timeline]");
  const timeline = document.createElementNS(SVG, "svg") as SVGSVGElement;
  timeline.setAttribute("viewBox", "0 0 1000 64");
  timeline.setAttribute("preserveAspectRatio", "none");
  timeline.setAttribute("aria-hidden", "true");
  const rangeRect = svgEl("rect", { class: "strip-range", y: 0, height: 64, visibility: "hidden" }, timeline) as SVGRectElement;
  const x = (s: number) => ((s - data.start_s) / (data.end_s - data.start_s)) * 1000;
  const peak = Math.max(1, ...data.fights.map((f) => Math.max(...f.kills)));
  const bars = data.fights.map((f) => {
    const g = svgEl("g", { class: "fight-bar" }, timeline);
    const left = x(f.start_s);
    const width = Math.max(4, x(f.start_s + f.duration) - left);
    svgEl("rect", { class: "strip-radiant", x: left, y: 32 - (f.kills[0] / peak) * 28, width, height: (f.kills[0] / peak) * 28 }, g);
    svgEl("rect", { class: "strip-dire", x: left, y: 32, width, height: (f.kills[1] / peak) * 28 }, g);
    return { f, g };
  });
  svgEl("line", { class: "strip-axis", x1: 0, x2: 1000, y1: 32, y2: 32 }, timeline);
  const ticks = el("div", { class: "ward-ticks", "aria-hidden": "true" });
  for (let s = 0; s <= data.end_s; s += 600) {
    const classes = [(s / 600) % 2 ? "ward-tick--minor" : "", x(s) < 40 ? "ward-tick--edge" : ""].filter(Boolean);
    ticks.append(el("span", { style: `left: ${x(s) / 10}%`, class: classes.join(" ") }, clock(s)));
  }
  strip.append(
    el(
      "p",
      { class: "strip-label" },
      "Kills in each fight",
      el("span", {}, el("i", { class: "strip-key strip-key--radiant", "aria-hidden": "true" }), "Radiant above"),
      el("span", {}, el("i", { class: "strip-key strip-key--dire", "aria-hidden": "true" }), "Dire below"),
      el("span", { class: "strip-hint" }, "Drag to pick a stretch; click for the whole match"),
    ),
    timeline,
    ticks,
  );
  attachRangePicker(timeline, {
    min: data.start_s,
    max: data.end_s,
    minSpan: MIN_RANGE_S,
    onRange(picked) {
      range = picked;
      render();
    },
    onClick() {
      range = null;
      render();
    },
  });

  // --- The list ---
  // One row per fight, kept across renders: render() only updates the head,
  // the selection, and which rows are shown, so the list keeps its scroll.
  const listBox = $<HTMLElement>("[data-list]");
  const list = el("ol", { class: "fight-list", "aria-label": "Fights" });
  const listHead = el("li", { class: "fight-list-head" });
  list.append(listHead);
  const listRows = new Map<number, { li: HTMLLIElement; button: HTMLButtonElement }>();
  for (const f of data.fights) {
    const button = el(
      "button",
      { type: "button", "data-fight": String(f.n), "aria-pressed": "false" },
      el("time", {}, f.start),
      el("span", { class: `fight-score fight-score--${f.more}` }, score(f)),
      el("span", {}, `${f.deaths} death${f.deaths === 1 ? "" : "s"}${f.box ? " · playback" : ""}${f.next ? ` · then ${f.next[0]}` : ""}`),
    );
    const li = el("li", {}, button);
    list.append(li);
    listRows.set(f.n, { li, button });
  }
  listBox.append(list);
  list.addEventListener("click", pickFrom);

  // --- The panel: the picked fight in Figure 3's player ---
  // The frame is built once and updated in place on every pick; only the
  // player's own widget (playback, strip, legend) is rebuilt per fight — the
  // player binds its listeners into it — and the player instance with it.
  const panel = $<HTMLElement>("[data-panel]");
  const panelHead = el("p", { class: "fights-panel-head", hidden: "" });
  const panelBox = el("div", { class: "fights-panel" });
  const layersBox = el("div", { class: "fight-layers", "data-layers": "", hidden: "" });
  const mapView = el("div", { class: "fights-panel-map" });
  const mapSvg = document.createElementNS(SVG, "svg") as SVGSVGElement;
  mapSvg.setAttribute("class", "map");
  mapSvg.setAttribute("data-map", "");
  const clipCircle = svgEl(
    "circle",
    { r: 11 },
    svgEl("clipPath", { id: "hero-badge", clipPathUnits: "userSpaceOnUse" }, svgEl("defs", {}, mapSvg)),
  ) as SVGCircleElement;
  const mapImage = svgEl("image", { preserveAspectRatio: "xMidYMid slice" }, mapSvg) as SVGImageElement;
  const dimRect = svgEl("rect", { class: "map-dim map-dim--fight" }, mapSvg) as SVGRectElement;
  mapView.append(layersBox, mapSvg);
  const statusBox = el("div", { class: "fight-status", "data-status": "", hidden: "" });
  const filtersBox = el("div", { class: "feed-filters", "data-filters": "", hidden: "" });
  const feedList = el("ol", { class: "feed", "data-feed": "", hidden: "" });
  const feedBox = el("div", { class: "fights-panel-feed" }, statusBox, filtersBox, feedList);
  panelBox.append(mapView, feedBox);
  panel.append(panelHead, panelBox);
  panelBox.style.display = "none";

  // The player's own DOM goes here between picks; the frame around it stays.
  function clearPlayerDom() {
    for (const node of mapView.querySelectorAll("[data-playback], [data-strip], [data-legend]")) node.remove();
    for (const node of mapSvg.querySelectorAll("g, defs > filter")) node.remove();
    layersBox.replaceChildren();
    statusBox.replaceChildren();
    filtersBox.replaceChildren();
    feedList.replaceChildren();
    for (const part of [layersBox, statusBox, filtersBox, feedList]) part.hidden = true;
  }

  async function openPanel() {
    const fight = data.fights.find((f) => f.n === selected);
    const ticket = ++loading;
    if (!fight) {
      player?.destroy();
      player = undefined;
      panel.classList.remove("is-live", "is-playing", "is-following");
      clearPlayerDom();
      panelHead.hidden = true;
      panelBox.style.display = "none";
      return;
    }
    // Fetch first: the panel keeps showing the previous fight until the new
    // one is ready, so it never collapses mid-pick and the page never jumps.
    let fightData: FightData;
    try {
      fightData = (await loadFight(fight.file)).default;
    } catch {
      if (ticket !== loading) return;
      panelHead.hidden = false;
      panelHead.textContent = "Couldn't load this fight's data.";
      return;
    }
    if (ticket !== loading) return; // another fight was picked meanwhile
    // One synchronous swap: teardown, new attributes, widgets, player.
    player?.destroy();
    player = undefined;
    panel.classList.remove("is-live", "is-playing", "is-following");
    clearPlayerDom();
    panelHead.hidden = false;
    panelBox.style.display = "";
    panelHead.textContent = `${fight.box ? "Its playback" : "Its breakdown"}: ${card(fight).title}`;
    const box = fight.box ?? [0, 0, 1000];
    const [bx, by, size] = box;
    mapSvg.setAttribute("viewBox", `${bx} ${by} ${size} ${size}`);
    dimRect.setAttribute("x", String(bx));
    dimRect.setAttribute("y", String(by));
    dimRect.setAttribute("width", String(size));
    dimRect.setAttribute("height", String(size));
    if (fight.box) {
      mapImage.setAttribute("href", `${assets}fights/${fight.n}.webp`);
      mapImage.setAttribute("x", String(bx));
      mapImage.setAttribute("y", String(by));
      mapImage.setAttribute("width", String(size));
      mapImage.setAttribute("height", String(size));
      mapImage.style.display = "";
    } else {
      mapImage.style.display = "none";
    }
    clipCircle.setAttribute("r", String(11 * (size / 480)));
    const shared = el("div");
    shared.innerHTML = playbackControls(clock(fightData.start_s + fightData.duration)) + damageStrip() + fightLegend();
    mapView.append(
      shared.querySelector("[data-playback]")!,
      shared.querySelector("[data-strip]")!,
      shared.querySelector("[data-legend]")!,
    );
    const view: View = {
      box: box as [number, number, number],
      px: size / 480,
      icons: opts?.icons ?? undefined,
    };
    player = createPlayer(panel, view, fightData);
  }

  function render() {
    const shown = fightsIn(data, range, minDeaths);
    // A picked fight the filters leave out gives way to the biggest one left, or
    // to none (and an empty panel) when no fight is left.
    const next = pickShown(selected, shown);
    if (next !== selected) {
      selected = next;
      openPanel();
    }
    dots.innerHTML = fightDots(shown, selected);
    placeRange(rangeRect, range, data.start_s, data.end_s);
    pill.hidden = range === null;
    if (range) {
      const label = `${clock(range[0])}–${clock(range[1])}`;
      pill.textContent = `${label} ✕`;
      pill.setAttribute("aria-label", `Show the whole match instead of ${label}`);
    }
    for (const { f, g } of bars) {
      g.classList.toggle("is-out", f.deaths < minDeaths);
      g.classList.toggle("is-selected", f.n === selected);
    }

    const fight = data.fights.find((f) => f.n === selected);
    const sideShown = [sideTitle, sideKills, sideGold, sideNext, sideBelow];
    if (fight) {
      const c = card(fight);
      sideTitle.textContent = c.title;
      sideKills.textContent = c.kills;
      sideKills.className = `fights-card-kills fights-card-kills--${fight.more}`;
      sideGold.textContent = c.gold;
      sideNext.textContent = c.next;
      sideBelow.textContent = fight.box ? "Below: its playback and breakdown." : "Below: its breakdown.";
      sideEmpty.hidden = true;
      for (const p of sideShown) p.hidden = false;
    } else {
      for (const p of sideShown) p.hidden = true;
      sideEmpty.hidden = false;
    }
    const counts = { r: 0, d: 0, x: 0 };
    for (const f of shown) counts[f.more]++;
    listHead.textContent = `${shown.length} fights · Radiant had more kills in ${counts.r}, Dire in ${counts.d}, even in ${counts.x}`;
    const shownNs = new Set(shown.map((f) => f.n));
    for (const [n, row] of listRows) {
      row.li.hidden = !shownNs.has(n);
      row.button.setAttribute("aria-pressed", String(n === selected));
    }
  }

  for (const part of [controls, side, strip, listBox, panel]) if (part) part.hidden = false;
  render();
  openPanel();
  if (named) figure.scrollIntoView({ block: "start" });
}
