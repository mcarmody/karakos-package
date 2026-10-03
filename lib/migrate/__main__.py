"""python3 -m lib.migrate [--dry-run|--auto|--force|--to-backup DIR]"""
import argparse
import os
import sys
from pathlib import Path

from lib.migrate import EXIT_OK, EXIT_STEP_FAILED, EXIT_USAGE


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(message, file=sys.stderr)
        sys.exit(EXIT_USAGE)


def main(argv=None) -> int:
    p = _Parser(prog="karakos migrate")
    p.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    p.add_argument("--auto", action="store_true", help="non-interactive (Docker)")
    p.add_argument("--force", action="store_true", help="proceed on unknown schema")
    p.add_argument("--to-backup", metavar="DIR", help="restore a backup directory")
    p.add_argument("--root", default=os.environ.get("WORKSPACE_ROOT", "/workspace"),
                   help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    root = Path(a.root)
    data, config = root / "data", root / "config"

    if a.to_backup:
        from lib.migrate import backup
        try:
            backup.restore(a.to_backup, data_dir=data, config_dir=config)
        except backup.BackupError as e:
            print(e, file=sys.stderr)
            return EXIT_STEP_FAILED
        print(f"restored from {a.to_backup}")
        return EXIT_OK

    from lib.migrate import runner
    return runner.run(data, config, backup_root=root / "backups",
                      dry_run=a.dry_run, force=a.force)


if __name__ == "__main__":
    sys.exit(main())
