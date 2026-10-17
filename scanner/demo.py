"""Human-readable scanner demo. Local and read-only.

From the repo root:
    python -m scanner.demo                      # scan the bundled example consumers
    python -m scanner.demo PATH [SERVICE_ID]    # scan any folder (default service: demo-payments)
    python -m scanner.demo --json               # also print the full ScanResult JSON

Uses test-double endpoint/snapshot IDs; real ones come from the backend.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .samples.generate import ROOT, build

TARGETS = [
    ("demo-payments", ROOT / "examples" / "consumers"),
    ("stripe", ROOT / "examples" / "consumers" / "stripe-merchant"),
]


def show(service_id: str, root: Path, as_json: bool) -> None:
    result = build(service_id, root)
    print("=" * 78)
    print(f"Scanned {root}  for service '{service_id}'")
    c = result.coverage
    print(f"Files scanned: {c.files_scanned}   skipped: {c.files_skipped}   "
          f"dependencies found: {len(result.dependencies)}")
    for i, d in enumerate(result.dependencies, 1):
        use = d.evidence[-1]
        operand = "" if d.usage.operand is None else f" {d.usage.operand}"
        dest = "" if d.usage.destination is None else f" -> {d.usage.destination}"
        print(f"\n  [{i}] {d.language}: {d.field_path}  {d.usage.operation}{operand}{dest}  ({d.confidence})")
        print(f"      origin: {d.origin.call} -> endpoint {d.endpoint_id}")
        print("      evidence chain:")
        for e in d.evidence:
            print(f"        {e.file}:{e.line_start}  {e.excerpt}")
    if c.unsupported_patterns:
        print("\n  Not followed (unknown, not safe):")
        for p in c.unsupported_patterns:
            print(f"    - {p}")
    if result.warnings:
        print("\n  Warnings:")
        for w in result.warnings:
            print(f"    - {w}")
    if as_json:
        print("\n" + result.model_dump_json(indent=2))
    print()


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    args = [a for a in argv if a != "--json"]
    if args:
        root = Path(args[0]).resolve()
        if not root.is_dir():
            print(f"Not a folder: {root}", file=sys.stderr)
            return 2
        show(args[1] if len(args) > 1 else "demo-payments", root, as_json)
    else:
        for service_id, root in TARGETS:
            show(service_id, root, as_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
