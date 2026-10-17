import { generateText } from "ai";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import { catalog, type CalendarSpec } from "../src/shared/catalog";
import { critique, mustShow, sharedPeriods } from "../src/shared/critic";
import { daysBetween, isoWeek, mondayOf, TODAY } from "../src/shared/dates";
import type { DesignRequest, DesignResponse, Persona, TraceStep } from "../src/shared/types";
import { mockDraft } from "./mock";
import { MODELS } from "./models";

const DRAFTS = Number(process.env.DESIGNER_DRAFTS ?? 3);
const REPAIRS = Number(process.env.DESIGNER_REPAIRS ?? 2);
const MODEL = MODELS.designer;

const crusoe = process.env.CRUSOE_API_KEY
  ? createOpenAICompatible({
      name: "crusoe",
      baseURL: process.env.CRUSOE_BASE_URL ?? "https://api.inference.crusoecloud.com/v1",
      apiKey: process.env.CRUSOE_API_KEY,
    })
  : null;

// The component list comes straight from the catalog, so the prompt never drifts from what the renderer supports.
function componentDocs(): string {
  const full = catalog.prompt();
  const start = full.indexOf("AVAILABLE COMPONENTS");
  const end = full.indexOf("AVAILABLE ACTIONS");
  return full.slice(start, end).trim();
}

export const RULES = [
  "Always offer a UI change as a suggestion. Never adopt it yourself: the user turns it on, and can turn it off again with one click, like a feature switch.",
  "Propose one change at a time, the smallest step that fits the habits. Never redesign everything at once.",
  "Animate layout transitions: keep time periods that are on screen now (the same day, week, month or year) on screen in the new view, so the app can morph one into the other. Prefer zooming in or out over replacing.",
  "Never hide an event. Everything this week, and every plan a week or longer that is still ahead, must stay visible somewhere.",
  "Base every change on the learned habits and say which habit, with its number, in the reason.",
  "Do not propose again anything the user dismissed or undid.",
  "If the current view already fits the habits, propose nothing.",
  "Keep views small: 14 elements or fewer.",
];

export function systemPrompt(): string {
  return [
    "You are the designer agent of Evolve, a calendar app whose layout adapts to each user.",
    "Another agent watches how the user plans and writes down what it has learned. You read that, look at the current view, and suggest a better view built from the components below.",
    "",
    componentDocs(),
    "",
    "A spec is a flat element map: {\"root\": \"<key>\", \"elements\": {\"<key>\": {\"type\": \"<Component>\", \"props\": {...}, \"children\": [\"<key>\", ...]}}}. Every element needs a children array; leaf views use []. Nullable props may be null.",
    "",
    "RULES:",
    ...RULES.map((r, i) => `${i + 1}. ${r}`),
    "",
    "OUTPUT: one JSON object and nothing else, no code fences:",
    '{"proposal": {"title": "<short question, e.g. Hide weekends?>", "reason": "<one or two sentences to the user, citing the habit>", "spec": <spec>}}',
    'or, when no change is worth suggesting: {"proposal": null, "why": "<one sentence>"}',
  ].join("\n");
}

function userPrompt(p: Persona, req: DesignRequest, issues?: string[], previous?: string): string {
  const evs = p.events;
  const long = evs.filter((e) => daysBetween(e.start, e.end) >= 6);
  const timed = evs.filter((e) => e.start.length > 10);
  const weekend = evs.filter((e) => [5, 6].includes((new Date(e.start.slice(0, 10)).getUTCDay() + 6) % 7));
  const sample = [...mustShow(evs)].slice(0, 25).map((e) => `- ${e.title}: ${e.start} to ${e.end} [${e.category}]`);
  const lines = [
    `Today is ${TODAY} (ISO week ${isoWeek(TODAY).week}, week starts Monday ${mondayOf(TODAY)}).`,
    "",
    `WHAT THE HABIT AGENT HAS LEARNED ABOUT ${p.name.toUpperCase()} (confidence ${p.profile.confidence}):`,
    ...p.profile.facts.map((f) => `- ${f}`),
    `Signals: ${JSON.stringify(p.profile.signals)}`,
    "",
    `EVENTS: ${evs.length} total, ${long.length} lasting a week or more, ${timed.length} timed, ${weekend.length} on weekends.`,
    `Range: ${evs.map((e) => e.start).sort()[0].slice(0, 10)} to ${evs.map((e) => e.end).sort().at(-1)!.slice(0, 10)}.`,
    "Events that must stay visible:",
    ...sample,
    "",
    "CURRENT VIEW:",
    JSON.stringify(req.currentSpec),
    "",
    "PAST SUGGESTIONS:",
    ...(req.history.length ? req.history.map((h) => `- "${h.title}": ${h.status}`) : ["- none"]),
  ];
  if (issues && previous) {
    lines.push("", "YOUR LAST DRAFT FAILED THESE CHECKS. Fix every one and output the corrected JSON:", ...issues.map((i) => `- ${i}`), "", "Last draft:", previous);
  }
  return lines.join("\n");
}

function parseJson(text: string): unknown {
  const cleaned = text.replace(/```(?:json)?/g, "").trim();
  const start = cleaned.indexOf("{");
  const end = cleaned.lastIndexOf("}");
  if (start < 0 || end < start) throw new Error("no JSON object in the reply");
  return JSON.parse(cleaned.slice(start, end + 1));
}

async function callModel(system: string, prompt: string, temperature: number): Promise<string> {
  for (let attempt = 0; ; attempt++) {
    try {
      const { text } = await generateText({ model: crusoe!(MODEL), system, prompt, temperature, maxOutputTokens: 6000 });
      return text;
    } catch (err) {
      const status = (err as { statusCode?: number }).statusCode;
      if (attempt < 3 && (status === 429 || status === 503)) {
        await new Promise((r) => setTimeout(r, 1000 * 2 ** attempt + Math.random() * 500));
        continue;
      }
      throw err;
    }
  }
}

// How many of the user's time scales the view shows. Ties go to views that share more with the current one.
const SCALES: Record<string, string[]> = { YearBand: ["year", "quarter"], WeekStrip: ["week", "year"], WeekGrid: ["week", "day"], MonthGrid: ["month", "day"] };
function fit(p: Persona, spec: CalendarSpec): number {
  const wanted = (p.profile.signals.timeScales as string[] | undefined) ?? [];
  const shown = new Set(Object.values(spec.elements).flatMap((e) => SCALES[e.type] ?? []));
  return wanted.filter((s) => shown.has(s)).length;
}

type Draft = { title: string; reason: string; spec: CalendarSpec; score: number } | { none: string } | null;

// One designer sub-agent: draft, run the critic, repair until it passes or runs out of tries.
async function runDraft(n: number, p: Persona, req: DesignRequest, trace: TraceStep[]): Promise<Draft> {
  const role = `Designer ${n + 1}`;
  let issues: string[] | undefined;
  let previous: string | undefined;
  for (let round = 0; round <= REPAIRS; round++) {
    let raw: unknown;
    try {
      raw = crusoe
        ? parseJson(await callModel(systemPrompt(), userPrompt(p, req, issues, previous), 0.4 + n * 0.25))
        : mockDraft(p, req, n, round);
    } catch (err) {
      trace.push({ role, text: `Draft unreadable: ${(err as Error).message}` });
      issues = ["The reply was not a single valid JSON object."];
      previous = "(unreadable)";
      continue;
    }
    const out = raw as { proposal?: { title?: string; reason?: string; spec?: unknown } | null; why?: string };
    if (out.proposal === null) {
      trace.push({ role, text: `No change needed: ${out.why ?? "the view already fits"}` });
      return { none: out.why ?? "" };
    }
    const prop = out.proposal ?? {};
    const verdict = critique(prop.spec, p.events, req.currentSpec);
    const label = prop.title ? `"${prop.title}"` : "draft";
    if (verdict.ok && prop.title && prop.reason) {
      const shared = sharedPeriods(req.currentSpec, verdict.spec!);
      trace.push({ role: "Critic", text: `${role}'s ${label} passed${round ? ` after ${round} repair${round > 1 ? "s" : ""}` : ""}. Shares ${shared} week${shared === 1 ? "" : "s"} with the current view.` });
      return { title: prop.title, reason: prop.reason, spec: verdict.spec!, score: 10 * fit(p, verdict.spec!) + shared / 10 - Object.keys(verdict.spec!.elements).length * 0.2 };
    }
    issues = verdict.issues.length ? verdict.issues : ["Missing title or reason."];
    previous = JSON.stringify(out.proposal);
    trace.push({ role: "Critic", text: `Rejected ${role}'s ${label}: ${issues[0]}${issues.length > 1 ? ` (+${issues.length - 1} more)` : ""}` });
  }
  return null;
}

export async function design(p: Persona, req: DesignRequest): Promise<DesignResponse> {
  const trace: TraceStep[] = [
    { role: "Designer", text: `Read ${p.profile.facts.length} learned habits (confidence ${p.profile.confidence}). Spawning ${DRAFTS} draft designers${crusoe ? ` on ${MODEL}` : " (offline mock: no CRUSOE_API_KEY)"}.` },
  ];
  const drafts = await Promise.all(Array.from({ length: DRAFTS }, (_, n) => runDraft(n, p, req, trace)));
  const good = drafts.filter((d): d is Extract<Draft, { spec: CalendarSpec }> => !!d && "spec" in d);
  if (!good.length) {
    const none = drafts.find((d) => d && "none" in d);
    trace.push({ role: "Designer", text: none ? "Nothing to suggest right now." : "No draft passed the checks. Keeping the current view." });
    return { mode: crusoe ? "live" : "mock", proposal: null, trace };
  }
  const best = good.sort((a, b) => b.score - a.score)[0];
  trace.push({ role: "Designer", text: `Picked "${best.title}" out of ${good.length} passing draft${good.length > 1 ? "s" : ""}. Offering it as a suggestion.` });
  return {
    mode: crusoe ? "live" : "mock",
    proposal: { id: `p${Date.now()}`, title: best.title, reason: best.reason, spec: best.spec },
    trace,
  };
}
