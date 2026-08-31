# Retrospective 2025 case study: Ashton Jeanty

## Read this first

This is a **retrospective replay generated on August 31, 2026**, using
DraftScope's current pipeline and rebuilt historical releases. It is not a
forecast that was saved before the 2025 NFL Draft. The model design and this
example were chosen after the outcome was known, so this case illustrates the
pipeline; it does not independently validate accuracy.

The question is deliberately narrow:

> What would the current DraftScope method have reported for Ashton Jeanty at a
> season-complete 2024 college checkpoint if the 2025 draft class were held out?

## What the replay said

| Output at the cutoff | Result |
| --- | ---: |
| Calibrated probability of selection in the immediately following draft | **45.3%** |
| Past-only RB training prevalence | 1.75% |
| Probability relative to that training prevalence | 25.9× |
| Rank among source-defined 2025 RB roster rows | **1 of 1,152** |
| Retained feature coverage | 14 of 14 (100%) |
| Out-of-distribution score | **7.86 — warning** |
| Combine-stage cross-check | **Withheld** — 3 of 11 inputs (27.3%) |

In plain English, the model identified Jeanty as the strongest next-draft RB
profile in its full-roster population. The 45.3% is intentionally lower than a
media-board-style number because the denominator includes every supported RB on
the selected FBS rosters, including backups, younger players, and players who
would return to college. DraftScope does not turn that percentage into a binary
"drafted/not drafted" call.

The 7.86 out-of-distribution score is important. DraftScope's warning threshold
is 3.0; Jeanty's production was far outside normal training history. His high
rank is meaningful, but the exact probability is an extrapolation and deserves
more caution than an ordinary in-distribution estimate.

> **Reconstructed pre-draft readout:** Jeanty is RB1 in this full-roster model
> population. His production sits at or near the top of the prior drafted-RB
> sample, while his height and weight are below that sample's norms. The model
> assigns a 45.3% selection probability, but the 7.86 outlier warning means the
> exact percentage is less trustworthy than the rank. The five closest prior
> profile comps support a 38/41/76 pick band; the Combine cross-check is withheld
> for missing evidence.

## Information available to the model

The replay used 2024 Boise State roster evidence and completed 2024-season game
data. DraftScope records the requested bound as Week 16. SportsDataverse's
postseason week numbering resets, so the source-defined cutoff also includes
postseason games through January 20, 2025 (January 21 UTC). Jeanty's own final
college game was the Fiesta Bowl on December 31, 2024.

| Jeanty evidence | Value |
| --- | ---: |
| Height / weight | 5 ft 8 in / 208 lb |
| Age at draft | 21.41 |
| Class | Junior |
| Rushing | 374 carries, 2,601 yards, 29 TD |
| Receiving | 23 catches, 138 yards, 1 TD |
| Scrimmage production | 2,739 yards, 30 TD |
| Observed yards per carry | 6.95 |
| Smoothed model yards per carry | 6.83 |

The smoothed rate adds a small position prior before division; it prevents a
small number of touches from producing an unstable efficiency feature. Boise
State's official record also reports 374 carries, 2,601 rushing yards, and 29
rushing touchdowns for Jeanty's 2024 season.

The 14 retained inputs were height, weight, derived BMI, age, class,
recorded-stat availability, fold-local conference and school context, carries,
rushing yards, scrimmage yards, total touchdowns, smoothed yards per carry, and
smoothed yards per touch. No player name, draft declaration, Combine result,
media ranking, draft result, NFL team, award, or professional outcome was a
model feature.

## Same-position context

Against drafted RBs from the strictly earlier 2017–2024 draft classes, Jeanty
was at the 100th percentile in the available sample for carries, rushing yards,
and scrimmage yards; the 99.7th percentile for touchdowns; the 90.6th percentile
for the smoothed yards-per-carry feature; and the 79.5th percentile for smoothed
yards per touch. His height was at the 7.1st percentile and his weight at the
32.6th percentile. His age was younger than roughly 77% of the drafted-prior
sample with a recorded age.

The closest time-safe drafted-profile comparisons were:

| Prior drafted RB | Draft | Similarity |
| --- | ---: | ---: |
| J.K. Dobbins | 2020, pick 55 | 67.3 |
| Jonathan Taylor | 2020, pick 41 | 65.4 |
| Rashaad Penny | 2018, pick 27 | 63.9 |
| Jeremy McNichols | 2017, pick 162 | 60.9 |
| Dalvin Cook | 2017, pick 41 | 59.9 |

These are descriptive profile similarities, not career forecasts. Across these
five comparisons, the 20th/50th/80th pick band was 38/41/76, conditional on
being drafted. It did **not** reach the eventual No. 6 pick, so the replay
identified Jeanty's draftability and within-position rank but understated his
draft-slot upside.

The Combine-stage model was withheld rather than backfilled. Only height,
weight, and BMI were present—3 of 11 retained inputs, or 27.3%, below the 30%
coverage requirement. No unofficial workout values were substituted.

## Outcome reveal

The history builder joined draft labels for later audit, but the scorer could
not access them. After the complete 2025 RB score ledger was generated and
hashed, the replay accessed those labels for evaluation. The [NFL's official
2025 draft tracker](https://www.nfl.com/draft/tracker/2025/teams/las-vegas-raiders)
records that the Las Vegas Raiders selected Ashton Jeanty in Round 1 with the
sixth overall pick.

That outcome does not mean DraftScope predicted "pick 6." The model produced a
within-RB rank, not a cross-position overall board or exact-pick forecast. No
retrospective team-fit result is shown because the repository does not have a
verified, dated pre-draft 2025 roster/contract/team-profile snapshot. Las Vegas
is the observed outcome only.

## Full held-out 2025 RB check

The named example was selected after the fact, so the full held-out class is a
better check than the single successful player:

| Revealed 2025 RB metric | Result |
| --- | ---: |
| Drafted / evaluated | 25 / 1,152 (2.17%) |
| Brier score | 0.01743 |
| Past-prevalence baseline Brier | 0.02125 |
| ROC-AUC | 0.9867 |
| Average precision | 0.7414 |
| Drafted players in top 10 | 9 of 10 |

The model's earlier-only publication gate also passed without using any 2025
outcome in fitting or evaluation: 6,766 evaluated 2019–2024 RB rows, 115
selections, Brier 0.01484 versus a 0.01671 baseline, ROC-AUC 0.9660, and average
precision 0.3717. The 2025 numbers are one checkpoint and one position, not a
replacement for the
[multi-position evaluation](../MODEL_CARD.md#position-level-results).

## Leakage controls and reproducibility

The replay:

1. rebuilt 2016–2024 college-season rows at one consistent season-complete
   checkpoint;
2. trained the RB model only on draft years 2017–2024 (9,199 rows, 161 picks);
3. fit preprocessing, feature selection, school/conference context, and the raw
   model only on those earlier classes;
4. fit calibration only on 7,985 earlier out-of-sample predictions from
   2018–2024;
5. deduplicated the 2025 holdout roster using checkpoint evidence before draft
   outcomes were joined;
6. exposed only allow-listed feature and checkpoint fields to the scorer; and
7. generated and hashed all 1,152 RB scores before accessing the already-joined
   2025 labels for evaluation.

Reproduce the compact JSON result from the public sources:

```bash
python3 tools/replay_retrospective_case.py
```

The first run can take several minutes. Downloaded rows remain under ignored
local cache/data paths and are not committed. Compare the generated source,
in-memory derived-row representation, and forecast hashes with the checked-in
[`retrospective_2025_ashton_jeanty.json`](retrospective_2025_ashton_jeanty.json).
Upstream release revisions or deliberate pipeline changes can legitimately
change those hashes and should be treated as a new reconstruction.

## Boundaries

- The code, hyperparameters, source reconstruction, and example selection all
  postdate the 2025 draft. This is a time-bounded backtest, not evidence of a
  live 2025 forecast.
- Public box scores do not encode film traits such as vision, contact balance,
  pass protection, decision-making, or injury/medical information.
- A single recognizable hit is vulnerable to selection bias. Use the full
  held-out metrics and the broader model card, not this case alone, to assess
  the method.
- The full-roster probability is conditional on DraftScope's source coverage,
  identity linkage, checkpoint, and position normalization.
- The all-era career-elite and current-active comparison catalogs were omitted
  because their current snapshots are not verified pre-2025 evidence.

## Sources

- College roster and game evidence: [SportsDataverse Data](https://github.com/sportsdataverse/sportsdataverse-data),
  transformed by DraftScope; see [NOTICE](../NOTICE.md) for attribution and
  redistribution boundaries.
- Official season totals: [Boise State Athletics](https://broncosports.com/sports/football/roster/ashton-jeanty/10215).
- Draft outcomes and player identities: [nflverse-data](https://github.com/nflverse/nflverse-data),
  transformed by DraftScope.
- Observed selection: [NFL 2025 Las Vegas Raiders draft tracker](https://www.nfl.com/draft/tracker/2025/teams/las-vegas-raiders).
