---
name: llm-council
description: Run an "LLM council" on a question - several independent answers from different perspectives, anonymous peer review and ranking, then one synthesized final answer. Works with no API key using Claude subagents with different personas (default), or across different LLMs (GPT, Gemini, Claude, Grok...) via OpenRouter when OPENROUTER_API_KEY is set. Use when the user says "llm council", "ask the council", "get multiple opinions/perspectives", "have agents debate/rank", "ask multiple models", "what do GPT/Gemini/Grok think", or wants a cross-checked answer on a hard question, decision, design or piece of code.
---

# LLM Council

Based on Karpathy's [llm-council](https://github.com/karpathy/llm-council):

1. **First opinions:** several members answer the question independently.
2. **Anonymous peer review:** each member reviews all answers labelled "Response A/B/C…" (each reviewer gets its own shuffled order) and ends with a `FINAL RANKING`. Rankings are averaged into a leaderboard.
3. **Chairman synthesis:** one final answer built from the best of the answers, corrected by the reviews.

## Pick a mode

| Mode | When | Members are |
|---|---|---|
| **Subagent** (default) | No `OPENROUTER_API_KEY`, or the user asks for agents/personas | Claude subagents, each with a different persona |
| **Multi-model** | `OPENROUTER_API_KEY` is set and the user wants different LLMs ("ask GPT and Gemini", "multi-model") | Different providers' models via OpenRouter |

Check with `[ -n "$OPENROUTER_API_KEY" ] && echo set || echo missing`. If the user asks for multi-model and the key is missing, tell them how to get one (https://openrouter.ai/keys, then `export OPENROUTER_API_KEY=...` in their shell profile and restart) and offer subagent mode instead. Never ask them to paste a key into the chat.

Both modes: if the question is about the user's code, files or situation, put the relevant context into the question file, because members cannot see this conversation.

---

## Subagent mode

Helper: `~/.claude/skills/llm-council/scripts/local_council.py` (stdlib only). It handles anonymizing, shuffling and vote counting so those stay mechanical and unbiased. You run the subagents.

Cost: `2 × N` subagent runs (default N = 4) plus your synthesis. Tell the user this before starting.

### 1. Set up the run

Write the question (plus needed context) to a file, then:

```bash
python3 ~/.claude/skills/llm-council/scripts/local_council.py new \
  --question-file /path/to/question.md --preset general
```

It prints the run directory (`RUN`) and the personas. Presets:
- `general`: first-principles analyst, skeptic, pragmatic practitioner, domain expert.
- `code`: correctness, security, performance, maintainability reviewers.
- `decision`: advocate, devil's advocate, risk and cost analyst, user voice.

If none fits, or the user names perspectives, write a custom `personas.json` (`[{"id": "p1", "name": "...", "brief": "...", "model": "top"}]`, 3–6 members, ids `p1`…) and pass `--personas file.json`. Personas should differ in *what they prioritise*, not just tone.

### Models: lead members use the best available model

Each persona has a `model`. Every preset gives its two lead members `"top"` and the rest `"sonnet"`; keep that shape for custom personas (the members whose judgement matters most get `"top"`).

- `"top"` = **Fable if the user's plan has it, otherwise the newest Opus.** Launch these members with the Agent tool's `model: "fable"`. If the launch is rejected, or the agent finishes with a model-availability error (plan, access or quota) instead of writing its file, relaunch the same member with `model: "opus"`, use `opus` for every other `"top"` member for the rest of the run, and tell the user once that Fable wasn't available.
- `"opus"`, `"sonnet"`, `"haiku"`: pass through as the Agent tool's `model`.
- Use the same model for a persona's stage-1 answer and its stage-2 review.

Mixing tiers also helps diversity: different models disagree in more useful ways than one model with different prompts.

### 2. Stage 1: answers (all subagents in ONE message, in parallel)

For each persona, launch an Agent (`subagent_type: "general-purpose"`, `model` per the rules above) with this prompt:

> You are a member of an expert council. Your perspective: **{name}**. {brief}
>
> Read the question in `{RUN}/question.md` and answer it fully from your perspective. Be specific and correct; say where you are uncertain. You may use tools (read files, search) if the question needs it, but do not modify any files except the one below.
>
> Write your complete answer (Markdown) to `{RUN}/answers/{id}.md`. Reply only with "done".

Do **not** show members each other's answers or the other personas.

### 3. Stage 2: anonymous review

```bash
python3 ~/.claude/skills/llm-council/scripts/local_council.py packets {RUN}
```

Then, in ONE message, launch one new reviewer Agent per persona:

> You are a reviewer on an expert council. Your perspective: **{name}**. {brief}
>
> Read `{RUN}/review_packets/{id}.md`. It has a question and several anonymous answers. Judge them on correctness and usefulness above all, then completeness and clarity, from your perspective but fairly. Do not reward length or confidence for their own sake, and do not try to guess who wrote which answer.
>
> For each answer, briefly note what it gets right and what it gets wrong or misses. Then end with a ranking of every answer, best first, in exactly this format with nothing after it:
>
> FINAL RANKING:
> 1. Response X
> 2. Response Y
> ...
>
> Write the whole review to `{RUN}/reviews/{id}.md`. Reply only with "done".

### 4. Tally

```bash
python3 ~/.claude/skills/llm-council/scripts/local_council.py aggregate {RUN}
```

It prints the leaderboard and any warnings (missing or incomplete rankings) and writes `{RUN}/chairman_packet.md`.

### 5. Stage 3: chairman

If Fable is available (a `"top"` member ran on it) and you are not already running on Fable, launch one more Agent with `model: "fable"` as chairman: have it read `{RUN}/chairman_packet.md`, follow the instructions below, write the result to `{RUN}/final.md` and reply "done". Then read `final.md` and present it. Otherwise you are the chairman.

Chairman instructions: read `chairman_packet.md` and write the final answer: take the strongest points, fix errors the reviewers caught, and settle disagreements by reasoning, not by majority vote. Keep any real remaining uncertainty visible. Then present the result (see below).

---

## Multi-model mode (OpenRouter)

Script: `~/.claude/skills/llm-council/scripts/council.py` (stdlib only). It runs all three stages itself.

Tell the user the cost first: `2 × N + 1` paid model calls (N = 4 by default), usually a few cents to under a dollar. `--skip-review` halves it.

```bash
python3 ~/.claude/skills/llm-council/scripts/council.py \
  --question-file /path/to/question.md --out-dir ~/llm-council-runs
```

Run with a long timeout (up to 10 minutes) or in the background.

- `--models a/x,b/y`: council (defaults: `openai/gpt-5.6-sol`, `google/gemini-3.1-pro-preview`, `claude-top`, `x-ai/grok-4.7`).
- `--chairman a/x`: chairman (default `claude-top`).
- `claude-top` is resolved at run time to the newest Claude Fable model OpenRouter lists, else the newest Claude Opus. The script prints the resolved council. Fable is the priciest model there; if the user wants a cheaper run, pass `--chairman anthropic/claude-opus-5.5` (or another id).
- `--list-models [prefix]`: valid ids with prices (no key needed). Use it to resolve loose names like "Gemini" or "DeepSeek" to the newest non-`:batch` id.
- Env `LLM_COUNCIL_MODELS` / `LLM_COUNCIL_CHAIRMAN` set personal defaults.

The question goes to several outside providers. Don't include secrets or anything the user wouldn't want shared with them; if in doubt, ask.

---

## Presenting the result (both modes)

1. The final answer.
2. The leaderboard, one line per member.
3. What's notable: real disagreements, an error several reviewers caught, or a good point only one member raised.
4. The run directory or report path, which model each member actually ran on (note any Fable → Opus fallback), and any members that failed.

In subagent mode, mention once that all members are Claude, so they share blind spots that a multi-model council would not; personas and a model mix reduce this but don't remove it.
