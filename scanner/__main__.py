"""Debug CLI: python -m scanner <request.json> <snapshot.json> <repo_root>

Prints a serialized ScanResult. Local and read-only; for development only.
"""
import json
import sys
from pathlib import Path

from .contracts import RepositorySnapshot, ScanRequest
from .service import scan_repository


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    req = ScanRequest.model_validate(json.loads(Path(argv[0]).read_text()))
    snap = RepositorySnapshot.model_validate(json.loads(Path(argv[1]).read_text()))
    print(scan_repository(req, snap, Path(argv[2])).model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
