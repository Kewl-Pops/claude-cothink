"""Tests for the mattpocock-derived grafts: shape gate, feed-forward, BLOCKED halt/stall, brief lint.
No CLIs: run_engine is monkeypatched."""
import io, json, re, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cothink  # noqa: E402

REPO_CFG = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())

PASS_ANALYST = "## Defects\nNone\n## Criteria check\n1. MET — `cli.py:3`\nVERDICT: PASS"
FAIL_ANALYST = "## Defects\nD1 MAJOR — cli.py:9 — bad — fix\nRepro: python3 cli.py 0\n## Criteria check\n1. NOT MET — x\nVERDICT: FAIL"


def role_of(prompt):
    return re.search(r"YOU ARE THE \*\*(\w+)\*\*", prompt).group(1).lower()


class ScriptedEngines:
    """run_engine stand-in driven by a per-role script: list of outputs consumed call by call
    (the last one repeats). Records every (role, engine, prompt)."""
    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def __call__(self, engine, prompt, role_dir, workspace, mode, cfg, timeout):
        role = role_of(prompt)
        self.calls.append((role, engine, prompt))
        outs = self.script.get(role, ["## Facts & Data\nok"])
        out = outs.pop(0) if len(outs) > 1 else outs[0]
        return out, True, ""

    def prompts(self, role):
        return [p for r, _, p in self.calls if r == role]


class RunHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "run"
        self.run_dir.mkdir()
        (self.run_dir / "brief.md").write_text(
            "## Objective\nfizzbuzz\n## Success criteria\n1. `python3 cli.py 3` prints Fizz\n## Out of scope\n- nothing\n")
        self._orig = cothink.run_engine

    def tearDown(self):
        cothink.run_engine = self._orig
        self.tmp.cleanup()

    def run_driver(self, fake, **flags):
        cothink.run_engine = fake
        args = SimpleNamespace(run_dir=str(self.run_dir), workspace="", **flags)
        with redirect_stdout(io.StringIO()):
            cothink.cmd_run(args)
        return json.loads((self.run_dir / "result.json").read_text())


class ShapeGateTests(unittest.TestCase):
    def test_malformed_analyst_report_falls_through_chain(self):
        tmp = tempfile.TemporaryDirectory(); run_dir = Path(tmp.name)
        calls = []
        def fake(engine, prompt, *a):
            calls.append(engine)
            if engine == "grok":
                return "Analyst review complete. The verdict is PASS.", True, ""  # kimi/grok summary
            return PASS_ANALYST, True, ""
        orig = cothink.run_engine; cothink.run_engine = fake
        try:
            events = []
            out, eng = cothink.run_role("analyst", "p", run_dir / "05", None, "read_only", REPO_CFG, 10,
                                        run_dir, events=events, accept=cothink.analyst_shape)
        finally:
            cothink.run_engine = orig
        self.assertEqual(eng, "claude"); self.assertEqual(calls, ["grok", "claude"])
        self.assertEqual(events[0]["event"], "malformed_report")
        self.assertIn("malformed report", (run_dir / "run.log").read_text())
        self.assertTrue(cothink.analyst_shape(PASS_ANALYST))
        self.assertFalse(cothink.analyst_shape("verdict is **PASS**"))
        self.assertTrue(cothink.tester_shape("...\nRESULT: FAIL")); self.assertFalse(cothink.tester_shape("done"))
        tmp.cleanup()


class ParserTests(unittest.TestCase):
    def test_blocked_items(self):
        txt = ("## Decisions\n- BLOCKED: prod information_schema for outputs\n- BLOCKED: none\n"
               "## Not fixed\n- D2 BLOCKED: git metadata is read-only\n- `T1` blocked: GIT metadata is read-only\n")
        self.assertEqual(cothink.blocked_items(txt),
                         ["prod information_schema for outputs", "git metadata is read-only"])
        self.assertEqual(cothink.blocked_items("no marker\nVERDICT: FAIL"), [])
        self.assertEqual(cothink.blocked_items(None), [])

    def test_criteria_marks(self):
        a = "## Defects\nNone\n## Criteria check\n1. MET — x\n2. BLOCKED — needs prod kubectl\n3. NOT MET — y\nVERDICT: FAIL"
        m = cothink.criteria_marks(a)
        self.assertEqual(m["not_met"], 1); self.assertEqual(len(m["blocked"]), 1)
        self.assertEqual(cothink.criteria_marks("no section"), {"not_met": 0, "blocked": []})
        # NOT VERIFIED counts like NOT MET (it is not environment-blocked, so the loop must go on)
        b = "## Criteria check\n1. NOT VERIFIED — could not run\n2. BLOCKED — prod only\n## Observations\n- NOT MET here is outside the section\nVERDICT: FAIL"
        self.assertEqual(cothink.criteria_marks(b), {"not_met": 1, "blocked": ["2. BLOCKED — prod only"]})

    def test_section_text(self):
        doc = "## Decisions\n- BLOCKED: prod schema\n## Risks the Coder must handle\n- the Researcher said BLOCKED: prod schema; we designed around it\n"
        self.assertIn("prod schema", cothink.section_text(doc, "Decisions"))
        self.assertNotIn("designed around", cothink.section_text(doc, "Decisions"))
        self.assertEqual(cothink.section_text(doc, "Missing"), "")

    def test_lint_brief(self):
        tmp = tempfile.TemporaryDirectory(); rd = Path(tmp.name)
        r = cothink.lint_brief("## Objective\nx\n## Success criteria (checkable)\n1. a\n2) b\n## Notes\n", rd)
        self.assertEqual(r["criteria_count"], 2); self.assertFalse(r["sections"]["Out of scope"])
        self.assertIn("missing section '## Out of scope'", (rd / "run.log").read_text())
        tmp.cleanup()


class FeedForwardTests(RunHarness):
    def test_prior_analyst_and_fixer_reach_the_next_iteration(self):
        fake = ScriptedEngines({
            "analyst": [FAIL_ANALYST, PASS_ANALYST],
            "fixer": ["## Fixed\n- D1 cli.py green\n## Not fixed\n- none"],
            "tester": ["## Remaining issues\nT1 MINOR — x\nRESULT: FAIL", "RESULT: PASS"],
        })
        r = self.run_driver(fake)
        self.assertEqual(r["status"], "passed"); self.assertEqual(r["iterations"], 2)
        a1, a2 = fake.prompts("analyst")
        self.assertNotIn("### Previous Analyst findings", a1)
        self.assertIn("### Previous Analyst findings", a2); self.assertIn("D1 MAJOR", a2)
        self.assertIn("### Fixer changelog since that review", a2); self.assertIn("D1 cli.py green", a2)
        self.assertIn("### Previous Tester findings", a2)
        f1, f2 = fake.prompts("fixer")
        self.assertNotIn("### Previous Fixer changelog", f1); self.assertIn("### Previous Fixer changelog", f2)
        t1 = fake.prompts("tester")[0]
        self.assertIn("### Fixer changelog this iteration", t1)
        self.assertIn("### Coder report\n", a1); self.assertIn("### Coder report (iteration 0", a2)  # stale report demoted
        self.assertEqual(r["blocked"], []); self.assertEqual(r["brief_lint"]["criteria_count"], 1)

    def test_untagged_outputs_keep_legacy_statuses(self):
        fake = ScriptedEngines({"analyst": [FAIL_ANALYST], "tester": ["RESULT: FAIL"],
                                "fixer": ["## Fixed\n- D1\n## Not fixed\n- none"]})
        r = self.run_driver(fake)
        self.assertEqual(r["status"], "max_iters_reached"); self.assertEqual(r["iterations"], 3)


class BlockedTests(RunHarness):
    def test_architect_blocked_halts_before_coder(self):
        fake = ScriptedEngines({"architect": ["## Decisions\n- BLOCKED: prod information_schema for outputs\n## BUILD PLAN\n1. x"]})
        r = self.run_driver(fake)
        self.assertEqual(r["status"], "blocked"); self.assertEqual(r["iterations"], 0)
        self.assertEqual(r["blocked"], ["prod information_schema for outputs"])
        self.assertNotIn("coder", r["engines_used"]); self.assertEqual([c[0] for c in fake.calls], ["researcher", "architect"])
        self.assertIn("halting before the Coder", (self.run_dir / "run.log").read_text())

    def test_blocked_outside_decisions_does_not_halt(self):
        arch = ("## Decisions\n- use argparse\n## Risks the Coder must handle\n"
                "- the Researcher wrote BLOCKED: prod schema unreachable; we build against the fixture instead\n")
        fake = ScriptedEngines({"architect": [arch], "analyst": [PASS_ANALYST], "tester": ["RESULT: PASS"]})
        r = self.run_driver(fake)
        self.assertEqual(r["status"], "passed"); self.assertEqual(r["blocked"], []); self.assertIn("coder", r["engines_used"])

    def test_not_verified_criterion_keeps_looping_even_with_blocked(self):
        a = "## Defects\nNone\n## Criteria check\n1. NOT VERIFIED — could not run\n2. BLOCKED — z\nVERDICT: FAIL"
        fake = ScriptedEngines({"analyst": [a], "fixer": ["## Not fixed\n- D1 BLOCKED: env"], "tester": ["RESULT: FAIL"]})
        r = self.run_driver(fake)
        self.assertEqual(r["status"], "max_iters_reached"); self.assertEqual(r["iterations"], 3)

    def test_no_halt_flag_keeps_building(self):
        fake = ScriptedEngines({"architect": ["## Decisions\n- BLOCKED: prod schema\n"],
                                "analyst": [PASS_ANALYST], "tester": ["RESULT: PASS"]})
        r = self.run_driver(fake, no_halt_on_blocked=True)
        self.assertEqual(r["status"], "passed"); self.assertIn("coder", r["engines_used"])

    def test_stall_stop_when_every_remaining_failure_is_blocked(self):
        blocked_analyst = ("## Defects\nNone\n## Criteria check\n1. MET — ok\n2. BLOCKED — needs prod kubectl\n"
                           "VERDICT: FAIL")
        fake = ScriptedEngines({"analyst": [blocked_analyst],
                                "fixer": ["## Fixed\n- none\n## Not fixed\n- D0 BLOCKED: no kubectl to prod from this sandbox"],
                                "tester": ["## Remaining issues\nT1 BLOCKED: no prod access\nRESULT: FAIL"]})
        r = self.run_driver(fake)
        self.assertEqual(r["status"], "blocked"); self.assertEqual(r["iterations"], 1)
        self.assertIn("no kubectl to prod from this sandbox", r["blocked"])
        self.assertIn("stopping early", (self.run_dir / "run.log").read_text())

    def test_not_met_criterion_keeps_looping_even_with_blocked(self):
        a = "## Defects\nD1 BLOCKER — x\n## Criteria check\n1. NOT MET — y\n2. BLOCKED — z\nVERDICT: FAIL"
        fake = ScriptedEngines({"analyst": [a], "fixer": ["## Not fixed\n- D1 BLOCKED: env"], "tester": ["RESULT: FAIL"]})
        r = self.run_driver(fake)
        self.assertEqual(r["status"], "max_iters_reached"); self.assertEqual(r["iterations"], 3)


if __name__ == "__main__":
    unittest.main()
