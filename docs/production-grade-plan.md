# Karakos: production-grade makeover plan

**Written** 2026-08-28 (overnight taskboard session, task `c8ca167b`).
**Origin** Mike, 2026-08-27 23:50 (#admin): *"Friends getting in on the agent
game has me wanting to spend a lot more time on karakos as a project. We need
to give it a production grade makeover."*

**Scope** is `mcarmody/karakos-package` — the public, installable package.
Not the private `karakos` workspace repo.

This plan is written against what is actually in the repo as of commit
`f84113a`, not against the task's original description. Two things in that
description turned out to be wrong, and correcting them changes the priorities:

- **"CI is lint+syntax only, no coverage tracking visible."** Half right. CI
  runs five jobs — lint/syntax, full unit tests, a Docker build smoke test,
  a preflight suite and Compose validation — over 47 test files against 71
  Python source files. What is missing is *coverage measurement* and an
  actual *linter* (the "lint" job only runs `py_compile`, which is a syntax
  check wearing a lint costume). So the gap is narrower and cheaper to close
  than "we have no tests."
- **Something more urgent than any of the four deliverables surfaced.** See
  §0.

---

## §0 — The release gap, which outranks everything below

`v1.4.1` was tagged **2026-08-04**. `main` is **43 commits ahead of it.**

The install one-liner in the README pulls the prebuilt image from GHCR, and
GHCR's `latest` is built by `release.yml`, which fires **only on a `v*` tag
push**. So every person who has run that curl command since August 4 — which
is to say every friend Mike has sent it to — installed a build that predates
dead-letter queues, rate-limit headroom tracking, Discord attachments, the
scheduler, slash commands, crash banners, the working `memory.recall`
embeddings fix, and the dashboard port.

Nothing else in this document matters as much as this. A polished website in
front of a three-week-old image is worse than no website, because it converts
more people into a bad first run.

**Action: cut `v1.5.0` before doing anything else in this plan.** The
mechanics are already there and known-good — `release.yml` builds
amd64+arm64, pushes to GHCR, then smoke-pulls both platforms by digest. It
needs a tag, and §3 gives it release notes to carry.

---

## §1 — QA

### What exists

| Piece | State |
|---|---|
| Test files | 47 under `tests/`, against 71 non-test Python files |
| CI jobs | 5 (`lint-and-syntax`, `unit-tests`, `docker-smoke`, `preflight`, `compose-check`) |
| Slow-test marker | Yes — `slow` marks Docker-dependent tests, CI runs `-m "not slow"` |
| Release verification | Yes — `smoke-pull` re-pulls the pushed digest per platform |
| Coverage measurement | **None** |
| Linter / formatter | **None** — `py_compile` only |
| Dependency / security scanning | **None** |
| Dashboard (TypeScript) tests | **None** — only a route-export smoke check |

### The honest read

The test suite is better than the task description implied and worse than the
file count suggests: 47 files is respectable, but with no coverage number
nobody — including this document — can say what fraction of those 71 source
files are exercised. That is the first thing to fix, because every other QA
decision here is currently being made blind.

### Plan, in order

1. **Measure before targeting.** Add `pytest-cov`; run
   `pytest --cov=bin --cov=system --cov=mcp --cov-report=term-missing`
   and publish the number in CI output. Do *not* set a coverage gate in the
   same change — set it one release later, at whatever the measured baseline
   is, so the gate starts as a ratchet rather than an instant red build.
2. **A real linter.** `ruff check` (fast, single binary, no config sprawl)
   plus `ruff format --check`. Introduce as non-blocking for one release,
   then make it a required check. Rename the CI job from `lint-and-syntax`
   to something true either way.
3. **Ratchet, don't target.** Rather than "reach 80% coverage" — a number
   nobody hits by choosing it — gate on *no decrease*, and require tests
   with any PR touching `system/` or `mcp/`. The suite grew to 47 files
   organically because bugs got tests; keep that pressure and add a floor.
4. **Close the three specific blind spots**, highest value first:
   - **Upgrade path.** `docs/UPGRADING.md` exists but nothing tests that a
     `v1.4.1` install survives becoming a `v1.5.0` install with its data
     intact. This is the failure that loses users permanently, and it is
     untested. A CI job that installs the previous release tag, seeds a
     database, upgrades, and asserts the data is still there would be the
     single highest-value test in the repo.
   - **Dashboard.** A Next.js app with zero tests beyond "the routes are
     exported." At minimum, a build check plus `tsc --noEmit` in CI.
   - **The installers.** `install.sh` / `install.ps1` are the first thing
     every user runs and are covered only by `bash -n`. A container-based
     run of `install.sh` against a clean Ubuntu image is a slow test worth
     having nightly rather than per-PR.
5. **Supply chain.** Enable Dependabot for `pip`, `npm`, `docker` and
   `github-actions`; add `pip-audit` to CI. `requirements.txt` is fully
   pinned, which is right — pinning without a bot to move the pins is how a
   repo ends up two years behind on a CVE.

**CI gates worth adding, ranked:** coverage-no-decrease → ruff → Dependabot →
upgrade-path job → dashboard typecheck → nightly installer run.

---

## §2 — Feature gaps to "production grade"

Polish is not what is missing. These are capability gaps, roughly ordered by
how often they will bite a new self-hoster.

1. **Observability the operator can act on.** Cost tracking exists and health
   detection exists. What is missing is the thing between them: *why was
   yesterday expensive?* Per-agent, per-channel, per-day token and dollar
   attribution in the dashboard, with a spend trend. Today the ceiling is
   configurable but the explanation is not.
2. **Backup and restore, as a first-class command.** There is a data volume
   and a purge tool; there is no `make backup` / `make restore`. Every
   self-hosted system that keeps memory needs one, and the moment a user
   needs it is the worst moment to discover it doesn't exist.
3. **Config validation with real error messages.** `preflight.sh` covers the
   host. Nothing validates `config/.env` semantically — a malformed Discord
   token or a wrong-shaped Anthropic key currently surfaces as a runtime
   failure somewhere downstream rather than "this key looks wrong" at setup.
4. **Multi-user / permission model.** Right now anyone in the Discord server
   is effectively an operator. Fine for a household; not fine for the first
   friend who adds it to a server with 30 people. An allowlist of who can
   trigger builder agents and `/restart` is a small change with a large
   blast-radius reduction.
5. **A non-Discord surface that isn't the dashboard.** `bin/kara` covers CLI.
   The gap is a stable local HTTP API documented for third parties — the
   agent server has one, but it is an internal contract, not a published one.
6. **Skill/agent distribution.** `docs/EXTENDING.md` explains how to write a
   skill. There is no way to *share* one. A convention for installable
   skill bundles is what turns "friends running Karakos" into an ecosystem
   rather than six divergent forks.
7. **Graceful degradation when Anthropic is down.** Inbound messages already
   spool when the agent server is unreachable (#88). The remaining case is
   the API itself being unavailable or rate-limited past headroom — the user
   should be told, in-channel, in plain words.

Deliberately **not** on this list: custom domain for the site, structured
data markup, and any rewrite of the agent loop. All three are either already
adequate or expensive relative to what they'd return.

---

## §3 — Versioning, changelog and release process

### Current state

Eight tags, and they disagree with each other:

```
v1.0.0  2026-03-30
v1.1.0  2026-04-08   <- .0 form
v1.1.1  2026-04-08
v1.1    2026-04-09   <- two-part form, tagged AFTER v1.1.1
v1.2    2026-04-09
v1.3    2026-04-30
v1.4.0  2026-06-18   <- back to .0 form
v1.4.1  2026-08-04
```

Both `vX.Y` and `vX.Y.Z` are in use, and `v1.1` was created *after*
`v1.1.1`, so the tag order does not match the history. `release.yml` accepts
both forms deliberately, which is why nothing has broken — but "the tooling
tolerates it" is not the same as "a user can tell what they're running."

There is no `CHANGELOG.md`, and no release notes on any tag.

### The process to adopt

1. **Semver, three parts, always.** `vX.Y.Z`. Tighten the regex in
   `release.yml` from `^v[0-9]+\.[0-9]+(\.[0-9]+)?$` to require the patch
   component, so the loose form cannot be created again. Leave the existing
   eight tags alone — rewriting published tags breaks anyone pinned to them,
   and the mess is now documented here instead.
2. **Keep a CHANGELOG.** `CHANGELOG.md` is added alongside this document, in
   Keep-a-Changelog format, seeded with the eight historical releases and an
   `[Unreleased]` section holding the 43 commits from §0.
3. **Conventional commits, lightly.** The history is already ~60%
   `feat:`/`fix:`-prefixed. Make it a documented convention in
   `CONTRIBUTING.md` and enforce it on PR *titles* only (squash-merge means
   the PR title becomes the commit) — a commit-message hook on every local
   commit is friction nobody thanks you for.
4. **Automate the notes.** On tag push, `release.yml` gains a step that cuts
   the matching `CHANGELOG.md` section and publishes it as the GitHub Release
   body. That makes the changelog load-bearing: if it is not updated, the
   release ships with an empty body, which is visible.
5. **A release checklist in `CONTRIBUTING.md`**: move `[Unreleased]` to a
   version heading → bump any version constant → tag `vX.Y.Z` → push →
   confirm `smoke-pull` green on both platforms → confirm GHCR `latest`
   digest matches.
6. **Cadence.** Tag whenever `main` is more than ~10 commits or two weeks
   ahead of the last release, whichever comes first. §0 is what the absence
   of a rule like this costs.

---

## §4 — Public website

### What shipped tonight

The site was a README rendered through the Cayman theme. It is now a real
landing page. Live at <https://mcarmody.github.io/karakos-package/>.

- **`index.html` + `_layouts/landing.html` + `assets/css/landing.css`** — a
  self-contained landing page with a hero and value proposition, a
  tabbed install block (Linux/macOS and Windows), a six-card feature grid, a
  requirements table, and a documentation index. Light and dark themes,
  responsive, no external dependency but Google Fonts.
- **`_config.yml` rewritten.** Added `jekyll-sitemap` — the one real gap the
  2026-08-27 indexing investigation (task `b11495d3`, Argus report
  `agents/argus/reports/2026-08-27-github-pages-seo-indexing.md`) found —
  which produces both `/sitemap.xml` and a `robots.txt`. Added `url` and
  `baseurl`, without which the sitemap emits relative `<loc>` entries that
  Search Console silently ignores. Added an `exclude` list so repo
  scaffolding (`docs/*.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `README.md`)
  stops being rendered as layout-less thin pages and stops polluting the
  sitemap. Dropped `theme:`, since no page uses a theme layout any more.

### Indexing: the open question is now closed

Argus's report left one thing unresolved — whether the Cayman fork had
inherited a `noindex` tag. **It had not.** A `view-source` check of the live
page on 2026-08-28 found no `robots` or `googlebot` meta tag of any kind, and
Jekyll SEO tag v2.8.0 was emitting a correct canonical, OpenGraph set and
JSON-LD block. On-page SEO was never the problem.

One new finding: **`https://mcarmody.github.io/` itself returns GitHub's
"Site not found" page.** There is no user-site repo, so there is no root
`robots.txt` and no indexed page anywhere on that host linking down into the
project site. That makes the missing backlink (below) more important than it
would otherwise be — the site currently has no inbound path at all, internal
or external.

### Remaining steps — these need Mike, they are not automatable from here

1. **Search Console.** Verify `https://mcarmody.github.io/karakos-package/`
   as a URL-prefix property, submit `/sitemap.xml`, and Request Indexing on
   the homepage. Requires Mike's Google account; nothing here can do it.
2. **One real backlink**, which Argus rated the highest-leverage item and
   this session's finding above raises further. Cheapest credible options:
   a link from `mikecarmody.net` (already indexed, personal Vercel scope,
   per the workspace playbook), and a link from the `mcarmody/karakos` repo
   README.
3. **Patience is the actual answer to the original question.** The site is
   ~2 weeks old and no 2026 Google or GitHub Pages change targets `github.io`
   or new sites. The "few friends, lately" pattern is very plausibly several
   people hitting the same well-known 2–4 week new-site window at once.

### Website work worth doing next

- An architecture diagram on the landing page — the one thing the current
  page describes in words that would be faster as a picture.
- A screenshot of the dashboard. Nothing on the site shows the product.
- A short "why not just a script?" section — that is the actual objection,
  and the README's value-proposition paragraph answers it better than the
  landing page currently does.
- Only after the above: consider `karakos.dev` or a subdomain of
  `mikecarmody.net`. A custom domain resets whatever indexing age the current
  URL has accrued, so it is a thing to do *once*, deliberately, not now.

---

## Suggested order of work

1. Cut `v1.5.0` (§0). Everything else is downstream of the shipped image
   being current.
2. `pytest-cov` + `ruff`, non-blocking, to get a baseline (§1.1–1.2).
3. Search Console + one backlink (§4) — small, needs Mike, unblocks the
   original complaint.
4. Automated release notes from `CHANGELOG.md` (§3.4).
5. Backup/restore and the upgrade-path CI job — the two "loses a user
   permanently" gaps (§2.2, §1.4).
6. The rest of §2, by how loudly it complains.
