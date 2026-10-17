import { design } from "../server/designer";
import { PERSONAS } from "../src/shared/personas";
const month = { root: "view", elements: { view: { type: "MonthGrid", props: { month: "2026-10" }, children: [] } } };
for (const p of PERSONAS) {
  let spec: any = month; const history: any[] = [];
  for (let i = 0; i < 3; i++) {
    const r = await design(p, { personaId: p.id, currentSpec: spec, history });
    console.log(`\n== ${p.id} step ${i}`); r.trace.forEach(t => console.log(`  [${t.role}] ${t.text}`));
    if (!r.proposal) break; spec = r.proposal.spec; history.push({ title: r.proposal.title, status: "accepted" });
  }
}
