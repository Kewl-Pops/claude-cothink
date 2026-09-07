"""Tests for `cothink doctor` — the preflight. No CLIs: which/run are injected."""
import json, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cothink  # noqa: E402

REPO_CFG = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())


class P:
    def __init__(self, out="1.0.0", rc=0):
        self.stdout, self.stderr, self.returncode = out, "", rc


def fake_env(present, versions=None, probed=None):
    versions = versions or {}
    which = lambda c: f"/usr/bin/{c}" if c in present else None
    def run(cmd, **kw):
        if probed is not None:
            probed.append(cmd[0])
        return P(versions.get(cmd[0], cmd[0] + " 9.9"))
    return which, run


ALL = {"claude-acct", "codex-acct", "gemini", "kimi", "grok", "vibe", "qwen"}


class DoctorTests(unittest.TestCase):
    def test_repo_config_all_present_is_ok(self):
        which, run = fake_env(ALL)
        r = cothink.doctor_report(REPO_CFG, which=which, run=run, env={})
        self.assertTrue(r["ok"], r["problems"]); self.assertEqual(r["problems"], [])
        by = {e["engine"]: e for e in r["engines"]}
        self.assertEqual(by["claude"]["binary"], "/usr/bin/claude-acct")  # dispatcher preferred
        self.assertEqual(by["codex"]["binary"], "/usr/bin/codex-acct")
        self.assertIn("via claude-acct", by["claude"]["detail"]); self.assertIn("via codex-acct", by["codex"]["detail"])
        self.assertIn("architect (primary)", by["claude"]["roles"]); self.assertIn("analyst (fallback)", by["claude"]["roles"])
        self.assertEqual(r["roles"]["analyst"]["chain"], ["grok", "claude", "gemini", "kimi"])
        self.assertEqual(r["settings"]["stop_when_blocked"], True)

    def test_env_override_wins(self):
        _, run = fake_env(ALL)
        which = lambda c: c if c.startswith("/") else (f"/usr/bin/{c}" if c in ALL else None)
        r = cothink.doctor_report(REPO_CFG, which=which, run=run, env={"COTHINK_CLAUDE_BIN": "/opt/my-claude"})
        self.assertEqual({e["engine"]: e["binary"] for e in r["engines"]}["claude"], "/opt/my-claude")

    def test_probe_hits_the_underlying_cli_not_the_dispatcher(self):
        probed = []
        which, run = fake_env(ALL | {"claude", "codex"}, probed=probed)
        cothink.doctor_report(REPO_CFG, which=which, run=run, env={"CLAUDE_ACCT_BIN": "/opt/claude-real"})
        self.assertNotIn("claude-acct", probed); self.assertNotIn("codex-acct", probed)
        self.assertIn("/opt/claude-real", probed); self.assertIn("codex", probed)

    def test_engine_binary_matches_run_engine_and_gemini_never_resolves_to_agy(self):
        which = lambda c: c in {"claude-acct", "codex", "agy"}
        self.assertEqual(cothink.engine_binary("claude", env={}, which=which), "claude-acct")
        self.assertEqual(cothink.engine_binary("codex", env={}, which=which), "codex")
        self.assertEqual(cothink.engine_binary("gemini", env={}, which=which), "gemini")
        self.assertEqual(cothink.engine_binary("claude", env={"COTHINK_CLAUDE_BIN": "/x/claude"}, which=which), "/x/claude")
        r = cothink.doctor_report(REPO_CFG, which=lambda c: f"/usr/bin/{c}" if c in (ALL - {"gemini"}) | {"agy"} else None,
                                  run=lambda cmd, **kw: P(), env={})
        self.assertFalse(r["ok"]); self.assertTrue(any("gemini: not on PATH" in p and "expose it as `gemini`" in p for p in r["problems"]))

    def test_fixer_family_counts_as_a_writer(self):
        which, run = fake_env(ALL)
        cfg = json.loads(json.dumps(REPO_CFG)); cfg["roles"]["fixer"]["engine"] = "grok"  # grok = analyst primary
        r = cothink.doctor_report(cfg, which=which, run=run, env={})
        self.assertFalse(r["ok"]); self.assertTrue(any("analyst: chain contains grok" in p and "Fixer (grok)" in p for p in r["problems"]))

    def test_plain_binaries_when_dispatchers_absent(self):
        which, run = fake_env((ALL - {"claude-acct", "codex-acct"}) | {"claude", "codex"})
        r = cothink.doctor_report(REPO_CFG, which=which, run=run, env={})
        by = {e["engine"]: e["binary"] for e in r["engines"]}
        self.assertEqual((by["claude"], by["codex"]), ("/usr/bin/claude", "/usr/bin/codex")); self.assertTrue(r["ok"])

    def test_missing_primary_fails_missing_fallback_warns(self):
        which, run = fake_env(ALL - {"grok"})  # grok = analyst primary
        r = cothink.doctor_report(REPO_CFG, which=which, run=run, env={})
        self.assertFalse(r["ok"]); self.assertTrue(any("grok: not on PATH" in p and "primary for" in p for p in r["problems"]))
        which, run = fake_env(ALL - {"qwen"})  # qwen = fallback only
        r = cothink.doctor_report(REPO_CFG, which=which, run=run, env={})
        self.assertTrue(r["ok"]); self.assertTrue(any("qwen: not on PATH" in w and "fallback only" in w for w in r["warnings"]))

    def test_whole_chain_missing_fails(self):
        cfg = json.loads(json.dumps(REPO_CFG)); cfg["roles"]["tester"] = {"engine": "kimi", "mode": "write", "fallbacks": ["grok"]}
        which, run = fake_env(ALL - {"kimi", "grok"})
        r = cothink.doctor_report(cfg, which=which, run=run, env={})
        self.assertTrue(any(p.startswith("tester: no engine in its chain") for p in r["problems"]))

    def test_family_collision_is_a_problem_for_primary_and_a_warning_for_fallback(self):
        which, run = fake_env(ALL)
        cfg = json.loads(json.dumps(REPO_CFG)); cfg["roles"]["analyst"]["engine"] = "codex"
        r = cothink.doctor_report(cfg, which=which, run=run, env={})
        self.assertFalse(r["ok"]); self.assertTrue(any("analyst: chain contains codex" in p for p in r["problems"]))
        cfg = json.loads(json.dumps(REPO_CFG)); cfg["roles"]["tester"]["fallbacks"].append("codex")
        r = cothink.doctor_report(cfg, which=which, run=run, env={})
        self.assertTrue(r["ok"]); self.assertTrue(any("tester: chain contains codex" in w and "fallback only" in w for w in r["warnings"]))

    def test_unknown_engine_bad_mode_and_pin_sanity(self):
        which, run = fake_env(ALL | {"llama"})
        cfg = json.loads(json.dumps(REPO_CFG))
        cfg["roles"]["researcher"]["fallbacks"].append("llama"); cfg["roles"]["fixer"]["mode"] = "yolo"
        cfg["models"]["claude"] = "fable"; cfg["models"]["codex"] = "gpt-5-codex"; cfg["models"]["vibe"] = "x"
        r = cothink.doctor_report(cfg, which=which, run=run, env={})
        self.assertFalse(r["ok"])
        self.assertTrue(any("llama: unknown engine" in p for p in r["problems"]))
        self.assertTrue(any("missing from config.families" in p for p in r["problems"]))
        self.assertTrue(any("fixer: mode 'yolo'" in p for p in r["problems"]))
        self.assertTrue(any("looks like an alias" in w for w in r["warnings"]))
        self.assertTrue(any("-codex" in w for w in r["warnings"])); self.assertTrue(any("models.vibe is ignored" in w for w in r["warnings"]))

    def test_probe_failure_is_reported_not_fatal(self):
        which = lambda c: "/usr/bin/" + c
        def run(cmd, **kw):
            raise OSError("boom")
        r = cothink.doctor_report(REPO_CFG, which=which, run=run, env={})
        self.assertTrue(r["ok"]); self.assertTrue(all("probe failed" in e["detail"] for e in r["engines"]))

    def test_cli_wiring(self):
        seen = {}; orig, argv = cothink.cmd_doctor, sys.argv
        cothink.cmd_doctor = lambda a: seen.update(vars(a)); sys.argv = ["cothink", "doctor", "--json"]
        try:
            cothink.main()
        finally:
            cothink.cmd_doctor, sys.argv = orig, argv
        self.assertTrue(seen["json"])


if __name__ == "__main__":
    unittest.main()
