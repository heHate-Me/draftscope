# Changelog

All notable changes to DraftScope will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-08-28

### Added

- Python 3.11+ command-line workflows for player discovery, lookup, scouting,
  ranking, auditing, historical refreshes, and weekly updates.
- Position-aware college pre-combine draft models with expanding-window,
  past-only validation, calibration, evidence gates, and baseline comparisons.
- Expected calibration error, within-position top-10/top-25/top-50 precision,
  recall, NDCG, and early-round recall, plus same-row recruiting-only and
  production-only forward baselines.
- A separately labeled NFL Combine-participant model.
- Same-position active-player, recent drafted-player, established-contributor,
  and all-history career-elite comparison cohorts.
- Evidence-weighted NFL team-fit analysis for all 32 teams, with optional
  manually reviewed profile overrides.
- Content-addressed model releases, champion selection, forecast tracking,
  immutable ledgers, weekly movement reports, and artifact integrity checks.
- Free SportsDataverse and nflverse data workflows, optional
  CollegeFootballData lookup, and sourced Wikipedia and Wikidata historical
  honors enrichment.
- A synthetic scouting input and generated readable report, a model card, a
  reproducible data-quality notebook, package metadata, contribution and
  security policies, citation metadata, and a third-party data notice.
- Continuous integration across supported Python versions, branch-coverage
  enforcement, wheel and source-distribution smoke tests, tracked-content
  publication-safety checks, pinned build/test tooling, dependency updates,
  and CodeQL configuration.
- Explicit no-board state and recent-draft search fallback when an upstream
  current-season roster or player-box release is unavailable; earlier-season
  players are never relabeled as current-season data.

### Security

- Bounded remote and cached-file reads, retry and backoff handling, credential
  redirect protection, URL validation, and explicit transport restrictions.
- Atomic weekly publication with locking, journaling, rollback, recovery, and
  path validation.
- Portable artifact fingerprints that omit absolute local filesystem paths.

### Distribution

- MIT licensing for original code and documentation, with a separate notice
  covering third-party data attribution and redistribution boundaries.
- Runtime data, caches, local configuration, reports, and model artifacts are
  excluded from source distributions.

[Unreleased]: https://github.com/heHate-Me/draftscope/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/heHate-Me/draftscope/releases/tag/v0.1.0
