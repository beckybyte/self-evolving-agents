# Evolve

A calendar whose layout adapts to each user. A habit agent writes down what it has learned about how someone plans; the **designer agent** reads that and suggests a better view. Nothing changes until the user turns the suggestion on, and Undo turns it off again.

## Run it

```bash
npm install
cp .env.example .env   # add CRUSOE_API_KEY for live Nemotron calls; leave empty for offline mock mode
npm run dev            # web on :5173, designer agent on :8787
```

`npm run try-designer` runs the designer loop for all three sample users in the terminal.

## How it fits together

- `src/shared/catalog.ts`: the json-render catalog. The six building blocks (Stack, Section, MonthGrid, WeekGrid, WeekStrip, YearBand) are the only things the designer can compose. Specs say *how* to show events; components read the events themselves.
- `server/designer.ts`: the designer agent. System prompt with its rules, then N parallel draft designers (`DESIGNER_DRAFTS`), each checked by the critic and repaired with the critic's feedback up to `DESIGNER_REPAIRS` times. The best passing draft is offered as a suggestion.
- `src/shared/critic.ts`: mechanical checks, no LLM. Every event this week and every plan of a week or longer ahead must be visible; week grids may not cut off meetings; the spec must match the catalog; the proposal must differ from the current view.
- `server/mock.ts`: offline stand-in for the model. Some first drafts are deliberately flawed so the critic and repair loop show up in the activity panel.
- `server/models.ts`: one Nemotron model per agent role on Crusoe's OpenAI-compatible endpoint (rate limits are per model).
- `src/registry.tsx`: React implementations. Every day, week and month gets a stable Motion `layoutId` (`day-2026-10-17`, `week-2026-42`, `month-2026-10`), so any two views morph into each other, including views the designer invents later.
- `src/App.tsx`: suggestion card (Try it / Not now), view switches with Undo, what the habit agent has learned, and the agent activity log.
- `src/shared/personas.ts`: three sample users (Mina, Jordan, Sam) with events and learned profiles. The profiles stand in for the habit agent until it exists.

## Designer rules (from the system prompt)

1. Always offer a UI change as a suggestion; the user switches it on, like a feature switch.
2. One change at a time, the smallest step that fits.
3. Animate layout transitions: keep periods that are on screen now on screen in the new view, so the app can morph between them.
4. Never hide an event.
5. Cite the habit, with its number, in the reason.
6. Don't re-propose anything dismissed or undone.
7. Propose nothing when the view already fits.
8. 14 elements or fewer.

## Not done yet

- Live mode is untested: no Crusoe key in the build environment. Check model IDs with `GET /v1/models`.
- The habit (observer) agent: profiles are hand-written in `personas.ts`.
- State lives in the browser only; refresh resets it.
