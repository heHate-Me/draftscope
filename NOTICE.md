# DraftScope data and attribution notice

Copyright (c) 2026 Drew Cecala

DraftScope's original source code and documentation are licensed under the
[MIT License](LICENSE). That license does not relicense third-party data,
trademarks, service content, or other material downloaded or processed by the
software.

## Repository distribution policy

This repository intentionally excludes `data/`, `.draftscope-cache/`, the local
`draftscope.toml`, player boards, team profiles, reports, snapshots, historical
training rows, model releases, forecast inputs, forecast ledgers, state files,
and logs. These are created locally when the software runs. The tracked
`examples/demo_players.csv` fixture is synthetic and is covered by the
repository's MIT license. The tracked `examples/sample_scouting_report.md` uses
that synthetic prospect input, but its displayed NFL names, draft facts,
comparison results, and team-context evidence were generated from the public
sources attributed below. The MIT license covers the project's original report
format and prose; it does not relicense those source facts or provider content.

The tracked Ashton Jeanty retrospective Markdown and compact JSON contain one
player's attributed public facts, transformed model inputs and outputs, and
aggregate holdout metrics. They do not contain SportsDataverse or nflverse
source rows, a player-board export, or a training-data extract. The MIT license
covers DraftScope's original replay code, report structure, and prose; it does
not claim ownership of the underlying facts, names, marks, or provider content.

Do not force-add downloaded or generated runtime data to a source release.
Anyone publishing a data export, model bundle, report collection, or other
derived artifact is responsible for reviewing the exact upstream assets used,
retaining their provenance, complying with all applicable licenses and terms,
and confirming that redistribution is permitted. A model output or transformed
CSV may still contain or be derived from protected source material; the MIT
license for DraftScope does not change that.

## Third-party sources

### SportsDataverse

DraftScope downloads college-football roster, team, schedule, and player-box
release assets from
[SportsDataverse Data](https://github.com/sportsdataverse/sportsdataverse-data).
The project's package metadata identifies the released data as licensed under
[Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/).

If redistributing those assets or adaptations, attribute SportsDataverse, link
the source repository and CC BY 4.0 license, identify material changes, and
retain the downloaded asset's source metadata. SportsDataverse aggregates data
from upstream services, including ESPN; upstream provider, website, API, and
trademark terms may impose additional limits that the repository license cannot
waive. Review the terms accompanying the specific release and its original
source before redistribution.

Suggested attribution:

> College-football data from SportsDataverse Data, licensed under CC BY 4.0;
> transformed by DraftScope. Source asset and retrieval date recorded in the
> accompanying provenance metadata.

### nflverse

DraftScope downloads Combine, draft-pick, player identity, roster, depth-chart,
weekly-stat, and snap-count assets from
[nflverse-data](https://github.com/nflverse/nflverse-data). nflverse-data is
licensed under
[Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/).

If redistributing those assets or adaptations, credit nflverse, link the source
repository and CC BY 4.0 license, identify material changes, and retain source
and retrieval metadata. Some nflverse fields originate with other providers;
their rights, NFL and team trademarks, publicity rights, and other applicable
terms are not granted by CC BY 4.0. Review the notices for each selected nflverse
asset before redistribution.

Suggested attribution:

> NFL data from nflverse-data, licensed under CC BY 4.0; transformed by
> DraftScope. Source release and retrieval date recorded in the accompanying
> provenance metadata.

### Wikipedia

The all-history honors catalog uses text and structured facts from specified
[Wikipedia](https://www.wikipedia.org/) pages. Wikipedia text is generally
available under
[Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)](https://creativecommons.org/licenses/by-sa/4.0/),
with page-specific exceptions possible.

When publishing an adaptation that contains Wikipedia material, credit the
relevant page authors through a page or revision-history link, link CC BY-SA
4.0, identify changes, and distribute the adapted material under a compatible
share-alike license. Preserve the exact page URL, revision identifier, and
retrieval time written by DraftScope. Media and separately marked page content
may have different terms and must be reviewed independently.

### Wikidata

DraftScope uses [Wikidata](https://www.wikidata.org/) identifiers, honors facts,
positions, and biographical measurements. Wikidata's structured data is
released under the
[Creative Commons CC0 1.0 Universal dedication](https://creativecommons.org/publicdomain/zero/1.0/).
Attribution is not required by CC0, but crediting Wikidata and retaining entity,
revision, and retrieval metadata is strongly recommended for reproducibility.

## Optional provider

[CollegeFootballData](https://collegefootballdata.com/) is an optional lookup
provider. Its API key, service terms, rate limits, and any source-specific
redistribution restrictions apply independently. DraftScope's MIT license does
not grant rights to republish CollegeFootballData responses.

This notice summarizes the project's release policy and is not legal advice.
