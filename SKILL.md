---
name: cothink
description: Run a task through the CoThink 8-role multi-agent methodology (Strategist → Researcher → Architect → Coder → Analyst → Fixer → Tester → Executor). Claude conducts and plays Strategist + Executor; the middle roles run on seven coding-agent CLIs — Gemini (Antigravity), an isolated Claude, Codex, Grok, Kimi, with Vibe and Qwen as fallbacks — always invoked FROM Claude Code, never the reverse. Use when the user says "use cothink", wants a task built via the CoThink chain, or asks for an orchestrated multi-agent build/research/design with strict role separation.
---

# CoThink conductor

You are the **conductor**. CoThink divides work into 8 roles, each with one responsibility, each passing output to the next. You personally play **role 1 (Strategist)** and **role 8 (Executor)**. A driver script runs **roles 2–7** by delegating to other CLIs. Context stays clean because every role reads prior artifacts from a run directory and writes only its own.

## Engine map (roles 2–7, set in `config.json`)
| Role | Engine (mode) | Fallback chain |
|------|---------------|----------------|
| 2 Researcher | Gemini / Antigravity, `gemini-3.6-flash-medium` (read_only) | grok → kimi |
| 3 Architect | Claude, `claude-fable-5-1` (read_only — one isolated `claude -p` call) | kimi → codex → grok |
| 4 Coder | Codex, `gpt-6-astra` default (write) | kimi → grok → qwen |
| 5 Analyst | Grok (read_only; independent of the Coder) | claude → gemini → kimi |
| 6 Fixer | Codex (write) | kimi → grok → qwen |
| 7 Tester | Kimi (write — it executes for real) | grok → claude |

Builder / validator / tester are three model families (OpenAI / xAI / Moonshot); `codex` is never in a judgment chain, and the driver skips any Analyst engine from the family that actually wrote the code in this run (logged; `result.json.guard_events`). **Vibe** and **Qwen** are wired engines kept as last-resort write fallbacks (pay-per-token). Every fallback is per role (`config.json → roles.<role>.fallbacks`), so one broken or quota-exhausted CLI never kills a run.

**Direction rule:** you (Claude Code) call the other CLIs. Nothing calls Claude Code. The `claude` engine is a separate, isolated `claude -p` process (no session persistence, no MCP, no settings) that you launch — that direction is fine.

## Procedure — follow in order

**1. Strategist (you).** Turn the user's request into a brief. Read `roles/strategist.md` for the exact template and its **Before you write** checks (one slice per run, brownfield baseline, spec mapping, questions only the user can answer). Then:
   - Run `python3 ~/.claude/skills/cothink/cothink.py init --title "<short title>"`. It prints JSON with `run_dir`, `workspace`, and `brief_path`.
   - Write your brief to `brief_path` (the `brief.md`). The **Success criteria** section is the bar the loop converges to — make every criterion objectively checkable (e.g. "`pytest` passes", "returns X for input Y"). If the deliverable is code, build it in `workspace` (or set a different path with `--workspace`).

**2. THE ONE GATE.** Show the user the **Objective**, **Success criteria** and **Out of scope** from your brief (and the ordered slice list, if you split the request) and ask them to confirm or adjust. This is the only stop. (If the user has said "just go" / "do it all", proceed without waiting.)

**3. Run roles 2–7.** Execute:
   ```
   python3 ~/.claude/skills/cothink/cothink.py run --run-dir "<run_dir>" [--workspace "<path>"]
   ```
   This runs Researcher → Architect → Coder, then loops Analyst → Fixer → Tester until the Analyst returns `VERDICT: PASS` **and** the Tester returns `RESULT: PASS`, or `max_iters` is hit. It halts before the Coder (`status: blocked`) if the Architect's `## Decisions` carries a `BLOCKED:` line, and stops the loop early when the Analyst and Fixer agree every remaining failure is environment-blocked (`--no-halt-on-blocked` / `stop_when_blocked` override). It writes numbered artifacts, `run.log`, and `result.json`. It may take several minutes — let it finish. Use `--workspace` to point at an existing project to build into.

**4. Executor (you).** When the driver finishes, follow `roles/executor.md`: read `result.json` and the final-iteration artifacts, inspect the workspace, then deliver to the user — what was built, how to use it, the run summary (engines per role, iterations, converged or capped), and any open items. **Be honest about non-convergence**: if `status` is `max_iters_reached`, say so and list the Tester's remaining issues; do not claim success. If `status` is `blocked`, lead with `result.json.blocked`: no engine could satisfy those items — the user must resolve them (or move the criterion to Out of scope) and re-run; do not describe it as a failed build. Finish with the one-line retro pointer (`retro.md`).

## Rules (the methodology)
- **One role at a time. No role leaks into another.** The role templates in `roles/` enforce this — don't loosen them.
- **Never skip a role.** The chain is the product.
- **You only ever play Strategist and Executor.** Don't do the Researcher/Architect/Coder/Analyst/Fixer/Tester work yourself — that defeats the multi-model error reduction. Delegate via the driver.
- Stop only when the Executor has delivered the finished asset.

## Files
- `cothink.py` — the driver (`init`, `run`). `config.json` — engine map, model pins, per-role fallback chains, families, `max_iters`, durable memory.
- `roles/*.md` — strict role-boundary prompt templates. `lib/context_client.py` — optional durable memory. `tests/` — stdlib unit tests (`python3 -m unittest discover -s tests`).
- Runs live under `~/.cothink/runs/<id>/` (override with `COTHINK_HOME`).

## Setup notes
- The driver reads `config.json` **from the directory it runs in** (`~/.claude/skills/cothink/`). Edits to a checkout elsewhere do nothing until copied there.
- Leave `models.codex` empty (ChatGPT-account auth rejects `*-codex` ids; the CLI default is current). Keep `models.claude` a **full** id — aliases resolve inconsistently across fleet accounts. `models.gemini` is pinned to Flash to protect the small Antigravity weekly pool.
- `codex_reasoning_effort` is applied per call (`-c model_reasoning_effort=…`), leaving account configs alone. `vibe_max_price_usd` caps vibe's real-dollar spend per call.
- If Claude quota is tight, swap the Architect's primary and first fallback (`claude` ↔ `kimi`) in `config.json`.
- Durable cross-run memory (shared-context REST) is **off by default**. To enable: set `durable_memory.enabled=true` in `config.json` and export the token in `COTHINK_CONTEXT_TOKEN`.
