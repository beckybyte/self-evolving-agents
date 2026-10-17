import type { CalendarSpec } from "./catalog";

export type CalEvent = {
  id: string;
  title: string;
  start: string; // YYYY-MM-DD or YYYY-MM-DDTHH:mm
  end: string; // inclusive day for all-day events, end time for timed ones
  category: string;
};

// Output of the habit agent ("What I've learned about X"). The designer reads this.
export type Profile = {
  facts: string[];
  signals: Record<string, string | number | boolean | string[]>;
  confidence: number;
};

export type Persona = {
  id: string;
  name: string;
  role: string;
  profile: Profile;
  events: CalEvent[];
};

export type Proposal = {
  id: string;
  title: string;
  reason: string;
  spec: CalendarSpec;
  status: "suggested" | "accepted" | "dismissed" | "undone";
};

export type TraceStep = { role: string; text: string };

export type DesignRequest = {
  personaId: string;
  currentSpec: CalendarSpec;
  history: { title: string; status: Proposal["status"] }[];
};

export type DesignResponse = {
  mode: "live" | "mock";
  proposal: Omit<Proposal, "status"> | null;
  trace: TraceStep[];
};
