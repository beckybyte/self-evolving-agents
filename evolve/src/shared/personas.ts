import type { CalEvent, Persona } from "./types";
import { addDays, weekday } from "./dates";

// Sample users. Each profile stands in for what the habit agent would derive from usage.

function jordanEvents(): CalEvent[] {
  const out: CalEvent[] = [];
  const extras: Record<number, [number, number, string][]> = {
    0: [[11, 1, "Design review"], [14, 0.5, "1:1 with Priya"]],
    1: [[10.5, 1.5, "Sprint planning"], [15, 1, "Customer call"]],
    2: [[13, 2, "Hiring loop"]],
    3: [[11, 1, "Roadmap sync"], [16, 1, "Interview"]],
    4: [[12, 1, "Team lunch"], [15, 1, "Demo day"]],
  };
  const t = (day: string, h: number) => `${day}T${String(Math.floor(h)).padStart(2, "0")}:${h % 1 ? "30" : "00"}`;
  let day = "2026-07-13";
  let n = 0;
  while (day <= "2026-11-08") {
    const wd = weekday(day);
    if (wd < 5) {
      out.push({ id: `j${n++}`, title: "Standup", start: t(day, 9.5), end: t(day, 10), category: "Meetings" });
      for (const [h, d, title] of extras[wd]) out.push({ id: `j${n++}`, title, start: t(day, h), end: t(day, h + d), category: "Meetings" });
    }
    day = addDays(day, 1);
  }
  return out;
}

export const PERSONAS: Persona[] = [
  {
    id: "mina",
    name: "Mina",
    role: "Life planner",
    profile: {
      facts: [
        "Plans span years: median event length is 14 months",
        "Has not opened a week or day view in 3 weeks",
        "Opens the app about twice a month",
        "Events are milestones, not meetings",
      ],
      signals: { timeScales: ["year", "quarter"], horizonYears: 5, weekendEvents: true, meetingsPerWeek: 0 },
      confidence: 0.86,
    },
    events: [
      { id: "m1", title: "H-1B status", start: "2026-01-01", end: "2027-09-30", category: "Immigration" },
      { id: "m2", title: "Green card: I-485 pending", start: "2026-10-01", end: "2028-06-30", category: "Immigration" },
      { id: "m3", title: "Naturalization window", start: "2030-04-01", end: "2030-12-31", category: "Immigration" },
      { id: "m4", title: "I-485 biometrics", start: "2026-10-20", end: "2026-10-20", category: "Immigration" },
      { id: "m5", title: "Passport renewal mailed", start: "2026-10-02", end: "2026-10-02", category: "Immigration" },
      { id: "m6", title: "Try for a baby", start: "2027-01-01", end: "2027-12-31", category: "Family" },
      { id: "m7", title: "Parental leave", start: "2028-01-01", end: "2028-06-30", category: "Family" },
      { id: "m8", title: "Save for down payment", start: "2026-01-01", end: "2028-12-31", category: "Home" },
      { id: "m9", title: "Buy a house", start: "2029-01-01", end: "2029-06-30", category: "Home" },
      { id: "m10", title: "Call immigration lawyer", start: "2026-10-28", end: "2026-10-28", category: "Immigration" },
    ],
  },
  {
    id: "jordan",
    name: "Jordan",
    role: "Work weeks",
    profile: {
      facts: [
        "Mostly meetings of 30 to 90 minutes",
        "Works 9 AM to 5 PM",
        "No weekend events in 14 weeks",
        "Chose the week view 9 of the last 10 visits",
      ],
      signals: { timeScales: ["week", "day"], horizonYears: 0, weekendEvents: false, meetingsPerWeek: 15, workHours: "9-17" },
      confidence: 0.93,
    },
    events: jordanEvents(),
  },
  {
    id: "sam",
    name: "Sam",
    role: "Founder",
    profile: {
      facts: [
        "Talks in startup years: \"from year 2 to year 4, release a new product\"",
        "Also plans week-long trips: \"two weeks in May, this year or next, Hawaii\"",
        "Mixes multi-year goals with 2-week blocks",
        "Company year 1 is 2026",
      ],
      signals: { timeScales: ["year", "week"], horizonYears: 5, companyYear1: 2026, weekendEvents: true },
      confidence: 0.78,
    },
    events: [
      { id: "s1", title: "Build v1", start: "2026-01-01", end: "2026-12-31", category: "Company" },
      { id: "s2", title: "Series A raise", start: "2027-01-01", end: "2027-06-30", category: "Company" },
      { id: "s3", title: "New product: build and release", start: "2027-01-01", end: "2029-12-31", category: "Product" },
      { id: "s4", title: "Hawaii, option A", start: "2027-05-10", end: "2027-05-23", category: "Personal" },
      { id: "s5", title: "Hawaii, option B", start: "2028-05-08", end: "2028-05-21", category: "Personal" },
      { id: "s6", title: "Board update", start: "2026-10-08T10:00", end: "2026-10-08T11:00", category: "Company" },
      { id: "s7", title: "Investor coffee", start: "2026-10-14T09:00", end: "2026-10-14T10:00", category: "Company" },
      { id: "s8", title: "Series A partner meeting", start: "2026-10-15T10:00", end: "2026-10-15T11:30", category: "Company" },
      { id: "s9", title: "Look up Hawaii flights", start: "2026-10-17T11:00", end: "2026-10-17T12:00", category: "Personal" },
      { id: "s10", title: "Product beta", start: "2027-10-04", end: "2027-10-08", category: "Product" },
      { id: "s11", title: "Launch", start: "2028-09-04", end: "2028-09-08", category: "Product" },
    ],
  },
];

export const personaById = (id: string) => PERSONAS.find((p) => p.id === id);
