---
name: cothink
description: Run a task through the CoThink 8-role multi-agent methodology (Strategist → Researcher → Architect → Coder → Analyst → Fixer → Tester → Executor). Claude conducts and plays Strategist + Executor; Gemini, Kimi, and Codex CLIs play the middle roles. Use when the user says "use cothink", wants a task built via the CoThink chain, or asks for an orchestrated multi-agent build/research/design with strict role separation.
---

# CoThink conductor

You are the **conductor**. CoThink divides work into 8 roles, each with one responsibility, each passing output to the next. You personally play **role 1 (Strategist)** and **role 8 (Executor)**. A driver script runs **roles 2–7** by delegating to other CLIs. Context stays clean because every role reads prior artifacts from a run directory and writes only its own.

## Engine map (roles 2–7, set in `config.json`)
| Role | Engine |
|------|--------|
| 2 Researcher | Gemini |
| 3 Architect | Kimi |
| 4 Coder | Codex (`gpt-5.4`) |
| 5 Analyst | Gemini (independent of the Coder) |
| 6 Fixer | Codex |
| 7 Tester | Kimi |

Each engine has a fallback (`config.json → fallbacks`) so one broken CLI never kills a run.

## Procedure — follow in order

**1. Strategist (you).** Turn the user's request into a brief. Read `roles/strategist.md` for the exact template. Then:
   - Run `python3 ~/.claude/skills/cothink/cothink.py init --title "<short title>"`. It prints JSON with `run_dir`, `workspace`, and `brief_path`.
   - Write your brief to `brief_path` (the `brief.md`). The **Success criteria** section is the bar the loop converges to — make every criterion objectively checkable (e.g. "`pytest` passes", "returns X for input Y"). If the deliverable is code, build it in `workspace` (or set a different path with `--workspace`).

**2. THE ONE GATE.** Show the user the **Objective** and **Success criteria** from your brief and ask them to confirm or adjust. This is the only stop. (If the user has said "just go" / "do it all", proceed without waiting.)

**3. Run roles 2–7.** Execute:
   ```
   python3 ~/.claude/skills/cothink/cothink.py run --run-dir "<run_dir>" [--workspace "<path>"]
   ```
   This runs Researcher → Architect → Coder, then loops Analyst → Fixer → Tester until the Analyst's verdict (`{"verdict": "pass"}`) **and** the Tester's result (`{"result": "pass"}`) both pass, or `max_iters` is hit. The driver parses each role's final fenced JSON block authoritatively and re-prompts a role that gives no parseable verdict. It writes numbered artifacts, `run.log`, and `result.json`. It may take several minutes — let it finish. Use `--workspace` to point at an existing project to build into.

**4. Executor (you).** When the driver finishes, follow `roles/executor.md`: read `result.json` and the final-iteration artifacts, inspect the workspace, then deliver to the user — what was built, how to use it, the run summary (engines per role, iterations, converged or capped), and any open items. **Be honest about non-convergence**: if `status` is `max_iters_reached`, say so and list the Tester's remaining issues; do not claim success.

## Rules (the methodology)
- **One role at a time. No role leaks into another.** The role templates in `roles/` enforce this — don't loosen them.
- **Never skip a role.** The chain is the product.
- **You only ever play Strategist and Executor.** Don't do the Researcher/Architect/Coder/Analyst/Fixer/Tester work yourself — that defeats the multi-model error reduction. Delegate via the driver.
- Stop only when the Executor has delivered the finished asset.

## Files
- `cothink.py` — the driver (`init`, `run`). `config.json` — engine map, models, `max_iters`, durable memory.
- `roles/*.md` — strict role-boundary prompt templates. `lib/context_client.py` — optional durable memory.
- Runs live under `~/.cothink/runs/<id>/` (override with `COTHINK_HOME`).

## Setup notes
- Run `python3 ~/.claude/skills/cothink/cothink.py doctor` once to confirm the engine CLIs (and fallbacks) are installed before the first run.
- Codex must use a plain chat model (`gpt-5.4`); the `*-codex` models are rejected on ChatGPT-account auth.
- Durable cross-run memory (shared-context REST) is **off by default**. To enable: set `durable_memory.enabled=true` in `config.json` and export the token in `COTHINK_CONTEXT_TOKEN`.
