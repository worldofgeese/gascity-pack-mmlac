"""Exercise the actual helper and gc in disposable file and SQLite cities."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


PACK = Path(__file__).resolve().parents[1]
FORMULAS = ("mol-deacon-patrol", "mol-witness-patrol", "mol-refinery-patrol")


def run(argv, city, env, *, success=True):
    result = subprocess.run(argv, cwd=city, env=env, text=True, capture_output=True, timeout=60)
    if (result.returncode == 0) != success:
        raise RuntimeError(
            f"{argv!r}: expected success={success}, exit={result.returncode}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result.stdout.strip()


def check(cli, storage):
    with tempfile.TemporaryDirectory(prefix="patrol-native-") as directory:
        root = Path(directory)
        city = root / "city"
        (city / "formulas").mkdir(parents=True)
        for formula in FORMULAS:
            shutil.copyfile(
                PACK / "formulas" / f"{formula}.toml",
                city / "formulas" / f"{formula}.formula.toml",
            )
        config = """
[workspace]
name = "patrol-test"
provider = "claude"

[providers.claude]
base = "builtin:claude"

[beads]
provider = "file"

[daemon]
formula_v2 = true

[[agent]]
name = "control-dispatcher"
start_command = "gc convoy control --serve --follow {{.Agent}}"
prompt_mode = "none"
process_names = ["gc"]
max_active_sessions = 1
"""
        if storage == "split-sqlite":
            config += """
[storage.classes]
work = "work"
graph = "infra"
sessions = "infra"
messaging = "infra"
orders = "infra"
nudges = "infra"

[storage.bindings.infra]
provider = "sqlite-beads"
path = ".gc/store"
"""
        (city / "city.toml").write_text(config)
        for name in ("home", "gc-home", "runtime", "bin"):
            (root / name).mkdir()
        (root / "bin" / "gc").symlink_to(cli)
        for name in ("bash", "jq", "git"):
            executable = shutil.which(name)
            if executable is None:
                raise RuntimeError(f"{name} is required")
            (root / "bin" / name).symlink_to(executable)
        env = {
            "HOME": str(root / "home"),
            "GC_HOME": str(root / "gc-home"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
            "GC_CITY_PATH": str(city),
            "GC_SESSION": "fake",
            "GC_BEADS": "file",
            "GC_DOLT": "skip",
            "GC_ALIAS": "test-execution",
            "GC_AGENT": "test-worker",
            "PATH": f"{root / 'bin'}:{os.defpath}",
            "NO_COLOR": "1",
        }
        if shutil.which("bd", path=env["PATH"]):
            raise RuntimeError("This integration must have no bd executable")
        (city / ".gc").mkdir()
        work_path = city / ".gc" / "beads.json"
        work_path.write_text(json.dumps({
            "seq": 1,
            "beads": [{
                "id": "work-1", "title": "unrelated work sentinel",
                "issue_type": "task", "status": "open",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }],
            "revisions": {"work-1": 1},
            "revisions_sealed": True,
        }))
        before = json.loads(work_path.read_text())
        helper = str(PACK / "assets" / "scripts" / "patrol.sh")
        for formula in FORMULAS:
            variables = [] if formula == "mol-witness-patrol" else ["--var", "binding_prefix=test."]
            if formula == "mol-refinery-patrol":
                variables += ["--var", "target_branch=main", "--var", "rig_name=test-rig"]
            first = run([helper, "ensure", formula, *variables], city, env)
            if not first or "\n" in first:
                raise RuntimeError("Helper did not return exactly one root ID")
            lookup = ["gc", "wisp", "list", "--formula", formula,
                      "--assignee", env["GC_ALIAS"], "--json"]
            listed = json.loads(run(lookup, city, env))
            if [row["id"] for row in listed["roots"]] != [first]:
                raise RuntimeError(f"The created root was not listed: {listed!r}")
            if run([helper, "ensure", formula, *variables], city, env) != first:
                raise RuntimeError("Restart created a duplicate root")
            following = run([helper, "new", formula, *variables], city, env)
            if not following or following == first:
                raise RuntimeError("Next iteration did not allocate a fresh root")
            run([helper, "ensure", formula, *variables], city, env, success=False)
            run([helper, "burn", formula, first], city,
                {**env, "GC_ALIAS": "another-execution"}, success=False)
            run([helper, "burn", formula, first], city, env)
            if run([helper, "ensure", formula, *variables], city, env) != following:
                raise RuntimeError("Restart did not reuse the surviving next root")
            run([helper, "burn", formula, following], city, env)
            remaining = json.loads(run(lookup, city, env))
            if remaining.get("ok") is not True or remaining.get("roots") != []:
                raise RuntimeError("Retired roots remain visible")
        after = json.loads(work_path.read_text())
        if before["beads"] != after["beads"] or before["revisions"] != after["revisions"]:
            raise RuntimeError("The ordinary work projection changed")
        return {"storage": storage, "formulas": list(FORMULAS), "unrelated_work_unchanged": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gc", type=Path, required=True, help="Path to the native gc executable")
    cli = parser.parse_args().gc.resolve()
    if not cli.is_file() or not os.access(cli, os.X_OK):
        parser.error("--gc must be an executable file")
    print(json.dumps({
        "ok": True,
        "cases": [check(cli, storage) for storage in ("single-file", "split-sqlite")],
    }, indent=2))
