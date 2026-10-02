# Claude Polecats & Codex Reviewer Pack

I use it the following way:

I add two folders to the my city:
- packs
- projects

Projects contains all the git repos for the city
Packs has my packs in it. In there, check out this pack as "mmlac"

In your city, refer the pack as follows:

```
[[rigs]]
name = "gascitysource"
prefix = "gct"
includes = ["./packs/mmlac"]
```

## Root-only patrols

The deacon, witness, and refinery patrols use `phase = "vapor"`: one
executable root per iteration, with instructions kept in the formula rather
than materialized as child beads. Refinery retains its graph compiler
contract.

These patrols require Bash, jq, and a Gas City build providing:

- `gc formula cook --json`
- `gc wisp list --formula ... --assignee ... --json`
- `gc wisp burn <root-id> --formula ... --assignee ... --force`
- Atomic isolated deletion in the selected graph-class store (SQLite or file)

Standalone cooks, restart lookup, and retirement use the graph-class
destination. Configure the city's graph storage binding when coordination
graphs must be separate from work. Ordinary work listing and shared-work
eligibility are unchanged.

The graph binding must support checking isolation and deleting the root in
one transaction or lock. A `bd`-only graph binding does not provide that
capability; configure a capable local graph binding before using these
patrols. This does not require changing the work store.

`assets/scripts/patrol.sh` centralizes the lifecycle:

| Operation | Behavior |
|---|---|
| `ensure <formula> [--var name=value ...]` | Reuse the execution's sole unfinished root; create and assign one only after a successful empty lookup |
| `new <formula> [--var name=value ...]` | Create and assign the next root, then print its ID |
| `burn <formula> <root-id>` | Retire exactly that root, without a digest, close, cascade, or cross-store sweep |

The execution is `GC_ALIAS`, falling back to `GC_AGENT`; this identifies a
patrol run, not ownership of work. Multiple roots, failed commands, malformed
results, or unsupported graph shapes stop the operation. The helper never
falls back to work-store graph creation. A new root must be successfully
created and assigned before the previous root is burned.

Creation and assignment are not an atomic singleton allocation. Run one
initializer per execution. If a cook or assignment fails, or a restart
leaves both the previous and next root, inspect the graph-class runs before
retrying. An empty assigned-root lookup alone does not prove that an
ambiguous cook created nothing.

Prompts invoke the helper through their rendered `ConfigDir`, so rig-only
imports and renamed pack bindings do not need a city-level pack command.
The helper supplies the required `patrol_script` formula variable for later
iterations. Startup also passes that variable to `gc formula show`, which
otherwise deliberately leaves placeholders intact.

This is a patrol lifecycle change, not a complete live multiplayer runtime.
It does not enable witness, change orphan recovery, or enable maintenance
orders.

### Local regression checks

Tests use Python's standard library and a fake `gc`, with all scratch files
inside this checkout. They do not connect to services or create real beads.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_patrol_lifecycle.py
shellcheck assets/scripts/patrol.sh
bash -n assets/scripts/patrol.sh
```
