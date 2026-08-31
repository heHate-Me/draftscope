# Contributing to DraftScope

Contributions that improve reproducibility, data quality, validation, command
line usability, documentation, or test coverage are welcome. DraftScope is
research software: changes should preserve its audit trail, probability
contracts, time boundaries, and explicit handling of missing evidence.

## Before opening a change

- Search existing issues and pull requests for related work.
- Use GitHub Issues for bugs and proposals. Report suspected vulnerabilities
  privately as described in [SECURITY.md](SECURITY.md).
- For model changes, describe the target population, checkpoint, labels,
  features, missing-value behavior, leakage controls, and validation method.
- Do not present scouting scores or team fits as guarantees, medical advice,
  inside information, or a claim that a team will draft a player.

## Development setup

DraftScope requires Python 3.11 or newer and has no runtime dependencies.

```bash
git clone https://github.com/heHate-Me/draftscope.git
cd draftscope
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m unittest discover -s tests -v
```

Use `py -3.11 -m venv .venv` and
`.\.venv\Scripts\Activate.ps1` on Windows PowerShell.

The full test suite must pass before a pull request is submitted. Add focused
tests for changed behavior, including failure paths and missing-data cases.
Tests should be deterministic and should not require network access, private
credentials, or an existing local cache.

## Change guidelines

- Keep the package compatible with the supported Python versions declared in
  `pyproject.toml`.
- Prefer the Python standard library unless a dependency has a clear,
  documented benefit.
- Keep model inputs checkpoint-safe. Outcomes, future declarations, Combine
  invitations, later measurements, or other post-checkpoint facts must not
  leak into earlier predictions.
- Preserve missing values instead of silently converting them to zero.
- Keep probabilities, comparisons, and team-fit outputs separately labeled.
- Record source, retrieval, and integrity metadata for new external inputs.
- Bound network and file reads, validate paths, and avoid writing credentials
  or absolute local paths into portable artifacts.
- Update user documentation and the changelog when behavior changes.

## Data and generated artifacts

Do not commit downloaded or generated runtime material, including `data/`,
`.draftscope-cache/`, `draftscope.toml`, model releases, player boards,
snapshots, report collections, state files, or logs. Do not force-add an ignored
artifact. Small fixtures must be synthetic or have documented redistribution
permission. A deliberately maintained documentation sample may show output from
a synthetic prospect when its generation date, source boundaries, and
third-party attribution are preserved as in
[`examples/sample_scouting_report.md`](examples/sample_scouting_report.md).

A compact real-player retrospective may be committed when it contains no bulk
or source-record export, uses only documented public facts and transformed
metrics, separates scoring from the outcome reveal, discloses that it was
reconstructed after the event, records a strict temporal cutoff and
reproducible hashes, and preserves every required source attribution and
redistribution boundary. See
[`examples/retrospective_2025_ashton_jeanty.md`](examples/retrospective_2025_ashton_jeanty.md).

DraftScope's MIT license covers its original code and documentation, not
third-party data. Review [NOTICE.md](NOTICE.md) before adding a source or
sharing a derived dataset, report, or model bundle. A contributor is
responsible for identifying the exact source, applicable terms, required
attribution, and redistribution rights for material they submit.

## Pull requests

A pull request should contain:

- a concise explanation of the problem and the chosen solution;
- tests or a reason tests are not applicable;
- validation results and relevant before/after metrics for model changes;
- documentation updates for user-visible behavior; and
- confirmation that no credentials, private data, caches, or generated runtime
  artifacts are included.

Keep commits focused and use descriptive messages. By submitting a
contribution, you agree that your submitted original work may be distributed
under the repository's MIT License and that you have the right to submit it.
