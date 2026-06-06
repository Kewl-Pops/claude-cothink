#!/usr/bin/env python3
"""CoThink driver — orchestrates the 8-role methodology across Claude + Gemini + Kimi + Codex.

Claude (the conductor) plays role 1 (Strategist) and role 8 (Executor) natively and calls
this driver to run roles 2-7 (Researcher, Architect, Coder, then the Analyst -> Fixer ->
Tester loop). See SKILL.md for how the conductor drives this.

Engines (validated headless commands):
  gemini : gemini -p <prompt> -o text --approval-mode {plan|yolo} [--include-directories WS]
  kimi   : kimi-cli --quiet -w <dir> [--plan] -p <prompt]      (--quiet => clean final message)
  codex  : codex exec -C <ws> --skip-git-repo-check -m gpt-5.4
           {--full-auto | -s read-only} --output-last-message <file> <prompt>
           (NOTE: *-codex models are rejected on ChatGPT-account auth; use a plain chat model.)

Subcommands:
  init   --title "..."                  -> create a run dir, print JSON {run_id, run_dir, ...}
  run    --run-dir DIR [--workspace WS] -> run roles 2-7; writes artifacts + result.json
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Engine -> the CLI binary it needs on PATH, and a cheap presence/version probe.
ENGINE_CLI = {
    "gemini": (["gemini", "--version"]),
    "kimi": (["kimi-cli", "--help"]),
    "codex": (["codex", "--version"]),
}
ENGINE_BIN = {"gemini": "gemini", "kimi": "kimi-cli", "codex": "codex"}

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


def strip_gemini(text):
    keep = [ln for ln in (text or "").splitlines() if not GEMINI_NOISE.search(ln)]
    return "\n".join(keep).strip()


# --------------------------------------------------------------------------- #
# Engine invocation
# --------------------------------------------------------------------------- #
def _run(cmd, cwd, timeout, out_file=None):
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True,
                           text=True, timeout=timeout)
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
    """mode: 'write' (may edit workspace) | 'read_only' | 'plan' (read-only design)."""
    models = cfg.get("models", {})
    role_dir = Path(role_dir)
    role_dir.mkdir(parents=True, exist_ok=True)
    is_write = (mode == "write")

    if engine == "gemini":
        cmd = ["gemini", "-o", "text",
               "--approval-mode", "yolo" if is_write else "plan"]
        if models.get("gemini"):
            cmd += ["-m", models["gemini"]]
        if workspace and not is_write:
            cmd += ["--include-directories", str(workspace)]
        cmd += ["-p", prompt]
        cwd = workspace if (workspace and not is_write) else role_dir
        out, ok, err = _run(cmd, cwd, timeout)
        return strip_gemini(out), ok, err

    if engine == "kimi":
        wdir = str(workspace) if workspace else str(role_dir)
        cmd = ["kimi-cli", "--quiet", "-w", wdir]
        if not is_write:
            cmd += ["--plan"]
        if models.get("kimi"):
            cmd += ["-m", models["kimi"]]
        cmd += ["-p", prompt]
        out, ok, err = _run(cmd, wdir, timeout)
        return out.strip(), ok, err

    if engine == "codex":
        ws = str(workspace) if workspace else str(role_dir)
        out_file = role_dir / "_codex_last.txt"
        if out_file.exists():
            out_file.unlink()
        cmd = ["codex", "exec", "-C", ws, "--skip-git-repo-check"]
        if models.get("codex"):
            cmd += ["-m", models["codex"]]
        cmd += (["--full-auto"] if is_write else ["-s", "read-only"])
        cmd += ["--output-last-message", str(out_file), prompt]
        out, ok, err = _run(cmd, ws, timeout, out_file=str(out_file))
        return out.strip(), ok, err

    return f"[unknown engine: {engine}]", False, "unknown engine"


def _looks_failed(out, ok):
    if not ok:
        return True
    if not (out or "").strip():
        return True
    return any(m in out for m in HARD_ERRORS)


def run_role(role, prompt, role_dir, workspace, mode, cfg, timeout, run_dir):
    """Run a role on its configured engine, falling back if it fails."""
    pref = cfg["roles"][role]["engine"]
    fb = cfg.get("fallbacks", {}).get(pref)
    order = [pref] + ([fb] if fb and fb != pref else [])
    last_err = ""
    for eng in order:
        log(run_dir, f"{role}: {eng} (mode={mode}) ...")
        out, ok, err = run_engine(eng, prompt, role_dir, workspace, mode, cfg, timeout)
        if not _looks_failed(out, ok):
            if eng != pref:
                out = f"_[CoThink fallback: {pref} unavailable, ran on {eng}]_\n\n" + out
            log(run_dir, f"{role}: {eng} done ({len(out)} chars)")
            return out, eng
        last_err = (err or out or "")[:300]
        log(run_dir, f"{role}: {eng} FAILED -> {last_err!r}")
    return (f"[CoThink] {role} failed on all engines ({', '.join(order)}). "
            f"Last error: {last_err}"), "none"


# Matches fenced ```json ... ``` blocks (authoritative machine-readable verdict).
_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S | re.I)


def extract_verdict(text, key):
    """Read a role's PASS/FAIL signal. Returns 'PASS', 'FAIL', or None (not found).

    Resolution order (most authoritative first):
      1. The LAST fenced ```json {"<key>": "pass"|"fail"} block.
      2. A bare trailing JSON object containing <key>.
      3. The legacy `KEY: PASS/FAIL` line (last match).
    A None return means the role gave no parseable verdict -- the caller re-prompts
    for one rather than silently guessing. Prose that merely *mentions* the legacy
    line never overrides an explicit JSON block, because the JSON is checked first.
    """
    text = text or ""
    k = key.lower()

    # 1 + 2: JSON (fenced blocks first, then any bare object), last wins.
    candidates = _JSON_FENCE.findall(text)
    if not candidates:
        candidates = re.findall(r"\{[^{}]*\}", text)
    for blob in reversed(candidates):
        try:
            obj = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            val = next((v for kk, v in obj.items() if kk.lower() == k), None)
            if isinstance(val, str) and val.strip().upper() in ("PASS", "FAIL"):
                return val.strip().upper()

    # 3: legacy line.
    m = re.findall(rf"{key}\s*[:=]\s*(PASS|FAIL)", text, re.I)
    return m[-1].upper() if m else None


def verdict(text, key):
    """Back-compat wrapper: a missing verdict is treated as FAIL (loop continues)."""
    v = extract_verdict(text, key)
    return v if v is not None else "FAIL"


def resolve_verdict(role, key, out, engine, role_dir, workspace, cfg, timeout, run_dir):
    """Get a role's PASS/FAIL. If the role emitted no parseable verdict, re-prompt
    the SAME engine once for a single machine-readable JSON line instead of guessing.
    """
    v = extract_verdict(out, key)
    if v is not None:
        return v == "PASS"
    if engine == "none":  # the role itself failed on all engines; stay conservative
        log(run_dir, f"{role}: no verdict and engine unavailable -> FAIL")
        return False
    log(run_dir, f"{role}: no parseable {key} verdict -> re-prompting {engine} for it")
    prompt = (
        "Below is your prior analysis. Do not redo the work. Output ONLY a single "
        f'fenced JSON code block with your final verdict, exactly:\n```json\n'
        f'{{\"{key.lower()}\": \"pass\"}}\n```\nor\n```json\n'
        f'{{\"{key.lower()}\": \"fail\"}}\n```\n\n--- prior analysis ---\n' + (out or "")
    )
    conf, ok, err = run_engine(engine, prompt, Path(role_dir) / "verdict", workspace,
                               "read_only", cfg, min(timeout, 300))
    v = extract_verdict(conf, key)
    if v is None:
        log(run_dir, f"{role}: re-prompt still gave no {key} verdict -> FAIL (conservative)")
        return False
    log(run_dir, f"{role}: re-prompt verdict={v}")
    return v == "PASS"


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_doctor(args):
    """Preflight: check that every CLI the config relies on is installed, before a run
    discovers it broken mid-chain. Auth itself is only fully proven by a real call, so
    this checks presence + a cheap probe and reports the engine map and fallbacks.
    """
    cfg = load_config()
    roles = cfg.get("roles", {})
    fallbacks = cfg.get("fallbacks", {})
    models = cfg.get("models", {})

    # Engines actually reachable given the config (primaries + their fallbacks).
    needed = set()
    for r in roles.values():
        eng = r.get("engine")
        if eng:
            needed.add(eng)
            fb = fallbacks.get(eng)
            if fb:
                needed.add(fb)

    print("CoThink doctor — environment preflight\n")
    print("Conductor: Claude Code (plays Strategist + Executor) — run this inside Claude Code.\n")

    rows, all_ok = [], True
    for eng in sorted(needed):
        binary = ENGINE_BIN.get(eng, eng)
        path = shutil.which(binary)
        if not path:
            rows.append((eng, binary, "MISSING", "not on PATH"))
            all_ok = False
            continue
        probe = ENGINE_CLI.get(eng, [binary, "--version"])
        try:
            p = subprocess.run(probe, capture_output=True, text=True, timeout=20)
            detail = (p.stdout or p.stderr or "").strip().splitlines()
            detail = detail[0][:60] if detail else "found"
            rows.append((eng, binary, "OK", detail))
        except (subprocess.TimeoutExpired, OSError) as e:
            rows.append((eng, binary, "WARN", f"probe failed: {e}"))

    w = max(len(r[1]) for r in rows) if rows else 8
    print(f"{'ENGINE':<8} {'CLI':<{w}} {'STATUS':<8} DETAIL")
    print("-" * (8 + w + 8 + 24))
    for eng, binary, status, detail in rows:
        model = models.get(eng) or "(CLI default)"
        print(f"{eng:<8} {binary:<{w}} {status:<8} {detail}  [model: {model}]")

    # Role -> engine map for visibility.
    print("\nRole engine map:")
    for role, spec in roles.items():
        print(f"  {role:<11} -> {spec.get('engine')} ({spec.get('mode')})"
              f"  fallback: {fallbacks.get(spec.get('engine'), '-')}")

    print(f"\nPython: {sys.version.split()[0]}")
    dm = cfg.get("durable_memory", {})
    print(f"Durable memory: {'on' if dm.get('enabled') else 'off'}"
          + (f" ({dm.get('base_url') or 'no base_url set'})" if dm.get("enabled") else ""))

    if all_ok:
        print("\nAll engines present. Note: auth is only fully verified by a live call;\n"
              "fallbacks cover an engine that is installed but unauthenticated.")
    else:
        print("\nSome engines are MISSING. Install them or set a different engine/fallback\n"
              "in config.json before running. See README → Requirements.")
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

    log(run_dir, f"=== CoThink run start: {run_dir.name} (workspace={workspace}) ===")

    # Role 2 — Researcher
    research, engines_used["researcher"] = run_role(
        "researcher",
        render("researcher.md", BRIEF=brief, WORKSPACE=workspace, PRIOR=""),
        run_dir / "02", None, "read_only", cfg, timeout, run_dir)
    (run_dir / "02-researcher.md").write_text(research)

    # Role 3 — Architect
    arch, engines_used["architect"] = run_role(
        "architect",
        render("architect.md", BRIEF=brief, WORKSPACE=workspace,
               PRIOR=section("Research / Fact Base", research)),
        run_dir / "03", None, "plan", cfg, timeout, run_dir)
    (run_dir / "03-architect.md").write_text(arch)

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
    analyst_pass = tester_pass = False
    it = 0
    while it < max_iters:
        it += 1
        itdir = run_dir / f"iter-{it}"
        itdir.mkdir(exist_ok=True)
        log(run_dir, f"--- iteration {it}/{max_iters} ---")

        # Analyst (read-only)
        a_prior = (section("Architecture", arch) + section("Coder report", coder)
                   + (section("Previous Tester findings", prev_tester) if prev_tester else ""))
        analyst, engines_used["analyst"] = run_role(
            "analyst", render("analyst.md", BRIEF=brief, WORKSPACE=workspace, PRIOR=a_prior),
            itdir / "05", workspace, "read_only", cfg, timeout, run_dir)
        (itdir / "05-analyst.md").write_text(analyst)
        analyst_pass = resolve_verdict("analyst", "VERDICT", analyst, engines_used["analyst"],
                                       itdir / "05", workspace, cfg, timeout, run_dir)

        # Fixer (only if there is something to fix)
        needs_fix = (not analyst_pass) or (prev_tester is not None and not prev_tester_pass)
        if needs_fix:
            f_prior = (section("Analyst findings", analyst)
                       + (section("Tester findings", prev_tester) if prev_tester else ""))
            fixer, engines_used["fixer"] = run_role(
                "fixer", render("fixer.md", BRIEF=brief, WORKSPACE=workspace, PRIOR=f_prior),
                itdir / "06", workspace, "write", cfg, timeout, run_dir)
            (itdir / "06-fixer.md").write_text(fixer)

        # Tester (executes; may add scaffolding)
        t_prior = section("Architecture", arch) + section("Latest Analyst findings", analyst)
        tester, engines_used["tester"] = run_role(
            "tester", render("tester.md", BRIEF=brief, WORKSPACE=workspace, PRIOR=t_prior),
            itdir / "07", workspace, "write", cfg, timeout, run_dir)
        (itdir / "07-tester.md").write_text(tester)
        tester_pass = resolve_verdict("tester", "RESULT", tester, engines_used["tester"],
                                      itdir / "07", workspace, cfg, timeout, run_dir)

        prev_tester, prev_tester_pass = tester, tester_pass
        history.append({"iter": it, "analyst_pass": analyst_pass,
                        "tester_pass": tester_pass, "fixer_ran": needs_fix})
        log(run_dir, f"iter {it}: analyst={'PASS' if analyst_pass else 'FAIL'} "
                     f"tester={'PASS' if tester_pass else 'FAIL'}")
        if analyst_pass and tester_pass:
            break

    converged = analyst_pass and tester_pass
    result = {
        "run_id": run_dir.name,
        "status": "passed" if converged else "max_iters_reached",
        "converged": converged,
        "iterations": it,
        "max_iters": max_iters,
        "history": history,
        "engines_used": engines_used,
        "workspace": str(workspace),
        "run_dir": str(run_dir),
        "artifacts": {
            "brief": str(run_dir / "brief.md"),
            "researcher": str(run_dir / "02-researcher.md"),
            "architect": str(run_dir / "03-architect.md"),
            "coder": str(run_dir / "04-coder.md"),
            "final_iter": str(run_dir / f"iter-{it}"),
        },
    }
    (run_dir / "result.json").write_text(json.dumps(result, indent=2))
    maybe_log_durable(cfg, run_dir, result, brief)
    log(run_dir, f"=== CoThink run done: {result['status']} after {it} iter(s) ===")
    print(json.dumps(result, indent=2))


def main():
    ap = argparse.ArgumentParser(prog="cothink", description="CoThink 8-role orchestration driver")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="create a run dir and print its paths")
    pi.add_argument("--title", default="run", help="short title for the run")
    pi.set_defaults(func=cmd_init)

    pr = sub.add_parser("run", help="run roles 2-7 over a run dir containing brief.md")
    pr.add_argument("--run-dir", required=True)
    pr.add_argument("--workspace", default="", help="defaults to <run-dir>/workspace")
    pr.set_defaults(func=cmd_run)

    pd = sub.add_parser("doctor", help="preflight: check required CLIs are installed/reachable")
    pd.set_defaults(func=cmd_doctor)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
