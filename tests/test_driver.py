"""Unit tests for the CoThink driver's chain/guard logic and engine command shapes.
No network, no CLIs: run_engine / _run are monkeypatched. Run: python3 -m unittest discover -s tests"""
import json, os, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cothink  # noqa: E402

REPO_CFG = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())


def cfg_with(**roles):
    c = json.loads(json.dumps(REPO_CFG))
    for r, v in roles.items():
        c["roles"][r].update(v)
    return c


class FakeEngines:
    """run_engine stand-in: engines listed in `failing` return empty output/failure."""
    def __init__(self, failing=()):
        self.failing, self.calls = set(failing), []

    def __call__(self, engine, prompt, role_dir, workspace, mode, cfg, timeout):
        self.calls.append((engine, mode))
        if engine in self.failing:
            return "", False, f"{engine} quota"
        return f"output from {engine}\nVERDICT: PASS", True, ""


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name)
        self._orig = cothink.run_engine

    def tearDown(self):
        cothink.run_engine = self._orig
        self.tmp.cleanup()

    def test_repo_config_shape(self):
        for role in ("researcher", "architect", "coder", "analyst", "fixer", "tester"):
            rc = REPO_CFG["roles"][role]
            self.assertIn(rc["engine"], REPO_CFG["families"])
            for e in rc["fallbacks"]:
                self.assertIn(e, REPO_CFG["families"])
        # codex (the builder's family) never sits in a judgment chain
        for role in ("analyst", "tester"):
            self.assertNotIn("codex", cothink.role_chain(REPO_CFG, role))
        # builder / validator / tester are three different families by default
        fam = lambda r: REPO_CFG["families"][REPO_CFG["roles"][r]["engine"]]
        self.assertEqual(len({fam("coder"), fam("analyst"), fam("tester")}), 3)

    def test_per_role_chain_order(self):
        self.assertEqual(cothink.role_chain(REPO_CFG, "analyst"), ["grok", "claude", "gemini", "kimi"])

    def test_legacy_per_engine_fallbacks_still_work(self):
        c = cfg_with(analyst={"engine": "qwen"})
        del c["roles"]["analyst"]["fallbacks"]
        c["fallbacks"] = {"qwen": ["gemini", "kimi"]}
        self.assertEqual(cothink.role_chain(c, "analyst"), ["qwen", "gemini", "kimi"])

    def test_fallback_runs_next_engine_and_marks_output(self):
        fake = FakeEngines(failing={"grok"}); cothink.run_engine = fake
        out, eng = cothink.run_role("analyst", "p", self.run_dir / "05", None, "read_only",
                                    REPO_CFG, 10, self.run_dir)
        self.assertEqual(eng, "claude")
        self.assertIn("fallback: grok unavailable, ran on claude", out)
        self.assertEqual([c[0] for c in fake.calls], ["grok", "claude"])

    def test_mode_comes_from_config(self):
        fake = FakeEngines(); cothink.run_engine = fake
        cothink.run_role("architect", "p", self.run_dir / "03", None, "plan", REPO_CFG, 10, self.run_dir)
        self.assertEqual(fake.calls[0], ("claude", "read_only"))  # config says read_only, caller said plan

    def test_family_guard_skips_the_code_writers_family(self):
        # Coder fell back to grok in this run -> the Analyst must not run on grok
        fake = FakeEngines(); cothink.run_engine = fake
        events = []
        out, eng = cothink.run_role("analyst", "p", self.run_dir / "05", None, "read_only", REPO_CFG, 10,
                                    self.run_dir, exclude_families={"xai"}, events=events)
        self.assertEqual(eng, "claude")
        self.assertEqual([c[0] for c in fake.calls], ["claude"])
        self.assertEqual(events, [])
        self.assertIn("skipping grok", (self.run_dir / "run.log").read_text())

    def test_family_guard_violation_is_loud_not_fatal(self):
        c = cfg_with(analyst={"engine": "grok", "fallbacks": []})
        fake = FakeEngines(); cothink.run_engine = fake
        events = []
        out, eng = cothink.run_role("analyst", "p", self.run_dir / "05", None, "read_only", c, 10,
                                    self.run_dir, exclude_families={"xai"}, events=events)
        self.assertEqual(eng, "grok")
        self.assertIn("WARNING: analyst ran on grok", out)
        self.assertEqual(events[0]["event"], "family_guard_violation")

    def test_all_engines_failed(self):
        fake = FakeEngines(failing={"kimi", "grok", "claude"}); cothink.run_engine = fake
        out, eng = cothink.run_role("tester", "p", self.run_dir / "07", None, "write", REPO_CFG, 10, self.run_dir)
        self.assertEqual(eng, "none")
        self.assertIn("failed on all engines", out)


class OverlapAndSubprocessTests(unittest.TestCase):
    def test_family_overlap_events(self):
        ev = cothink.family_overlap_events(REPO_CFG, {"coder": "kimi", "analyst": "grok", "tester": "kimi"}, 1)
        self.assertEqual([e["event"] for e in ev], ["writer_tester_same_family"])
        ev = cothink.family_overlap_events(REPO_CFG, {"coder": "codex", "analyst": "grok", "tester": "grok"}, 2)
        self.assertEqual([e["event"] for e in ev], ["analyst_tester_same_family"])
        self.assertEqual(cothink.family_overlap_events(REPO_CFG, {"coder": "codex", "analyst": "grok", "tester": "kimi"}, 1), [])
        self.assertEqual(cothink.family_overlap_events(REPO_CFG, {"coder": "codex", "analyst": "grok", "tester": "none"}, 1), [])

    def test_run_never_inherits_stdin(self):
        # codex exec blocks on an open non-TTY stdin ("Reading additional input from stdin...")
        seen = {}
        orig = cothink.subprocess.run
        def fake(cmd, **kw):
            seen.update(kw)
            class P: stdout, stderr, returncode = "x", "", 0
            return P()
        cothink.subprocess.run = fake
        try:
            cothink._run(["true"], ".", 5)
        finally:
            cothink.subprocess.run = orig
        self.assertIs(seen.get("stdin"), cothink.subprocess.DEVNULL)


class VerdictTests(unittest.TestCase):
    def test_last_verdict_wins_and_default_is_fail(self):
        self.assertEqual(cothink.verdict("VERDICT: PASS\n...\nVERDICT: FAIL", "VERDICT"), "FAIL")
        self.assertEqual(cothink.verdict("blah RESULT = pass", "RESULT"), "PASS")
        self.assertEqual(cothink.verdict("no verdict here", "VERDICT"), "FAIL")
        self.assertEqual(cothink.verdict(None, "VERDICT"), "FAIL")


class CommandShapeTests(unittest.TestCase):
    """Check what the driver would shell out, without shelling out."""
    def setUp(self):
        self.captured = []
        self._orig = cothink._run
        def fake_run(cmd, cwd, timeout, out_file=None):
            self.captured.append(cmd); return "ok", True, ""
        cothink._run = fake_run
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        cothink._run = self._orig; self.tmp.cleanup()

    def cmd(self, engine, mode, workspace=None):
        cothink.run_engine(engine, "prompt", Path(self.tmp.name), workspace, mode, REPO_CFG, 60)
        return self.captured[-1]

    def test_claude_read_only_is_isolated_and_cannot_write_but_can_test(self):
        c = self.cmd("claude", "read_only", workspace=self.tmp.name)
        for flag in ("--no-session-persistence", "--strict-mcp-config", "--setting-sources"):
            self.assertIn(flag, c)
        self.assertIn("dontAsk", c); self.assertNotIn("--dangerously-skip-permissions", c)
        self.assertIn("Bash(pytest *)", c); self.assertIn("--add-dir", c)
        self.assertEqual(c[c.index("--model") + 1], "claude-fable-5-1")

    def test_claude_write_and_plan(self):
        self.assertIn("--dangerously-skip-permissions", self.cmd("claude", "write"))
        c = self.cmd("claude", "plan"); self.assertIn("plan", c); self.assertNotIn("--tools", c)

    def test_codex_effort_override_and_sandbox(self):
        c = self.cmd("codex", "read_only")
        self.assertIn('model_reasoning_effort="high"', c); self.assertIn("read-only", c)
        self.assertIn("workspace-write", self.cmd("codex", "write"))

    def test_grok_read_only_denies_writes(self):
        c = self.cmd("grok", "read_only")
        self.assertIn("--always-approve", c); self.assertIn("--no-memory", c)
        self.assertEqual(c.count("--deny"), 2)
        self.assertNotIn("--deny", self.cmd("grok", "write"))

    def test_gemini_timeout_and_write_permissions(self):
        c = self.cmd("gemini", "read_only")
        self.assertIn("--print-timeout", c); self.assertNotIn("--dangerously-skip-permissions", c)
        self.assertIn("shell/terminal commands are NOT available", c[c.index("-p") + 1])  # read-only: no shell
        self.assertNotIn("NOT available", self.cmd("gemini", "write")[-1])
        self.assertEqual(c[c.index("--model") + 1], "gemini-3.6-flash-medium")
        self.assertIn("--dangerously-skip-permissions", self.cmd("gemini", "write"))

    def test_vibe_has_price_cap_and_no_model_flag(self):
        c = self.cmd("vibe", "read_only")
        self.assertIn("--max-price", c); self.assertNotIn("--model", c)


if __name__ == "__main__":
    unittest.main()
