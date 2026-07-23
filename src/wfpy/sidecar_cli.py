"""wfpy.sidecar_cli — helper CLI to send JSONL ops to wfpy-sidecar."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


def _parse_json_arg(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a single wfpy sidecar op.")
    parser.add_argument("--file", required=True, help="Path to Python workflow file")
    parser.add_argument("--op", required=True, help="Operation name, e.g. wfpy.createNode")
    parser.add_argument("--args", default="{}", help="JSON object with op args")
    parser.add_argument("--sidecar", default="wfpy-sidecar", help="Sidecar command")
    args = parser.parse_args()

    payload = {
        "file": args.file,
        "op": args.op,
        "args": _parse_json_arg(args.args),
    }

    proc = subprocess.run(
        [args.sidecar],
        input=json.dumps(payload) + "\n",
        text=True,
        capture_output=True,
    )

    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(proc.returncode)

    if proc.stdout:
        sys.stdout.write(proc.stdout)


if __name__ == "__main__":
    main()
