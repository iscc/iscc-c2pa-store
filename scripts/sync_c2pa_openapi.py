"""Copy the subset of the C2PA Soft Binding Resolution API definition that this store implements.

The C2PA OpenAPI document (CC BY 4.0) lives in the C2PA specification repository. This script keeps the operations
listed in OPERATIONS and every component schema they reference, verbatim, and writes them to
`iscc_c2pa_store/openapi/c2pa-sbr.json` together with the source commit. `tests/test_openapi.py` checks that the
hand-written openapi.yaml stays compatible with it. Rerun the script after
upstream changes; the git diff of the output shows what changed.

Usage:

    uv run poe sync-c2pa <path to a specs-core checkout>
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "iscc_c2pa_store" / "openapi" / "c2pa-sbr.json"
SOURCE = "docs/modules/softbinding/partials/softbinding-resolution-api.openapi.json"
OPERATIONS = {
    "/matches/byBinding": ["get", "post"],
    "/manifests": ["post"],
    "/manifests/{activeManifestId}": ["get", "delete"],
    "/services/supportedAlgorithms": ["get"],
    "/services/status": ["get"],
    "/services/capabilities": ["get"],
    "/.well-known/c2pa-soft-binding-resolution": ["get"],
}
REF = re.compile(r'"\$ref":\s*"#/components/schemas/([^"]+)"')


def git(repo, *args):
    # type: (Path, str) -> str
    """Run a git command in `repo` and return its output."""
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout  # noqa: S603, S607


def referenced_schemas(node, schemas):
    # type: (object, dict) -> set[str]
    """Names of all component schemas reachable from `node`, following references transitively."""
    found = set()  # type: set[str]
    pending = set(REF.findall(json.dumps(node)))
    while pending:
        name = pending.pop()
        if name not in found:
            found.add(name)
            pending |= set(REF.findall(json.dumps(schemas[name])))
    return found


def subset(spec, commit):
    # type: (dict, str) -> dict
    """Return the implemented operations and their schemas from the full upstream document."""
    paths = {}
    for path, methods in OPERATIONS.items():
        item = spec["paths"][path]
        paths[path] = {key: value for key, value in item.items() if key in methods or key in ("servers", "parameters")}
    schemas = spec["components"]["schemas"]
    names = referenced_schemas(paths, schemas)
    return {
        "openapi": spec["openapi"],
        "info": {
            **spec["info"],
            "x-source": f"https://github.com/c2pa-org/specs-core/blob/{commit}/{SOURCE}",
            "x-subset": "Operations implemented by iscc-c2pa-store, copied verbatim by scripts/sync_c2pa_openapi.py",
        },
        "paths": paths,
        "components": {
            "schemas": {name: schemas[name] for name in schemas if name in names},
            "securitySchemes": spec["components"]["securitySchemes"],
        },
    }


def main(repo):
    # type: (Path) -> None
    """Write the subset from the committed upstream document at HEAD of `repo`."""
    commit = git(repo, "rev-parse", "HEAD").strip()
    spec = json.loads(git(repo, "show", f"HEAD:{SOURCE}"))
    TARGET.write_bytes((json.dumps(subset(spec, commit), indent=2, ensure_ascii=False) + "\n").encode())
    print(f"Wrote {TARGET.relative_to(ROOT)} from {repo.name}@{commit[:8]} (C2PA API {spec['info']['version']})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: uv run poe sync-c2pa <path to a specs-core checkout>")
    main(Path(sys.argv[1]))
