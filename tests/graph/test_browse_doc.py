"""docs/graph-browse-api.md examples must keep the real output's key set."""
import json
import re
from pathlib import Path

from lib.graph import browse
from tests.graph.helpers import _fresh_embedder, store  # noqa: F401,E402  (fixtures)

DOC = Path(__file__).resolve().parents[2] / "docs" / "graph-browse-api.md"
OPAQUE = {"last_consolidation"}


def shape(v):
    if isinstance(v, dict):
        return {k: (None if k in OPAQUE else shape(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [shape(v[0])] if v else []
    return None


def examples():
    text = DOC.read_text()
    out = [json.loads(m) for m in re.findall(r"```json\n(.*?)\n```", text, re.S)]
    assert len(out) == 4
    return out


def test_doc_examples_match_real_output(store, tmp_path):
    oid, _ = store.add_observation("alpha prefers green tea", entity="Alpha", domain="home",
                                   agent="a", tags=["pref"], confidence=0.8, embed=False)
    store.add_entity("Alpha", aliases=["Al"])
    store.add_edge("Alpha", "Beta", "knows")
    with store.write() as c:
        beta = c.execute("SELECT id FROM entities WHERE name='Beta'").fetchone()[0]
        c.execute("INSERT INTO observation_mentions VALUES (?,?)", (oid, beta))
        c.execute("INSERT INTO meta(key, value) VALUES ('last_consolidation', '{}')")
    data = store.path.parent.parent
    real = [browse.handle("status", {}, data)[1],
            browse.handle("observations", {"limit": "1"}, data)[1],
            browse.handle("entities", {}, data)[1],
            browse.handle("entity", {}, data, store.get_entity("Alpha")["id"])[1]]
    # observations example documents a non-null `next`; real has one row only
    real[1]["next"] = "b:1"
    for doc, got in zip(examples(), real):
        assert shape(doc) == shape(got), (doc, got)
