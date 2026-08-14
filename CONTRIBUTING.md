# Contributing to Karakos

## Getting set up

```bash
git clone https://github.com/mcarmody/karakos-package.git
cd karakos-package
pip install -r requirements.txt pytest
```

Docker (24+, Compose v2) is only needed for the fast/live tests that exercise
the built container — pure Python/shell tests run without it.

## Running tests

```bash
pytest                          # full suite
pytest -m "not slow"            # skip the Docker-dependent tests
pytest tests/test_setup.py -v   # a single file
```

`ci.yml` runs the same lint/syntax and test steps on every push and PR to
`main` — check it locally with `bash -n` on shell scripts and `python -m
py_compile` on Python scripts before opening a PR if you touched either.

## Making a change

1. Fork the repo and branch from `main`.
2. Keep changes focused — one logical change per PR.
3. Add or update tests for behavior you change.
4. Run the test suite locally; make sure CI is green before requesting review.
5. Open a PR against `main` and fill in the PR template.

Note `config/protected-paths.json`: some paths (`system/`, `config/`,
`bin/agent-server.py`, `bin/relay.py`, `bin/entrypoint.sh`,
`bin/scheduler.py`, `.karakos/`, `Dockerfile`) are tier-1 protected in
deployed instances — changes there get extra scrutiny since they affect
process lifecycle and security boundaries.

## Reporting bugs / requesting features

Use the issue templates on GitHub (Issues → New Issue). Include your OS,
Docker version, and relevant logs from `logs/` where applicable.
