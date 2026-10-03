"""python3 -m lib.graph init [DATA_DIR] | status [DATA_DIR]

init is the only place (with the 4.4 migration step) that creates graph.db.
On a fresh install it stamps the data dir first, because creating graph.db
makes the directory non-empty and `guard.py stamp --fresh` refuses those.
"""
import json
import os
import sys
from pathlib import Path

from lib.graph.schema import GraphNotInitialised
from lib.graph.store import open_graph


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    data = Path(argv[1] if len(argv) > 1 else os.environ.get("KARAKOS_DATA_DIR", "data"))
    if cmd == "init":
        from lib.migrate import guard
        if not guard.stamp_fresh(data):
            print("refusing: data dir holds unmigrated 1.x data; run: karakos migrate",
                  file=sys.stderr)
            return 78
        open_graph(data, create=True)
        print(f"graph: initialised {data / 'memory' / 'graph.db'}")
        return 0
    if cmd == "status":
        try:
            print(json.dumps(open_graph(data).status(), indent=2, default=str))
        except GraphNotInitialised as e:
            print(f"graph: {e}", file=sys.stderr)
            return 1
        return 0
    print("usage: python3 -m lib.graph init|status [DATA_DIR]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
