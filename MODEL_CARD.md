# DraftScope model card

- **Software version:** 0.1.0
- **Card date:** 2026-08-28
- **Evaluation snapshot:** 2026-08-27 preseason checkpoint
**Status:** Research alpha — share with the limitations in this card

## Technical summary

DraftScope estimates the probability that a supported-position player listed on
an FBS roster at a stated checkpoint will be selected in the immediately
following NFL Draft. The primary model is a position-aware, regularized logistic
model built from checkpoint-safe roster, age/class, recruiting, and college
production fields. Numeric gaps are imputed from each training fold, school and
conference context is fit inside each training fold, and raw player identities
are not model features. A second model estimates draft probability only within
the later NFL Scouting Combine-participant population; the two probabilities
are never combined.

The audited preseason training artifact contains 154,279 unique FBS
player-seasons from the 2016–2025 college seasons and 2,404 immediately following
draft outcomes from the 2017–2026 drafts. Eleven core position models passed the
current publication gate. K, P, and LS estimates remain withheld because the
forward evaluation contains too few drafted outcomes for the specialist gate;
descriptive benchmarks and comparisons can still be shown for those positions.

This is not a personnel-decision system or a draft guarantee. It is best used to
organize public evidence, compare a prospect with clearly defined cohorts, and
identify where more scouting work is needed.

## Outputs and their boundaries

| Output | Meaning | Does not mean |
| --- | --- | --- |
| College draft probability | `P(drafted in the immediately following NFL Draft | supported-position FBS roster player at the stated checkpoint)` | Probability conditional on declaring, being eligible, receiving a Combine invitation, or appearing on a media draft board |
| Combine cross-check | `P(drafted | NFL Scouting Combine participant with the available protocol measurements)` | A second estimate of the full-roster probability or a value that can be multiplied by it |
| Profile grade | Evidence-weighted physical, production, film-rubric, and age/class summary | A calibrated probability |
| Player comparisons | Descriptive similarity within the named active, recent-drafted, or career-elite cohort | A training label, causal forecast, or claim of equal career outcome |
| Team fit | Evidence-weighted roster opportunity, prototype, and draft-access context | A prediction that a team will select the player |

Current-active and established-contributor comparisons are survivorship- and
recency-biased. The all-history career-elite catalog is outcome-defined and is
kept outside every probability model. Team-fit scores are downstream heuristics
and never alter draft probability.

## Training population and labels

- **Unit:** one roster player at one college-season/checkpoint, deduplicated by
  stable player-season identity.
- **Population:** supported-position players in the selected FBS roster
  snapshots, including players without recorded box-score production and
  players who did not enter the next draft.
- **Positive label:** linked to an FBS selection in the immediately following
  NFL Draft.
- **Negative label:** present in the source-defined roster population and not
  selected in that immediately following draft. A negative label does not mean
  that the player never reached the NFL.
- **Preseason feature cutoff:** the current roster plus the prior completed
  college season. Weekly models use only games through the latest fully
  completed week and select no later historical checkpoint.
- **Position handling:** QB, RB, WR, TE, EDGE, IDL, LB, CB, S, K, P, and LS use
  their normalized role groups. OT and IOL share one college source cohort
  because public rosters frequently use the generic `OL` label; their
  measurement comparisons remain position-specific.

The saved contract audit reported zero duplicate player-season keys, zero
invalid probability contracts, zero invalid immediate-draft alignments, and
2,404 of 2,404 expected source-eligible FBS picks linked. Twelve reviewed draft
picks absent from their SportsDataverse roster snapshot were excluded rather
than manufactured as rows. See the executable
[`draftscope_model_data_quality_audit.ipynb`](notebooks/draftscope_model_data_quality_audit.ipynb)
for the source-grain, identity, linkage, leakage, and integrity checks.

## Features and model specification

Each position has an allow-listed set drawn from:

- roster height and weight;
- class year and age at draft when source-backed;
- optional pre-checkpoint recruiting rating, stars, and national rank;
- an explicit `has_recorded_stats` field; and
- position-relevant passing, rushing, receiving, defensive, kicking, punting,
  and return production.

The automatic SportsDataverse artifact does not currently populate advanced
passing/rushing success fields, and public box scores do not provide complete
route, blocking, pressure, or coverage charting. Manually entered film traits
contribute to the scouting profile and prototype matching, not the trained
draft-probability model.

Training uses regularized logistic regression with position-specific feature
selection, bounded case-control negative sampling, intercept correction back to
the full roster prevalence, and logistic calibration. Preprocessing, feature
selection, school/conference context, model fitting, and calibration are all
performed without access to the held-out year. The deployment model is then fit
on the complete approved history.

## Temporal evaluation

Evaluation uses an expanding-window design grouped by NFL Draft year. The 2017
and 2018 classes are warmup data; each class from 2019 through 2026 is predicted
using only earlier classes. Earlier out-of-sample predictions may calibrate a
later class. The audit verified the no-future-training rule across 112
position-year folds and eight held-out draft years.

The existing publication gate requires at least 50 evaluated rows, lower Brier
score than the past-only position-prevalence baseline, ROC-AUC of at least 0.55,
and average precision at least two percentage points above prevalence. The
college stage additionally requires at least four draft classes, 30 evaluated
drafted outcomes, 100 evaluated undrafted outcomes, and an out-of-time
calibrator. These gates reduce unsupported output; passing them does not prove
real-world decision quality.

### Calibration, ranking, and simple baselines

Each position report also includes expected calibration error (ECE) across ten
equal-width probability bins. ECE is descriptive and depends on binning; it is
not used as a publication gate.

Draft-board diagnostics rank the held-out predictions independently within one
position/model population and one evaluated draft year. They report precision,
recall, binary NDCG, and—when every drafted row has a round outcome—Rounds 1–3
recall at top 10, 25, and 50. Counts are then aggregated across held-out years.
These are not top-10, top-25, or top-50 results for a cross-position overall
draft board. Draft round is evaluation-only and never becomes a feature.

Recruiting-only and production-only logistic baselines repeat the same
expanding-window, earlier-years-only fitting and prequential calibration. A
direct comparison is reported only when the baseline scores exactly the same
held-out rows as the primary model. If a feature family does not retain at least
two trainable inputs in the past-only folds, that baseline is shown as
unavailable instead of silently substituting another signal.

For the audited preseason WR model, the held-out results were:

| Diagnostic | Top 10 | Top 25 | Top 50 |
| --- | ---: | ---: | ---: |
| Selected across 8 held-out years | 80 | 200 | 400 |
| Drafted hits | 39 | 88 | 135 |
| Precision | 48.8% | 44.0% | 33.8% |
| Recall | 15.4% | 34.8% | 53.4% |
| Mean binary NDCG | 0.470 | 0.443 | 0.499 |
| Rounds 1–3 recall | 21.7% | 39.2% | 55.0% |

That WR model's ECE was 0.004. The same-row production-only baseline had
ROC-AUC 0.941, average precision 0.317, ECE 0.003, and precision at 10 of
55.0%; the primary model had ROC-AUC 0.963, average precision 0.343, and
precision at 10 of 48.8%. Recruiting-only was unavailable because fewer than
two trainable recruiting features survived every published past-only fold.
This mixed result is important: the primary model improves discrimination over
the available simple baseline but does not beat it on every board metric. The
publication gate does not currently require a board-metric improvement.

### Position-level results

The values below are from the audited 2026 preseason artifact. `N` and
`Drafted` count held-out rows, not training rows. OT and IOL are two displayed
roles backed by the same shared offensive-line model, so their identical row is
not an independent result. Average precision must be interpreted against each
position's low base rate.

| Position | Status | N | Drafted | ROC-AUC | Avg. precision | Brier | Prevalence baseline |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| QB | Pass | 6,035 | 85 | 0.968 | 0.495 | 0.0122 | 0.0139 |
| RB | Pass | 8,955 | 151 | 0.959 | 0.372 | 0.0145 | 0.0166 |
| WR | Pass | 16,813 | 253 | 0.963 | 0.343 | 0.0134 | 0.0148 |
| TE | Pass | 7,429 | 117 | 0.952 | 0.298 | 0.0134 | 0.0155 |
| OT / IOL shared | Pass | 20,188 | 336 | 0.838 | 0.088 | 0.0159 | 0.0164 |
| EDGE | Pass | 4,074 | 98 | 0.867 | 0.206 | 0.0216 | 0.0235 |
| IDL | Pass | 14,120 | 243 | 0.881 | 0.176 | 0.0160 | 0.0169 |
| LB | Pass | 15,651 | 215 | 0.916 | 0.151 | 0.0127 | 0.0136 |
| CB | Pass | 5,160 | 104 | 0.906 | 0.177 | 0.0185 | 0.0198 |
| S | Pass | 17,247 | 300 | 0.903 | 0.168 | 0.0158 | 0.0171 |
| K | Withheld | 3,669 | 20 | 0.916 | 0.113 | 0.0053 | 0.0054 |
| P | Withheld | 2,099 | 10 | 0.897 | 0.059 | 0.0051 | 0.0047 |
| LS | Withheld | 2,618 | 5 | 0.921 | 0.078 | 0.0020 | 0.0019 |

K lacks the minimum 30 held-out positives. P and LS also fail that support gate
and do not improve Brier score over their baseline. High specialist ROC-AUC
therefore does not override the probability withholding rule.

## Uncertainty and robustness

- Candidate reports require position-specific minimum evidence and withhold a
  probability when too few retained features are observed.
- Candidate probability intervals use an 80% draft-year bootstrap, which
  reflects variation across historical classes but not every source or model
  uncertainty.
- An out-of-distribution diagnostic warns when a candidate's retained inputs
  are far from the training distribution.
- Missing numeric values use training-fold medians. The college model disables
  implicit per-feature missing indicators; `has_recorded_stats` is the sole
  explicit availability feature.
- Content-addressed training checkpoints, releases, champion decisions, and
  forecast ledgers make source and model changes auditable. Artifact hashes
  detect later mutation; they do not establish that upstream facts are correct.

When valid current player rows are available, `update` refits the position
models used for the board. During a season, newly completed-game statistics can
change player features, probabilities, and board ranks. Those weekly
observations do not become new draft-outcome labels; genuinely new supervised
evidence arrives only after another draft class is completed and the historical
artifact is rebuilt. If current-season source rows are unavailable, an update
can save the audited history without publishing a current board or forecast.

## Known limitations

1. **Low base rate and eligibility ambiguity.** Most roster players are not
   selected in the next draft, and a roster source does not establish draft
   eligibility or intent. The unconditional probability will be much lower than
   estimates conditioned on declaration or media-prospect status.
2. **Source availability can correlate with outcomes.** Date-of-birth coverage
   is sparse and differs materially between drafted and undrafted rows across
   source years. Missingness indicators are disabled and the gaps are reported,
   but imputation cannot remove every source-selection bias.
3. **Public production is incomplete.** Offensive-line play, route quality,
   coverage responsibility, pressures, medicals, interviews, and verified film
   traits are absent or incomplete.
4. **Identity linkage is fallible.** The builder rejects ambiguous matches and
   audits omissions, but name changes, transfers, duplicate roster identities,
   and upstream corrections can alter the population or labels.
5. **Historical drift.** Rules, transfer patterns, extra eligibility, position
   usage, Combine protocols, and NFL preferences change over time. The 2021
   testing class is explicitly labeled as a nonstandard pro-day protocol.
6. **Sparse specialist outcomes.** K, P, and LS probabilities are withheld in
   the evaluated artifact. Descriptive comparisons for them are not substitutes
   for a validated probability.
7. **Comparison bias.** Active-player and established-contributor cohorts omit
   many drafted players who are no longer on the current roster and give recent
   classes less time to attrit. Career-elite comparisons are selected on known
   success and must never be read as causal evidence.
8. **Team-fit incompleteness.** Public free sources do not provide fully current
   contracts, injuries, starter quality, scheme decisions, interviews, medicals,
   or future pick ownership. Fit scores shrink toward neutral when evidence is
   sparse and are not outcome-validated mock selections.
9. **No validated cross-position board.** Current top-10, top-25, and top-50
   metrics are within-position diagnostics. They do not measure whether players
   from different positions are ordered correctly on one overall NFL Draft
   board, and they are not part of the probability publication gate.

## Intended and excluded uses

Appropriate uses include research, exploratory scouting, reproducible public
data analysis, prospect shortlisting, and identifying missing evidence. Users
should inspect the probability population, checkpoint, feature coverage,
validation status, interval, comparison cohort, and team-fit evidence before
drawing a conclusion.

Do not use DraftScope as the sole basis for employment, scholarship, medical,
betting, financial, or contract decisions. Do not represent a probability as a
promise that a player will or will not be drafted. Do not infer a player's
health, character, effort, or protected characteristics from missing public
data. Program and conference context can reflect unequal exposure and resource
distribution; it should be treated as contextual signal, not player merit.

## Reproduction and monitoring

Build a fresh local artifact and inspect its evidence:

```bash
cp draftscope.toml.example draftscope.toml
./run_draftscope.py doctor --config draftscope.toml
./run_draftscope.py update --config draftscope.toml
./run_draftscope.py model-status --output-dir data --json
```

The default data workflow is credential-free. Source files, generated data,
model releases, and reports stay outside version control. Record the model
version, checkpoint, source retrieval metadata, artifact hashes, and full JSON
output when comparing runs. Re-run the test suite and the data-quality notebook
after changing population definitions, identity rules, features, preprocessing,
labels, or validation.

As verified on 2026-08-28, the default provider had published the 2026 schedule
but not the required 2026 roster and player-box partitions. In that state a
clean update saves and audits the historical training artifact, records that no
current board is available, and returns a nonzero status; it does not substitute
2025 players as a 2026 board. Reproduction of a live current board therefore
also depends on upstream current-season release readiness.

## Recommended next evidence

- Accumulate more specialist draft classes before lowering any K/P/LS gate.
- Add consistently licensed, checkpoint-bounded film/charting features with a
  documented identity join and missingness audit.
- Continue tracking checkpointed forecast calibration after draft outcomes
  mature; never backfill future facts into an earlier checkpoint.
- Add and predeclare a cross-position overall-board evaluation protocol before
  describing the current within-position ranking diagnostics as overall-board
  performance.
- Measure stability across alternate lookback windows, source revisions, and
  position mappings.
- Validate team-fit rankings against a predeclared outcome and historical
  roster-state dataset before presenting them as predictive.

## Sources and governance

College data are provided by SportsDataverse releases; NFL draft, Combine,
identity, roster, usage, and snap data are provided by nflverse. Wikipedia and
Wikidata supply the separately scoped historical-honors catalog. Source terms,
attribution, and redistribution boundaries are documented in
[`NOTICE.md`](NOTICE.md). Model-affecting changes should update this card and
[`CHANGELOG.md`](CHANGELOG.md), preserve the temporal contract, and include
before/after validation evidence as described in
[`CONTRIBUTING.md`](CONTRIBUTING.md).
