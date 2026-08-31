# DraftScope

DraftScope is a standalone Python command-line scouting tool for NCAA players entering the 2026 season (normally prospects for the 2027 NFL Draft). It benchmarks a player against the latest ten completed NFL draft classes, estimates draft likelihood, finds drafted-player comps, ranks a prospect board, and scores possible NFL team fits.

The DraftScope CLI and modeling pipeline use only the Python standard library. Every score exposes its comparison population, feature coverage, and validation metrics.

[Model card](MODEL_CARD.md) · [Retrospective 2025 case](examples/retrospective_2025_ashton_jeanty.md) · [Synthetic sample report](examples/sample_scouting_report.md) ·
[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md)

## What it does

- Compares measurements and available college production with same-position active-drafted averages and established active-NFL-contributor cohorts over the ten-draft window.
- Handles QB, RB, WR, TE, OT, IOL, EDGE, IDL, LB, CB, S, K, P, and LS role groups.
- Calculates position-aware physical, production, film-skill, age, and available recruiting-pedigree evidence without silently filling missing values with zero.
- Fits separate college pre-combine and NFL Combine-stage models; their populations and probabilities are never blended.
- Validates with expanding-window, past-only draft-year tests: each evaluated class is predicted using only earlier classes, with prequential calibration from earlier out-of-sample predictions. Reports aggregate and per-year Brier score versus the training-prevalence baseline, ROC-AUC, average precision, expected calibration error, input coverage, and an 80% draft-year-bootstrap interval. College models also report recruiting-only and production-only baselines plus within-position precision, recall, NDCG, and early-round recall at 10, 25, and 50. A position model that fails its validation or evidence gate emits no probability.
- Finds up to eight same-position comps among current-active drafted players, the separately scoped all-era career-elite catalog, and the full drafted subset of the configured recent history supporting the pick range.
- Ranks NFL fits from broad need, dated starter/contract evidence, scheme/prototype compatibility, roster timeline, coaching stability, and likely draft access.
- When the required current-season releases are available and `update` is run, discovers a national FBS production pool, refreshes explicitly tracked players, snapshots the raw evidence, rebuilds the board, and reports rank/profile/probability movement through the latest completed week.

## Held-out validation evidence

[![DraftScope held-out validation chart comparing within-position average precision with actual NFL draft rate for 2019–2026](docs/figures/draftscope_position_validation.png)](docs/figures/draftscope_position_validation.png)

This chart summarizes expanding-window tests across the 2019–2026 NFL Drafts. Every held-out class was scored using only earlier seasons. Within each position, the gray diamond is the actual draft rate and the blue circle is average precision, a ranking metric—not an individual player's draft probability. A larger gap means drafted players were more concentrated near the top of DraftScope's same-position rankings than in a no-skill ranking. The `× no-skill` value is average precision divided by draft rate; it is not a general accuracy multiplier. This figure shows ranking separation, not probability calibration.

Ten displayed validation rows covering eleven roles passed the documented publication gates; OT and IOL share one pooled college-model result. K, P, and LS probabilities remain withheld. See the [model card](MODEL_CARD.md#position-level-results) for the full protocol, numerical results, calibration metrics, limitations, and withholding rules. The optional figure generator, [`tools/build_position_validation_chart.py`](tools/build_position_validation_chart.py), reads that canonical table directly and requires Pillow; Pillow is not part of DraftScope's runtime.

## Retrospective 2025 replay

The [Ashton Jeanty case study](examples/retrospective_2025_ashton_jeanty.md)
reconstructs a season-complete 2024 checkpoint and holds out the entire 2025
draft class. Using only 2017–2024 draft outcomes, the current DraftScope method
assigned Jeanty a 45.3% full-roster probability and ranked him first among
1,152 source-defined RB rows. He was later selected sixth overall.

This is a replay created after the draft—not a forecast saved in 2025. It
publishes the full holdout metrics, an out-of-distribution warning, an honest
draft-slot miss, source and artifact hashes, and the exact reproduction command.

## Accuracy boundary

DraftScope reports two different probability contracts.

The primary college pre-combine model estimates:

`P(drafted in the immediately following NFL Draft | FBS roster player at the stated checkpoint)`

Its historical denominator is every supported-position player on the selected FBS rosters—not only starters, statistical leaders, leavers, declared players, or eventual prospects. Players with no recorded box-score statistics and players who returned to college remain negative examples for that immediately following draft. Week 0 uses the current roster plus prior-completed-season production; Week N uses only production through completed Week N. Current-season players update the inputs but never become new outcome labels.

This probability is unconditional for the defined FBS-roster checkpoint population. It is not a claim about players outside that population, and it still depends on roster completeness, player/draft identity linkage, and public-stat coverage. The model uses checkpoint-safe roster measurements, age/class when available, and position-specific college production. Optional recruiting fields are used only when they were joined before the checkpoint from a documented source. Missing numeric fields use training-fold median imputation and are never filled with zero. The college stage disables implicit missing-value indicators so source-era gaps cannot become hidden outcome signals; `has_recorded_stats` is its sole explicit production-availability feature. Each report discloses material year-by-outcome coverage gaps. School and conference context is learned as smoothed, fold-local historical context rather than an unexplained name bonus.

The separate later-stage cross-check uses drafted and undrafted nflverse Combine participants:

`P(drafted | NFL Scouting Combine participant, available Combine-protocol measurements)`

That percentage applies only after reaching the Combine population. DraftScope never multiplies it by an entry estimate, never treats it as another next-draft probability, and never relabels a Combine-only fallback when a college build or model fails. A school-listed or private-workout 40 is not equivalent to an official Combine result.

Every position is validated separately with expanding-window draft-year predictions and past-only calibration. The first two classes are training/calibration warmup; every published validation year is fit without that year or any later year. Sparse or unlearnable position models are withheld. Public kicker and punter production can participate when sufficient; long snapper probability will often be withheld because reliable public production and drafted samples are limited. Benchmarks and team fits remain available when probability is withheld.

The top-10, top-25, and top-50 ranking checks are computed independently within
one position/model population and one held-out draft year, then aggregated across
evaluated years. They are not cross-position overall-board results. Draft round
is used only as an evaluation outcome for early-round recall; it is never a model
feature. See the [model card](MODEL_CARD.md) for definitions and current evidence.

Because public college rosters frequently label tackles and guards alike as generic `OL`, the college stage validates OT and IOL on one disclosed offensive-line cohort; drafted-player measurement benchmarks remain position-specific. This avoids pretending that the small explicit-`OT` subset is a complete tackle denominator.

No public structured source reliably measures film skills such as processing, route technique, hand use, or coverage instincts. Those fields use a transparent 0–100 manual rubric. Use the same rubric for every player and, ideally, multiple graders.

School and conference remain identity and audit fields. The college model may derive strongly smoothed program/conference context inside each training fold; the raw name is never treated as a numeric grade, and the held-out draft year never contributes to its own context encoding.

## Comparison cohorts

Displayed active averages, active-player comps, and draft-slot comps are same-position and use the configured ten-draft window. Career-elite comparisons use a separate comparison-only historical catalog:

- **Active average:** players drafted within the window who are joined to the current nflverse roster snapshot with status `ACT`, `RES`, `E14`, `INA`, `PUP`, `SUS`, `EXE`, or `DEV`. Physical averages additionally require that specific Combine/pro-day measurement. College-production averages use the same active registry joined back to checkpoint-aligned college rows by stable nflverse IDs.
- **Established active contributor:** the same active subset with a known fixed first-three-NFL-season outcome and at least 500 regular-season offense-plus-defense snaps or 150 special-teams snaps during that horizon, again requiring the displayed metric. A label is withheld until all three seasons are complete; right-censored recent classes and incomplete snap partitions are excluded. When this cohort has no usable same-position value—or older/custom data do not contain these outcomes—the report explicitly falls back to active players drafted in the first 64 picks.
- **Historical career elite:** same-position players across NFL history with a Hall of Fame designation, at least one first-team All-Pro selection, or at least three Pro Bowl selections. Hall of Fame membership spans every NFL era in the source registry, including undrafted players. The separate honors catalog parses one disclosed NFL first-team All-Pro selector for every season from 1920 through the latest completed season and Pro Bowl selections from 1950 forward; AFL All-Star, All-AFL, and AAFC-only selections are excluded. Pro Bowl A–Z lists are reconciled against every usable annual roster, but some older annual pages do not publish a complete roster, so the report identifies that population-coverage boundary instead of claiming perfect recall. Multi-position players are indexed only in supported, source-backed roles. This outcome-defined group is displayed separately from active-player and draft-slot comps and is never a model feature or training row. Older bio measurements are labeled separately from verified Combine/pro-day testing.

Here “active” means included by that exact roster-source status rule. It does not prove game-day status, health, contract security, starter quality, or a final 53-man role. `RET`, `CUT`, free-agent/released statuses, ambiguous roster identities, and players missing from the snapshot are excluded.

The exact roster download time and SHA-256 are recorded in `.draftscope-cache/roster_2026.csv.source.json`. Refreshing the source changes the as-of date and can change cohort membership; reports retain that source metadata when shared.

Active-average and established-active-contributor comparisons have survivorship and recency bias. They omit drafted players who retired, were released, suffered career-altering injuries, moved outside the captured roster population, or otherwise left the current snapshot. Recent draft classes also have had less time to attrit and may be right-censored from the fixed contributor label. These cohorts can describe current-roster prototypes, but they can overstate traits associated with longevity and must not be treated as unbiased draft-probability training populations. The full drafted subset is retained for separately labeled comparable-pick support, while each probability model retains its full drafted-and-undrafted risk set; neither becomes the displayed active average. The college probability uses its separate full-FBS-roster denominator described above.

## Quick start

Clone the repository using the URL shown by GitHub, then initialize a clean checkout:

```bash
cd draftscope
python3 -m venv .venv
./run_draftscope.py --help
cp draftscope.toml.example draftscope.toml
./run_draftscope.py template players --out data/players_2026.csv
./run_draftscope.py doctor --config draftscope.toml
./run_draftscope.py update --config draftscope.toml
```

The first update downloads the public source files and builds the historical checkpoints and local state under the ignored `data/` and `.draftscope-cache/` directories. When current-season roster and player-box releases are available, it also builds the current board and player reports. It can take several minutes. The default SportsDataverse and nflverse workflow does not require an API key.

As verified on 2026-08-28, SportsDataverse had published the 2026 schedule but
not the required 2026 roster and player-box partitions. A clean first update
therefore saves the audited historical training artifact, records
`current_board_available = false`, prints a clear source-readiness warning, and
returns a nonzero status without publishing an empty board. `board` explains why
no current board exists; `search` still searches the most recent completed NFL
Draft class. Re-run `update` after the current-season releases appear. DraftScope
does not relabel a prior-season roster as 2026 data.

After initialization, normal use is:

```bash
./run_draftscope.py board --top 50
./run_draftscope.py search "Player Name"
```

`search` searches the current college board and then the most recent completed NFL Draft class. When no current board was published, it searches only that completed draft class. Run it without a name for an interactive prompt, or add `--full` for model validation, cohort definitions, every comparison table, and all warnings. Data refreshes only when you run `update` yourself or explicitly install the optional weekly schedule described below; installing the package alone does not create a background job.

The project launcher selects this checkout's `.venv` when present, adds `src/` to the import path, and changes to the repository directory. Shell activation is not required on macOS or Linux.

Setup and data-management commands:

```bash
./run_draftscope.py doctor
./run_draftscope.py add "Player Name" --position WR --school "School Name" \
  --season 2026 --height 6-2 --weight 205 --entry-probability 65% \
  --out data/players_2026.csv
./run_draftscope.py audit --players data/players_2026.csv
```

Those commands do not need an API key or live network access. To install the shorter, cross-platform `draftscope` console command:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

draftscope template players --out data/players_2026.csv
draftscope template history --out data/historical_prospects.csv
draftscope template teams --out data/team_profiles_2026.csv
draftscope template config --out draftscope.toml
```

On Windows PowerShell, create the environment with `py -3.11 -m venv .venv`,
activate it with `.\.venv\Scripts\Activate.ps1`, and run the same `python -m pip`
and `draftscope` commands.

Refresh the latest completed ten-draft benchmark (the ending class is selected dynamically from the source files):

```bash
draftscope refresh-history --lookback 10 --refresh
```

This refresh supplies active-drafted benchmarks, the full-history pick-band support set, and the separate Combine-stage cross-check. It does not build the full-roster college model.

CollegeFootballData is now optional. Use it only when you specifically want its
single-player lookup endpoint:

```bash
draftscope configure-key
draftscope lookup "Player Name" --team "School Name" --season 2026 --out data/players_2026.csv
```

`configure-key` prompts once without echo and writes directly through macOS Keychain Services under the scoped `draftscope-cfbd` service. It does not pass the key to a shell or subprocess. `CFBD_API_KEY` remains the first-priority alternative when it is set. DraftScope never stores a key in its config file.

Run the two-stage refresh workflow manually whenever you want new data. No API key is required for the default providers:

```bash
test -f draftscope.toml || cp draftscope.toml.example draftscope.toml
./run_draftscope.py doctor --config draftscope.toml
./run_draftscope.py update --config draftscope.toml
./run_draftscope.py search "Player Name"
./run_draftscope.py board --top 50
```

The guarded copy preserves an already customized `draftscope.toml`. The first update downloads public bulk files and builds and audits ten checkpoint-aligned historical FBS roster seasons; later same-week runs reuse the audited artifact. SportsDataverse and nflverse need no API key and impose no per-key monthly quota, though GitHub availability and fair-use limits still apply. The completed history is saved at `data/model_history/college_history.csv`; `data/state.json` makes later `search`, `scout`, and `rank` commands select it automatically.

Or discover a national board through the latest fully completed week:

```bash
draftscope discover --season 2026 --per-position 50 \
  --provider sportsdataverse --cache-dir .draftscope-cache \
  --out data/players_2026.csv
```

If the new-season player releases have not appeared yet, `update` can retain a
previous same-week audited player snapshot, when one exists, rather than marking
every player stale. On a clean checkout with no valid current snapshot, it saves
the historical artifact but deliberately publishes no board. It automatically
starts using the new roster and player-box files once published.

Then add verified testing and film grades to the generated CSV. Evaluate or rank:

```bash
draftscope scout "Player Name" \
  --players data/players_2026.csv \
  --team-profiles data/team_profiles_2026.csv \
  --lookback 10

draftscope rank --players data/players_2026.csv --lookback 10 --top 50
```

Add `--json` for machine-readable output. Use `--history` only for a complete, audited historical population with a single declared probability contract.

The legacy research command below builds an entry-conditioned leaver/recorded-production cohort. The automatic updater no longer uses it for the primary college probability, and it is not interchangeable with the full-roster model:

```bash
draftscope build-weekly-history --season 2026 --week 6 --lookback 10 \
  --out data/model_history/history_through_week_06.csv
```

A clearly synthetic end-to-end example is included:

```bash
draftscope scout "Demo Wide Receiver" --players examples/demo_players.csv --lookback 10
```

See the [generated sample scouting report](examples/sample_scouting_report.md)
for a readable walkthrough of the probability, scouting profile, active and
all-history career-elite comparisons, comparable pick range, and team fits. The
prospect, school, conference, and input row in that example are synthetic.

## Candidate data

Important identity and provenance fields:

- `name`, `position`, `school`, `conference`, `season`, `projected_draft_year`
- `as_of_week`, the fully completed statistical checkpoint
- `draft_entry_probability` (`0`–`1`), `draft_declared`, and `draft_eligible`
- `date_of_birth` (ISO `YYYY-MM-DD`) or `age_at_draft`
- optional `recruit_rating`, `recruit_stars`, and `recruit_national_rank`, with a documented pre-checkpoint source and identity join
- `measurement_source`: for example `school_roster`, `verified_workout`, `pro_day`, or `nfl_combine`
- `measurement_date`, `measurements_verified`
- `espn_player_id` for SportsDataverse bulk refresh; `cfbd_player_id` remains supported for optional CFBD lookup

Measurements use inches, pounds, seconds, and repetitions:

- `height_in`, `weight_lb`, `arm_length_in`, `hand_size_in`, `wingspan_in`
- `forty_s`, `ten_split_s`, `bench_reps`, `vertical_in`, `broad_jump_in`, `three_cone_s`, `shuttle_s`

Position-specific production fields start with `prod_`; film rubric fields start with `trait_`. Run `draftscope template players` to get every supported column. Rate fields should be decimals (`0.124`) or explicit percentages (`12.4%`).

Rates derived from SportsDataverse player-box totals use transparent preseason priors (pseudo-attempts/touches) so one early-game play cannot create a perfect-looking efficiency feature. Raw observed rates remain in `observed_*` fields; the shrunk `prod_*` rates are used for modeling and converge toward the observed rate as volume grows. Advanced play-success metrics remain missing unless supplied from a consistently bounded optional source.

## Full historical model data

A probability-training row needs the candidate fields plus:

- `model_stage`: `college_precombine` for the full-roster model or `combine` for a Combine-stage dataset
- `draft_year`
- `drafted` (`true` or `false`)
- `draft_round`, `draft_ovr`, and `elite` when applicable
- `population`, a stable name for the risk set
- `probability_kind`: `conditional_on_entry`, `conditional_on_combine_invitation`, or `unconditional_next_draft`
- `probability_condition`, a plain-language probability contract shown in every report
- `checkpoint`, `as_of_week`, and `feature_cutoff_season` for time-matched models

Keep one player-checkpoint row per `player_id`, `draft_year`, and `as_of_week`. For `college_precombine`, include the complete supported-position FBS roster denominator—including players without statistics and players who did not enter that draft—and set `draft_year` to the immediately following draft. Filtering to entrants, leavers, starters, statistical qualifiers, or known prospects changes the probability and is invalid for `unconditional_next_draft`.

Features must contain only facts available at that checkpoint. Do not include declarations, future roster departure, Combine invitations, pro-day/Combine testing, mock-draft consensus, pre-draft grades, or draft outcomes as inputs. DraftScope's automatic builder performs a contract/leakage audit and refuses strict builds that fail it. Its metadata also records the completed CSV's byte size and SHA-256 digest; automatic selection and reuse reject a history whose data no longer match that sidecar.

For a Week 6 board, DraftScope selects the latest historical checkpoint at or before Week 6 for each player. That prevents partial 2026 totals from being compared with completed historical seasons.

## NFL team profiles

DraftScope automatically creates a conservative, evidence-labeled position prior for all 32 teams. It compares current nflverse listed-room size, average age/experience, each team's position-specific selections and draft capital across the latest three completed drafts, the latest depth-chart snapshot, and prior/current weekly NFL usage-output evidence for projected starters. Every fit reports evidence coverage, source confidence, and profile date. The free nflverse contract release is stale as of 2022, so automatic contract scores are explicitly withheld. Current injuries are also omitted because that nflverse feed ended after 2024. Medicals, coaching stability, future pick ownership, and true film-based starter quality still require dated manual evidence. A current team-position row in `team_profiles_2026.csv` overrides and enriches that prior:

- `team`, `position`, `season`, `scheme`, and `notes`
- `need_score`, `starter_quality_need_score`, `contract_need_score`, `timeline_score`, and `coaching_stability_score` on 0–100 scales, with corresponding optional confidence, evidence-coverage, evidence, source, and as-of fields
- `draft_access_min` and `draft_access_max`, the range of overall picks a team is expected to control or plausibly reach
- optional `draft_access_confidence` and `scheme_confidence` from 0–1
- `importance_trait_*` for traits important to the scheme
- optional `ideal_*`, `tolerance_*`, and `importance_*` values for physical prototypes

The team-fit score is 45% broad need, 10% starter-quality opportunity, 10% contract-window opportunity, 20% scheme/prototype match, 10% draft access, 3% roster timeline, and 2% coaching stability. Each component is weighted by its coverage and confidence, and the result is shrunk toward 50 when support is sparse. A missing component is displayed as unavailable and contributes nothing. Draft access is withheld when the player's probability/comparable evidence is inadequate.

Team fit is downstream scouting context: it never changes the player's draft probability. It is not a mock pick or a claim that a team will select the player. The automatic evidence does not know depth-chart quality, injuries, guarantees, free agency, trades, compensatory picks, coaching changes, interviews, medicals, or private evaluations. Treat manually reviewed profiles as dated inputs and update them as NFL needs and draft order change.

Use `--no-auto-team-needs` on `scout` or `rank` if you want only manually reviewed team profiles.

## Weekly updates and optional scheduling

Copy and edit the sample config, then run an update manually:

```bash
cp draftscope.toml.example draftscope.toml
draftscope update --config draftscope.toml
```

Nothing is scheduled by setup or installation. When the required current-season
sources are available, each successful manual `update` command:

1. identifies the latest fully completed college week—an active week is never treated as complete;
2. downloads SportsDataverse's current FBS roster and player-box partitions, retains labeled roster-only fallbacks for OL/K/P/LS and other players without mapped box scores, and refreshes explicitly tracked players by ESPN athlete ID;
3. joins only games whose schedule week is fully complete, aggregates player-box totals through that checkpoint, and derives comparable early-season-shrunk rate statistics;
4. builds or reuses ten full-roster historical FBS checkpoints for the same week, labels only the immediately following drafts, and runs the model-data contract audit;
5. fits the college pre-combine next-draft model and retains the Combine-participant model only as a separately labeled cross-check;
6. caches downloaded source files locally, preserves provider request evidence where available, and omits failed/stale refreshes from the new board;
7. snapshots players, reports, and the board under `data/snapshots/<season>/week_<NN>/` and records rank/profile/probability movement;
8. preserves content-addressed training checkpoints, fitted-model releases, conservative champion decisions, and every available forecast in an immutable ledger.

New completed-game statistics change player inputs and can change probabilities
and board ranks on the next successful update. They are not new supervised
outcomes and do not prove that the model has learned which current players will
be drafted. New outcome labels arrive only after another NFL Draft is completed
and the historical artifact is rebuilt.

Set `discover_candidates = false` for a tracked-player-only board. `discovery_per_position` caps the national working set while every explicitly tracked player remains included.

Keep `build_weekly_history = true` for the automatic full-roster college model and `strict_model_data = true` so linkage, checkpoint, denominator, and leakage failures stop the model build. `historical_raw_cache_dir` stores reusable source responses. Set `historical_file` only to override the automatic cohort with a complete, consistently contracted file. If the college-history build fails, the update warns explicitly and may produce only the narrower Combine-stage cross-check; it does not relabel that cross-check as next-draft probability.

Keep `players_file` beneath `output_dir` (the example uses `data/players_2026.csv` beneath `data`). This lets each update publish the refreshed player pool, board, reports, and model state as one atomic checkpoint.

SportsDataverse player boxes do not provide complete offensive-line blocking, coverage, route, pressure, or film data. National discovery therefore retains OL/specialists and other roster-only players using a clearly labeled class/measurement triage score—not a draft probability. Add charting/film inputs from a consistent source before treating that pool as a complete scouting list.

On macOS, you may explicitly install the built-in Tuesday 6:00 a.m. launchd schedule:

```bash
./run_draftscope.py schedule install --config draftscope.toml --weekday 2 --hour 6
./run_draftscope.py schedule status
```

Confirm that `schedule status` reports the expected repository and config paths. The job runs the project launcher directly, so shell activation and an API credential are unnecessary for the default providers. To remove it, run `./run_draftscope.py schedule remove`.

On systems with cron, replace the example repository path with the absolute path to your checkout before installing this line:

```cron
0 6 * * 2 cd /absolute/path/to/draftscope && ./run_draftscope.py update --config draftscope.toml >> data/weekly.log 2>&1
```

Do not install both schedulers for the same checkout. Scheduled runs depend on network and upstream availability; inspect `data/weekly.log` and run `draftscope model-status --output-dir data` after failures.

If you separately enable the optional CFBD provider or fallback, its key stays in macOS Keychain or `CFBD_API_KEY`; never put it in the config or source code.

## Data quality checks

```bash
draftscope audit --players data/players_2026.csv
draftscope model-status --output-dir data
```

The first command detects duplicate player-seasons, invalid positions, impossible physical ranges, and trait grades outside 0–100. The second verifies model-release and forecast-ledger hashes and summarizes champion and validation health. Its JSON output includes the latest-checkpoint calibration, within-position board-ranking checks, and available simple-baseline comparisons. Missing values remain missing. Each displayed benchmark includes its sample size and reference population.

## Sources and terms

The MIT license covers DraftScope's original code and documentation, not downloaded source data or generated datasets containing it. Runtime data and caches are excluded from version control. See [`NOTICE.md`](NOTICE.md) before redistributing downloaded or derived data.

- [SportsDataverse public college-football releases](https://github.com/sportsdataverse/sportsdataverse-data) supply season rosters, teams, schedules, and player box scores as downloadable CSV partitions. They require no API key or metered monthly account quota. The release package identifies its data license as [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Raw files are cached locally; source-provider and platform terms still apply.
- [nflverse public data releases](https://github.com/nflverse/nflverse-data), including combine, draft-pick, player identity, current roster, depth-chart, weekly-stat, and snap-count data, supply the ten-draft benchmark, outcome labels, active-player comparisons, and automatic NFL opportunity prior. nflverse is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), while upstream data terms also apply. The stale contract file and ended injury feed are not represented as current facts.
- Wikipedia's [Pro Football Hall of Fame inductee list](https://en.wikipedia.org/wiki/List_of_Pro_Football_Hall_of_Fame_inductees), annual [All-Pro team pages](https://en.wikipedia.org/wiki/All-Pro), and [Pro Bowl player lists](https://en.wikipedia.org/wiki/Lists_of_Pro_Bowl_players), together with [Wikidata](https://www.wikidata.org/), supply the all-history comparison-only honors catalog and explicitly sourced biographical measurements when nflverse has no player-registry measurement. Wikipedia text is licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/), and Wikidata structured data is offered under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). DraftScope selector-filters, league-filters, identity-merges, position-maps, and revision-caches this material. Exact source coverage and unresolved positions are recorded with every build; incomplete historical Pro Bowl roster publication is disclosed rather than represented as complete population coverage.
- [CollegeFootballData API](https://api.collegefootballdata.com/) remains an optional single-player lookup or explicitly enabled fallback. It is not used by the default weekly workflow.
- The 2021 class used decentralized pro-day testing rather than normal in-person combine workouts. DraftScope marks that protocol when 2021 enters a selected window.

## Tests

```bash
python -m unittest discover -s tests -v
```

GitHub Actions runs the standard-library suite on Python 3.11 through 3.14,
enforces branch coverage on Python 3.11, rejects unsafe tracked publication
content, and builds and installs both distribution formats. A pinned CodeQL
workflow runs for public repositories; private repositories can opt in after
enabling GitHub Advanced Security. Network-backed refresh commands are not part
of CI.

## License

DraftScope's original code and documentation are available under the [MIT License](LICENSE). Third-party data remains under its own terms as described in [NOTICE.md](NOTICE.md).
