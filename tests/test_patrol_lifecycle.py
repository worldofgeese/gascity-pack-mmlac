import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import tomllib
import unittest


PACK = Path(__file__).resolve().parents[1]
HELPER = PACK / "assets/scripts/patrol.sh"
FORMULAS = ("mol-deacon-patrol", "mol-witness-patrol", "mol-refinery-patrol")
SURFACES = [
    *(PACK / "formulas" / f"{formula}.toml" for formula in FORMULAS),
    *(PACK / "agents" / role / "prompt.template.md" for role in ("deacon", "witness", "refinery")),
]


def success(**values):
    return {"schema_version": "1", "ok": True, **values}


def root_record(root_id="gcg-current", formula=FORMULAS[0], assignee="seat-1"):
    return {
        "id": root_id,
        "assignee": assignee,
        "ref": formula,
        "metadata": {"gc.kind": "wisp", "gc.formula_name": formula},
    }


FAKE_GC = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

path = Path(os.environ["PATROL_TEST_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
with Path(os.environ["PATROL_TEST_CALLS"]).open("a") as log:
    log.write(json.dumps(args) + "\n")

def flag(name):
    for i, arg in enumerate(args):
        if arg == name:
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return ""

def finish(operation, default):
    path.write_text(json.dumps(state))
    print(state.get(operation + "_output", default))
    code = state.get(operation + "_code", 0)
    if code:
        print("stub " + operation + " failed", file=sys.stderr)
    raise SystemExit(code)

if args[:2] == ["wisp", "list"]:
    roots = [row for row in state["roots"]
             if row["assignee"] == flag("--assignee") and row["ref"] == flag("--formula")]
    finish("list", json.dumps({"schema_version": "1", "ok": True, "roots": roots}))
if args[:2] == ["formula", "cook"]:
    state["cooks"] += 1
    state["last_formula"] = args[2]
    finish("cook", json.dumps({"schema_version": "1", "ok": True,
                               "root_id": "gcg-" + str(state["cooks"])}))
if args[:2] == ["bd", "update"]:
    if args[2].startswith("work-"):
        finish("work_update", "Updated work")
    if not state.get("update_code", 0):
        state["roots"].append({"id": args[2], "assignee": flag("--assignee"),
                              "ref": state["last_formula"],
                              "metadata": {"gc.kind": "wisp",
                                           "gc.formula_name": state["last_formula"]}})
    finish("update", "Updated root")
if args[:2] == ["wisp", "burn"]:
    if not state.get("burn_code", 0):
        matches = [row for row in state["roots"] if row["id"] == args[2]
                   and row["ref"] == flag("--formula") and row["assignee"] == flag("--assignee")]
        if len(matches) != 1 or "--force" not in args:
            print("stub refused non-exact burn", file=sys.stderr)
            raise SystemExit(79)
        state["roots"].remove(matches[0])
    finish("burn", "Burned root")
if args[:2] in (["mail", "send"], ["runtime", "drain-ack"]):
    finish("other", "")
print("unexpected gc command: " + repr(args), file=sys.stderr)
raise SystemExit(78)
"""


class PatrolLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix=".patrol-case-", dir=PACK / "tests")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        stub = self.bin / "gc"
        stub.write_text(FAKE_GC)
        stub.chmod(0o755)
        self.state = self.root / "state.json"
        self.calls = self.root / "calls.jsonl"
        self.env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.root),
            "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GC_ALIAS": "seat-1",
            "GC_AGENT": "rig/worker",
            "GC_BEAD_ID": "gcg-current",
            "PATROL_TEST_STATE": str(self.state),
            "PATROL_TEST_CALLS": str(self.calls),
        }
        self.reset()

    def reset(self, **overrides):
        self.state.write_text(json.dumps({"roots": [], "cooks": 0, **overrides}))
        self.calls.write_text("")

    def run_helper(self, mode="ensure", formula=FORMULAS[0], *args, helper=HELPER):
        return subprocess.run(
            [str(helper), mode, formula, *args], cwd=self.root, env=self.env,
            capture_output=True, text=True, check=False,
        )

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def assert_no_retirement(self):
        self.assertFalse(any(call[:2] in (["wisp", "burn"], ["runtime", "drain-ack"])
                             for call in self.recorded()))

    def test_initial_create_and_restart_reuse(self):
        for formula in FORMULAS:
            with self.subTest(formula=formula):
                self.reset()
                first = self.run_helper("ensure", formula)
                second = self.run_helper("ensure", formula)
                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(first.stdout, "gcg-1\n")
                self.assertEqual(second.stdout, first.stdout)
                calls = self.recorded()
                self.assertEqual([call[:2] for call in calls],
                                 [["wisp", "list"], ["formula", "cook"],
                                  ["bd", "update"], ["wisp", "list"]])
                self.assertIn(f"--formula={formula}", calls[0])
                self.assertIn("--assignee=seat-1", calls[0])
                self.assertIn("--json", calls[0])
                self.assertEqual(calls[1][2], formula)
                self.assertIn("--json", calls[1])
                self.assertIn(f"patrol_script={HELPER}", calls[1])
                self.assertEqual(calls[2], ["bd", "update", "gcg-1", "--assignee=seat-1"])

    def test_next_iteration_does_not_reuse_current(self):
        self.reset(roots=[root_record()])
        result = self.run_helper("new")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "gcg-1\n")
        self.assertEqual([call[:2] for call in self.recorded()],
                         [["formula", "cook"], ["bd", "update"]])
        self.assertEqual(len(json.loads(self.state.read_text())["roots"]), 2)

    def test_foreign_execution_or_formula_is_not_reused(self):
        foreign = [root_record("gcg-other", assignee="seat-2"),
                   root_record("gcg-another", formula=FORMULAS[1])]
        self.reset(roots=foreign)
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "gcg-1\n")
        self.assertEqual(json.loads(self.state.read_text())["roots"][:2], foreign)

    def test_agent_fallback_and_missing_identity(self):
        self.env.pop("GC_ALIAS")
        result = self.run_helper("new")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--assignee=rig/worker", self.recorded()[-1])
        self.env.pop("GC_AGENT")
        self.reset()
        result = self.run_helper("new")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.recorded(), [])

    def test_import_path_and_variable_values_with_spaces(self):
        other = self.root / "pack with spaces" / "assets" / "scripts" / "patrol.sh"
        other.parent.mkdir(parents=True)
        shutil.copy2(HELPER, other)
        result = self.run_helper("new", FORMULAS[2], "--var", "target_branch=branch with spaces",
                                 "--var", "binding_prefix=prefix.", helper=other)
        self.assertEqual(result.returncode, 0, result.stderr)
        cook = self.recorded()[0]
        self.assertIn("target_branch=branch with spaces", cook)
        self.assertIn("binding_prefix=prefix.", cook)
        self.assertIn(f"patrol_script={other}", cook)

    def test_failed_lookup_and_cook_keep_exit_status(self):
        for operation, code in (("list", 17), ("cook", 47)):
            with self.subTest(operation=operation):
                self.reset(**{f"{operation}_code": code})
                result = self.run_helper()
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn(f"stub {operation} failed", result.stderr)
                self.assertFalse(any(call[:2] == ["bd", "update"] for call in self.recorded()))
                self.assert_no_retirement()

    def test_failed_assignment_keeps_exit_status(self):
        self.reset(update_code=29)
        result = self.run_helper("new")
        self.assertEqual(result.returncode, 29, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("stub update failed", result.stderr)
        self.assert_no_retirement()

    def test_cook_rejects_malformed_or_missing_root_ids(self):
        outputs = ["", "{", "null", "[]", "{}", json.dumps({"new_epic_id": "gcg-1"}),
                   json.dumps(success(root_id="gcg-1")) * 2,
                   json.dumps(success(root_id="gcg-1")) + "\nwarning",
                   json.dumps({"schema_version": "1", "ok": False, "root_id": "gcg-1"}),
                   json.dumps({"schema_version": "2", "ok": True, "root_id": "gcg-1"})]
        outputs.extend(json.dumps(success(root_id=value)) for value in
                       (None, "", 7, False, [], {}, " ", " gcg-1", "gcg-1 ", "gcg-1\n",
                        "gcg-1\r", "gcg-1\t", "gcg-\x00", "--all", "gcg/1", "gcg-☃"))
        for output in outputs:
            with self.subTest(output=output):
                self.reset(cook_output=output)
                result = self.run_helper("new")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr)
                self.assertEqual([call[:2] for call in self.recorded()], [["formula", "cook"]])

    def test_lookup_rejects_ambiguous_or_malformed_results(self):
        outputs = ["", "{", "null", "[]", json.dumps(success()),
                   json.dumps(success(roots=None)), json.dumps(success(roots=[root_record()])),
                   json.dumps(success(roots=[root_record(), root_record("gcg-2")]))]
        outputs[6] *= 2
        outputs.extend(json.dumps(success(roots=[root_record(value)]))
                       for value in (None, "", 7, "gcg-1\n", "--all"))
        outputs.extend((json.dumps(success(roots=[root_record(assignee="seat-2")])),
                        json.dumps(success(roots=[root_record(formula=FORMULAS[1])]))))
        for output in outputs:
            with self.subTest(output=output):
                self.reset(list_output=output)
                result = self.run_helper()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual([call[:2] for call in self.recorded()], [["wisp", "list"]])

    def test_burn_is_exact_and_preserves_failure(self):
        self.reset(roots=[root_record(), root_record("gcg-next")])
        result = self.run_helper("burn", FORMULAS[0], "gcg-current")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded(), [
            ["wisp", "burn", "gcg-current", f"--formula={FORMULAS[0]}",
             "--assignee=seat-1", "--force"],
        ])
        self.assertEqual(json.loads(self.state.read_text())["roots"], [root_record("gcg-next")])
        self.reset(burn_code=38)
        result = self.run_helper("burn", FORMULAS[0], "gcg-current")
        self.assertEqual(result.returncode, 38)
        self.assertIn("stub burn failed", result.stderr)

    def test_invalid_helper_arguments_do_not_call_gc(self):
        for args in (("other", FORMULAS[0]), ("new", "arbitrary-formula"),
                     ("new", FORMULAS[0], "--attach", "work-1"),
                     ("new", FORMULAS[0], "--var"),
                     ("new", FORMULAS[0], "--var", "patrol_script=/other/script"),
                     ("burn", FORMULAS[0]), ("burn", FORMULAS[0], "--all"),
                     ("burn", FORMULAS[0], "gcg-1\n")):
            with self.subTest(args=args):
                self.reset()
                result = self.run_helper(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.recorded(), [])

    @staticmethod
    def render(code):
        replacements = {
            "{{patrol_script}}": str(HELPER), "{{ .ConfigDir }}": str(PACK),
            "{{target_branch}}": "main", "{{ .DefaultBranch }}": "main",
            "{{rig_name}}": "rig", "{{ .RigName }}": "rig",
            "{{binding_prefix}}": "prefix.", "{{ .BindingPrefix }}": "prefix.",
        }
        for old, new in replacements.items():
            code = code.replace(old, new)
        return code

    def test_every_capture_site_stops_on_cook_parse_or_assignment_failure(self):
        captures = [(path, match.group(0).strip()) for path in SURFACES
                    for match in re.finditer(r"^\s*(?:NEXT|NEW_WISP|WISP)=\$\(.+$",
                                             path.read_text(), re.MULTILINE)]
        self.assertEqual(len(captures), 9)
        for path, capture in captures:
            for failure in ({"cook_code": 47}, {"cook_output": "{}"}, {"update_code": 29}):
                with self.subTest(path=path.relative_to(PACK), capture=capture, failure=failure):
                    self.reset(**failure)
                    script = self.render(capture) + "\ngc runtime drain-ack\n"
                    result = subprocess.run(["bash", "-c", script], cwd=self.root, env=self.env,
                                            capture_output=True, text=True, check=False)
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assert_no_retirement()

    def test_every_next_capture_assigns_next_before_burning_current(self):
        captures = [(path, match.group(0).strip()) for path in SURFACES
                    for match in re.finditer(r"^\s*NEXT=\$\(.+$", path.read_text(), re.MULTILINE)]
        self.assertEqual(len(captures), 6)
        for path, capture in captures:
            formula = next(name for name in FORMULAS if name in capture)
            with self.subTest(path=path.relative_to(PACK), capture=capture):
                self.reset(roots=[root_record(formula=formula)])
                script = (self.render(capture) +
                          f'\n"{HELPER}" burn {formula} gcg-current || exit $?\n' +
                          'printf "%s\\n" "$NEXT"\n')
                result = subprocess.run(["bash", "-c", script], cwd=self.root, env=self.env,
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "Burned root\ngcg-1\n")
                self.assertEqual([call[:2] for call in self.recorded()],
                                 [["formula", "cook"], ["bd", "update"], ["wisp", "burn"]])
                self.assertEqual(json.loads(self.state.read_text())["roots"],
                                 [root_record("gcg-1", formula=formula)])

    def test_nested_refinery_failure_never_burns_or_acknowledges(self):
        formula = tomllib.loads((PACK / "formulas/mol-refinery-patrol.toml").read_text())
        body = next(step["description"] for step in formula["steps"] if step["id"] == "merge-push")
        function = re.search(r"block_existing_pr\(\) \{.*?\n\}", body, re.DOTALL)
        self.assertIsNotNone(function)
        script = textwrap.dedent("""
            WORK=work-1
            EXISTING_PR=https://example.invalid/pull/1
            BRANCH=topic
            TARGET=main
        """) + self.render(function.group(0)) + '\nblock_existing_pr "invalid handoff"\n'
        for failure in ({"cook_code": 47}, {"cook_output": "{}"},
                        {"update_code": 29}, {"burn_code": 38}):
            with self.subTest(failure=failure):
                self.reset(roots=[root_record(formula=FORMULAS[2])], **failure)
                result = subprocess.run(["bash", "-c", script], cwd=self.root, env=self.env,
                                        capture_output=True, text=True, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(call[:2] == ["runtime", "drain-ack"] for call in self.recorded()))
                if "burn_code" not in failure:
                    self.assert_no_retirement()
        self.env.pop("GC_BEAD_ID")
        self.reset()
        result = subprocess.run(["bash", "-c", script], cwd=self.root, env=self.env,
                                capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_retirement()


class PatrolSurfaceTests(unittest.TestCase):
    def test_formulas_remain_root_only_with_their_instructions(self):
        for formula, count in zip(FORMULAS, (7, 8, 8)):
            with self.subTest(formula=formula):
                body = tomllib.loads((PACK / "formulas" / f"{formula}.toml").read_text())
                self.assertEqual(body["phase"], "vapor")
                self.assertFalse(body.get("pour", False))
                self.assertEqual(len(body["steps"]), count)
                self.assertTrue(body["vars"]["patrol_script"]["required"])
                if formula == FORMULAS[2]:
                    self.assertEqual(body["contract"], "graph.v2")

    def test_all_patrol_creation_and_retirement_surfaces_use_helper(self):
        for path in SURFACES:
            with self.subTest(path=path.relative_to(PACK)):
                text = path.read_text()
                self.assertNotRegex(text, r"gc bd mol wisp mol-(?:deacon|witness|refinery)-patrol")
                self.assertNotIn(".new_epic_id", text)
                self.assertNotIn("gc bd mol burn", text)
                if path.suffix == ".toml":
                    self.assertIn('"{{patrol_script}}"', text)
                else:
                    self.assertIn('"{{ .ConfigDir }}/assets/scripts/patrol.sh"', text)
        deacon = (PACK / "agents/deacon/prompt.template.md").read_text()
        self.assertIn("gc bd mol wisp gc --age 24h --dry-run", deacon)


if __name__ == "__main__":
    unittest.main()
