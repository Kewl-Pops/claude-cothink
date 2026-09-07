# claude-cothink

A [Claude Code](https://claude.com/claude-code) skill that runs a task through the **CoThink**
methodology — an 8-role multi-agent orchestration framework — by conducting **seven** coding-agent
CLIs: **Claude Code**, **Codex**, **Gemini (Antigravity CLI)**, **Kimi Code**, **Grok**,
**Mistral Vibe**, and **Qwen Code**.

Claude Code conducts and plays the **Strategist** (role 1) and **Executor** (role 8). A small Python
driver delegates the middle roles to the CLI best suited to each. Every role has a single
responsibility, reads the prior roles' artifacts, and writes only its own — keeping context clean
and reducing error by **validating with a different model family than the one that wrote the code.**

**Direction matters.** Claude Code is the only entry point: it invokes the other CLIs (including a
second, isolated `claude` process), and nothing ever invokes Claude Code. Driving Claude Code from
another agent is a terms-of-service risk; the reverse is fine.

```
Strategist(Claude Code) → Researcher(Gemini) → Architect(Claude) → Coder(Codex)
   → [ Analyst(Grok) → Fixer(Codex) → Tester(Kimi) ]   (loop until criteria met, max 3)
   → Executor(Claude Code)
```

## The 8 roles

| # | Role | Responsibility | Default engine | Fallback chain |
|---|------|----------------|----------------|----------------|
| 1 | Strategist | Objectives, constraints, output format, success criteria | Claude Code (conductor) | — |
| 2 | Researcher | Facts, data, requirements, dependencies, risks | Gemini (`gemini-3.6-flash-medium`) | grok → kimi |
| 3 | Architect | Components, interfaces, flows, ordered build plan | Claude (`claude-fable-5-1`, read-only) | kimi → codex → grok |
| 4 | Coder | Build exactly to the blueprint | Codex (`gpt-6-astra` default) | kimi → grok → qwen |
| 5 | Analyst | Validate correctness/security against the criteria | Grok (read-only) | claude → gemini → kimi |
| 6 | Fixer | Apply the Analyst's corrections | Codex | kimi → grok → qwen |
| 7 | Tester | Edge cases, stress, failure paths (executes for real) | Kimi (write) | grok → claude |
| 8 | Executor | Package and deliver the final asset | Claude Code (conductor) | — |

The engine for each role is a config edit, not a code change (see [Configuration](#configuration)).
Builder, validator and tester are **three different model families** (OpenAI / xAI / Moonshot), so
no vendor both writes and judges the code, and `codex` never appears in a judgment chain. The driver
also enforces this at run time: the Analyst is never run on the family that actually wrote the code
in this run (Coder or Fixer, fallbacks included) — such engines are skipped and logged, and if
nothing independent is left the run proceeds with a loud warning recorded in `result.json`.

### Why this mapping

Decided 2026-09-07 from three kinds of evidence: this box's own run logs (hundreds of prior role
calls), live headless probes of every CLI, and published evaluations. In short:

- **Codex builds.** Highest edit fidelity on record here (52/53 Coder runs completed, ~95% patch
  success, always wrote real files and ran the tests it claimed) and the published Terminal-Bench
  leader. Fixer stays on Codex too: short bounded turns, same-family continuity helps targeted edits,
  and the Fixer is not a judge.
- **Grok validates.** Best measured review precision among the non-Claude engines (55% of its
  findings independently corroborated in a 579-finding adjudicated review), a different family from
  both the builder and the conductor, subscription-backed with no throttling observed. It replaces
  Qwen, whose Analyst runs made zero tool calls and cited tests that did not exist.
- **Kimi tests.** It actually executes (82 of 98 Tester sessions ran real commands) and posts the
  best shell/agentic scores among the remaining engines; write mode is the one headless mode where
  its behaviour is unambiguous.
- **Claude architects.** One read-only call per run with the most downstream leverage, and the
  strongest planning evidence in the fleet. It is pinned to a full model id because unpinned calls
  land on whatever each account defaults to. It is kept out of the write loop, which is where the
  conductor's own quota burns fastest, and serves as the Analyst's first fallback.
- **Gemini researches.** Native web tools headless and a clean JSON contract. Pinned to Flash because
  the Antigravity weekly pool is small; one Pro-high research run has locked the account out for days.
- **Vibe and Qwen** are wired engines kept as last-resort write fallbacks: both bill per token, vibe
  timed out on a third of headless calls in the largest local sample, and qwen is a generation behind.

## Engines

| Engine | CLI | How the driver calls it |
|--------|-----|-------------------------|
| `claude` | Claude Code (`claude`, or the `claude-acct` fleet dispatcher when on `PATH`) | `claude -p … --output-format text --no-session-persistence --strict-mcp-config --setting-sources ''`; write → `--dangerously-skip-permissions` (`IS_SANDBOX=1`), read-only → `--permission-mode dontAsk --tools Read,Glob,Grep,Bash --allowedTools 'Bash(pytest *)' …`, plan → `--permission-mode plan` |
| `codex` | OpenAI Codex (`codex`, or `codex-acct` when on `PATH`) | `codex exec -C <ws> --skip-git-repo-check -s workspace-write\|read-only -c model_reasoning_effort="high" --output-last-message <file>` |
| `gemini` | Google Antigravity CLI (`agy`; `gemini` shim) | `gemini --output-format text --print-timeout <role budget> --mode accept-edits\|plan [--dangerously-skip-permissions] [--add-dir <ws>] -p …` |
| `kimi` | Moonshot Kimi Code | `kimi --quiet -w <ws> --yolo\|--plan -p …` (plan-mode blueprint harvested from `~/.kimi/plans/`) |
| `grok` | xAI Grok | `grok -p … --output-format plain --no-alt-screen --cwd <ws> --no-memory --always-approve [--deny Write --deny Edit]` |
| `vibe` | Mistral Vibe | `vibe -p … --output text --workdir <ws> --auto-approve\|--agent plan --max-price <cap>` |
| `qwen` | Qwen Code | `qwen -o text --approval-mode yolo\|plan -m qwen3-coder-plus -p …` |

Override the binaries with `COTHINK_CLAUDE_BIN` / `COTHINK_CODEX_BIN` (useful when you don't run the
fleet dispatchers).

## Requirements

- [Claude Code](https://claude.com/claude-code) (the conductor) — plus, for the engines you assign:
- [`codex`](https://github.com/openai/codex) — authenticated (ChatGPT account or API key)
- [`agy`](https://antigravity.google/docs/cli) (Antigravity CLI) exposed as `gemini` — signed in
- [`kimi`](https://github.com/MoonshotAI/kimi-cli) — logged in
- [`grok`](https://docs.x.ai/) (Grok CLI) — logged in
- [`vibe`](https://github.com/mistralai/mistral-vibe) — `MISTRAL_API_KEY` set
- [`qwen`](https://github.com/QwenLM/qwen-code) — provider key in `~/.qwen/.env`
- Python 3.8+ (standard library only — no pip dependencies)

Any engine you don't have can simply be left out of `config.json`; each role's fallback chain keeps
a run alive when one CLI is missing, broken, or out of quota.

## Install

Clone this repo directly into your Claude Code skills directory:

```bash
git clone https://github.com/Kewl-Pops/claude-cothink ~/.claude/skills/cothink
```

That's it — Claude Code will discover the `cothink` skill. (Use `~/.claude/skills/` for a global
skill, or `<project>/.claude/skills/` to scope it to one project.) The driver reads `config.json`
from the directory it lives in, so edit the installed copy.

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
numbered role artifacts, per-iteration `iter-N/` dirs, `run.log`, and `result.json`. Point
`--workspace` at an existing project for brownfield work — every role, including the Researcher and
Architect, gets read access to it.

## Configuration — `config.json`

- `models` — per-engine model pin (empty string = the CLI's own default). Pin **full** ids for
  `claude`; `vibe` has no model flag (its model lives in `~/.vibe/config.toml`).
- `families` — model family per engine; drives the Analyst independence guard.
- `roles` — per role: `engine` (primary), `mode` (`read_only` | `plan` | `write`), and `fallbacks`
  (ordered chain tried when the primary fails). A legacy top-level per-engine `fallbacks` table is
  still honoured for roles without their own chain.
- `codex_reasoning_effort` — passed per call as `-c model_reasoning_effort=…` (default `high`) so
  CoThink's Codex spend doesn't inherit an `xhigh` account default. Empty = inherit.
- `vibe_max_price_usd` — hard per-call USD cap for vibe, the only pay-per-token engine with one.
- `max_iters` — cap on the Analyst→Fixer→Tester loop (default `3`).
- `timeout_seconds` — per-role timeout (default `1800`).
- `durable_memory` — optional: log run summaries to *your own* context-store REST API across runs.
  Off by default. Set `base_url` and export your token in the env var named by `token_env`.

## Convergence

The loop stops when the **Analyst** emits `VERDICT: PASS` *and* the **Tester** emits `RESULT: PASS`,
or when `max_iters` is reached. If it caps out, `result.json.status` is `max_iters_reached` and the
Executor reports the remaining issues honestly rather than claiming success. `result.json` also lists
`engines_used` per role and any `guard_events` (an Analyst that had to run on the builder's family,
or an Analyst and Tester that ended up on the same family after fallbacks).

## Notes & gotchas (per engine, all verified headless)

- **Claude:** as root, `--dangerously-skip-permissions` needs `IS_SANDBOX=1` (the driver sets it).
  Role calls are isolated from your own session — no session persistence, no MCP servers, no
  settings/hooks. Pin full model ids (`claude-fable-5-1`), not aliases. Fable has its own scoped
  weekly meter on Max/Team seats, which is another reason it only plays one read-only role per run.
- **Codex:** on a ChatGPT account the `*-codex` model ids are rejected, so leave `models.codex`
  empty. Its sandbox blocks network, so it never plays the Researcher.
- **Gemini** here means the **Antigravity CLI** (`agy`) — the driver uses its flags (`--mode`,
  `--print-timeout`, `--dangerously-skip-permissions`). Headless, its shell tool needs a permission
  nobody can grant, so a read-only turn that reaches for it is cancelled with empty output; skipping
  permissions is not the answer because `--mode plan` then writes files (verified). Read-only gemini
  roles are therefore told the shell is unavailable (file/grep/web tools only) and cannot run tests;
  write mode skips permissions so it can. The free weekly pool is small; the Flash pin is a quota
  decision, not a capability one.
- **Kimi:** `--plan` writes the full blueprint to `~/.kimi/plans/*.md` and prints only a summary; the
  driver harvests the plan file. `--plan` is also porous in print mode (plan exit is auto-approved),
  so read-only seats stay on engines with real deny rules.
- **Grok:** a headless turn is cancelled silently on the first permission prompt in the default,
  `dontAsk`, and `plan` modes. The driver always auto-approves and, for read-only roles, adds
  `--deny Write --deny Edit`, which also blocks shell redirects while leaving shell reads working.
- **Vibe:** no `--model` flag; the driver caps spend with `--max-price`. First-request timeouts were
  common in the largest local sample, so it is a fallback, not a primary.
- **Qwen:** `--approval-mode` is hidden from `--help` in 0.21.x but works; plain `-p` only
  *describes* edits, so the driver always passes `yolo` or `plan`.

## Tests

```bash
python3 -m unittest discover -s tests
```

Stdlib `unittest`; no network — engine calls are stubbed. Covers the per-role chains, the family
guard, config-driven modes, verdict parsing, and the exact command shape sent to each CLI.

## CoThink methodology

CoThink divides work into eight single-responsibility roles passed in a chain, keeping context clean
and reducing drift/hallucination versus monolithic prompting. One role at a time; no role leaks into
another; the chain repeats until the Strategist's success criteria are met.

## License

[MIT](LICENSE).
