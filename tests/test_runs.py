"""Tests for `cothink runs` — run history from temporary run directories."""
import argparse, contextlib, io, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cothink  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PASSED = {"run_id": "20260101-000000-a", "status": "passed", "iterations": 2,
          "engines_used": {"researcher": "gemini", "architect": "claude", "coder": "codex",
                           "analyst": "grok", "fixer": "codex", "tester": "kimi"},
          "guard_events": [{"role": "analyst"}], "blocked": []}


def make_runs(tmp, spec):
    for run_id, result in spec.items():
        run_dir = Path(tmp) / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        if result is not None:
            (run_dir / "result.json").write_text(json.dumps(result) if isinstance(result, dict) else result)


class RunsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_runs_root = cothink.RUNS_ROOT
        cothink.RUNS_ROOT = Path(self.tmp.name) / "runs"

    def tearDown(self):
        cothink.RUNS_ROOT = self.original_runs_root
        self.tmp.cleanup()

    def test_cli_help_lists_flags(self):
        proc = subprocess.run([sys.executable, "cothink.py", "runs", "--help"],
                              cwd=REPO, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in ("--last", "--status", "--json"):
            self.assertIn(flag, proc.stdout)

    def test_cli_table_and_json_end_to_end(self):
        make_runs(self.tmp.name, {
            "20260101-000000-a": {"run_id": "20260101-000000-a", "status": "passed", "iterations": 1,
                                  "engines_used": {}, "guard_events": [], "blocked": []},
            "20260102-000000-b": None,
        })
        env = {**os.environ, "COTHINK_HOME": self.tmp.name}
        proc = subprocess.run([sys.executable, "cothink.py", "runs"], cwd=REPO,
                              env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "RUN ID  STATUS  ITERS  ENGINES  GUARD  BLOCKED")
        self.assertTrue(lines[1].startswith("20260102-000000-b  -"))
        self.assertTrue(lines[2].startswith("20260101-000000-a  passed"))
        proc = subprocess.run([sys.executable, "cothink.py", "runs", "--json"], cwd=REPO,
                              env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIsInstance(payload, list)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["run_id"], "20260101-000000-a")

    def test_missing_runs_root_prints_header_only(self):
        cothink.RUNS_ROOT = Path(self.tmp.name) / "does-not-exist"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cothink.cmd_runs(argparse.Namespace(last=20, status=None, json=False))
        self.assertEqual(out.getvalue(), "RUN ID  STATUS  ITERS  ENGINES  GUARD  BLOCKED\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cothink.cmd_runs(argparse.Namespace(last=20, status=None, json=True))
        self.assertEqual(json.loads(out.getvalue()), [])

    def test_json_output_is_array_of_results(self):
        make_runs(self.tmp.name, {"20260101-000000-a": PASSED, "20260102-000000-b": None})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cothink.cmd_runs(argparse.Namespace(last=20, status=None, json=True))
        payload = json.loads(out.getvalue())
        self.assertIsInstance(payload, list)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["run_id"], "20260101-000000-a")
        self.assertEqual(payload[0]["status"], "passed")
        self.assertEqual(payload[0], PASSED)

    def test_missing_and_malformed_result_json(self):
        make_runs(self.tmp.name, {
            "20260103-000000-c": "{not json",
            "20260102-000000-b": None,
            "20260101-000000-a": PASSED,
        })
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cothink.cmd_runs(argparse.Namespace(last=20, status=None, json=False))
        self.assertEqual(out.getvalue(),
                         "RUN ID  STATUS  ITERS  ENGINES  GUARD  BLOCKED\n"
                         "20260102-000000-b  -  -  r=- a=- c=- an=- f=- t=-  -  -\n"
                         "20260101-000000-a  passed  2  r=gemini a=claude c=codex an=grok f=codex t=kimi  1  0\n")
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertTrue(err.getvalue().startswith("[cothink] runs: skipping 20260103-000000-c"))

    def test_status_filter_excludes_missing_result(self):
        make_runs(self.tmp.name, {
            "20260101-000000-a": PASSED,
            "20260102-000000-b": {**PASSED, "run_id": "20260102-000000-b", "status": "blocked"},
            "20260103-000000-c": None,
        })
        for status, expected in (("passed", ["20260101-000000-a"]),
                                 ("blocked", ["20260102-000000-b"]), ("max_iters_reached", [])):
            rows = cothink.list_runs(cothink.RUNS_ROOT, status=status)
            self.assertEqual([r["run_id"] for r in rows], expected)

    def test_last_caps_rows(self):
        make_runs(self.tmp.name, {
            "20260101-000000-a": PASSED,
            "20260103-000000-c": {**PASSED, "run_id": "20260103-000000-c", "status": "blocked"},
            "20260102-000000-b": {**PASSED, "run_id": "20260102-000000-b", "status": "max_iters_reached"},
        })
        rows = cothink.list_runs(cothink.RUNS_ROOT, last=2)
        self.assertEqual([r["run_id"] for r in rows], ["20260103-000000-c", "20260102-000000-b"])
        self.assertEqual(cothink.list_runs(cothink.RUNS_ROOT, last=0), [])

    def test_newest_first_by_name(self):
        make_runs(self.tmp.name, {
            "20260101-000000-a": PASSED,
            "20260103-000000-c": {**PASSED, "run_id": "20260103-000000-c", "status": "blocked"},
            "20260102-000000-b": {**PASSED, "run_id": "20260102-000000-b", "status": "max_iters_reached"},
        })
        rows = cothink.list_runs(cothink.RUNS_ROOT)
        self.assertEqual([r["run_id"] for r in rows],
                         ["20260103-000000-c", "20260102-000000-b", "20260101-000000-a"])


if __name__ == "__main__":
    unittest.main()
