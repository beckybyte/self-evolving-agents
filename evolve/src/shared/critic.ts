import { catalog, type CalendarSpec } from "./catalog";
import type { CalEvent } from "./types";
import { addDays, daysBetween, hourOf, isoWeek, mondayOf, TODAY, weeksInIsoYear, mondayOfIsoWeek } from "./dates";

// Mechanical checks the designer's drafts must pass. No LLM involved, so results are repeatable.

type El = CalendarSpec["elements"][string];

const evStart = (e: CalEvent) => e.start.slice(0, 10);
const evEnd = (e: CalEvent) => e.end.slice(0, 10);
const overlaps = (e: CalEvent, from: string, to: string) => evStart(e) <= to && evEnd(e) >= from;

// Date range a component shows. Events overlapping it count as visible.
export function rangeOf(el: El): [string, string] | null {
  const p = el.props as Record<string, unknown>;
  switch (el.type) {
    case "MonthGrid": {
      const [y, m] = String(p.month).split("-").map(Number);
      const next = m === 12 ? `${y + 1}-01-01` : `${y}-${String(m + 1).padStart(2, "0")}-01`;
      return [`${String(p.month)}-01`, addDays(next, -1)];
    }
    case "WeekGrid": {
      const mon = mondayOf(String(p.weekOf));
      return [mon, addDays(mon, 6)];
    }
    case "WeekStrip": {
      const y = Number(p.year);
      return [mondayOfIsoWeek(y, 1), addDays(mondayOfIsoWeek(y, weeksInIsoYear(y)), 6)];
    }
    case "YearBand":
      return [`${p.from}-01-01`, `${p.to}-12-31`];
    default:
      return null;
  }
}

// Events the user must always be able to see: everything this week, and every plan of a week or longer still ahead.
export function mustShow(events: CalEvent[]): CalEvent[] {
  const mon = mondayOf(TODAY);
  const sun = addDays(mon, 6);
  return events.filter((e) => overlaps(e, mon, sun) || (daysBetween(evStart(e), evEnd(e)) >= 6 && evEnd(e) >= TODAY));
}

export function critique(spec: unknown, events: CalEvent[], current?: CalendarSpec): { ok: boolean; issues: string[]; spec?: CalendarSpec } {
  const issues: string[] = [];
  const parsed = catalog.validate(spec);
  if (!parsed.success) {
    const msg = String((parsed as { error?: { message?: string } }).error?.message ?? "invalid spec");
    return { ok: false, issues: [`Spec does not match the catalog: ${msg.replace(/\s+/g, " ").slice(0, 400)}`] };
  }
  const s = parsed.data as CalendarSpec;
  if (!s.elements[s.root]) issues.push(`Root "${s.root}" is not defined in elements.`);
  const seen = new Set<string>();
  const walk = (key: string, depth: number) => {
    if (seen.has(key)) return issues.push(`Element "${key}" appears twice in the tree.`);
    seen.add(key);
    const el = s.elements[key];
    if (!el) return issues.push(`Child "${key}" is referenced but not defined.`);
    if (depth > 6) return issues.push("Tree is nested too deeply.");
    for (const c of el.children ?? []) walk(c, depth + 1);
  };
  if (s.elements[s.root]) walk(s.root, 0);
  const els = Object.values(s.elements);
  if (els.length > 14) issues.push(`Too many elements (${els.length}); keep views to 14 or fewer.`);

  const views = els.filter((e) => rangeOf(e));
  if (!views.length) issues.push("No calendar view in the spec (MonthGrid, WeekGrid, WeekStrip or YearBand).");

  for (const e of mustShow(events)) {
    const covered = views.some((v) => {
      const r = rangeOf(v)!;
      return overlaps(e, r[0], r[1]);
    });
    if (!covered) issues.push(`"${e.title}" (${evStart(e)} to ${evEnd(e)}) is not visible in any view.`);
  }

  for (const v of views.filter((v) => v.type === "WeekGrid")) {
    const p = v.props as { startHour: number; endHour: number };
    if (p.endHour <= p.startHour) issues.push("WeekGrid endHour must be after startHour.");
    const r = rangeOf(v)!;
    const outside = events.filter((e) => e.start.length > 10 && overlaps(e, r[0], r[1]) && (hourOf(e.start) < p.startHour || hourOf(e.end) > p.endHour));
    if (outside.length) issues.push(`WeekGrid hours ${p.startHour}-${p.endHour} cut off ${outside.length} event(s), e.g. "${outside[0].title}" at ${outside[0].start.slice(11)}.`);
  }

  const wk = isoWeek(TODAY);
  if (views.length && !views.some((v) => { const r = rangeOf(v)!; return r[0] <= TODAY && r[1] >= TODAY; }))
    issues.push(`No view includes today (${TODAY}, week ${wk.week}).`);

  if (current && JSON.stringify(normalize(s)) === JSON.stringify(normalize(current))) issues.push("The proposal is the same as the current view.");

  return { ok: issues.length === 0, issues, spec: s };
}

// Order-independent shape used to compare specs.
function normalize(s: CalendarSpec) {
  return Object.values(s.elements).map((e) => e.type + JSON.stringify(e.props)).sort();
}

// How many time periods two views have in common. Shared periods are what the app can animate between.
export function sharedPeriods(a: CalendarSpec, b: CalendarSpec): number {
  const weeks = (s: CalendarSpec) => {
    const set = new Set<string>();
    for (const e of Object.values(s.elements)) {
      const r = rangeOf(e);
      if (!r) continue;
      for (let d = mondayOf(r[0]); d <= r[1] && set.size < 2000; d = addDays(d, 7)) {
        const w = isoWeek(d);
        set.add(`${w.year}-${w.week}`);
      }
    }
    return set;
  };
  const wa = weeks(a);
  let n = 0;
  for (const w of weeks(b)) if (wa.has(w)) n++;
  return n;
}
