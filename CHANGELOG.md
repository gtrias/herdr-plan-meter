# Changelog

All notable changes to this project are documented here. The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- DeepSeek's pay-as-you-go account balance, in the tab bar (`$19.39 left`) and the popup (`topped up`/`granted` split). The key is read from `$DEEPSEEK_API_KEY`, the plugin config's `deepseek_key`, or omp's credential store (`~/.omp/agent/agent.db`, read-only). DeepSeek publishes no usage or quota endpoint, so there is no percentage and no reset time.
- A rejected DeepSeek key reports "API key rejected" instead of a login-expiry prompt.

### Fixed

- Codex and Z.AI stayed stale (`~`, "Network error") on hosts whose IPv6 route drops packets: each IPv6 address used the full 10 s timeout before IPv4 was tried, so the fetch missed the 15 s deadline. Connections now give each address 3 s and alternate address families.
- The popup clipped the Z.AI block: it was still sized for two providers. It is taller, and the detail view switches to compact when the rows still do not fit.

## [0.2.0] - 2026-10-10

### Added

- Z.AI GLM Coding Plan 5-hour and weekly quota windows, using Pi's existing `zai` credential.
- Z.AI `TOKENS_LIMIT` and `CREDIT_LIMIT` response parsing.

### Changed

- README screenshots show real usage instead of demo data.
- Demo mode's per-model weekly window is Fable, the one Claude's usage endpoint reports, instead of an Opus window that does not exist.

## [0.1.0] - 2026-09-15

### Added

- Tab bar line with the tightest Claude Code and Codex plan window: percent used and time to reset.
- Popup with every window (5h, weekly, per-model weekly), usage bars, reset times, and a detail/compact toggle.
- Refresh when an agent turn ends (`pane.agent_status_changed`, at most every 2 minutes), plus a 5-minute poll. Manual refresh with `r` in the popup or the `refresh` action.
- Setup popup that prints the config snippet and copies it to the clipboard. The tab bar runs a shim in the plugin config dir, rewritten on every herdr start, so config.toml never points at herdr's internal install path.
- English and Korean UI (follows the macOS UI language or `LANG`), and optional Nerd Font brand icons.
- Shared cache with a fetch lock, `Retry-After` backoff on 429, and last good readings kept on failure.
- Demo mode (`PLAN_METER_DEMO=1`) and tests that run without credentials or network.
