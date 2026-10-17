import { defineCatalog } from "@json-render/core";
import { schema } from "@json-render/react/schema";
import { z } from "zod";

// The building blocks the designer agent may compose into a calendar view.
// Components read events from the app; specs only say *how* to show them.
export const catalog = defineCatalog(schema, {
  components: {
    Stack: {
      props: z.object({
        direction: z.enum(["row", "column"]),
        gap: z.enum(["sm", "md", "lg"]).nullable(),
      }),
      description: "Lays out its children in a row or column.",
    },
    Section: {
      props: z.object({
        title: z.string().nullable(),
        note: z.string().nullable(),
      }),
      description: "A titled group of views with an optional one-line note.",
    },
    MonthGrid: {
      props: z.object({
        month: z.string().describe("YYYY-MM"),
      }),
      description: "Classic month grid, one row per week, Monday to Sunday. Shows every event in that month as a chip.",
    },
    WeekGrid: {
      props: z.object({
        weekOf: z.string().describe("YYYY-MM-DD of the Monday"),
        days: z.array(z.enum(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])).min(1),
        startHour: z.number().int().min(0).max(23),
        endHour: z.number().int().min(1).max(24),
      }),
      description:
        "Hour-by-hour grid for one week with one column per listed day. Days left out are collapsed; any event on a collapsed day still appears in an overflow row.",
    },
    WeekStrip: {
      props: z.object({
        year: z.number().int(),
        label: z.string().nullable(),
      }),
      description: "One row of 52 small bricks, one per ISO week of the year. Bricks are tinted where events fall; the current week is dark.",
    },
    YearBand: {
      props: z.object({
        from: z.number().int().describe("first calendar year"),
        to: z.number().int().describe("last calendar year, inclusive"),
        labels: z.enum(["calendar", "relative"]).describe("calendar: 2026, 2027; relative: Year 1, Year 2"),
      }),
      description:
        "Multi-year timeline split into quarters. Events lasting a month or more become bars on rows grouped by category; shorter events become dots.",
    },
  },
  actions: {},
});

export type CalendarSpec = {
  root: string;
  elements: Record<string, { type: string; props: Record<string, unknown>; children?: string[] }>;
};
