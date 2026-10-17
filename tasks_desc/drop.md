TASK the team solves: DROP reading comprehension with discrete reasoning (counting, adding, subtracting, sorting,
comparing dates/numbers over a passage). Each sample = passage + question. The team's FINAL output must end with
a line "Answer: <answer>": a number (digits only), a date, or text span(s) copied from the passage (comma-separated
if several). Grading: token-level F1 against the gold answer spans (max over gold answers), so extra words cost
score. Tool roles may run Python for arithmetic; the calendar helper library in genome_schema.md is irrelevant.
Evaluation uses unseen passages, so prefer general mechanisms.
