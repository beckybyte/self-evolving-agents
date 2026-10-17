import express from "express";
import { design } from "./designer";
import { personaById } from "../src/shared/personas";
import type { DesignRequest } from "../src/shared/types";

const app = express();
app.use(express.json({ limit: "1mb" }));

app.post("/api/design", async (req, res) => {
  const body = req.body as DesignRequest;
  const persona = personaById(body?.personaId);
  if (!persona || !body.currentSpec) return res.status(400).json({ error: "personaId and currentSpec are required" });
  try {
    res.json(await design(persona, { personaId: persona.id, currentSpec: body.currentSpec, history: body.history ?? [] }));
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: (err as Error).message });
  }
});

const port = Number(process.env.PORT ?? 8787);
app.listen(port, () => console.log(`designer agent listening on :${port}${process.env.CRUSOE_API_KEY ? "" : " (mock mode: set CRUSOE_API_KEY for live)"}`));
