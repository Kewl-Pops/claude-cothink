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
        self.assertIn("### Latest Fixer changelog", a2); self.assertIn("D1 cli.py green", a2)
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
        self.assertIsNone(r["artifacts"]["final_iter"]); self.assertIn("brief_lint", r)

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
        self.assertEqual(r["blocked"], ["prod schema"])

    def test_stall_stop_when_every_remaining_failure_is_blocked(self):
        blocked_analyst = ("## Defects\nNone\n## Criteria check\n1. MET — ok\n2. BLOCKED — needs prod kubectl\n"
                           "VERDICT: FAIL")
        fake = ScriptedEngines({"analyst": [blocked_analyst],
                                "fixer": ["## Fixed\n- none\n## Not fixed\n- D0 BLOCKED: no kubectl to prod from this sandbox"],
                                "tester": ["## Remaining issues\nT1 BLOCKED: no prod access\nRESULT: FAIL"]})
        r = self.run_driver(fake)
        self.assertEqual(r["status"], "blocked"); self.assertEqual(r["iterations"], 1)
        self.assertEqual(r["blocked"], ["2. BLOCKED — needs prod kubectl", "no kubectl to prod from this sandbox", "no prod access"])
        self.assertIn("stopping early", (self.run_dir / "run.log").read_text())

    def test_not_met_criterion_keeps_looping_even_with_blocked(self):
        a = "## Defects\nD1 BLOCKER — x\n## Criteria check\n1. NOT MET — y\n2. BLOCKED — z\nVERDICT: FAIL"
        fake = ScriptedEngines({"analyst": [a], "fixer": ["## Not fixed\n- D1 BLOCKED: env"], "tester": ["RESULT: FAIL"]})
        r = self.run_driver(fake)
        self.assertEqual(r["status"], "max_iters_reached"); self.assertEqual(r["iterations"], 3)


class ByEngine(ScriptedEngines):
    """Like ScriptedEngines but the per-role value may be {engine: output, "*": default}."""
    def __init__(self, script):
        self.script, self.calls = script, []

    def __call__(self, engine, prompt, *a):
        role = role_of(prompt); self.calls.append((role, engine, prompt))
        v = self.script.get(role, ["## Facts & Data\nok"])
        return (v.get(engine, v["*"]) if isinstance(v, dict) else v[0]), True, ""


class ReviewRegressionParserTests(unittest.TestCase):
    def test_blocked_marker_decorations(self):
        for ln in ("1. BLOCKED: x", "1) BLOCKED: x", "+ BLOCKED: x", "- **BLOCKED:** x", "- **BLOCKED**: x",
                   "- `BLOCKED:` x", "- D2: BLOCKED: x", "- **D2**: BLOCKED: x", "- D2 — BLOCKED: x",
                   "1. **T3** – BLOCKED: x", "- **D2** BLOCKED: x"):
            self.assertEqual(cothink.blocked_items(ln), ["x"], ln)
        for ln in ("the Researcher wrote BLOCKED: x", "- Prod schema: BLOCKED: x", "| 2 | BLOCKED: x |",
                   "T2 MAJOR — cli.py:3 — BLOCKED: no prod"):
            self.assertEqual(cothink.blocked_items(ln), [], ln)
        self.assertEqual(cothink.blocked_items("## Remaining issues\nT2 BLOCKED: no prod access\nRESULT: FAIL"), ["no prod access"])

    def test_blocked_none_placeholders(self):
        self.assertEqual(cothink.blocked_items("- BLOCKED: (none)\n- BLOCKED: N/A — all resolved\n- BLOCKED: none, D1 fixed\n- BLOCKED: none (all addressed)"), [])
        self.assertEqual(cothink.blocked_items("- BLOCKED: none of the roles can reach prod postgres"), ["none of the roles can reach prod postgres"])

    def test_section_text_levels(self):
        doc3 = "### Decisions\n- BLOCKED: prod schema\n### Risks\n- BLOCKED: other"
        self.assertIn("prod schema", cothink.section_text(doc3, "Decisions")); self.assertNotIn("other", cothink.section_text(doc3, "Decisions"))
        doc4 = "## Decisions\n### infra\n- BLOCKED: prod schema\n## Risks the Coder must handle\n- BLOCKED: designed around"
        self.assertIn("prod schema", cothink.section_text(doc4, "Decisions")); self.assertNotIn("designed around", cothink.section_text(doc4, "Decisions"))
        fenced = "## Criteria check\n1. MET — x\n```\n# comment\n```\n2. NOT MET — y\n## Next\n"
        self.assertEqual(cothink.criteria_marks(fenced), {"not_met": 1, "blocked": []})

    def test_criteria_marks_vocabulary(self):
        self.assertEqual(cothink.criteria_marks("## Criteria check\n1. Not met — y\n2. Blocked — z\nVERDICT: FAIL"),
                         {"not_met": 1, "blocked": ["2. Blocked — z"]})
        a = "## Criteria check\n2. BLOCKED — prod only\n   Evidence: tests NOT VERIFIED because kubectl is absent\nVERDICT: FAIL"
        self.assertEqual(cothink.criteria_marks(a), {"not_met": 0, "blocked": ["2. BLOCKED — prod only"]})
        self.assertEqual(cothink.criteria_marks("## Criteria check\n1. NOT MET — fix is BLOCKED on nothing\n"), {"not_met": 1, "blocked": []})
        self.assertEqual(cothink.criteria_marks("## Criteria check\n1. MET — network calls BLOCKED by the mock\n"), {"not_met": 0, "blocked": []})
        for st in ("PARTIALLY MET", "Unverified", "NOT VERIFIABLE", "**NOT MET**"):
            self.assertEqual(cothink.criteria_marks(f"## Criteria check\n1. {st} — y\n")["not_met"], 1, st)
        # unmarked criterion line mentioning an open status stays open (conservative)
        self.assertEqual(cothink.criteria_marks("## Criteria check\n1. Criterion one: NOT MET — y\n")["not_met"], 1)

    def test_shape_gate_h3_and_criteria_required(self):
        self.assertTrue(cothink.analyst_shape("# Review\n### Defects\nNone\n### Criteria check\n1. MET — cli.py:3\nVERDICT: PASS"))
        self.assertFalse(cothink.analyst_shape("## Defects\nNone\nVERDICT: PASS"))

    def test_lint_brief_headings_and_bullets(self):
        tmp = tempfile.TemporaryDirectory(); rd = Path(tmp.name)
        r = cothink.lint_brief("## Objective\nx\n## Success Criteria\n1. a\n## Out of scope\n- b\n", rd)
        self.assertEqual(r["criteria_count"], 1); self.assertTrue(r["sections"]["Success criteria"])
        r = cothink.lint_brief("## Objective\nx\n##  Success criteria\n1. a\n2) b\n## Out of scope\n- b\n", rd)
        self.assertEqual(r["criteria_count"], 2)
        r = cothink.lint_brief("## Objective\nx\n## Success criteria\n- a\n- b\n## Out of scope\n- n\n", rd)
        self.assertEqual(r["criteria_count"], 0); self.assertIn("no numbered success criteria", (rd / "run.log").read_text())
        tmp.cleanup()


class ReviewRegressionRunTests(RunHarness):
    def test_fixer_unblocking_under_fixed_does_not_stall(self):
        a = "## Defects\nNone\n## Criteria check\n1. MET — ok\n2. BLOCKED — needs network for the DNS test\nVERDICT: FAIL"
        fixer = "## Fixed\n- T1 BLOCKED: no network — mocked DNS in conftest.py; Repro green\n## Not fixed\n- none"
        fake = ScriptedEngines({"analyst": [a, PASS_ANALYST], "fixer": [fixer], "tester": ["RESULT: PASS"]})
        r = self.run_driver(fake)
        self.assertEqual(r["status"], "passed"); self.assertEqual(r["iterations"], 2); self.assertEqual(r["blocked"], [])

    def test_off_vocabulary_open_criterion_keeps_looping(self):
        for status in ("PARTIALLY MET", "Not met", "Unverified", "NOT VERIFIABLE", "NOT MET — BLOCKED by missing creds"):
            with self.subTest(status=status):
                a = f"## Defects\nD1 BLOCKER — x\n## Criteria check\n1. {status} — y\n2. BLOCKED — z\nVERDICT: FAIL"
                fake = ScriptedEngines({"analyst": [a], "fixer": ["## Not fixed\n- D1 BLOCKED: env"], "tester": ["RESULT: FAIL"]})
                r = self.run_driver(fake)
                self.assertEqual(r["status"], "max_iters_reached"); self.assertEqual(r["iterations"], 3)

    def test_no_halt_flag_records_architect_items_when_capped_and_when_stalled(self):
        fake = ScriptedEngines({"architect": ["## Decisions\n- BLOCKED: prod schema\n"], "analyst": [FAIL_ANALYST],
                                "fixer": ["## Fixed\n- D1\n## Not fixed\n- none"], "tester": ["RESULT: FAIL"]})
        r = self.run_driver(fake, no_halt_on_blocked=True)
        self.assertEqual(r["status"], "max_iters_reached"); self.assertEqual(r["blocked"], ["prod schema"])
        fake = ScriptedEngines({"architect": ["## Decisions\n- BLOCKED: prod schema\n"],
                                "analyst": ["## Criteria check\n1. MET — ok\n2. BLOCKED — needs prod kubectl\nVERDICT: FAIL"],
                                "fixer": ["## Not fixed\n- D2 BLOCKED: no kubectl"], "tester": ["RESULT: FAIL"]})
        r = self.run_driver(fake, no_halt_on_blocked=True)
        self.assertEqual(r["status"], "blocked"); self.assertEqual(r["blocked"], ["prod schema", "2. BLOCKED — needs prod kubectl", "no kubectl"])

    def test_max_iters_zero_still_writes_result(self):
        cfg = json.loads(json.dumps(REPO_CFG)); cfg["max_iters"] = 0
        orig = cothink.load_config; cothink.load_config = lambda: cfg
        try:
            r = self.run_driver(ScriptedEngines({}))
        finally:
            cothink.load_config = orig
        self.assertEqual((r["status"], r["iterations"], r["blocked"]), ("max_iters_reached", 0, []))
        self.assertIsNone(r["artifacts"]["final_iter"])

    def test_tester_malformed_report_is_recorded(self):
        fake = ByEngine({"analyst": {"grok": "Analyst review complete. The verdict is PASS.", "*": PASS_ANALYST},
                         "tester": {"kimi": "All good, done.", "*": "RESULT: PASS"}})
        r = self.run_driver(fake)
        self.assertEqual(r["engines_used"]["analyst"], "claude"); self.assertEqual(r["engines_used"]["tester"], "grok")
        self.assertIn({"role": "analyst", "engine": "grok", "event": "malformed_report"}, r["guard_events"])
        self.assertIn({"role": "tester", "engine": "kimi", "event": "malformed_report"}, r["guard_events"])

    def test_blocked_none_placeholder_with_explanation_is_not_a_stall(self):
        analyst = "## Defects\nNone\n## Criteria check\n1. MET — ok\n2. BLOCKED — needs prod kubectl\nVERDICT: FAIL"
        fixer = "## Fixed\n- D1 mocked DNS\n## Not fixed\n- BLOCKED: none (all addressed)"
        r = self.run_driver(ScriptedEngines({"analyst": [analyst], "fixer": [fixer], "tester": ["RESULT: PASS"]}))
        self.assertEqual(r["status"], "max_iters_reached"); self.assertEqual(r["iterations"], 3)

    def test_fixer_changelog_survives_a_fixer_less_iteration(self):
        fake = ScriptedEngines({"analyst": [FAIL_ANALYST, PASS_ANALYST, PASS_ANALYST],
                                "fixer": ["## Fixed\n- D1 rewrote parse() in cli.py\n## Not fixed\n- none",
                                          "## Fixed\n- T1 fixed\n## Not fixed\n- none"],
                                "tester": ["RESULT: PASS", "## Remaining issues\nT1 MINOR — x\nRESULT: FAIL", "RESULT: PASS"]})
        r = self.run_driver(fake)
        self.assertEqual((r["status"], r["iterations"]), ("passed", 3))
        f2 = fake.prompts("fixer")[1]
        self.assertIn("### Previous Fixer changelog", f2); self.assertIn("rewrote parse()", f2)
        a3 = fake.prompts("analyst")[2]
        self.assertIn("### Coder report (iteration 0", a3); self.assertIn("rewrote parse()", a3)

    def test_all_engines_malformed_keeps_first_report_as_last_resort(self):
        odd = "## Prior findings\nnone\n## Findings\nD1 MAJOR — cli.py:9 — bad\nRepro: python3 cli.py 0\n## Success criteria\n1. NOT MET — x\nVERDICT: FAIL"
        fake = ScriptedEngines({"analyst": [odd], "fixer": ["## Fixed\n- D1\n## Not fixed\n- none"], "tester": ["RESULT: FAIL"]})
        r = self.run_driver(fake)
        self.assertEqual(r["engines_used"]["analyst"], "grok")
        self.assertIn("D1 MAJOR", fake.prompts("fixer")[0])
        self.assertIn("failed the shape check", (self.run_dir / "iter-1" / "05-analyst.md").read_text())
        # and an off-shape report can never count as PASS
        odd_pass = odd.replace("NOT MET", "MET").replace("VERDICT: FAIL", "VERDICT: PASS")
        r = self.run_driver(ScriptedEngines({"analyst": [odd_pass], "fixer": ["## Not fixed\n- none"], "tester": ["RESULT: PASS"]}))
        self.assertFalse(r["history"][0]["analyst_pass"]); self.assertEqual(r["status"], "max_iters_reached")

    def test_architect_h3_decisions_still_halts(self):
        fake = ScriptedEngines({"architect": ["### Decisions\n- BLOCKED: prod schema\n### BUILD PLAN\n1. x"], "analyst": [PASS_ANALYST], "tester": ["RESULT: PASS"]})
        r = self.run_driver(fake)
        self.assertEqual(r["status"], "blocked"); self.assertEqual(r["blocked"], ["prod schema"]); self.assertNotIn("coder", r["engines_used"])

    def test_analyst_blocked_alone_keeps_looping_when_fixer_is_silent(self):
        a = "## Defects\nNone\n## Criteria check\n1. MET — ok\n2. BLOCKED — needs prod kubectl\nVERDICT: FAIL"
        fake = ScriptedEngines({"analyst": [a], "fixer": ["## Fixed\n- none\n## Not fixed\n- D2 could not do it, needs prod access"], "tester": ["RESULT: FAIL"]})
        r = self.run_driver(fake)
        self.assertEqual((r["status"], r["iterations"]), ("max_iters_reached", 3))
        self.assertEqual(r["blocked"], ["2. BLOCKED — needs prod kubectl"])  # the Analyst's mark is still reported
        self.assertEqual(len(fake.prompts("fixer")), 3)
        self.assertNotIn("stopping early", (self.run_dir / "run.log").read_text())

    def test_stop_when_blocked_false_keeps_looping(self):
        cfg = json.loads(json.dumps(REPO_CFG)); cfg["stop_when_blocked"] = False
        orig = cothink.load_config; cothink.load_config = lambda: cfg
        try:
            r = self.run_driver(ScriptedEngines({
                "analyst": ["## Defects\nNone\n## Criteria check\n1. MET — ok\n2. BLOCKED — needs prod\nVERDICT: FAIL"],
                "fixer": ["## Not fixed\n- D2 BLOCKED: no kubectl"], "tester": ["RESULT: FAIL"]}))
        finally:
            cothink.load_config = orig
        self.assertEqual((r["status"], r["iterations"], r["blocked"]), ("max_iters_reached", 3, ["2. BLOCKED — needs prod", "no kubectl"]))


class CliWiringTests(unittest.TestCase):
    def test_cli_flag_reaches_cmd_run(self):
        seen = {}
        orig, argv = cothink.cmd_run, sys.argv
        cothink.cmd_run = lambda a: seen.update(vars(a))
        sys.argv = ["cothink", "run", "--run-dir", "x", "--no-halt-on-blocked"]
        try:
            cothink.main()
        finally:
            cothink.cmd_run, sys.argv = orig, argv
        self.assertTrue(seen["no_halt_on_blocked"]); self.assertEqual(seen["run_dir"], "x")


class TemplateContractTests(unittest.TestCase):
    """Cheap guards so later edits do not silently drop wording the driver relies on."""
    ROLES = Path(__file__).resolve().parents[1] / "roles"

    def read(self, n):
        return (self.ROLES / n).read_text()

    def test_template_wording(self):
        self.assertIn("never begin a decided line", self.read("architect.md"))
        self.assertIn("brief Prerequisites", self.read("architect.md")); self.assertIn("brief Prerequisites", self.read("researcher.md"))
        self.assertIn("Constraints", self.read("analyst.md").split("## Output contract")[1]); self.assertIn("Constraint", self.read("tester.md"))
        self.assertIn("Repro:", self.read("tester.md")); self.assertIn("highest `T<n>`", self.read("tester.md"))
        self.assertIn("`- D<n> BLOCKED:", self.read("fixer.md"))
        self.assertNotIn("have a shell", self.read("strategist.md")); self.assertIn("pytest", self.read("strategist.md"))
        for n in ("researcher", "architect", "coder", "analyst", "fixer", "tester", "strategist", "executor"):
            self.assertNotIn("{{MEMORY}}", self.read(n + ".md"))


if __name__ == "__main__":
    unittest.main()
