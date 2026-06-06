# claude-cothink

A [Claude Code](https://claude.com/claude-code) skill that runs a task through the **CoThink**
methodology — an 8-role multi-agent orchestration framework — by coordinating four AI coding
agents: **Claude**, **Gemini CLI**, **Kimi CLI**, and **Codex CLI**.

Claude conducts and plays the **Strategist** (role 1) and **Executor** (role 8). A small Python
driver delegates the middle roles to the CLI best suited to each. Every role has a single
responsibility, reads the prior roles' artifacts, and writes only its own — keeping context clean
and reducing error by **validating with a different model than the one that wrote the code.**

```
Strategist(Claude) → Researcher(Gemini) → Architect(Kimi) → Coder(Codex)
   → [ Analyst(Gemini) → Fixer(Codex) → Tester(Kimi) ]   (loop until criteria met)
   → Executor(Claude)
```

## The 8 roles

| # | Role | Responsibility | Default engine |
|---|------|----------------|----------------|
| 1 | Strategist | Objectives, constraints, output format, success criteria | Claude |
| 2 | Researcher | Facts, data, requirements, dependencies, risks | Gemini |
| 3 | Architect | Components, interfaces, flows, ordered build plan | Kimi |
| 4 | Coder | Build exactly to the blueprint | Codex |
| 5 | Analyst | Validate correctness/security against the criteria | Gemini |
| 6 | Fixer | Apply the Analyst's corrections | Codex |
| 7 | Tester | Edge cases, stress, failure paths | Kimi |
| 8 | Executor | Package and deliver the final asset | Claude |

The engine for each role is a config edit, not a code change (see [Configuration](#configuration)).
The Analyst (validator) defaults to a different model than the Coder, on purpose.

## Requirements

- [Claude Code](https://claude.com/claude-code)
- [`gemini`](https://github.com/google-gemini/gemini-cli) — authenticated
- [`kimi-cli`](https://github.com/MoonshotAI/kimi-cli) — logged in (`kimi-cli`, then `/login`)
- [`codex`](https://github.com/openai/codex) — authenticated
- Python 3 (standard library only — no pip dependencies)

## Install

Clone this repo directly into your Claude Code skills directory:

```bash
git clone https://github.com/Kewl-Pops/claude-cothink ~/.claude/skills/cothink
```

That's it — Claude Code will discover the `cothink` skill. (Use `~/.claude/skills/` for a global
skill, or `<project>/.claude/skills/` to scope it to one project.)

## Usage

In Claude Code:

```
/cothink build a CLI that deduplicates a CSV by a chosen column
```

Claude writes a brief, shows you the **objective + success criteria** for a one-time confirmation,
then runs the full chain and delivers the packaged result with an honest convergence report.

Manual / scripted:

```bash
# 1. create a run
python3 ~/.claude/skills/cothink/cothink.py init --title "csv-deduper"
# -> prints {run_id, run_dir, workspace, brief_path}

# 2. write the Strategist brief to brief_path (template: roles/strategist.md), then:
python3 ~/.claude/skills/cothink/cothink.py run --run-dir <run_dir> [--workspace <path>]
```

Runs are stored under `~/.cothink/runs/<id>/` (override with `COTHINK_HOME`). Each run keeps the
numbered role artifacts, per-iteration `iter-N/` dirs, `run.log`, and `result.json`.

## Configuration — `config.json`

- `models` — per-engine model override (empty string = the CLI's own default).
- `roles` — which `engine` + `mode` plays each role.
- `fallbacks` — engine used if a role's primary engine fails (so one broken CLI never kills a run).
- `max_iters` — cap on the Analyst→Fixer→Tester loop (default `3`).
- `timeout_seconds` — per-role timeout (default `1800`).
- `durable_memory` — optional: log run summaries to *your own* context-store REST API across runs.
  Off by default. Set `base_url` and export your token in the env var named by `token_env`.

## Convergence

The loop stops when the **Analyst** emits `VERDICT: PASS` *and* the **Tester** emits `RESULT: PASS`,
or when `max_iters` is reached. If it caps out, `result.json.status` is `max_iters_reached` and the
Executor reports the remaining issues honestly rather than claiming success.

## Notes & gotchas

- **Codex on a ChatGPT account:** if Codex is logged in via a ChatGPT account (not an API key), the
  `*-codex` models (`gpt-5-codex`, `gpt-5.1-codex`, `codex-mini-latest`, …) are rejected with
  *"not supported when using Codex with a ChatGPT account."* Use a plain chat model — `config.json`
  defaults `models.codex` to `gpt-5.4`.
- **Kimi output:** the driver uses `kimi-cli --quiet` (= `--print --output-format text
  --final-message-only`) so it captures clean final text instead of internal event objects.
- **Gemini noise:** Gemini may print startup/MCP messages to stderr; the driver reads stdout only
  and strips known noise lines.

## CoThink methodology

CoThink divides work into eight single-responsibility roles passed in a chain, keeping context clean
and reducing drift/hallucination versus monolithic prompting. One role at a time; no role leaks into
another; the chain repeats until the Strategist's success criteria are met.

## License

[MIT](LICENSE).
