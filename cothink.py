#!/usr/bin/env python3
"""CoThink driver — orchestrates the 8-role methodology across Claude + Gemini + Kimi + Codex + Grok + Vibe.

Claude (the conductor) plays role 1 (Strategist) and role 8 (Executor) natively and calls
this driver to run roles 2-7 (Researcher, Architect, Coder, then the Analyst -> Fixer ->
Tester loop). See SKILL.md for how the conductor drives this.

Engines (validated headless commands, verified 2026-08):
  gemini : gemini --mode {accept-edits|plan} --output-format text [--add-dir WS] -p <prompt>
  kimi   : kimi --quiet -w <ws> {--yolo|--plan} -p <prompt>          (--quiet => clean final message)
  claude : claude -p <prompt> --output-format text --no-session-persistence --strict-mcp-config
           {write: --dangerously-skip-permissions | plan: --permission-mode plan | read_only: dontAsk + read tools}
           (IS_SANDBOX=1; via claude-acct when installed) — optional middle-role engine
  codex  : codex-acct exec -C <ws>  (codex-acct = multi-account dispatcher; falls back to codex) --skip-git-repo-check -s {workspace-write|read-only}
           --output-last-message <file> <prompt>   (leave models.codex empty: ChatGPT-account auth
           rejects *-codex model ids, and the CLI's own default is current, e.g. gpt-5.6-sol)
  grok   : grok -p <prompt> --output-format plain --no-alt-screen --cwd <ws> [--always-approve]
  vibe   : vibe -p <prompt> --output text --workdir <ws> {--auto-approve|--agent plan}

Run statuses: passed | max_iters_reached | blocked (halted before the Coder on an Architect
`## Decisions` line that begins with BLOCKED:, or the loop stalled with every remaining failure
environment-blocked and the Fixer agreeing under `## Not fixed`).

Subcommands:
  doctor [--json]                       -> preflight: CLIs on PATH, model pins, role map, config invariants
  init   --title "..."                  -> create a run dir, print JSON {run_id, run_dir, ...}
  run    --run-dir DIR [--workspace WS] [--no-halt-on-blocked] -> run roles 2-7; writes artifacts + result.json
"""
import argparse
import shutil
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
ROLES_DIR = SKILL_DIR / "roles"
RUNS_ROOT = Path(os.environ.get("COTHINK_HOME", str(Path.home() / ".cothink"))) / "runs"

# Strings that mean an engine call failed even if the process exited 0.
HARD_ERRORS = (
    "not supported when using Codex",
    "invalid_request_error",
    "stream error",
    "Unauthorized",
    "401 Unauthorized",
)
# Lines of gemini stdout noise (from the broken shared-context MCP / refreshes) to strip.
GEMINI_NOISE = re.compile(
    r"(MCP issues detected|Scheduling MCP|Executing MCP|MCP context refresh|"
    r"StreamableHTTP|^\s*at .*\.js:|process\.processTicks|^\s*code:\s*\d+|^\s*\}\s*$|^\s*\{\s*$)"
)


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def log(run_dir, msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    if run_dir:
        try:
            with open(Path(run_dir) / "run.log", "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


def load_config():
    return json.loads((SKILL_DIR / "config.json").read_text())


def slugify(s):
    return (re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40]) or "run"


def render(template_name, **kw):
    t = (ROLES_DIR / template_name).read_text()
    for k, v in kw.items():
        t = t.replace("{{" + k + "}}", str(v))
    return t


def section(title, body):
    if not body:
        return ""
    return f"\n\n### {title}\n{body.strip()}\n"


def lint_brief(brief, run_dir):
    """Warn-only: brief.md is the contract every engine is handed verbatim."""
    found = {}
    for sec in ("Objective", "Success criteria", "Out of scope"):
        found[sec] = bool(re.search(rf"^##\s*{re.escape(sec)}\b", brief or "", re.M | re.I))
        if not found[sec]:
            log(run_dir, f"brief: WARNING missing section '## {sec}'")
    n = len(re.findall(r"^\s*\d+[.)]", section_text(brief, "Success criteria"), re.M))
    if n == 0:
        log(run_dir, "brief: WARNING no numbered success criteria found")
    return {"sections": found, "criteria_count": n}


def analyst_shape(out):
    """A usable Analyst report has a `## Criteria check` section (the one the driver parses)
    AND a verdict line. Level-3 headings are tolerated (kimi/gemini nest under a title)."""
    return bool(re.search(r"^#{2,3}\s*Criteria check\b", out or "", re.I | re.M)
                and re.search(r"VERDICT\s*[:=]\s*(PASS|FAIL)", out or "", re.I))


def tester_shape(out):
    return bool(re.search(r"RESULT\s*[:=]\s*(PASS|FAIL)", out or "", re.I))


# `BLOCKED: <reason>` as the first token of a line, after optional list decoration and a D<n>/T<n>
# id. One-shot engines bold/backtick markers and number lists, so all of that is tolerated; prose
# ("the Researcher wrote BLOCKED: ...") and topic-first lines ("- Prod schema: BLOCKED: ...") are not.
_MD = r"(?:\*\*|`)?"
BLOCKED_RE = re.compile(
    r"^[ \t]*(?:(?:[-*+]|\d+[.)])[ \t]*)?"            # bullet or numbered-list marker
    + rf"(?:{_MD}[DT]\d+{_MD}[ \t]*[:—–-]?[ \t]*)?"    # D<n>/T<n> id, optional `:` or dash
    + rf"{_MD}BLOCKED{_MD}:{_MD}\s*(.+?)\s*$", re.M | re.I)
# "BLOCKED: none", "(none)", "n/a — all resolved" are placeholders, not items
BLOCKED_NONE_RE = re.compile(r"^[(\[`]?\s*(?:none|n/?a|nothing)\s*(?:$|[)\]`.,;:(\[—–-])", re.I)


def blocked_items(text):
    """`BLOCKED: <reason>` lines — things no role in this run can do from inside the workspace."""
    seen, out = set(), []
    for b in BLOCKED_RE.findall(text or ""):
        if BLOCKED_NONE_RE.match(b.strip()):
            continue
        if b.lower() not in seen:
            seen.add(b.lower()); out.append(b)
    return out


def blocked_union(seed, *texts):
    """`seed` items first, then every BLOCKED: line from `texts`, deduped case-insensitively."""
    seen, out = set(), []
    for b in list(seed) + [b for tx in texts for b in blocked_items(tx)]:
        if b.lower() not in seen:
            seen.add(b.lower()); out.append(b)
    return out


def section_text(text, heading):
    """Body of the `## <heading>` (or `### <heading>`) section, up to the next heading of the same
    or higher level (level-1 `# ` lines are ignored: they occur inside code fences), or ""."""
    m = re.search(rf"^(#{{2,3}})\s*{re.escape(heading)}\b[^\n]*\n", text or "", re.M | re.I)
    if not m:
        return ""
    lvl = len(m.group(1))
    body = text[m.end():]
    e = re.search(rf"^#{{2,{lvl}}}\s", body, re.M)
    return body[:e.start()] if e else body


# A criteria-check line is classified by its LEADING mark (after list decoration / bold):
# MET, NOT MET, NOT VERIFIED (and common off-vocabulary spellings), BLOCKED.
MARK_RE = re.compile(r"^\s*(?:\d+[.)]|[-*+])?\s*(?:\*\*|`)?"
                     r"(MET|NOT MET|NOT VERIFIED|NOT VERIFIABLE|PARTIALLY MET|PARTLY MET|UNMET|UNVERIFIED|BLOCKED)\b", re.I)
OPEN_RE = re.compile(r"\b(?:NOT|PARTIALLY|PARTLY)\s+(?:MET|VERIFIED|VERIFIABLE)\b|\bUN(?:MET|VERIFIED)\b", re.I)


def criteria_marks(analyst_text):
    """Inside the Analyst's `## Criteria check`: the BLOCKED criterion lines, and the count of
    criteria that are still open (NOT MET / NOT VERIFIED / partial) without being BLOCKED — any
    open one keeps the loop going. Indented continuation lines are ignored; an unmarked criterion
    line that mentions an open status is counted as open (conservative: better one more
    iteration than a false `blocked`)."""
    not_met, blocked = 0, []
    for ln in section_text(analyst_text, "Criteria check").splitlines():
        if not ln.strip() or re.match(r"\s*VERDICT\b", ln, re.I):
            continue
        m = MARK_RE.match(ln)
        if m:
            k = m.group(1).upper()
            if k == "BLOCKED":
                blocked.append(ln.strip())
            elif k != "MET":
                not_met += 1
        elif not ln[:1].isspace() and OPEN_RE.search(ln):
            not_met += 1
    return {"not_met": not_met, "blocked": blocked}


CLAUDE_LIMIT_RE = re.compile(r"out of usage credits|usage limit|rate.?limit|hit your (session|usage|5.?hour|weekly) limit"
                             r"|too many requests|\b429\b|limit reached", re.I)


def log_stderr(msg):
    print(f"[cothink] {msg}", file=sys.stderr)


def normalize_headings(text):
    """grok's plain output glues streamed narration onto the report's first heading
    ("...then report.## Prior findings"); give every `## `/`### ` heading its own line."""
    return re.sub(r"(?<=[^\n])(?=#{2,3} [A-Z])", "\n", (text or "").strip())


def strip_gemini(text):
    keep = [ln for ln in (text or "").splitlines() if not GEMINI_NOISE.search(ln)]
    return "\n".join(keep).strip()


# --------------------------------------------------------------------------- #
# Engine invocation
# --------------------------------------------------------------------------- #
# Engine -> (dispatcher-first binary candidates, env override). run_engine and doctor resolve
# binaries through engine_binary() so the preflight checks exactly what a run would exec.
ENGINE_BINS = {
    "claude": (["claude-acct", "claude"], "COTHINK_CLAUDE_BIN"),
    "codex": (["codex-acct", "codex"], "COTHINK_CODEX_BIN"),
    "gemini": (["gemini"], None),   # the Antigravity CLI must be exposed as `gemini` (agy alone is not enough)
    "kimi": (["kimi"], None),
    "grok": (["grok"], None),
    "vibe": (["vibe"], None),
    "qwen": (["qwen"], None),
}
# what a dispatcher wraps, for --version probes that must not route/spend anything
DISPATCHER_UNDERLYING = {"claude-acct": ("CLAUDE_ACCT_BIN", "claude"), "codex-acct": ("CODEX_ACCT_BIN", "codex")}


def engine_binary(engine, env=os.environ, which=shutil.which):
    """argv[0] a run would exec for this engine: the env override if set, else the first candidate
    on PATH (dispatchers first), else the plain name (so a missing CLI fails visibly at exec)."""
    cands, env_key = ENGINE_BINS.get(engine, ([engine], None))
    if env_key and env.get(env_key):
        return env[env_key]
    for c in cands:
        if which(c):
            return c
    return cands[-1]


def _run(cmd, cwd, timeout, out_file=None):
    try:
        # Never inherit stdin: `codex exec` blocks forever ("Reading additional input from
        # stdin...") when it sees an open non-TTY pipe, e.g. when the driver runs in the background.
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True,
                           text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return "", False, f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return "", False, f"command not found: {e}"
    out = p.stdout or ""
    if out_file and Path(out_file).exists():
        fc = Path(out_file).read_text().strip()
        if fc:
            out = fc
    return out, (p.returncode == 0), (p.stderr or "")


def run_engine(engine, prompt, role_dir, workspace, mode, cfg, timeout):
    """mode: 'write' (may edit workspace) | 'read_only' | 'plan' (read-only design).

    Headless invocations verified 2026-08 against: gemini 1.1.x, kimi 1.49,
    codex-cli 0.144, grok 1.0, vibe 2.16, qwen 0.21. Each maps mode -> that CLI's own
    read-only/plan vs auto-approve/write flag, targets the workspace, and returns
    the model's final text on stdout (codex via --output-last-message).
    """
    models = cfg.get("models", {})
    role_dir = Path(role_dir)
    role_dir.mkdir(parents=True, exist_ok=True)
    is_write = (mode == "write")
    ws = str(workspace) if workspace else str(role_dir)

    if engine == "claude":
        # Claude as a middle-role engine. CoThink is always driven FROM Claude Code, so
        # Claude Code -> claude CLI is the allowed direction (never the reverse). Spread
        # across the Claude fleet via claude-acct when installed; COTHINK_CLAUDE_BIN overrides.
        # Isolation: no session written to the (shared) session store, no MCP servers, no
        # user/project settings or hooks leaking in from the conductor's own setup.
        # IS_SANDBOX=1 is the documented hatch that lets --dangerously-skip-permissions run as root.
        claude_bin = engine_binary("claude")
        cmd = ["env", "IS_SANDBOX=1", claude_bin, "-p", prompt, "--output-format", "text",
               "--no-session-persistence", "--strict-mcp-config", "--setting-sources", ""]
        if is_write:
            cmd += ["--dangerously-skip-permissions"]
        elif mode == "plan":
            cmd += ["--permission-mode", "plan", "--permission-prompts", "none"]
        else:  # read_only: read tools + read-only shell; anything that would prompt is denied
            cmd += ["--permission-mode", "dontAsk", "--permission-prompts", "none"]
        if models.get("claude"):
            cmd += ["--model", models["claude"]]  # pin FULL ids (claude-fable-5-1), not aliases
        if workspace:
            cmd += ["--add-dir", str(workspace)]
        if not is_write and mode != "plan":
            # read tools + shell; dontAsk denies anything that would prompt, so explicitly allow the
            # test runner (an Analyst that cannot run the suite can only guess). Variadic flags: last.
            cmd += ["--tools", "Read,Glob,Grep,Bash",
                    "--allowedTools", "Bash(pytest *)", "Bash(python3 -m pytest *)"]
        out, ok, err = _run(cmd, ws, timeout)
        if not ok and CLAUDE_LIMIT_RE.search((out or "") + (err or "")):
            # the account the dispatcher picked has no credits/quota for this model (a pinned Fable id
            # on a team seat, a spent 5h window): the dispatcher cools it down, so one retry lands elsewhere
            log_stderr(f"claude: limit/credits signature from the picked account — retrying once via {claude_bin}")
            out, ok, err = _run(cmd, ws, timeout)
        return out.strip(), ok, err

    if engine == "gemini":
        # Antigravity CLI (agy) flags. --print-timeout defaults to 5m, far below a role's budget.
        # Headless, the shell ("command") tool needs a permission nobody can grant, so a turn that
        # reaches for it is CANCELED with empty output. Write mode skips permissions (it needs to
        # run tests). Read-only mode must NOT: with permissions skipped, --mode plan happily writes
        # files (verified). So read-only calls keep plan mode and are told the shell is unavailable.
        cmd = ["gemini", "--output-format", "text", "--print-timeout", f"{int(timeout)}s",
               "--mode", "accept-edits" if is_write else "plan"]
        if is_write:
            cmd += ["--dangerously-skip-permissions"]
        else:
            prompt = ("NOTE: shell/terminal commands are NOT available in this session — use your file "
                      "read, grep/search and web tools only; never call a run-command tool.\n\n" + prompt)
        if models.get("gemini"):
            cmd += ["--model", models["gemini"]]
        if workspace:
            cmd += ["--add-dir", str(workspace)]
        cmd += ["-p", prompt]
        out, ok, err = _run(cmd, ws, timeout)
        return strip_gemini(out), ok, err

    if engine == "kimi":
        # In --plan mode kimi writes the full blueprint to ~/.kimi/plans/<name>.md and prints only a
        # short summary, so harvest any plan file created during this call and prefer it when longer.
        # (Caveat: --plan is porous in print mode — ExitPlanMode is auto-approved — so a plan/read_only
        # kimi role can still write into its cwd; keep read-only roles on engines with real deny rules.)
        plans_dir = Path.home() / ".kimi" / "plans"
        before = {p: p.stat().st_mtime for p in plans_dir.glob("*.md")} if plans_dir.is_dir() else {}
        t_start = time.time()
        cmd = ["kimi", "--quiet", "-w", ws, ("--yolo" if is_write else "--plan")]
        if models.get("kimi"):
            cmd += ["-m", models["kimi"]]
        cmd += ["-p", prompt]
        out, ok, err = _run(cmd, ws, timeout)
        out = out.strip()
        if not is_write and plans_dir.is_dir():
            new_plans = [p for p in plans_dir.glob("*.md")
                         if p.stat().st_mtime >= t_start - 1 and p.stat().st_mtime > before.get(p, 0)]
            if new_plans:
                plan_text = max(new_plans, key=lambda p: p.stat().st_mtime).read_text().strip()
                if len(plan_text) > len(out):
                    out = plan_text + ("\n\n---\n" + out if out else "")
        return out, ok, err

    if engine == "codex":
        out_file = role_dir / "_codex_last.txt"
        if out_file.exists():
            out_file.unlink()
        # Route through the codex-acct dispatcher when installed (spreads load across the
        # ChatGPT accounts); COTHINK_CODEX_BIN overrides; plain `codex` otherwise.
        codex_bin = engine_binary("codex")
        cmd = [codex_bin, "exec", "-C", ws, "--skip-git-repo-check",
               "-s", ("workspace-write" if is_write else "read-only")]
        effort = cfg.get("codex_reasoning_effort")
        if effort:  # per-call override; the accounts' own config.toml is untouched
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        if models.get("codex"):
            cmd += ["-m", models["codex"]]
        cmd += ["--output-last-message", str(out_file), prompt]
        out, ok, err = _run(cmd, ws, timeout, out_file=str(out_file))
        return out.strip(), ok, err

    if engine == "grok":
        # Headless grok has nobody to answer permission prompts: in the default "ask" mode (and in
        # dontAsk / plan modes) the turn is CANCELLED silently on the first gated tool call and only a
        # preamble comes back. So every mode auto-approves, and read_only/plan add deny rules: verified
        # that Write/Edit tools AND shell redirects (`echo x > f`) are refused ("deny rule on edit")
        # while shell reads (wc, find, cat) still run.
        cmd = ["grok", "-p", prompt, "--output-format", "plain",
               "--no-alt-screen", "--cwd", ws, "--no-memory", "--always-approve"]
        if not is_write:
            cmd += ["--deny", "Write", "--deny", "Edit"]
        if models.get("grok"):
            cmd += ["-m", models["grok"]]
        out, ok, err = _run(cmd, ws, timeout)
        return normalize_headings(out), ok, err

    if engine == "vibe":
        # vibe has no --model flag (its model lives in ~/.vibe/config.toml); models.vibe is ignored.
        # It is the only pay-per-token engine with a hard spend cap, so apply one per call.
        cmd = ["vibe", "-p", prompt, "--output", "text", "--workdir", ws,
               *(["--auto-approve"] if is_write else ["--agent", "plan"])]
        max_price = cfg.get("vibe_max_price_usd")
        if max_price:
            cmd += ["--max-price", str(max_price)]
        out, ok, err = _run(cmd, ws, timeout)
        return out.strip(), ok, err

    if engine == "qwen":
        # Qwen Code (Gemini-CLI fork). v0.21 flags: -o text, --approval-mode {yolo|plan},
        # -m model; cwd (ws) is the working dir. Auth via ~/.qwen/.env (OpenAI-compatible).
        cmd = ["qwen", "-o", "text",
               "--approval-mode", "yolo" if is_write else "plan"]
        if models.get("qwen"):
            cmd += ["-m", models["qwen"]]
        cmd += ["-p", prompt]
        out, ok, err = _run(cmd, ws, timeout)
        return out.strip(), ok, err

    return f"[unknown engine: {engine}]", False, "unknown engine"


def _looks_failed(out, ok):
    if not ok:
        return True
    if not (out or "").strip():
        return True
    return any(m in out for m in HARD_ERRORS)


def _family(cfg, engine):
    """Model family of an engine (config.families), used for the independence guard."""
    return (cfg.get("families") or {}).get(engine, engine)


def role_chain(cfg, role):
    """Ordered engine list for a role: its primary, then its own fallback chain
    (roles.<role>.fallbacks). Legacy per-engine tables (fallbacks.<engine>) still work
    for configs that predate per-role chains."""
    rc = cfg["roles"][role]
    pref = rc["engine"]
    fb = rc.get("fallbacks")
    if fb is None:
        fb = cfg.get("fallbacks", {}).get(pref, [])
    if isinstance(fb, str):  # accept a single engine or an ordered chain
        fb = [fb]
    order = [pref]
    for e in fb:
        if e and e not in order:
            order.append(e)
    return order


def run_role(role, prompt, role_dir, workspace, mode, cfg, timeout, run_dir,
             exclude_families=(), events=None, accept=None):
    """Run a role on its configured engine, falling back down its chain if it fails.

    mode comes from roles.<role>.mode when set (the caller's value is the default).
    exclude_families: model families that must not play this role in this run — the
    Analyst must never share a family with whatever actually wrote the code. Excluded
    engines are skipped and logged; if nothing independent is left the run proceeds on an
    excluded engine with a loud warning (recorded in `events`) rather than dying.
    accept: optional shape check on the output; a report that fails it (e.g. a kimi plan-mode
    summary with no VERDICT line) is treated as an engine failure and falls through the chain.
    Returns (output, engine_that_ran) — engine is "none" if every engine failed.
    """
    rc = cfg["roles"][role]
    pref = rc["engine"]
    mode = rc.get("mode") or mode
    order = role_chain(cfg, role)
    excluded = [e for e in order if exclude_families and _family(cfg, e) in exclude_families]
    independent = [e for e in order if e not in excluded]
    for e in excluded:
        log(run_dir, f"{role}: skipping {e} — same model family ({_family(cfg, e)}) as the code writer")
    last_err = ""
    malformed = None  # first shape-failed report (highest-preference engine), kept as a last resort
    for tier, engines in (("independent", independent), ("violation", excluded)):
        for eng in engines:
            if tier == "violation":
                log(run_dir, f"{role}: WARNING no independent engine left; running {eng} despite family overlap")
                if events is not None:
                    events.append({"role": role, "engine": eng, "event": "family_guard_violation"})
            log(run_dir, f"{role}: {eng} (mode={mode}) ...")
            out, ok, err = run_engine(eng, prompt, role_dir, workspace, mode, cfg, timeout)
            banner = ""
            if eng != pref:
                banner += f"_[CoThink fallback: {pref} unavailable, ran on {eng}]_\n\n"
            if tier == "violation":
                banner += (f"_[CoThink WARNING: {role} ran on {eng}, the same model family as the "
                           f"code writer — this verdict is not independent]_\n\n")
            failed = _looks_failed(out, ok)
            if not failed and accept is not None and not accept(out):
                failed, err = True, "malformed report: missing required section or verdict line"
                if events is not None:
                    events.append({"role": role, "engine": eng, "event": "malformed_report"})
                if malformed is None:
                    malformed = (banner + out, eng)
            if not failed:
                log(run_dir, f"{role}: {eng} done ({len(out)} chars)")
                return banner + out, eng
            # keep both streams: dispatchers print only a routing line on stderr while the CLI's
            # real error (e.g. "out of usage credits") lands on stdout
            last_err = " | ".join(x.strip() for x in (err, out) if x and x.strip())[:300]
            log(run_dir, f"{role}: {eng} FAILED -> {last_err!r}")
    if malformed is not None:  # a real report with the wrong headings beats a failure banner
        out, eng = malformed
        log(run_dir, f"{role}: all engines failed; keeping {eng}'s malformed report as a last resort")
        return (f"_[CoThink WARNING: {role} report from {eng} failed the shape check "
                f"(missing required section or verdict line); kept as a last resort]_\n\n" + out), eng
    return (f"[CoThink] {role} failed on all engines ({', '.join(order)}). "
            f"Last error: {last_err}"), "none"


def family_overlap_events(cfg, engines_used, it):
    """Correlated-verdict notes for one iteration: the Tester sharing a model family with the
    Analyst, or with whoever actually wrote the code (Coder/Fixer). Soft losses — logged, never fatal."""
    ev = []
    t = engines_used.get("tester")
    if not t or t == "none":
        return ev
    tf = _family(cfg, t)
    a = engines_used.get("analyst")
    if a and a != "none" and _family(cfg, a) == tf:
        ev.append({"role": "tester", "engine": t, "event": "analyst_tester_same_family", "iter": it})
    writers = [e for e in (engines_used.get("coder"), engines_used.get("fixer")) if e and e != "none"]
    if any(_family(cfg, w) == tf for w in writers):
        ev.append({"role": "tester", "engine": t, "event": "writer_tester_same_family", "iter": it})
    return ev


def verdict(text, key):
    m = re.findall(rf"{key}\s*[:=]\s*(PASS|FAIL)", text or "", re.I)
    return m[-1].upper() if m else "FAIL"  # default FAIL => loop continues (conservative)


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# doctor — preflight
# --------------------------------------------------------------------------- #
MODES = ("read_only", "plan", "write")


def doctor_report(cfg, which=shutil.which, run=subprocess.run, env=os.environ):
    """Preflight without spending tokens: every engine the config can reach (primaries and
    per-role chains) is on PATH and answers --version; model pins look sane; the role map keeps
    the builder/validator families apart. Returns a dict; `ok` is False on anything that would
    break or silently weaken a run (a missing PRIMARY, an unknown engine, a family collision)."""
    roles = cfg.get("roles", {})
    families = cfg.get("families", {})
    models = cfg.get("models", {})
    problems, warnings = [], []

    def chain(role):
        try:
            return role_chain(cfg, role)
        except KeyError:
            return []

    needed = {}
    for role, rc in roles.items():
        for i, eng in enumerate(chain(role)):
            needed.setdefault(eng, set()).add(("primary" if i == 0 else "fallback", role))

    engines = []
    for eng in sorted(needed):
        binary = engine_binary(eng, env=env, which=which)   # exactly what run_engine would exec
        path = which(binary)
        detail = None
        if path:
            # probe the CLI itself, never through a dispatcher (that would route and bump its state)
            probe_bin, via = binary, ""
            if os.path.basename(binary) in DISPATCHER_UNDERLYING:
                env_key, default = DISPATCHER_UNDERLYING[os.path.basename(binary)]
                probe_bin, via = env.get(env_key, default), f" via {os.path.basename(binary)}"
            try:
                pr = run([probe_bin, "--version"], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
                first = ((pr.stdout or pr.stderr or "").strip().splitlines() or ["found"])[0][:60]
                detail = (first if pr.returncode == 0 else f"exit {pr.returncode}: {first}") + via
            except (subprocess.TimeoutExpired, OSError) as e:
                detail = f"probe failed: {e}{via}"
        uses = sorted(needed[eng])
        is_primary = any(k == "primary" for k, _ in uses)
        status = "OK" if path else "MISSING"
        if not path:
            cands = ENGINE_BINS.get(eng, ([eng], None))[0]
            hint = " (agy is installed — expose it as `gemini`, e.g. a shim or symlink)" if eng == "gemini" and which("agy") else ""
            (problems if is_primary else warnings).append(
                f"{eng}: not on PATH (tried {', '.join(cands)}){hint} — "
                + ("primary for " + ", ".join(r for k, r in uses if k == "primary") if is_primary
                   else "fallback only (" + ", ".join(r for _, r in uses) + ")"))
        if eng not in families:
            problems.append(f"{eng}: referenced by roles but missing from config.families")
        if eng not in ENGINE_BINS:
            problems.append(f"{eng}: unknown engine (no run_engine branch)")
        engines.append({"engine": eng, "binary": path or binary, "status": status, "detail": detail or "",
                        "model": models.get(eng) or "(CLI default)", "family": families.get(eng, "?"),
                        "roles": [f"{r} ({k})" for k, r in uses]})

    # every chain needs at least one installed engine
    for role in roles:
        ch = chain(role)
        if ch and not any(e["status"] == "OK" for e in engines if e["engine"] in ch):
            problems.append(f"{role}: no engine in its chain is installed ({', '.join(ch)})")
    for role, rc in roles.items():
        if rc.get("mode") not in MODES:
            problems.append(f"{role}: mode {rc.get('mode')!r} is not one of {MODES}")

    # family independence: no writer's family (Coder or Fixer primary) may sit in a judgment seat
    fam = lambda e: families.get(e, e)
    writers = {roles.get(r, {}).get("engine") for r in ("coder", "fixer")} - {None}
    for role in ("analyst", "tester"):
        for w in sorted(writers):
            same = [e for e in chain(role) if fam(e) == fam(w)]
            if same:
                primary_hit = roles.get(role, {}).get("engine") in same
                (problems if primary_hit else warnings).append(
                    f"{role}: chain contains {', '.join(same)} — same family ({fam(w)}) as the {'Coder' if w == roles.get('coder', {}).get('engine') else 'Fixer'} ({w})"
                    + ("" if primary_hit else " (fallback only; the runtime guard skips it)"))

    # model pins
    cm = models.get("claude") or ""
    if cm and not re.match(r"^claude-[a-z]+-\d", cm):
        warnings.append(f"models.claude={cm!r} looks like an alias — aliases resolve inconsistently across accounts; pin a full id")
    if (models.get("codex") or "").endswith("-codex"):
        warnings.append("models.codex ends with -codex: ChatGPT-account auth rejects those ids")
    if models.get("vibe"):
        warnings.append("models.vibe is ignored (vibe has no --model flag; set it in ~/.vibe/config.toml)")

    return {
        "ok": not problems, "problems": problems, "warnings": warnings, "engines": engines,
        "roles": {r: {"engine": rc.get("engine"), "mode": rc.get("mode"), "chain": chain(r)} for r, rc in roles.items()},
        "settings": {"max_iters": cfg.get("max_iters"), "timeout_seconds": cfg.get("timeout_seconds"),
                     "stop_when_blocked": cfg.get("stop_when_blocked", True),
                     "codex_reasoning_effort": cfg.get("codex_reasoning_effort"),
                     "vibe_max_price_usd": cfg.get("vibe_max_price_usd"),
                     "durable_memory": bool(cfg.get("durable_memory", {}).get("enabled"))},
        "python": sys.version.split()[0],
        "skill_dir": str(SKILL_DIR),
    }


RUN_ROLES = (("r", "researcher"), ("a", "architect"), ("c", "coder"),
             ("an", "analyst"), ("f", "fixer"), ("t", "tester"))
RUNS_HEADER = "RUN ID  STATUS  ITERS  ENGINES  GUARD  BLOCKED"


def list_runs(root, last=20, status=None):
    """Return run rows newest-first, with result=None for missing result files."""
    if not root.is_dir():
        return []
    dirs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    rows = []
    for p in dirs:
        rp = p / "result.json"
        if not rp.is_file():
            result = None
        else:
            try:
                result = json.loads(rp.read_text())
                if not isinstance(result, dict):
                    raise ValueError("not a JSON object")
            except (ValueError, OSError) as e:
                log_stderr(f"runs: skipping {p.name}: result.json is not valid JSON ({e})")
                continue
        if status is not None and (result is None or result.get("status") != status):
            continue
        rows.append({"run_id": p.name, "result": result})
    return rows[:last] if last is not None and last >= 0 else rows


def format_run_row(row):
    """Return a table line with cells separated by two spaces, without padding."""
    result = row["result"]
    data = result if result is not None else {}
    engines_used = data.get("engines_used") or {}
    engines = " ".join(f"{k}={engines_used.get(role) or '-'}" for k, role in RUN_ROLES)
    return "  ".join((row["run_id"], str(data.get("status", "-")),
                      str(data.get("iterations", "-")), engines,
                      str(len(data.get("guard_events") or [])) if result is not None else "-",
                      str(len(data.get("blocked") or [])) if result is not None else "-"))


def cmd_runs(args):
    """List past runs from RUNS_ROOT."""
    rows = list_runs(RUNS_ROOT, args.last, args.status)
    if getattr(args, "json", False):
        print(json.dumps([row["result"] for row in rows if row["result"] is not None], indent=2))
        return
    print(RUNS_HEADER)
    for row in rows:
        print(format_run_row(row))


def cmd_doctor(args):
    rep = doctor_report(load_config())
    if getattr(args, "json", False):
        print(json.dumps(rep, indent=2))
        sys.exit(0 if rep["ok"] else 1)
    print(f"CoThink doctor — preflight for {rep['skill_dir']} (python {rep['python']})")
    print("Conductor: Claude Code plays Strategist + Executor; the engines below play roles 2-7.\n")
    w = max(len(e["binary"]) for e in rep["engines"]) if rep["engines"] else 8
    print(f"{'ENGINE':<8} {'CLI':<{w}} {'STATUS':<8} {'FAMILY':<10} {'MODEL':<26} DETAIL / ROLES")
    for e in rep["engines"]:
        print(f"{e['engine']:<8} {e['binary']:<{w}} {e['status']:<8} {e['family']:<10} {e['model'][:26]:<26} "
              f"{e['detail']}  [{', '.join(e['roles'])}]")
    print("\nRoles:")
    for r, spec in rep["roles"].items():
        print(f"  {r:<11} {spec['engine']} ({spec['mode']})  chain: {' -> '.join(spec['chain'])}")
    s = rep["settings"]
    print(f"\nSettings: max_iters={s['max_iters']} timeout={s['timeout_seconds']}s stop_when_blocked={s['stop_when_blocked']} "
          f"codex_effort={s['codex_reasoning_effort']} vibe_cap=${s['vibe_max_price_usd']} durable_memory={'on' if s['durable_memory'] else 'off'}")
    for msg in rep["warnings"]:
        print(f"WARN  {msg}")
    for msg in rep["problems"]:
        print(f"FAIL  {msg}")
    if rep["ok"]:
        print("\nOK — every primary engine is installed and the role map keeps builder and judges apart. "
              "(Auth is only proven by a live call; a fallback chain covers an installed-but-logged-out CLI.)")
    else:
        print("\nFix the FAIL lines in config.json or install the missing CLIs before running.")
        sys.exit(1)


def cmd_init(args):
    rid = time.strftime("%Y%m%d-%H%M%S") + "-" + slugify(args.title)
    rd = RUNS_ROOT / rid
    (rd / "workspace").mkdir(parents=True, exist_ok=True)
    info = {
        "run_id": rid,
        "run_dir": str(rd),
        "workspace": str(rd / "workspace"),
        "brief_path": str(rd / "brief.md"),
    }
    print(json.dumps(info, indent=2))


def maybe_log_durable(cfg, run_dir, result, brief):
    dm = cfg.get("durable_memory", {})
    if not dm.get("enabled"):
        return
    token = os.environ.get(dm.get("token_env", ""), "")
    if not token:
        log(run_dir, "durable memory enabled but token env is empty; skipping")
        return
    try:
        sys.path.insert(0, str(SKILL_DIR / "lib"))
        from context_client import ContextClient
        c = ContextClient(dm["base_url"], token)
        pid = dm.get("project_id", "cothink")
        c.ensure_project(pid, "CoThink Runs", "Durable memory for CoThink methodology runs")
        c.log_session(pid, f"CoThink run {result['run_id']}: {result['status']} "
                            f"after {result['iterations']} iter(s).")
        log(run_dir, "durable memory: session logged")
    except Exception as e:  # best-effort only
        log(run_dir, f"durable memory log failed (non-fatal): {e}")


def cmd_run(args):
    cfg = load_config()
    run_dir = Path(args.run_dir).resolve()
    if not (run_dir / "brief.md").exists():
        sys.exit(f"ERROR: {run_dir}/brief.md not found. The Strategist (Claude) must write it first.")
    workspace = Path(args.workspace).resolve() if args.workspace else (run_dir / "workspace")
    workspace.mkdir(parents=True, exist_ok=True)
    brief = (run_dir / "brief.md").read_text()
    timeout = int(cfg.get("timeout_seconds", 1800))
    max_iters = int(cfg.get("max_iters", 3))
    engines_used = {}
    events = []  # independence-guard events, surfaced in result.json

    def writer_families():
        return {_family(cfg, e) for e in (engines_used.get("coder"), engines_used.get("fixer"))
                if e and e != "none"}

    log(run_dir, f"=== CoThink run start: {run_dir.name} (workspace={workspace}) ===")
    brief_lint = lint_brief(brief, run_dir)
    blocked = []

    def finish(status, it, hist, converged=False):
        result = {
            "run_id": run_dir.name,
            "status": status,
            "converged": converged,
            "iterations": it,
            "max_iters": max_iters,
            "history": hist,
            "engines_used": engines_used,
            "guard_events": events,
            "blocked": blocked,
            "brief_lint": brief_lint,
            "workspace": str(workspace),
            "run_dir": str(run_dir),
            "artifacts": {
                "brief": str(run_dir / "brief.md"),
                "researcher": str(run_dir / "02-researcher.md"),
                "architect": str(run_dir / "03-architect.md"),
                "coder": str(run_dir / "04-coder.md"),
                "final_iter": str(run_dir / f"iter-{it}") if it else None,
            },
        }
        (run_dir / "result.json").write_text(json.dumps(result, indent=2))
        maybe_log_durable(cfg, run_dir, result, brief)
        log(run_dir, f"=== CoThink run done: {result['status']} after {it} iter(s) ===")
        print(json.dumps(result, indent=2))
        return result

    # Role 2 — Researcher
    research, engines_used["researcher"] = run_role(
        "researcher",
        render("researcher.md", BRIEF=brief, WORKSPACE=workspace, PRIOR=""),
        run_dir / "02", workspace, "read_only", cfg, timeout, run_dir)
    (run_dir / "02-researcher.md").write_text(research)

    # Role 3 — Architect
    arch, engines_used["architect"] = run_role(
        "architect",
        render("architect.md", BRIEF=brief, WORKSPACE=workspace,
               PRIOR=section("Research / Fact Base", research)),
        run_dir / "03", workspace, "read_only", cfg, timeout, run_dir)
    (run_dir / "03-architect.md").write_text(arch)

    # Halt before the Coder on anything the Architect could not design around: every BLOCKED
    # criterion in the logs (prod schema, read-only git, no Postgres) burned all 3 iterations.
    # Only the `## Decisions` section counts — a Researcher line quoted elsewhere does not.
    arch_blocked = blocked_items(section_text(arch, "Decisions"))
    blocked = list(arch_blocked)
    for b in arch_blocked:
        log(run_dir, f"architect: BLOCKED {b}")

    def all_blocked(analyst_marks, fixer_text, tester_text):
        """Architect BLOCKED decisions, then the Analyst's BLOCKED criteria lines, then the Fixer's
        `## Not fixed` and the Tester's `BLOCKED:` reasons — one case-insensitive dedupe."""
        return blocked_union(arch_blocked + list(analyst_marks),
                             section_text(fixer_text, "Not fixed"), tester_text)

    if arch_blocked and not getattr(args, "no_halt_on_blocked", False):
        log(run_dir, f"halting before the Coder: {len(arch_blocked)} BLOCKED item(s) need the operator")
        return finish("blocked", 0, [])

    # Role 4 — Coder
    coder, engines_used["coder"] = run_role(
        "coder",
        render("coder.md", BRIEF=brief, WORKSPACE=workspace,
               PRIOR=section("Research", research) + section("Architecture / Blueprint", arch)),
        run_dir / "04", workspace, "write", cfg, timeout, run_dir)
    (run_dir / "04-coder.md").write_text(coder)

    # Roles 5-7 — Analyst -> Fixer -> Tester loop
    history = []
    prev_tester, prev_tester_pass = None, False
    prev_analyst, prev_fixer = None, None
    analyst = fixer = tester = None  # stay bound when max_iters <= 0 skips the loop
    analyst_pass = tester_pass = False
    stalled = False
    it = 0
    while it < max_iters:
        it += 1
        itdir = run_dir / f"iter-{it}"
        itdir.mkdir(exist_ok=True)
        log(run_dir, f"--- iteration {it}/{max_iters} ---")

        # Analyst (read-only)
        # Fixed point: the previous Analyst report + the Fixer changelog since it, so findings
        # keep their IDs and converge instead of being re-discovered from scratch every iteration.
        coder_title = ("Coder report (iteration 0 — superseded by the Fixer changelog below)"
                       if prev_fixer else "Coder report")
        a_prior = (section("Architecture", arch) + section(coder_title, coder)
                   + (section("Previous Analyst findings", prev_analyst) if prev_analyst else "")
                   + (section("Latest Fixer changelog", prev_fixer) if prev_fixer else "")
                   + (section("Previous Tester findings", prev_tester) if prev_tester else ""))
        analyst, engines_used["analyst"] = run_role(
            "analyst", render("analyst.md", BRIEF=brief, WORKSPACE=workspace, PRIOR=a_prior),
            itdir / "05", workspace, "read_only", cfg, timeout, run_dir,
            exclude_families=writer_families(), events=events, accept=analyst_shape)
        (itdir / "05-analyst.md").write_text(analyst)
        # a report kept despite failing the shape check can never count as PASS
        analyst_pass = analyst_shape(analyst) and verdict(analyst, "VERDICT") == "PASS"

        # Fixer (only if there is something to fix)
        needs_fix = (not analyst_pass) or (prev_tester is not None and not prev_tester_pass)
        fixer = None
        if needs_fix:
            f_prior = (section("Analyst findings", analyst)
                       + (section("Tester findings", prev_tester) if prev_tester else "")
                       + (section("Previous Fixer changelog", prev_fixer) if prev_fixer else ""))
            fixer, engines_used["fixer"] = run_role(
                "fixer", render("fixer.md", BRIEF=brief, WORKSPACE=workspace, PRIOR=f_prior),
                itdir / "06", workspace, "write", cfg, timeout, run_dir)
            (itdir / "06-fixer.md").write_text(fixer)

        # Tester (executes; may add scaffolding)
        t_prior = (section("Architecture", arch) + section("Latest Analyst findings", analyst)
                   + (section("Fixer changelog this iteration", fixer) if fixer else ""))
        tester, engines_used["tester"] = run_role(
            "tester", render("tester.md", BRIEF=brief, WORKSPACE=workspace, PRIOR=t_prior),
            itdir / "07", workspace, "write", cfg, timeout, run_dir, events=events, accept=tester_shape)
        (itdir / "07-tester.md").write_text(tester)
        tester_pass = verdict(tester, "RESULT") == "PASS"
        for ev in family_overlap_events(cfg, engines_used, it):
            log(run_dir, f"iter {it}: NOTE {ev['event'].replace('_', ' ')} ({ev['engine']}) — "
                         f"the verdicts are correlated, not independent")
            events.append(ev)

        prev_tester, prev_tester_pass = tester, tester_pass
        prev_analyst = analyst
        if fixer:
            prev_fixer = fixer  # last changelog that actually ran; survives Fixer-less iterations
        history.append({"iter": it, "analyst_pass": analyst_pass,
                        "tester_pass": tester_pass, "fixer_ran": needs_fix})
        log(run_dir, f"iter {it}: analyst={'PASS' if analyst_pass else 'FAIL'} "
                     f"tester={'PASS' if tester_pass else 'FAIL'}")
        if analyst_pass and tester_pass:
            break
        # Stall stop: two families agree that every remaining failure is environment-blocked
        # (Analyst: zero NOT MET / NOT VERIFIED and >=1 BLOCKED criterion; Fixer: a `BLOCKED:`
        # line under `## Not fixed` — not under `## Fixed`, where it would describe an unblocking).
        marks = criteria_marks(analyst)
        if (marks["blocked"] and marks["not_met"] == 0 and fixer
                and blocked_items(section_text(fixer, "Not fixed")) and cfg.get("stop_when_blocked", True)):
            blocked = all_blocked(marks["blocked"], fixer, tester)
            stalled = True
            log(run_dir, f"iter {it}: every remaining failure is BLOCKED by the environment — stopping early")
            break

    converged = analyst_pass and tester_pass
    if not converged and not stalled:
        blocked = all_blocked(criteria_marks(analyst)["blocked"], fixer, tester)
    finish("passed" if converged else ("blocked" if stalled else "max_iters_reached"),
           it, history, converged)


def main():
    ap = argparse.ArgumentParser(prog="cothink", description="CoThink 8-role orchestration driver")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="create a run dir and print its paths")
    pi.add_argument("--title", default="run", help="short title for the run")
    pi.set_defaults(func=cmd_init)

    pr = sub.add_parser("run", help="run roles 2-7 over a run dir containing brief.md")
    pr.add_argument("--run-dir", required=True)
    pr.add_argument("--workspace", default="", help="defaults to <run-dir>/workspace")
    pr.add_argument("--no-halt-on-blocked", action="store_true",
                    help="record the Architect's BLOCKED items in result.json but keep building")
    pr.set_defaults(func=cmd_run)

    pd = sub.add_parser("doctor", help="preflight: engine CLIs on PATH, model pins, role map, config invariants")
    pd.add_argument("--json", action="store_true", help="machine-readable report")
    pd.set_defaults(func=cmd_doctor)

    prs = sub.add_parser("runs", help="list past runs from $COTHINK_HOME/runs (newest first)")
    prs.add_argument("--last", type=int, default=20, help="show at most N runs (default 20)")
    prs.add_argument("--status", choices=["passed", "max_iters_reached", "blocked"],
                     help="only runs with this status (runs without result.json are excluded)")
    prs.add_argument("--json", action="store_true", help="JSON array of result.json objects, newest first")
    prs.set_defaults(func=cmd_runs)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
