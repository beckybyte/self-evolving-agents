import type { CalendarSpec } from "../src/shared/catalog";
import type { DesignRequest, Persona } from "../src/shared/types";
import { mondayOf, TODAY } from "../src/shared/dates";

// Offline stand-in for the model, used when no CRUSOE_API_KEY is set.
// Some first drafts are deliberately flawed so the critic and repair loop have something to catch.

type El = CalendarSpec["elements"][string];
const leaf = (type: string, props: Record<string, unknown>): El => ({ type, props, children: [] });
const one = (el: El): CalendarSpec => ({ root: "view", elements: { view: el } });

const WEEK = mondayOf(TODAY);
const ALL = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const WORK = ["mon", "tue", "wed", "thu", "fri"];

function hybrid(strips: number[], band: [number, number] | null): CalendarSpec {
  const elements: CalendarSpec["elements"] = {};
  const top: string[] = [];
  if (band) {
    elements.years = { type: "Section", props: { title: "Years", note: "Company years, by quarter" }, children: ["band"] };
    elements.band = leaf("YearBand", { from: band[0], to: band[1], labels: "relative" });
    top.push("years");
  }
  elements.weeks = { type: "Section", props: { title: "52 weeks", note: "Where two-week blocks fit" }, children: strips.map((y) => `strip-${y}`) };
  for (const y of strips) elements[`strip-${y}`] = leaf("WeekStrip", { year: y, label: `Year ${y - 2025}` });
  top.push("weeks");
  elements.root = { type: "Stack", props: { direction: "column", gap: "lg" }, children: top };
  return { root: "root", elements };
}

export function mockDraft(p: Persona, req: DesignRequest, n: number, round: number): unknown {
  const types = Object.values(req.currentSpec.elements).map((e) => e.type);
  const week = Object.values(req.currentSpec.elements).find((e) => e.type === "WeekGrid");
  const fix = round > 0;
  const say = (title: string, reason: string, spec: CalendarSpec) => ({ proposal: { title, reason, spec } });

  if (p.id === "mina") {
    if (types.includes("YearBand")) return { proposal: null, why: "The years view already matches how Mina plans." };
    const reason = "Your plans run from 2026 to 2030 and the median one lasts 14 months, but you haven't opened a week or day in 3 weeks. A years view shows all of them at once.";
    if (n === 1 && !fix) return say("Show this year as weeks?", reason, one(leaf("WeekStrip", { year: 2026, label: null })));
    if (n === 2 && !fix) return say("Show your plans by year?", reason, one(leaf("YearBand", { from: 2026, to: 2028, labels: "calendar" })));
    return say("Show your plans by year?", reason, one(leaf("YearBand", { from: 2026, to: 2030, labels: "calendar" })));
  }

  if (p.id === "jordan") {
    const days = (week?.props.days as string[] | undefined) ?? [];
    if (!week) {
      const reason = "You chose the week view on 9 of your last 10 visits, and nearly all your events are 30 to 90 minute meetings.";
      const hours = n === 1 && !fix ? [10, 17] : [9, 17];
      return say("Make the week your default view?", reason, one(leaf("WeekGrid", { weekOf: WEEK, days: ALL, startHour: hours[0], endHour: hours[1] })));
    }
    if (days.length > 5) {
      const reason = "You haven't scheduled anything on a Saturday or Sunday in 14 weeks. If something does land on a weekend, it will still show up.";
      const hours = n === 1 && !fix ? [9, 16] : [9, 17];
      return say("Hide weekends?", reason, one(leaf("WeekGrid", { weekOf: WEEK, days: WORK, startHour: hours[0], endHour: hours[1] })));
    }
    return { proposal: null, why: "The work week view already matches Jordan's habits." };
  }

  if (types.includes("WeekStrip")) return { proposal: null, why: "The years and weeks view already fits how Sam plans." };
  const reason = "You plan in startup years (\"year 2 to year 4\") and in weeks (\"two weeks in May\"). No standard view shows both, so this one stacks years over 52-week strips.";
  if (n === 1 && !fix) return say("Try a view designed for you?", reason, hybrid([2027, 2028], null));
  if (n === 2 && !fix) return say("Try a view designed for you?", reason, hybrid([2026, 2027, 2028], [2027, 2029]));
  return say("Try a view designed for you?", reason, hybrid([2026, 2027, 2028], [2026, 2030]));
}
