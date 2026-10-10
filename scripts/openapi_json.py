"""Write `openapi.json` next to the hand-written `openapi.yaml`, so browsers can view the contract instead of
downloading it. Run by `uv run poe codegen`; `tests/test_openapi.py` checks that both files agree.
"""

import json
from pathlib import Path

import yaml

SPEC_DIR = Path(__file__).parent.parent / "iscc_c2pa_store" / "openapi"


def main():
    # type: () -> None
    """Convert openapi.yaml to openapi.json."""
    spec = yaml.safe_load((SPEC_DIR / "openapi.yaml").read_text(encoding="utf-8"))
    text = json.dumps(spec, indent=2, ensure_ascii=False) + "\n"
    (SPEC_DIR / "openapi.json").write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
