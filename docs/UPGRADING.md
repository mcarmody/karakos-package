# Upgrading Karakos

Manual upgrade instructions. Karakos does not auto-update.

Karakos ships prebuilt multi-arch images to GHCR, so upgrading is a pull and a
restart — there is no local build step.

`latest` always tracks the newest release. To control when you upgrade, pin
`KARAKOS_VERSION` in `config/.env` (e.g. `KARAKOS_VERSION=v1.3`). Remove the
pin or update it when you are ready to move.

## A note on the Docker commands below

The compose file lives at `config/docker-compose.yml`, not at the repo root,
so a bare `docker compose` run from your checkout will not find it. Every
command here uses the `make` targets, which pass the right flags for you. The
long form, if you prefer typing it, is:

```bash
docker compose -f config/docker-compose.yml --env-file config/.env <command>
```

There is one service, named `karakos`. Address it by that name — `docker
compose exec karakos …`, not by a guessed container name.

## Version check

`bin/check-updates.sh` runs weekly (Mondays 05:00) and pokes your signals
channel when a newer release exists, once per release. It compares against
`KARAKOS_VERSION` — the same value `config/docker-compose.yml` uses to pick the
image — so it reports at whatever precision you pinned: on `v1.3` it tells you
about `v1.4`, not about the `v1.3.1` build that is already published under the
`v1.3` tag you are tracking. On the default `latest` it names the new release
and tells you to pull, since there is no version to compare against.

To check on demand:

```bash
bin/check-updates.sh          # --force re-announces a release already seen
```

The notice arrives through `bin/poke.sh`, not a direct webhook post, so an
agent reads the release notes and tells you what changed. That is the opposite
choice from `bin/cli-upgrade-watchdog.sh` below, which bypasses the agent queue
deliberately — the thing *it* reports is that agents cannot answer, which does
not apply here.

## Upgrade

### 1. Back up, while it is still running

The data directory is a **named Docker volume**, not a folder in your
checkout. Copying `./data` from the host backs up nothing. Copy it out of the
container instead:

```bash
cd ~/karakos
docker compose -f config/docker-compose.yml --env-file config/.env \
  cp karakos:/workspace/data ./data-backup-$(date +%Y%m%d)

cp config/.env config/.env.backup    # credentials — keep this somewhere safe
```

`config/`, `agents/` and `.karakos/` *are* bind-mounted from your checkout, so
those are backed up by copying the directories or committing them.

### 2. Stop the system

```bash
make down
```

Shutdown is graceful: agents finalize their sessions first, up to 45 seconds.

### 3. Pull the new code

```bash
git pull origin main
```

With local modifications, stash them first:

```bash
git stash && git pull origin main && git stash pop
```

### 4. Read the release notes

Breaking changes are documented on the GitHub release for the version you are
moving to. Check them before the next step, not after.

Note that `git pull` does **not** change which image runs. The image tag comes
from `KARAKOS_VERSION` in `config/.env`, defaulting to `latest`. If you have
pinned a version, bump the pin here or the next step will re-pull what you are
already on. Releases are tagged `v<major>.<minor>` and `v<major>` only — there
are no patch-level image tags.

### 5. Pull the image and start

```bash
make pull
make up
```

### 6. Verify

1. `make logs` — watch for startup errors.
2. Open the dashboard; confirm agents show as running.
3. Say something to your agent in Discord and confirm it answers. There is no
   startup health report to check for — the health sweep posts only on
   failure, and only at 04:00.

If startup fails saying `data/`, `logs/` or `inbox/` is not writable, a
previous run left root-owned volumes behind. That needs
`docker compose -f config/docker-compose.yml --env-file config/.env down -v`,
which destroys those volumes — restore from the step-1 backup afterwards.

## Database schema

There is no migration command to run and no `bin/migrate.py`. The agent server
creates its tables with `CREATE TABLE IF NOT EXISTS` and adds new columns to
existing tables on startup, guarded by a `PRAGMA table_info` check. Both are
idempotent, so starting a newer image on an older database is the whole
procedure.

If startup fails with a database error, that is a bug worth an issue — not
something to fix by hand.

## The Claude CLI rolls back on its own

Everything above is about upgrading Karakos. The agent loop also runs on the
Claude CLI, which this project does not release and which is replaced on every
image pull.

A bad CLI release is the one failure that gives you no signal: it installs
cleanly, `claude --version` answers, every container process stays up, and
messages simply stop being answered.

`bin/cli-upgrade-watchdog.sh` runs at startup and hourly. When the installed
CLI version differs from the last one this install completed a turn on, it
sends one probe message through the CLI. If no answer comes back, it
reinstalls the known-good version and posts a notice to your signals channel.
No API call is made when the version has not changed.

To upgrade the CLI deliberately, behind the same guarantee:

```bash
docker compose -f config/docker-compose.yml --env-file config/.env \
  exec -u karakos karakos bin/upgrade-claude-cli.sh --to 1.2.3
```

It records the current version before touching anything, verifies the new one
can complete a turn, and reverts if it cannot. Exit codes: `0` upgraded and
verified, `1` reverted, `2` reverted and the revert failed too, `3` refused to
run.

To confirm the guard is armed without breaking your CLI to find out:

```bash
bin/cli-upgrade-watchdog.sh --selftest
bin/upgrade-claude-cli.sh --selftest
```

Both run entirely against fake `npm` and `claude` binaries and change nothing.

The rollback state lives in `data/health/claude-cli.json` inside the
container. Delete it to make the watchdog adopt whatever is installed now.

## Rolling back

```bash
make down

# Restore the data volume from the backup you took in step 1
make up
docker compose -f config/docker-compose.yml --env-file config/.env \
  cp ./data-backup-YYYYMMDD/. karakos:/workspace/data
make down

cp config/.env.backup config/.env
```

Then roll the **image** back, which is the part that matters. Checking out an
old git tag does not do it — set the pin explicitly in `config/.env`:

```bash
KARAKOS_VERSION=v1.2         # the release you were on
```

```bash
git checkout v1.2            # match the checkout to the pin
make pull
make up
```

Restoring into the volume needs the container to exist, which is why it starts
and stops again around the copy.

## Version history

The full changelog is on the GitHub releases page.
