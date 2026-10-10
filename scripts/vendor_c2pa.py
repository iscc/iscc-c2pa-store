"""Vendor the public C2PA data the store reads at runtime into `iscc_c2pa_store/vendor/`, unmodified.

- `softbinding-algorithm-list.json` - the C2PA Soft Binding Algorithm List. The store indexes and answers queries
  only for algorithms on this list and reports them by their `type` in `GET /services/supportedAlgorithms`.
- `C2PA-TRUST-LIST.pem` - the C2PA trust list. Signing credentials are checked against it; the outcome is recorded,
  not required.

Both are by the Coalition for Content Provenance and Authenticity (C2PA), licensed under CC BY 4.0. Each file is
taken at the latest commit that changed it; `README.md` records the commits. Rerun after upstream changes; the git
diff shows what changed.

Usage:

    uv run poe vendor-c2pa
"""

import json
from pathlib import Path

import httpx2

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "iscc_c2pa_store" / "vendor"
SOURCES = (  # (repository, path in repository, local file name)
    ("c2pa-org/softbinding-algorithm-list", "softbinding-algorithm-list.json", "softbinding-algorithm-list.json"),
    ("c2pa-org/conformance-public", "trust-list/C2PA-TRUST-LIST.pem", "C2PA-TRUST-LIST.pem"),
)


def latest_commit(client, repo, path):
    # type: (httpx2.Client, str, str) -> str
    """Return the SHA of the latest commit that changed `path` in `repo`."""
    response = client.get(f"https://api.github.com/repos/{repo}/commits", params={"path": path, "per_page": 1})
    response.raise_for_status()
    return response.json()[0]["sha"]


def download(client, repo, commit, path):
    # type: (httpx2.Client, str, str, str) -> bytes
    """Download `path` from `repo` at `commit`."""
    response = client.get(f"https://raw.githubusercontent.com/{repo}/{commit}/{path}")
    response.raise_for_status()
    return response.content


def readme(entries):
    # type: (list[tuple[str, str, str, str]]) -> str
    """Attribution and provenance of the vendored files."""
    lines = ["# Vendored C2PA data", ""]
    for repo, path, name, commit in entries:
        lines.append(f"- `{name}`: unmodified copy of `{path}` from [{repo}](https://github.com/{repo}) at `{commit}`")
    lines += [
        "",
        "By the Coalition for Content Provenance and Authenticity (C2PA), licensed under",
        "[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Written by `scripts/vendor_c2pa.py`; do not edit.",
        "",
    ]
    return "\n".join(lines)


def main():
    # type: () -> None
    """Download every source at its latest commit and write the files with a README."""
    VENDOR.mkdir(parents=True, exist_ok=True)
    entries = []
    with httpx2.Client(timeout=60, follow_redirects=True) as client:
        for repo, path, name in SOURCES:
            commit = latest_commit(client, repo, path)
            data = download(client, repo, commit, path)
            if name.endswith(".json"):
                json.loads(data)  # refuse to vendor a broken document
            (VENDOR / name).write_bytes(data)
            entries.append((repo, path, name, commit))
            print(f"Wrote vendor/{name} from {repo}@{commit[:8]}")
    (VENDOR / "README.md").write_text(readme(entries), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
