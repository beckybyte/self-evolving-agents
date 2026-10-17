// One Nemotron model per agent role, all on Crusoe's OpenAI-compatible endpoint.
// Rate limits are per model, so spreading roles across models gives more headroom.
export const MODELS = {
  planner: process.env.MODEL_PLANNER ?? "nvidia/Nemotron-3-Super-120B-A12B",
  designer: process.env.MODEL_DESIGNER ?? "nvidia/nemotron-3.5-lightning-30b-a3b",
  observer: process.env.MODEL_OBSERVER ?? "nvidia/nemotron-3.5-lightning-30b-a3b",
  router: process.env.MODEL_ROUTER ?? "nvidia/Nemotron-3-Nano-30B-A3B",
};
