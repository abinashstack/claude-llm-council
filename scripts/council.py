#!/usr/bin/env python3
"""LLM Council: ask several LLMs, have them rank each other anonymously, and let a
chairman model synthesize the final answer.

Based on the idea in https://github.com/karpathy/llm-council, rewritten as a
standard-library-only CLI so it can run from a Claude Code skill.

Stages
  1. Every council model answers the question independently (in parallel).
  2. Every council model reviews the anonymized answers ("Response A", "B", ...)
     and ends with a FINAL RANKING. Each reviewer sees its own shuffled order to
     reduce position bias.
  3. The chairman sees all answers (with model names), all reviews and the
     aggregate ranking, and writes the final answer.

Usage
  council.py "your question"
  council.py --question-file q.md --models a/x,b/y --chairman a/x
  echo "question" | council.py -

Environment
  OPENROUTER_API_KEY   required
  LLM_COUNCIL_MODELS   optional comma-separated default council
  LLM_COUNCIL_CHAIRMAN optional default chairman
  OPENROUTER_BASE_URL  optional (default https://openrouter.ai/api/v1)
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# "claude-top" resolves at run time to the newest Claude Fable model OpenRouter lists,
# falling back to the newest Claude Opus (see resolve_aliases). It can also be passed
# in --models / --chairman.
TOP_CLAUDE = "claude-top"
TOP_CLAUDE_PREFERENCE = ["anthropic/claude-fable-", "anthropic/claude-opus-"]
TOP_CLAUDE_FALLBACK = "anthropic/claude-opus-5.5"

DEFAULT_MODELS = [
    "openai/gpt-5.6-sol",
    "google/gemini-3.1-pro-preview",
    TOP_CLAUDE,
    "x-ai/grok-4.7",
]
DEFAULT_CHAIRMAN = TOP_CLAUDE

BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")


def fetch_model_list() -> list[dict]:
    """OpenRouter's public model list ([] if it can't be fetched)."""
    try:
        with urllib.request.urlopen(f"{BASE_URL}/models", timeout=20) as resp:
            return json.load(resp)["data"]
    except Exception:
        return []


def resolve_aliases(ids: list[str], catalog: list[dict]) -> list[str]:
    """Replace "claude-top" with the newest listed Fable, else newest Opus, else a pinned id."""
    top = TOP_CLAUDE_FALLBACK
    for prefix in TOP_CLAUDE_PREFERENCE:
        hits = [m for m in catalog if m["id"].startswith(prefix) and ":" not in m["id"]]
        if hits:
            top = max(hits, key=lambda m: m.get("created", 0))["id"]
            break
    return [top if i == TOP_CLAUDE else i for i in ids]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- API


def chat(model: str, prompt: str, *, api_key: str, timeout: int, retries: int = 2) -> dict:
    """Call OpenRouter chat completions. Returns {"text", "usage"} or raises."""
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "usage": {"include": True},
        }
    ).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "llm-council-skill",
        },
    )
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.load(resp)
            if "error" in data:
                raise RuntimeError(data["error"].get("message", str(data["error"])))
            text = data["choices"][0]["message"].get("content") or ""
            if not text.strip():
                raise RuntimeError("empty response")
            return {"text": text, "usage": data.get("usage", {})}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            last_err = RuntimeError(f"HTTP {e.code}: {detail}")
            if e.code not in (408, 429, 500, 502, 503, 504):
                break
        except Exception as e:  # network errors, timeouts, bad JSON
            last_err = e
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{model}: {last_err}")


def run_parallel(jobs: dict[str, tuple[str, str]], *, api_key: str, timeout: int) -> tuple[dict, dict]:
    """jobs: {key: (model, prompt)} -> ({key: result}, {key: error})."""
    ok, failed = {}, {}
    with cf.ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        futs = {
            pool.submit(chat, model, prompt, api_key=api_key, timeout=timeout): key
            for key, (model, prompt) in jobs.items()
        }
        for fut in cf.as_completed(futs):
            key = futs[fut]
            try:
                ok[key] = fut.result()
                log(f"  ✓ {key}")
            except Exception as e:
                failed[key] = str(e)
                log(f"  ✗ {key}: {e}")
    return ok, failed


def check_models(models: list[str], catalog: list[dict]) -> list[str]:
    """Return model ids that OpenRouter does not list ([] if the list is unavailable)."""
    if not catalog:
        return []
    known = {m["id"] for m in catalog}
    return [m for m in models if m not in known]


# ----------------------------------------------------------------------- prompts

STAGE2_PROMPT = """You are one reviewer on a council of AI models. Several models answered the question below. Their answers are anonymized.

QUESTION:
{question}

ANSWERS:
{answers}

Task:
1. For each answer, briefly note what it gets right and what it gets wrong or misses. Check facts and reasoning; do not reward length or confidence for their own sake.
2. Then end your reply with a ranking from best to worst, in exactly this format and with nothing after it:

FINAL RANKING:
1. Response X
2. Response Y
...

Rank every answer exactly once, using the labels shown above."""

STAGE3_PROMPT = """You are the chairman of a council of AI models. Each member answered the user's question, then each member reviewed and ranked the anonymized answers.

USER QUESTION:
{question}

MEMBER ANSWERS:
{answers}

PEER REVIEWS:
{reviews}

AGGREGATE RANKING (average position, lower is better):
{leaderboard}

Write the single best final answer to the user's question. Use the strongest points from the answers, correct any errors the reviews identified, and resolve disagreements by reasoning about which position is right (not by majority vote alone). If an important point remains genuinely uncertain or disputed, say so briefly. Answer the user directly; do not describe the council process."""


# ------------------------------------------------------------------------ stages


def parse_ranking(text: str, labels: list[str]) -> list[str]:
    """Extract ordered labels like 'Response C' from a review."""
    section = text.split("FINAL RANKING:")[-1] if "FINAL RANKING:" in text else text
    numbered = re.findall(r"^\s*\d+[.)]\s*\**\s*(Response [A-Z])", section, re.M)
    found = numbered or re.findall(r"Response [A-Z]", section)
    order: list[str] = []
    for lab in found:
        if lab in labels and lab not in order:
            order.append(lab)
    return order


def stage1(question, models, api_key, timeout):
    log(f"Stage 1: collecting answers from {len(models)} models")
    ok, failed = run_parallel({m: (m, question) for m in models}, api_key=api_key, timeout=timeout)
    return {m: ok[m] for m in models if m in ok}, failed


def stage2(question, answers, api_key, timeout, seed):
    members = list(answers)
    rng = random.Random(seed)
    jobs, label_maps = {}, {}
    for reviewer in members:
        order = members[:]
        rng.shuffle(order)  # per-reviewer order reduces position bias
        labels = {f"Response {chr(65 + i)}": m for i, m in enumerate(order)}
        label_maps[reviewer] = labels
        block = "\n\n".join(f"--- {lab} ---\n{answers[m]['text']}" for lab, m in labels.items())
        jobs[reviewer] = (reviewer, STAGE2_PROMPT.format(question=question, answers=block))
    log(f"Stage 2: {len(jobs)} anonymous peer reviews")
    ok, failed = run_parallel(jobs, api_key=api_key, timeout=timeout)

    reviews = {}
    for reviewer in members:
        if reviewer not in ok:
            continue
        labels = label_maps[reviewer]
        ranked_labels = parse_ranking(ok[reviewer]["text"], list(labels))
        reviews[reviewer] = {
            "text": ok[reviewer]["text"],
            "usage": ok[reviewer]["usage"],
            "label_to_model": labels,
            "ranking": [labels[lab] for lab in ranked_labels],
        }
    return reviews, failed


def aggregate(reviews, members):
    positions = {m: [] for m in members}
    for r in reviews.values():
        for pos, m in enumerate(r["ranking"], 1):
            positions[m].append(pos)
    board = [
        {"model": m, "avg_rank": round(sum(p) / len(p), 2), "votes": len(p)}
        for m, p in positions.items()
        if p
    ]
    return sorted(board, key=lambda x: x["avg_rank"])


def stage3(question, answers, reviews, board, chairman, api_key, timeout):
    ans_block = "\n\n".join(f"--- {m} ---\n{a['text']}" for m, a in answers.items())
    rev_parts = []
    for reviewer, r in reviews.items():
        legend = ", ".join(f"{lab} = {m}" for lab, m in r["label_to_model"].items())
        rev_parts.append(f"--- Review by {reviewer} (labels: {legend}) ---\n{r['text']}")
    lb = "\n".join(f"{i}. {b['model']} (avg {b['avg_rank']}, {b['votes']} votes)" for i, b in enumerate(board, 1))
    prompt = STAGE3_PROMPT.format(
        question=question,
        answers=ans_block,
        reviews="\n\n".join(rev_parts) or "(no reviews succeeded)",
        leaderboard=lb or "(no rankings parsed)",
    )
    log(f"Stage 3: chairman {chairman} is synthesizing")
    return chat(chairman, prompt, api_key=api_key, timeout=timeout)


# ------------------------------------------------------------------------ output


def total_cost(*groups) -> float | None:
    costs = [
        u.get("cost")
        for g in groups
        for item in g
        for u in [item.get("usage") or {}]
        if isinstance(u.get("cost"), (int, float))
    ]
    return round(sum(costs), 4) if costs else None


def render_markdown(question, models, chairman, answers, failed, reviews, board, final, cost):
    out = [
        "# LLM Council",
        "",
        f"**Council:** {', '.join(models)}  ",
        f"**Chairman:** {chairman}  ",
        f"**Date:** {dt.datetime.now().isoformat(timespec='seconds')}"
        + (f"  \n**Cost:** ${cost}" if cost is not None else ""),
        "",
        "## Question",
        "",
        question,
        "",
        "## Final answer",
        "",
        final["text"] if final else "_Chairman failed; see individual answers below._",
        "",
        "## Peer ranking",
        "",
        "| # | Model | Avg rank | Votes |",
        "|---|---|---|---|",
        *[f"| {i} | {b['model']} | {b['avg_rank']} | {b['votes']} |" for i, b in enumerate(board, 1)],
        "",
    ]
    if failed:
        out += ["**Failures:**", *[f"- {k}: {v}" for k, v in failed.items()], ""]
    out += ["## Stage 1: individual answers", ""]
    for m, a in answers.items():
        out += [f"### {m}", "", a["text"], ""]
    out += ["## Stage 2: peer reviews", ""]
    for reviewer, r in reviews.items():
        legend = ", ".join(f"{lab} = {m}" for lab, m in r["label_to_model"].items())
        out += [f"### Review by {reviewer}", "", f"_Labels: {legend}_", "", r["text"], ""]
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description="Run an LLM council via OpenRouter.")
    p.add_argument("question", nargs="?", help="question text, or '-' to read stdin")
    p.add_argument("--question-file", help="read the question from a file")
    p.add_argument("--models", help="comma-separated OpenRouter model ids")
    p.add_argument("--chairman", help="OpenRouter model id for the chairman")
    p.add_argument("--out-dir", default=".", help="where to write the report (default: cwd)")
    p.add_argument("--timeout", type=int, default=300, help="per-request timeout in seconds")
    p.add_argument("--seed", type=int, default=None, help="seed for reviewer shuffling")
    p.add_argument("--skip-review", action="store_true", help="skip stage 2 (cheaper, faster)")
    p.add_argument("--list-models", metavar="PREFIX", nargs="?", const="",
                   help="list OpenRouter model ids (optionally filtered by prefix) and exit")
    args = p.parse_args()

    if args.list_models is not None:
        with urllib.request.urlopen(f"{BASE_URL}/models", timeout=30) as resp:
            data = json.load(resp)["data"]
        for m in sorted(data, key=lambda m: -m.get("created", 0)):
            if m["id"].startswith(args.list_models) and ":" not in m["id"]:
                pr = m.get("pricing", {})
                print(f"{m['id']:55} in ${float(pr.get('prompt', 0)) * 1e6:.2f}/M  "
                      f"out ${float(pr.get('completion', 0)) * 1e6:.2f}/M")
        return 0

    if args.question_file:
        question = Path(args.question_file).read_text()
    elif args.question == "-" or (args.question is None and not sys.stdin.isatty()):
        question = sys.stdin.read()
    else:
        question = args.question
    if not question or not question.strip():
        p.error("no question given")
    question = question.strip()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        log("ERROR: OPENROUTER_API_KEY is not set. Get a key at https://openrouter.ai/keys "
            "and add `export OPENROUTER_API_KEY=...` to your shell profile.")
        return 2

    models = [m.strip() for m in (args.models or os.environ.get("LLM_COUNCIL_MODELS", "")).split(",") if m.strip()]
    models = models or DEFAULT_MODELS
    chairman = args.chairman or os.environ.get("LLM_COUNCIL_CHAIRMAN") or DEFAULT_CHAIRMAN
    catalog = fetch_model_list()
    models = list(dict.fromkeys(resolve_aliases(models, catalog)))  # dedupe, keep order
    chairman = resolve_aliases([chairman], catalog)[0]
    log(f"Council: {', '.join(models)} | chairman: {chairman}")

    unknown = check_models(models + [chairman], catalog)
    if unknown:
        log(f"ERROR: OpenRouter does not list these model ids: {', '.join(unknown)}")
        log("Run with --list-models <prefix> to see valid ids.")
        return 2

    answers, failed = stage1(question, models, api_key, args.timeout)
    if not answers:
        log("ERROR: every council model failed.")
        return 1

    reviews, board = {}, []
    if not args.skip_review and len(answers) >= 2:
        reviews, f2 = stage2(question, answers, api_key, args.timeout, args.seed)
        failed.update({f"review:{k}": v for k, v in f2.items()})
        board = aggregate(reviews, list(answers))

    final = None
    try:
        final = stage3(question, answers, reviews, board, chairman, api_key, args.timeout)
    except Exception as e:
        failed[f"chairman:{chairman}"] = str(e)
        log(f"  ✗ chairman: {e}")

    cost = total_cost(answers.values(), reviews.values(), [final] if final else [])
    md = render_markdown(question, models, chairman, answers, failed, reviews, board, final, cost)

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    md_path = out_dir / f"council-{stamp}.md"
    json_path = out_dir / f"council-{stamp}.json"
    md_path.write_text(md)
    json_path.write_text(json.dumps({
        "question": question, "models": models, "chairman": chairman,
        "answers": answers, "reviews": reviews, "leaderboard": board,
        "final": final, "failed": failed, "cost_usd": cost,
    }, indent=2))

    # Compact summary on stdout for the calling agent.
    print("## Final answer\n")
    print(final["text"] if final else "(chairman failed; see report)")
    if board:
        print("\n## Peer ranking (avg position, lower is better)")
        for i, b in enumerate(board, 1):
            print(f"{i}. {b['model']}: {b['avg_rank']} ({b['votes']} votes)")
    if failed:
        print("\n## Failures")
        for k, v in failed.items():
            print(f"- {k}: {v[:200]}")
    print(f"\nCost: ${cost}" if cost is not None else "\nCost: unknown")
    print(f"Report: {md_path.resolve()}")
    print(f"Raw JSON: {json_path.resolve()}")
    return 0 if final else 1


if __name__ == "__main__":
    sys.exit(main())
