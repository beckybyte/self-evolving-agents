"""Example tool role: on step 1, list the APIs of every app the instruction mentions."""


def run(blackboard, ctx):
    if ctx["step"] > 1 and "api_catalog" in blackboard:
        return {"text": blackboard["api_catalog"]}
    docs = ctx["api_docs"]
    instr = ctx["instruction"].lower()
    apps = [a for a in docs.apps() if a.replace("_", " ") in instr or a in instr] or ["supervisor"]
    if "supervisor" not in apps:
        apps.append("supervisor")
    lines = []
    for a in apps:
        lines.append(f"{a}: " + ", ".join(x["name"] for x in docs.api_list(a)))
    return {"text": "\n".join(lines)}
