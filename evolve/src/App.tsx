import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { LayoutGroup } from "motion/react";
import { JSONUIProvider, Renderer } from "@json-render/react";
import { registry, CalendarContext, owners } from "./registry";
import type { CalendarSpec } from "./shared/catalog";
import { PERSONAS } from "./shared/personas";
import type { DesignResponse, Proposal, TraceStep } from "./shared/types";
import { TODAY } from "./shared/dates";

// Everyone starts on a plain month view; the designer agent suggests changes from there.
const DAY_ONE: CalendarSpec = { root: "view", elements: { view: { type: "MonthGrid", props: { month: TODAY.slice(0, 7) }, children: [] } } };

type Accepted = Proposal & { previous: CalendarSpec };
type UserState = {
  spec: CalendarSpec;
  history: Accepted[]; // every suggestion and what the user did with it
  pending: (Omit<Proposal, "status">) | null;
  trace: TraceStep[];
  loading: boolean;
  mode?: DesignResponse["mode"];
  dismissedNote?: string;
};

const fresh = (): UserState => ({ spec: DAY_ONE, history: [], pending: null, trace: [], loading: false });

export default function App() {
  const [personaId, setPersonaId] = useState(PERSONAS[0].id);
  const [users, setUsers] = useState<Record<string, UserState>>(() => Object.fromEntries(PERSONAS.map((p) => [p.id, fresh()])));
  const persona = PERSONAS.find((p) => p.id === personaId)!;
  const u = users[personaId];
  const asked = useRef(new Set<string>());

  const patch = useCallback((id: string, f: (s: UserState) => Partial<UserState>) => setUsers((all) => ({ ...all, [id]: { ...all[id], ...f(all[id]) } })), []);

  const runDesigner = useCallback(
    async (id: string, spec: CalendarSpec, history: Accepted[]) => {
      patch(id, () => ({ loading: true, pending: null, dismissedNote: undefined, trace: [{ role: "Designer", text: "Working on a suggestion..." }] }));
      try {
        const res = await fetch("/api/design", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ personaId: id, currentSpec: spec, history: history.map((h) => ({ title: h.title, status: h.status })) }),
        });
        const data = (await res.json()) as DesignResponse & { error?: string };
        if (!res.ok) throw new Error(data.error ?? res.statusText);
        patch(id, () => ({ loading: false, pending: data.proposal, trace: data.trace, mode: data.mode }));
      } catch (err) {
        patch(id, () => ({ loading: false, trace: [{ role: "Designer", text: `Could not reach the designer agent: ${(err as Error).message}` }] }));
      }
    },
    [patch],
  );

  // Ask once per user when they first open the app.
  useEffect(() => {
    if (asked.current.has(personaId)) return;
    asked.current.add(personaId);
    runDesigner(personaId, users[personaId].spec, users[personaId].history);
  }, [personaId, users, runDesigner]);

  // The suggestion is a switch: turning it on swaps the view, and Undo turns it back off.
  const accept = () => {
    if (!u.pending) return;
    const rec: Accepted = { ...u.pending, status: "accepted", previous: u.spec };
    const history = [...u.history, rec];
    patch(personaId, () => ({ spec: rec.spec, history, pending: null }));
    runDesigner(personaId, rec.spec, history);
  };
  const dismiss = () => {
    if (!u.pending) return;
    const rec: Accepted = { ...u.pending, status: "dismissed", previous: u.spec };
    patch(personaId, (s) => ({ history: [...s.history, rec], pending: null, dismissedNote: "Okay, keeping this view. The designer won't suggest that again." }));
  };
  const undo = () => {
    const last = [...u.history].reverse().find((h) => h.status === "accepted");
    if (!last) return;
    patch(personaId, (s) => ({ spec: last.previous, pending: null, history: s.history.map((h) => (h === last ? { ...h, status: "undone" } : h)) }));
  };
  const reset = () => {
    asked.current.delete(personaId);
    setUsers((all) => ({ ...all, [personaId]: fresh() }));
  };

  const lastAccepted = [...u.history].reverse().find((h) => h.status === "accepted");
  const ctx = useMemo(() => ({ events: persona.events, owner: owners(u.spec) }), [persona, u.spec]);

  return (
    <div className="min-h-screen px-4 pb-12 pt-6 sm:px-6">
      <div className="mx-auto flex max-w-[1400px] flex-col gap-5">
        <header className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <div className="font-display text-2xl font-bold tracking-tight">Evolve</div>
            <div className="text-sm text-muted">A calendar that redesigns itself around how you plan</div>
          </div>
          <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Choose a user">
            {PERSONAS.map((p) => (
              <button key={p.id} type="button" aria-pressed={p.id === personaId} onClick={() => setPersonaId(p.id)}
                className={`flex min-h-11 flex-col items-start rounded-xl border px-3.5 py-1.5 text-left text-sm leading-tight ${p.id === personaId ? "border-ink bg-ink text-white" : "border-line-strong bg-white"}`}>
                <span className="font-semibold">{p.name}</span>
                <span className={`text-xs ${p.id === personaId ? "text-line-strong" : "text-muted"}`}>{p.role}</span>
              </button>
            ))}
            <button type="button" onClick={reset} className="min-h-11 rounded-xl border border-line-strong bg-white px-3.5 text-sm">Start over</button>
          </div>
        </header>

        <div className="flex flex-wrap items-start gap-5">
          <main className="min-w-0 flex-[999_1_640px] rounded-2xl border border-line bg-white p-5">
            <CalendarContext.Provider value={ctx}>
              <JSONUIProvider registry={registry}>
                <LayoutGroup id={personaId}>
                  <Renderer key={personaId} spec={u.spec} registry={registry} />
                </LayoutGroup>
              </JSONUIProvider>
            </CalendarContext.Provider>
          </main>

          <aside className="flex min-w-0 flex-[1_1_340px] flex-col gap-4">
            {u.pending && (
              <section aria-label="Suggestion" className="flex flex-col gap-2.5 rounded-2xl border border-[#F0CFA5] bg-[#FDF1E3] p-4">
                <div className="text-xs font-semibold uppercase tracking-wider text-[#8A4305]">Suggestion</div>
                <h2 className="m-0 font-display text-xl font-semibold">{u.pending.title}</h2>
                <p className="m-0 leading-relaxed text-[#3C2A17]">{u.pending.reason}</p>
                <div className="flex flex-wrap gap-2 pt-1">
                  <button type="button" onClick={accept} className="min-h-11 rounded-xl bg-ink px-4 font-semibold text-white">Try it</button>
                  <button type="button" onClick={dismiss} className="min-h-11 rounded-xl border border-[#D9B48A] px-4 font-medium">Not now</button>
                </div>
                <div className="text-xs text-[#6B4A26]">Nothing changes until you say so, and you can undo in one click.</div>
              </section>
            )}

            {u.dismissedNote && <section className="rounded-2xl border border-line bg-white p-4 text-sm">{u.dismissedNote}</section>}

            {lastAccepted && (
              <section aria-label="What changed" className="flex flex-col gap-2 rounded-2xl border border-[#A9D8CF] bg-[#E3F3F0] p-4">
                <div className="text-xs font-semibold uppercase tracking-wider text-[#0A5C53]">On</div>
                <div className="font-display text-lg font-semibold">{lastAccepted.title.replace(/\?$/, "")}</div>
                <button type="button" onClick={undo} className="min-h-11 self-start rounded-xl border border-[#A9D8CF] bg-white px-4 text-sm font-medium">Undo</button>
              </section>
            )}

            <section aria-label="What I've learned" className="flex flex-col gap-3 rounded-2xl border border-line bg-white p-4">
              <div className="flex items-baseline justify-between gap-2">
                <h2 className="m-0 font-display text-base font-semibold">What I've learned about {persona.name}</h2>
                <span className="font-mono text-xs text-muted">confidence {persona.profile.confidence.toFixed(2)}</span>
              </div>
              <div className="h-1.5 overflow-hidden rounded bg-subtle">
                <div className="h-full rounded bg-accent" style={{ width: `${persona.profile.confidence * 100}%` }} />
              </div>
              <ul className="m-0 flex list-none flex-wrap gap-1.5 p-0">
                {persona.profile.facts.map((f) => <li key={f} className="rounded-full border border-line bg-subtle px-2.5 py-1 text-xs text-ink-soft">{f}</li>)}
              </ul>
            </section>

            {u.history.length > 0 && (
              <section aria-label="View switches" className="flex flex-col gap-2 rounded-2xl border border-line bg-white p-4">
                <h2 className="m-0 font-display text-base font-semibold">View switches</h2>
                <ul className="m-0 flex list-none flex-col gap-1.5 p-0 text-sm">
                  {u.history.map((h) => (
                    <li key={h.id} className="flex items-center justify-between gap-2">
                      <span className="truncate">{h.title.replace(/\?$/, "")}</span>
                      <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${h.status === "accepted" ? "bg-[#E3F3F0] text-[#0A5C53]" : "bg-subtle text-muted"}`}>{h.status === "accepted" ? "on" : h.status}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section aria-label="Agent activity" className="flex flex-col gap-3 rounded-2xl bg-ink p-4 text-[#E8EAED]">
              <div className="flex items-baseline justify-between gap-2">
                <h2 className="m-0 font-display text-base font-semibold text-white">Agent activity</h2>
                <span className="font-mono text-[11px] text-faint">{u.loading ? "working..." : u.mode === "mock" ? "offline mock" : u.mode === "live" ? "live" : ""}</span>
              </div>
              <ol className="m-0 flex list-none flex-col gap-2.5 p-0">
                {u.trace.map((t, i) => (
                  <li key={i} className="flex items-start gap-2.5">
                    <span className={`min-w-20 flex-none rounded bg-[#262A31] px-1.5 py-0.5 text-center font-mono text-[11px] ${t.role === "Critic" ? "text-[#F29B9B]" : t.role.startsWith("Designer ") ? "text-[#8FD6C8]" : "text-[#F2B36B]"}`}>{t.role}</span>
                    <span className="text-[13px] leading-snug text-[#D5D9DF]">{t.text}</span>
                  </li>
                ))}
              </ol>
              <button type="button" disabled={u.loading} onClick={() => runDesigner(personaId, u.spec, u.history)}
                className="min-h-11 self-start rounded-xl border border-[#3A3F48] px-4 text-sm font-medium text-white disabled:opacity-50">Ask the designer again</button>
            </section>
          </aside>
        </div>
      </div>
    </div>
  );
}
