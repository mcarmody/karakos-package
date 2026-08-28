# Changelog

All notable changes to Karakos are recorded here, in
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. This project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note on historical tags.** Releases before `v1.5.0` used two tag forms —
> `vX.Y` and `vX.Y.Z` — and `v1.1` was created *after* `v1.1.1`, so the tag
> order does not match the history. Those tags are left as they are, because
> rewriting published tags breaks anyone pinned to them. From `v1.5.0`
> onward, every release is a three-part `vX.Y.Z`. Entries below `v1.4.1` are
> reconstructed from commit history and are less granular than entries going
> forward will be.

## [Unreleased]

43 commits since `v1.4.1` (2026-08-04). The GHCR image the installer pulls is
built only on tag push, so none of the following has reached anyone who
installed via the one-liner. See `docs/production-grade-plan.md` §0.

### Added

- Scheduler: an agent can schedule future work that survives a restart (#132)
- Discord slash commands, registered on startup (#86, #116, #134)
- `AskUserQuestion` gets a Discord surface (#101, #135)
- Discord attachments are delivered to the agent (#127)
- Dead-letter queue for undeliverable replies (#124)
- Rate-limit headroom tracking, not just dollar spend (#128)
- Health detection for an agent that is alive but wedged (#129)
- Inbound messages spool when the agent server is unreachable (#88, #139)
- Mid-turn tool activity surfaced in the channel (#91, #143)
- Turn activity indicator at the bottom of the turn (#76)
- A queued channel is told it was heard, and drained when the turn ends (#121, #142)
- Unexpected subprocess respawns are announced instead of silently forgotten (#90, #141)
- A banner is rendered when the agent crashed mid-turn (#64, #140)
- Weekly update check that runs, and reports what it finds (#158)
- Roll back a Claude CLI upgrade that cannot complete a turn (#106, #133)
- Relay accepts messages from more than one Discord server (#82)
- Relay reply gate for shared channels, plus a bot-to-bot turn cap (#123)
- `/clear`, `/reload` and `/status` handled directly in the relay (#122)
- Hooks: recall re-injection, reviewable permissions/env (#120); wait-for
  primitive, symlink and sleep-poll PreToolUse hooks, deferred-work Stop hook (#119)
- Dashboard: theming, live turns, conversation metrics, PWA support
- `LICENSE`, `CONTRIBUTING.md` and a PR template for public sharing
- Jekyll config for the GitHub Pages landing site
- Public landing page, sitemap and SEO config (this release)
- `docs/production-grade-plan.md` and this changelog (this release)

### Fixed

- `memory.recall` now actually uses the stored embeddings (#149, #159)
- Every dashboard call hits a route the agent server registers (#151, #161)
- The agent stream log the session summarizer reads is now written (#148, #157)
- `purge-data` and capture point at the databases that actually exist (#150, #155)
- Settings page renders agent config, not runtime state (#130)
- Chat page reads the agent dropdown as an array, not a dict (#125)
- `crash_recovery`'s unposted sweep commits per message (#126)
- `stderr_reader` tasks are cancelled on kill and respawn (#111)
- `--settings` is wired on the `claude` spawn line (#94, #114)
- system-tools registered at repo root; skill discovery depth fixed (#83, #84, #112)
- Duplicate `PreToolUse`/`Stop` entries removed from `claude-settings.json` (#153, #154)
- The install path in the docs matches the software (#147)
- Route tests no longer pass five deleted routes while failing a reformat
- Half the health checks could never have passed; they can now

### Changed

- `test(kara)`: weak assertions replaced with real behavior coverage (#113)
- `preflight`: a `--verify-gates` negative control for every check (#117)
- Issues require an install-visible acceptance test (#109, #115)
- Docs distinguish Karakos skills from Claude Code Agent Skills; root
  `CLAUDE.md` added (#118)
- `ARCHITECTURE.md`: fixed gaps retired, two new ones recorded (#162)

## [1.4.1] — 2026-08-04

### Fixed

- A root-owned leftover volume no longer kills startup with an unhelpful error.

## [1.4.0] — 2026-06-18

### Fixed

- Purge retention test uses a relative date, so it stops failing with time (#79).

## [1.3] — 2026-04-30

### Fixed

- Container shell scripts tolerate CRLF line endings.

## [1.2] — 2026-04-09

### Changed

- Removed the redundant `docker compose` launch from both installers.

## [1.1] — 2026-04-09

### Changed

- Replaced API-key auth with `claude login`.

> Tagged after `v1.1.1` despite the lower version number.

## [1.1.1] — 2026-04-08

### Fixed

- `.gitignore` no longer excludes `dashboard/lib/`, which broke auth verification.

## [1.1.0] — 2026-04-08

### Fixed

- Windows bash detection in the installer.

## [1.0.0] — 2026-03-30

Initial release: an installable multi-agent household system — Discord
integration, local dashboard, episodic memory, session persistence, cost
tracking, and builder/reviewer agents behind guardrails.

[Unreleased]: https://github.com/mcarmody/karakos-package/compare/v1.4.1...HEAD
[1.4.1]: https://github.com/mcarmody/karakos-package/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/mcarmody/karakos-package/compare/v1.3...v1.4.0
[1.3]: https://github.com/mcarmody/karakos-package/compare/v1.2...v1.3
[1.2]: https://github.com/mcarmody/karakos-package/compare/v1.1...v1.2
[1.1]: https://github.com/mcarmody/karakos-package/compare/v1.1.1...v1.1
[1.1.1]: https://github.com/mcarmody/karakos-package/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/mcarmody/karakos-package/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/mcarmody/karakos-package/releases/tag/v1.0.0
