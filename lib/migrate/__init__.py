"""Karakos data migrator. The migrator is the only writer of 1.x data; boot
code may only check the schema stamp (see lib/migrate/guard.py)."""

SCHEMA_VERSION = 2
PACKAGE_VERSION = "2.0.0"
STAMP_NAME = ".schema-version"
EXIT_OK, EXIT_STEP_FAILED, EXIT_USAGE, EXIT_REFUSED, EXIT_GUARD = 0, 1, 2, 3, 78
