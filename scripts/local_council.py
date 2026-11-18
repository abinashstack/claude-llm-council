#!/usr/bin/env python3
"""Bookkeeping for the subagent version of the LLM council.

Claude (the orchestrator) runs the subagents; this script does the parts that
must be mechanical and unbiased: anonymizing and shuffling answers for each
reviewer, parsing rankings, and tallying the leaderboard.

Run directory layout
  question.md                 the question (plus any context)
  personas.json               [{"id": "p1", "name": "...", "brief": "...", "model": "top"}, ...]
                              model: "top" (Fable, else newest Opus), "opus", "sonnet" or "haiku"
  answers/<id>.md             stage 1, one per persona (written by subagents)
  review_packets/<id>.md      stage 2 input, made by `packets`
  label_maps.json             reviewer -> {"Response A": persona id, ...}
  reviews/<id>.md             stage 2 output (written by subagents)
  leaderboard.json            made by `aggregate`
  chairman_packet.md          made by `aggregate`, input for the final synthesis

Commands
  local_council.py new --question-file q.md [--personas file.json | --preset general] [--root DIR]
  local_council.py packets RUN_DIR [--seed N]
  local_council.py aggregate RUN_DIR
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import sys
from pathlib import Path

PRESETS = {
    "general": [
        {"id": "p1", "name": "First-principles analyst", "model": "top",
         "brief": "Reason from fundamentals. Make assumptions explicit, build the answer step by step, and check the logic."},
        {"id": "p2", "name": "Skeptic", "model": "top",
         "brief": "Look for what is wrong, missing or risky. Challenge the obvious answer, name failure modes and edge cases, and say what evidence would change your mind."},
        {"id": "p3", "name": "Pragmatic practitioner", "model": "sonnet",
         "brief": "Focus on what actually works in practice. Give concrete steps, trade-offs, costs and a clear recommendation someone could act on today."},
        {"id": "p4", "name": "Domain expert", "model": "sonnet",
         "brief": "Answer as a senior expert in the field this question belongs to. Use the field's established knowledge, terminology and best practice, and point out common misconceptions."},
    ],
    "code": [
        {"id": "p1", "name": "Correctness reviewer", "model": "top",
         "brief": "Prioritise correctness: logic errors, edge cases, concurrency, error handling, data integrity."},
        {"id": "p2", "name": "Security reviewer", "model": "top",
         "brief": "Prioritise security and safety: input validation, auth, secrets, injection, unsafe defaults, blast radius."},
        {"id": "p3", "name": "Performance and scale engineer", "model": "sonnet",
         "brief": "Prioritise performance, scalability and operational behaviour: complexity, I/O, memory, failure under load."},
        {"id": "p4", "name": "Maintainability advocate", "model": "sonnet",
         "brief": "Prioritise simplicity, readability, testability and fit with existing conventions. Prefer the smallest change that solves the problem."},
    ],
    "decision": [
        {"id": "p1", "name": "Advocate", "model": "top",
         "brief": "Make the strongest honest case for the most promising option."},
        {"id": "p2", "name": "Devil's advocate", "model": "top",
         "brief": "Make the strongest honest case against the obvious choice and for the best alternative."},
        {"id": "p3", "name": "Risk and cost analyst", "model": "sonnet",
         "brief": "Quantify costs, risks, reversibility and second-order effects of each option."},
        {"id": "p4", "name": "User / customer voice", "model": "sonnet",
         "brief": "Judge each option by its effect on the people who will use or be affected by the outcome."},
    ],
}


def read_personas(run: Path) -> list[dict]:
    personas = json.loads((run / "personas.json").read_text())
    ids = [p["id"] for p in personas]
    if len(set(ids)) != len(ids):
        sys.exit("personas.json has duplicate ids")
    return personas


# ---------------------------------------------------------------- ranking parse


def parse_ranking(text: str, labels: list[str]) -> list[str]:
    section = text.split("FINAL RANKING:")[-1] if "FINAL RANKING:" in text else text
    numbered = re.findall(r"^\s*\d+[.)]\s*\**\s*(Response [A-Z])", section, re.M)
    found = numbered or re.findall(r"Response [A-Z]", section)
    order: list[str] = []
    for lab in found:
        if lab in labels and lab not in order:
            order.append(lab)
    return order


# --------------------------------------------------------------------- commands


def cmd_new(args) -> None:
    question = Path(args.question_file).read_text().strip()
    if not question:
        sys.exit("question file is empty")
    if args.personas:
        personas = json.loads(Path(args.personas).read_text())
    else:
        personas = PRESETS[args.preset]
    root = Path(args.root).expanduser()
    run = root / dt.datetime.now().strftime("council-%Y%m%d-%H%M%S")
    for sub in ("answers", "review_packets", "reviews"):
        (run / sub).mkdir(parents=True, exist_ok=True)
    (run / "question.md").write_text(question + "\n")
    (run / "personas.json").write_text(json.dumps(personas, indent=2))
    print(run.resolve())
    for p in personas:
        print(f"  {p['id']}: {p['name']}" + (f" [{p['model']}]" if p.get("model") else ""))


def cmd_packets(args) -> None:
    run = Path(args.run_dir)
    question = (run / "question.md").read_text().strip()
    personas = read_personas(run)
    answers = {}
    for p in personas:
        f = run / "answers" / f"{p['id']}.md"
        if f.exists() and f.read_text().strip():
            answers[p["id"]] = f.read_text().strip()
        else:
            print(f"warning: no answer for {p['id']} ({p['name']}); leaving it out", file=sys.stderr)
    if len(answers) < 2:
        sys.exit("need at least 2 answers to review")

    rng = random.Random(args.seed)
    label_maps = {}
    for reviewer in personas:
        order = list(answers)
        rng.shuffle(order)  # every reviewer gets its own order
        labels = {f"Response {chr(65 + i)}": pid for i, pid in enumerate(order)}
        label_maps[reviewer["id"]] = labels
        body = "\n\n".join(f"--- {lab} ---\n{answers[pid]}" for lab, pid in labels.items())
        (run / "review_packets" / f"{reviewer['id']}.md").write_text(
            f"QUESTION:\n{question}\n\nANSWERS:\n{body}\n"
        )
    (run / "label_maps.json").write_text(json.dumps(label_maps, indent=2))
    print(f"wrote {len(label_maps)} review packets for {len(answers)} answers")


def cmd_aggregate(args) -> None:
    run = Path(args.run_dir)
    question = (run / "question.md").read_text().strip()
    personas = {p["id"]: p for p in read_personas(run)}
    label_maps = json.loads((run / "label_maps.json").read_text())
    answered = sorted({pid for m in label_maps.values() for pid in m.values()})

    positions = {pid: [] for pid in answered}
    problems, reviews = [], {}
    for reviewer, labels in label_maps.items():
        f = run / "reviews" / f"{reviewer}.md"
        if not f.exists() or not f.read_text().strip():
            problems.append(f"{reviewer}: no review")
            continue
        text = f.read_text().strip()
        ranked = parse_ranking(text, list(labels))
        if len(ranked) < len(labels):
            problems.append(f"{reviewer}: ranked {len(ranked)}/{len(labels)} answers")
        for pos, lab in enumerate(ranked, 1):
            positions[labels[lab]].append(pos)
        reviews[reviewer] = {"text": text, "ranking": [labels[lab] for lab in ranked], "labels": labels}

    board = sorted(
        ({"id": pid, "name": personas[pid]["name"], "avg_rank": round(sum(p) / len(p), 2), "votes": len(p)}
         for pid, p in positions.items() if p),
        key=lambda b: b["avg_rank"],
    )
    (run / "leaderboard.json").write_text(json.dumps({"leaderboard": board, "problems": problems}, indent=2))

    def who(pid):
        return f"{personas[pid]['name']} ({pid})"

    parts = [f"USER QUESTION:\n{question}", "", "MEMBER ANSWERS:"]
    for pid in answered:
        parts += [f"--- {who(pid)} ---", (run / "answers" / f"{pid}.md").read_text().strip(), ""]
    parts.append("PEER REVIEWS (labels decoded):")
    for reviewer, r in reviews.items():
        legend = ", ".join(f"{lab} = {who(pid)}" for lab, pid in r["labels"].items())
        parts += [f"--- Review by {who(reviewer)} | {legend} ---", r["text"], ""]
    parts.append("AGGREGATE RANKING (average position, lower is better):")
    parts += [f"{i}. {b['name']} ({b['id']}): avg {b['avg_rank']}, {b['votes']} votes" for i, b in enumerate(board, 1)]
    (run / "chairman_packet.md").write_text("\n".join(parts) + "\n")

    print("Peer ranking (avg position, lower is better):")
    for i, b in enumerate(board, 1):
        print(f"{i}. {b['name']} ({b['id']}): {b['avg_rank']} ({b['votes']} votes)")
    for p in problems:
        print(f"warning: {p}")
    print(f"Chairman packet: {(run / 'chairman_packet.md').resolve()}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="create a run directory")
    n.add_argument("--question-file", required=True)
    g = n.add_mutually_exclusive_group()
    g.add_argument("--personas", help="JSON file with custom personas")
    g.add_argument("--preset", choices=sorted(PRESETS), default="general")
    n.add_argument("--root", default="~/llm-council-runs")
    n.set_defaults(fn=cmd_new)

    p = sub.add_parser("packets", help="build anonymized, shuffled review packets")
    p.add_argument("run_dir")
    p.add_argument("--seed", type=int)
    p.set_defaults(fn=cmd_packets)

    a = sub.add_parser("aggregate", help="parse reviews, build leaderboard and chairman packet")
    a.add_argument("run_dir")
    a.set_defaults(fn=cmd_aggregate)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
