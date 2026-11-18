# llm-council: a Claude Code skill

Ask a **council** instead of a single model. Several members answer your question independently, review each other's answers anonymously and rank them, and a chairman writes one final answer. That answer keeps the strongest points and fixes the errors the reviewers caught.

Inspired by Andrej Karpathy's [llm-council](https://github.com/karpathy/llm-council). This version is a [Claude Code](https://claude.com/claude-code) skill: no web app, no dependencies beyond Python 3.9+.

## Two modes

| Mode | Needs | Council members |
|---|---|---|
| **Subagent** (default) | Nothing but Claude Code | Claude subagents, each with a different persona (e.g. analyst, skeptic, practitioner, domain expert). Lead members use **Fable** when your plan has it, otherwise the newest **Opus**; the rest use Sonnet. |
| **Multi-model** | An [OpenRouter](https://openrouter.ai/keys) API key | Different providers' models: GPT, Gemini, Claude (newest Fable, else Opus) and Grok by default. Fully configurable. |

## How a run works

1. **First opinions:** every member answers independently, in parallel.
2. **Anonymous peer review:** every member reviews all answers labelled "Response A/B/C…". Each reviewer sees its own shuffled order, which reduces position bias. Each review ends with a ranking, and the rankings are averaged into a leaderboard.
3. **Chairman synthesis:** the chairman reads every answer, review and the leaderboard. It writes the final answer and settles disagreements by reasoning, not by majority vote.

Anonymizing, shuffling and vote counting are done by small scripts, not by the model, so they stay mechanical and unbiased. Every run is saved to disk: answers, reviews, leaderboard and final answer.

## Install

```bash
git clone https://github.com/abinashstack/claude-llm-council ~/.claude/skills/llm-council
```

Restart Claude Code. The skill shows up as `llm-council`.

For multi-model mode, also add this to your shell profile (`~/.zprofile`, `~/.bashrc`, …) and restart:

```bash
export OPENROUTER_API_KEY=sk-or-...
```

## Use

Just ask Claude Code:

- "Ask the council: should we use Postgres or MongoDB for this service?"
- "llm council, code preset: review this function for bugs" (with code pasted or a file referenced)
- "Ask GPT, Gemini and Grok what they think about …" (multi-model)

Persona presets for subagent mode: `general`, `code` (correctness, security, performance, maintainability) and `decision` (advocate, devil's advocate, risk and cost, user voice). You can also describe your own perspectives.

### Scripts directly

```bash
# Multi-model, fully automatic
python3 scripts/council.py "your question" --out-dir ~/llm-council-runs
python3 scripts/council.py --list-models google/      # see valid model ids and prices
python3 scripts/council.py --question-file q.md --models openai/gpt-5.6-sol,anthropic/claude-opus-5.5 --chairman claude-top

# Subagent mode helpers (Claude runs the subagents; see SKILL.md)
python3 scripts/local_council.py new --question-file q.md --preset general
python3 scripts/local_council.py packets <run-dir>
python3 scripts/local_council.py aggregate <run-dir>
```

Optional env vars: `LLM_COUNCIL_MODELS` (comma-separated) and `LLM_COUNCIL_CHAIRMAN` for your own multi-model defaults. `claude-top` is resolved at run time to the newest Claude Fable model, falling back to the newest Opus.

## Cost

- **Subagent mode:** 2 × N subagent runs (8 with the default 4 members), plus the chairman, all on your Claude plan.
- **Multi-model mode:** 2 × N + 1 paid OpenRouter calls (9 by default). That's usually a few cents to under a dollar, depending on question length and models. `--skip-review` halves it.

## Caveats

- In subagent mode every member is Claude, so they share blind spots that a multi-model council would not. Personas and a mix of model tiers reduce this but don't remove it.
- Reviewers can still prefer answers written in their own persona's style, even when anonymized. Check whether the leaderboard changes when self-votes are excluded.
- In multi-model mode your question goes to several outside providers. Don't include secrets or data you can't share with them.

## License

MIT
