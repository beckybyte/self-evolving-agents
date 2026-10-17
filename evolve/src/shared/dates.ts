// Plain date helpers. Dates are "YYYY-MM-DD"; times are "YYYY-MM-DDTHH:mm" (local, no zone).

export const TODAY = "2026-10-17";

export const DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
export type DayKey = (typeof DAY_KEYS)[number];

export function parseDay(s: string): Date {
  const [y, m, d] = s.slice(0, 10).split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

export function fmtDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export function addDays(s: string, n: number): string {
  const d = parseDay(s);
  d.setUTCDate(d.getUTCDate() + n);
  return fmtDay(d);
}

export function daysBetween(a: string, b: string): number {
  return Math.round((parseDay(b).getTime() - parseDay(a).getTime()) / 86400000);
}

// 0 = Monday ... 6 = Sunday
export function weekday(s: string): number {
  return (parseDay(s).getUTCDay() + 6) % 7;
}

export function mondayOf(s: string): string {
  return addDays(s, -weekday(s));
}

export function isoWeek(s: string): { year: number; week: number } {
  const d = parseDay(s);
  const thursday = new Date(d);
  thursday.setUTCDate(d.getUTCDate() + 3 - ((d.getUTCDay() + 6) % 7));
  const year = thursday.getUTCFullYear();
  const yearStart = Date.UTC(year, 0, 1);
  const week = Math.ceil(((thursday.getTime() - yearStart) / 86400000 + 1) / 7);
  return { year, week };
}

export function weeksInIsoYear(year: number): number {
  return isoWeek(`${year}-12-28`).week;
}

export function mondayOfIsoWeek(year: number, week: number): string {
  const jan4 = `${year}-01-04`;
  return addDays(mondayOf(jan4), (week - 1) * 7);
}

export function hourOf(s: string): number {
  if (s.length <= 10) return 0;
  const [h, m] = s.slice(11, 16).split(":").map(Number);
  return h + m / 60;
}

export const weekId = (s: string) => {
  const w = isoWeek(s);
  return `week-${w.year}-${String(w.week).padStart(2, "0")}`;
};
export const monthId = (s: string) => `month-${s.slice(0, 7)}`;
export const dayId = (s: string) => `day-${s.slice(0, 10)}`;

export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
