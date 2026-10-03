"""Final gate before the stamp. The runner core remains the only stamp writer
(after the report is written); this step proves the data is fit to be stamped:
every database in the data directory opens and passes sqlite's quick_check."""
import sqlite3
from pathlib import Path

from lib.migrate.detect import ro_uri
from lib.migrate.runner import Step


def _dbs(data: Path):
    return [p for p in sorted(data.rglob("*.db")) if p.is_file()]


def _verify(ctx):
    bad = []
    for db in _dbs(Path(ctx.data_dir)):
        try:
            con = sqlite3.connect(ro_uri(db), uri=True)
            try:
                if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    bad.append(db.name)
            finally:
                con.close()
        except sqlite3.Error:
            bad.append(db.name)
    if bad:
        raise RuntimeError("database(s) failed quick_check: " + ", ".join(bad))


STEP = Step("90_stamp", 1, 2, detect=lambda ctx: True, apply=lambda ctx: None,
            verify=_verify)
