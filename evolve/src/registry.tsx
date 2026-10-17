import { createContext, useContext } from "react";
import { motion } from "motion/react";
import { defineRegistry } from "@json-render/react";
import { catalog, type CalendarSpec } from "./shared/catalog";
import type { CalEvent } from "./shared/types";
import {
  addDays, DAY_KEYS, dayId, daysBetween, hourOf, isoWeek, mondayOf, mondayOfIsoWeek, monthId, MONTHS, TODAY, weekday, weekId, weeksInIsoYear,
} from "./shared/dates";

// ---------- shared context: events plus who owns each animation id ----------

type Ctx = { events: CalEvent[]; owner: Map<string, string> };
export const CalendarContext = createContext<Ctx>({ events: [], owner: new Map() });

// Every day, week and month on screen gets a stable layoutId, so Motion morphs it between any two views.
// When two components in one view would render the same period, the finer-grained one owns the id.
const PRIORITY: Record<string, number> = { WeekGrid: 4, MonthGrid: 3, WeekStrip: 2, YearBand: 1 };

function idsFor(type: string, p: Record<string, unknown>): string[] {
  switch (type) {
    case "WeekGrid": {
      const mon = mondayOf(String(p.weekOf));
      return [weekId(mon), ...(p.days as string[]).map((d) => dayId(addDays(mon, DAY_KEYS.indexOf(d as never))))];
    }
    case "MonthGrid": {
      const first = `${p.month}-01`;
      const ids = [monthId(first)];
      for (let d = first; d.slice(0, 7) === p.month; d = addDays(d, 1)) ids.push(dayId(d));
      for (let w = mondayOf(first); w.slice(0, 7) <= String(p.month); w = addDays(w, 7)) ids.push(weekId(w));
      return ids;
    }
    case "WeekStrip": {
      const y = Number(p.year);
      return Array.from({ length: weeksInIsoYear(y) }, (_, i) => weekId(mondayOfIsoWeek(y, i + 1)));
    }
    case "YearBand": {
      const y = Number(TODAY.slice(0, 4));
      return Number(p.from) <= y && y <= Number(p.to) ? [monthId(TODAY)] : [];
    }
  }
  return [];
}

export function owners(spec: CalendarSpec): Map<string, string> {
  const m = new Map<string, string>();
  for (const el of Object.values(spec.elements)) {
    for (const id of idsFor(el.type, el.props)) {
      const cur = m.get(id);
      if (!cur || PRIORITY[el.type] > PRIORITY[cur]) m.set(id, el.type);
    }
  }
  return m;
}

function useCal(type: string) {
  const c = useContext(CalendarContext);
  return { events: c.events, lid: (id: string) => (c.owner.get(id) === type ? id : undefined) };
}

// ---------- helpers ----------

const PALETTE = [
  { bg: "#DCE7FA", fg: "#1E4FA8", solid: "#3B6FD0" },
  { bg: "#D3EEE9", fg: "#0A5C53", solid: "#0E7C70" },
  { bg: "#FBE3C6", fg: "#8A4305", solid: "#D9822B" },
  { bg: "#E8DDF7", fg: "#5B2E91", solid: "#7E4CC2" },
  { bg: "#F7D9DE", fg: "#8E2238", solid: "#C2415B" },
];
const colorFor = (cat: string) => PALETTE[[...cat].reduce((a, c) => a + c.charCodeAt(0), 0) % PALETTE.length];
const day0 = (e: CalEvent) => e.start.slice(0, 10);
const day1 = (e: CalEvent) => e.end.slice(0, 10);
const on = (e: CalEvent, from: string, to: string) => day0(e) <= to && day1(e) >= from;
const isLong = (e: CalEvent) => daysBetween(day0(e), day1(e)) >= 6;
const spring = { type: "spring", stiffness: 260, damping: 32 } as const;
function groupByQuarter(evs: CalEvent[], q: (d: string) => number): [number, CalEvent[]][] {
  const m = new Map<number, CalEvent[]>();
  for (const e of evs) m.set(q(day0(e)), [...(m.get(q(day0(e))) ?? []), e]);
  return [...m.entries()].sort((a, b) => a[0] - b[0]);
}
const fmtHour = (h: number) => `${((Math.floor(h) + 11) % 12) + 1}${h % 1 ? ":30" : ""} ${h >= 12 ? "PM" : "AM"}`;

function Chip({ e }: { e: CalEvent }) {
  const c = colorFor(e.category);
  return (
    <div title={`${e.title} (${e.start} to ${e.end})`} className="truncate rounded px-1.5 py-0.5 text-[11px] font-medium" style={{ background: c.bg, color: c.fg }}>
      {e.title}
    </div>
  );
}

// ---------- components ----------

export const { registry } = defineRegistry(catalog, {
  components: {
    Stack: ({ props, children }) => (
      <motion.div layout transition={spring} className={`flex ${props.direction === "row" ? "flex-row flex-wrap" : "flex-col"} ${props.gap === "lg" ? "gap-8" : props.gap === "sm" ? "gap-2" : "gap-4"}`}>
        {children}
      </motion.div>
    ),

    Section: ({ props, children }) => (
      <motion.section layout transition={spring} className="flex min-w-0 flex-1 flex-col gap-3">
        {(props.title || props.note) && (
          <div className="flex items-baseline gap-3">
            {props.title && <h2 className="font-display text-sm font-semibold uppercase tracking-wider text-muted">{props.title}</h2>}
            {props.note && <span className="text-xs text-muted">{props.note}</span>}
          </div>
        )}
        {children}
      </motion.section>
    ),

    MonthGrid: ({ props }) => {
      const { events, lid } = useCal("MonthGrid");
      const first = `${props.month}-01`;
      const weeks: string[] = [];
      for (let w = mondayOf(first); w.slice(0, 7) <= props.month; w = addDays(w, 7)) weeks.push(w);
      const [y, m] = props.month.split("-").map(Number);
      return (
        <motion.div layoutId={lid(monthId(first))} transition={spring} className="overflow-hidden rounded-xl border border-line bg-white">
          <div className="border-b border-line px-4 py-3 font-display text-lg font-semibold">{MONTHS[m - 1]} {y}</div>
          <div className="grid grid-cols-7 border-b border-line bg-subtle text-xs font-semibold text-muted">
            {DAY_KEYS.map((d) => <div key={d} className="px-2 py-1.5 capitalize">{d}</div>)}
          </div>
          {weeks.map((w) => (
            <motion.div key={w} layoutId={lid(weekId(w))} transition={spring} className="grid grid-cols-7 border-b border-line last:border-b-0">
              {DAY_KEYS.map((_, i) => {
                const d = addDays(w, i);
                const inMonth = d.slice(0, 7) === props.month;
                const evs = events.filter((e) => on(e, d, d));
                return (
                  <motion.div key={d} layoutId={inMonth ? lid(dayId(d)) : undefined} transition={spring} className="flex min-h-24 min-w-0 flex-col gap-1 border-l border-line p-1.5 first:border-l-0">
                    <span className={`flex h-6 w-6 items-center justify-center rounded-full text-xs font-semibold ${d === TODAY ? "bg-ink text-white" : inMonth ? "" : "text-faint"}`}>{Number(d.slice(8))}</span>
                    {evs.slice(0, 3).map((e) => <Chip key={e.id} e={e} />)}
                    {evs.length > 3 && <span className="px-1 text-[11px] text-muted">+{evs.length - 3} more</span>}
                  </motion.div>
                );
              })}
            </motion.div>
          ))}
        </motion.div>
      );
    },

    WeekGrid: ({ props }) => {
      const { events, lid } = useCal("WeekGrid");
      const mon = mondayOf(props.weekOf);
      const H = 44;
      const hours = Array.from({ length: props.endHour - props.startHour }, (_, i) => props.startHour + i);
      const sun = addDays(mon, 6);
      const allDay = events.filter((e) => e.start.length <= 10 && on(e, mon, sun));
      const hidden = DAY_KEYS.filter((d) => !props.days.includes(d));
      const overflow = events.filter((e) => on(e, mon, sun) && hidden.includes(DAY_KEYS[weekday(day0(e))]));
      const w = isoWeek(mon);
      return (
        <motion.div layoutId={lid(weekId(mon))} transition={spring} className="overflow-hidden rounded-xl border border-line bg-white">
          <div className="flex items-baseline justify-between border-b border-line px-4 py-3">
            <span className="font-display text-lg font-semibold">Week of {MONTHS[Number(mon.slice(5, 7)) - 1]} {Number(mon.slice(8))}</span>
            <span className="text-xs text-muted">Week {w.week}</span>
          </div>
          <div className="flex border-b border-line bg-subtle">
            <div className="w-14 flex-none" />
            {DAY_KEYS.map((d, i) => {
              const show = props.days.includes(d);
              const date = addDays(mon, i);
              return (
                <motion.div key={d} initial={false} animate={{ flexGrow: show ? 1 : 0, opacity: show ? 1 : 0 }} transition={spring}
                  className="flex min-w-0 basis-0 items-baseline gap-1.5 overflow-hidden whitespace-nowrap border-l border-line px-2 py-1.5" style={{ borderLeftWidth: show ? 1 : 0, paddingInline: show ? undefined : 0 }}>
                  <span className="text-xs font-semibold capitalize text-muted">{d}</span>
                  <span className={`font-display text-base font-semibold ${date === TODAY ? "rounded bg-ink px-1 text-white" : ""}`}>{Number(date.slice(8))}</span>
                </motion.div>
              );
            })}
          </div>
          {allDay.length > 0 && (
            <div className="flex flex-wrap gap-1 border-b border-line px-2 py-1.5 pl-16">
              {allDay.map((e) => <Chip key={e.id} e={e} />)}
            </div>
          )}
          <div className="flex">
            <div className="flex w-14 flex-none flex-col">
              {hours.map((h) => <div key={h} style={{ height: H }} className="pr-2 pt-0.5 text-right text-[11px] text-muted">{fmtHour(h)}</div>)}
            </div>
            {DAY_KEYS.map((d, i) => {
              const show = props.days.includes(d);
              const date = addDays(mon, i);
              const evs = events.filter((e) => e.start.length > 10 && day0(e) === date);
              return (
                <motion.div key={d} layoutId={show ? lid(dayId(date)) : undefined} initial={false} animate={{ flexGrow: show ? 1 : 0, opacity: show ? 1 : 0 }} transition={spring}
                  className="relative min-w-0 basis-0 overflow-hidden border-l border-line"
                  style={{ height: hours.length * H, borderLeftWidth: show ? 1 : 0, background: i >= 5 ? "#FAFAFB" : "#fff", backgroundImage: `repeating-linear-gradient(to bottom, #EEF0F3 0, #EEF0F3 1px, transparent 1px, transparent ${H}px)` }}>
                  {evs.map((e) => {
                    const c = colorFor(e.category);
                    const top = (hourOf(e.start) - props.startHour) * H;
                    const height = (hourOf(e.end) - hourOf(e.start)) * H;
                    return (
                      <div key={e.id} className={`absolute inset-x-1 flex overflow-hidden rounded-md px-1.5 ${height > 30 ? "flex-col py-1" : "items-center gap-1.5"}`} style={{ top: top + 2, height: height - 4, background: c.bg, color: c.fg, borderLeft: `3px solid ${c.solid}` }}>
                        <span className="truncate text-xs font-semibold leading-tight">{e.title}</span>
                        <span className="flex-none text-[11px] leading-tight opacity-80">{fmtHour(hourOf(e.start))}</span>
                      </div>
                    );
                  })}
                </motion.div>
              );
            })}
          </div>
          {hidden.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 border-t border-dashed border-line bg-subtle px-4 py-2 text-xs text-muted">
              <span>{hidden.map((d) => d[0].toUpperCase() + d.slice(1)).join(" and ")} hidden.</span>
              {overflow.length ? overflow.map((e) => <Chip key={e.id} e={e} />) : <span>Nothing is scheduled there; anything that lands on those days shows up here.</span>}
            </div>
          )}
        </motion.div>
      );
    },

    WeekStrip: ({ props }) => {
      const { events, lid } = useCal("WeekStrip");
      const n = weeksInIsoYear(props.year);
      const bricks = Array.from({ length: n }, (_, i) => {
        const mon = mondayOfIsoWeek(props.year, i + 1);
        const evs = events.filter((e) => on(e, mon, addDays(mon, 6)));
        const short = evs.filter((e) => !isLong(e) || daysBetween(day0(e), day1(e)) < 21);
        const long = evs.filter((e) => isLong(e));
        const now = mondayOf(TODAY) === mon;
        const focus = short.find((e) => isLong(e)) ?? short[0];
        const bg = now ? "#15171C" : focus ? colorFor(focus.category).solid : long.length ? colorFor(long[long.length - 1].category).bg : "#E9ECF0";
        return { mon, evs, bg, now, i };
      });
      const monthStarts = MONTHS.map((label, m) => {
        const first = `${props.year}-${String(m + 1).padStart(2, "0")}-01`;
        const w = isoWeek(first);
        return { label, col: w.year === props.year ? w.week : 1 };
      });
      return (
        <div className="flex items-start gap-3">
          <div className="flex w-20 flex-none flex-col">
            <span className="font-display text-base font-semibold">{props.year}</span>
            {props.label && <span className="text-xs text-muted">{props.label}</span>}
          </div>
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <div className="grid text-[10px] text-muted" style={{ gridTemplateColumns: `repeat(${n}, minmax(0, 1fr))` }}>
              {monthStarts.map((m) => <span key={m.label} style={{ gridColumnStart: m.col }} className="col-span-4 border-l border-line pl-1">{m.label}</span>)}
            </div>
            <div className="grid gap-[2px]" style={{ gridTemplateColumns: `repeat(${n}, minmax(0, 1fr))` }}>
              {bricks.map((b) => (
                <motion.div key={b.mon} layoutId={lid(weekId(b.mon))} transition={spring} title={`Week ${b.i + 1}${b.evs.length ? ": " + b.evs.map((e) => e.title).join(", ") : ""}`}
                  className="h-7 rounded-[3px]" style={{ background: b.bg }} />
              ))}
            </div>
          </div>
        </div>
      );
    },

    YearBand: ({ props }) => {
      const { events, lid } = useCal("YearBand");
      const years = Array.from({ length: props.to - props.from + 1 }, (_, i) => props.from + i);
      const Q = years.length * 4;
      const qIndex = (d: string) => (Number(d.slice(0, 4)) - props.from) * 4 + Math.floor((Number(d.slice(5, 7)) - 1) / 3);
      const from = `${props.from}-01-01`;
      const to = `${props.to}-12-31`;
      const inRange = events.filter((e) => on(e, from, to));
      const cats = [...new Set(inRange.map((e) => e.category))];
      const nowQ = qIndex(TODAY);
      return (
        <div className="flex flex-col gap-2">
          <div className="flex gap-3">
            <div className="w-28 flex-none" />
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <div className="grid" style={{ gridTemplateColumns: `repeat(${years.length}, minmax(0, 1fr))` }}>
                {years.map((y, i) => (
                  <div key={y} className="flex flex-col border-l border-line-strong pl-1.5">
                    <span className="font-display text-lg font-semibold">{props.labels === "relative" ? `Year ${i + 1}` : y}</span>
                    {props.labels === "relative" && <span className="text-xs text-muted">{y}</span>}
                  </div>
                ))}
              </div>
              <div className="grid" style={{ gridTemplateColumns: `repeat(${Q}, minmax(0, 1fr))` }}>
                {Array.from({ length: Q }, (_, i) => (
                  <div key={i} className={`flex items-center gap-1 px-1 py-0.5 text-[10px] ${i % 4 === 0 ? "border-l border-line-strong" : ""} ${i === nowQ ? "rounded bg-ink font-semibold text-white" : "text-muted"}`}>
                    Q{(i % 4) + 1}
                    {i === nowQ && <motion.span layoutId={lid(monthId(TODAY))} transition={spring} className="rounded-sm bg-white/25 px-1">{MONTHS[Number(TODAY.slice(5, 7)) - 1]}</motion.span>}
                  </div>
                ))}
              </div>
            </div>
          </div>
          {cats.map((cat) => {
            const c = colorFor(cat);
            const bars = inRange.filter((e) => e.category === cat && daysBetween(day0(e), day1(e)) >= 27);
            const dots = inRange.filter((e) => e.category === cat && daysBetween(day0(e), day1(e)) < 27);
            const lanes: number[] = [];
            const placed = bars.map((e) => {
              const s = qIndex(day0(e) < from ? from : day0(e));
              const end = Math.min(Q - 1, qIndex(day1(e) < to ? day1(e) : to));
              let lane = lanes.findIndex((free) => free <= s);
              if (lane < 0) { lane = lanes.length; lanes.push(0); }
              lanes[lane] = end + 1;
              return { e, s, end, lane };
            });
            return (
              <div key={cat} className="flex gap-3">
                <div className="w-28 flex-none pt-2 text-sm font-semibold text-ink-soft">{cat}</div>
                <div className="grid min-w-0 flex-1 gap-y-1.5 border-b border-subtle py-1.5"
                  style={{ gridTemplateColumns: `repeat(${Q}, minmax(0, 1fr))`, gridAutoRows: "30px", backgroundImage: `repeating-linear-gradient(to right, #E2E5EA 0, #E2E5EA 1px, transparent 1px, transparent ${100 / years.length}%)` }}>
                  {placed.map(({ e, s, end, lane }) => (
                    <div key={e.id} title={`${e.title} (${e.start} to ${e.end})`} className="flex min-w-0 items-center overflow-hidden whitespace-nowrap rounded-md px-2 text-xs font-medium"
                      style={{ gridColumn: `${s + 1} / ${end + 2}`, gridRow: lane + 1, background: c.bg, color: c.fg }}>{e.title}</div>
                  ))}
                  {groupByQuarter(dots, qIndex).map(([q, evs], i, all) => (
                    <div key={q} title={evs.map((e) => `${e.title} (${e.start.slice(0, 10)})`).join("\n")} className="flex min-w-0 items-center gap-1 overflow-hidden whitespace-nowrap text-[11px]" style={{ gridColumn: `${q + 1} / span ${Math.max(1, Math.min(4, (all[i + 1]?.[0] ?? Q) - q, Q - q))}`, gridRow: lanes.length + 1, color: c.fg }}>
                      <span className="h-2 w-2 flex-none rounded-full" style={{ background: c.solid }} />
                      <span className="truncate">{evs.length > 1 ? `${evs.length} events` : evs[0].title}</span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      );
    },
  },
});
