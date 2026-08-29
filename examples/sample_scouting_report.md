# Sample scouting report

This report uses the entirely synthetic row in
[`demo_players.csv`](demo_players.csv). `Demo Wide Receiver`, `Synthetic
University`, and `Synthetic Conference` are not real people or organizations.
The output was generated on 2026-08-28 after building the audited 2026
preseason history. Exact probabilities, comparisons, team fits, and coverage
notes can change when source snapshots or model history are refreshed.

Command:

```bash
./run_draftscope.py scout "Demo Wide Receiver" \
  --players examples/demo_players.csv \
  --lookback 10
```

Output:

```text
DRAFTSCOPE PLAYER SUMMARY
Demo Wide Receiver | WR | Synthetic University | 2027 NFL Draft
Updated 2026-08-28 (Week 6)

BOTTOM LINE
Draft chance: 2% for the next NFL Draft
Plain English: about 2 of 100 comparable checkpoint profiles were drafted in the immediately following draft.
Historical position baseline: 1.5% at the same checkpoint
If drafted: comparable range #46–#167 (middle comparable #157)
Data status: model inputs 11/12; broader scouting profile 76% complete
Broader profile: 67/100 — Draftable traits with development upside
Historical model check: passed

SCOUTING SNAPSHOT — 76% COMPLETE
- Physical: 71/100 (9/12 inputs)
- Production: 43/100 (4/13 inputs)
- Skills: 77/100 (7/7 inputs)
- Age: 77/100 (1/1 inputs)

WHY THE MODEL LIKES THE PLAYER
- Weight: player 205 lb | active drafted avg 197 lb | elite avg 197 lb | position fit 98/100
- Height: player 74 in | active drafted avg 72.65 in | elite avg 72.36 in | position fit 93/100
- Weight-adjusted speed: player 109 | active drafted avg 101 | elite avg 101 | position fit 81/100

CONCERNS / MISSING INFORMATION
- Receptions: player 38 | active drafted avg 48.14 | elite avg 49.32 | position fit 34/100
- Receiving Yards: player 612 | active drafted avg 721 | elite avg 772 | position fit 39/100

ACTIVE NFL COMPARISONS
- Terrace Marshall Jr. — 85% similar, drafted #59 in 2021, 8 shared measurements/metrics
- Justin Jefferson — 81% similar, drafted #22 in 2020, 8 shared measurements/metrics
- Rome Odunze — 81% similar, drafted #9 in 2024, 10 shared measurements/metrics
- Andrei Iosivas — 80% similar, drafted #206 in 2023, 10 shared measurements/metrics
- Elic Ayomanor — 80% similar, drafted #136 in 2025, 8 shared measurements/metrics

CAREER-ELITE NFL COMPARISONS — ALL-HISTORY SOURCES — PARTIAL SOURCE COVERAGE
- Amari Cooper — 89% similar, drafted #4 in 2015, 4 shared measurements/metrics, 5x Pro Bowl, measurements: NFL Combine
- Justin Jefferson — 85% similar, drafted #22 in 2020, 4 shared measurements/metrics, 2x first-team All-Pro, 4x Pro Bowl, measurements: NFL Combine
- Cordarrelle Patterson — 84% similar, drafted #29 in 2013, 4 shared measurements/metrics, 4x first-team All-Pro, 4x Pro Bowl, measurements: NFL Combine
- Ahmad Rashad — 84% similar, 2 shared measurements/metrics, 4x Pro Bowl, measurements: nflverse player biography
- Chimere Dike — 81% similar, drafted #103 in 2025, 4 shared measurements/metrics, 1x first-team All-Pro, 1x Pro Bowl, measurements: NFL Combine
Elite means Hall of Fame, at least one first-team All-Pro, or at least three Pro Bowls.
Coverage: all NFL eras for Hall of Fame inductees; canonical first-team All-Pro selections 1920–2025; Pro Bowl selections 1950–2025; nflverse drafted-player count reconciliation 1980–2026.
Coverage boundary: All-Pro coverage uses one canonical first-team selector per season. Pro Bowl data span the full 1950–2025 era, but 23 seasons lack a complete annual roster; A–Z lists cover those years without proving full recall. The catalog is comparison-only and never trains the draft model. 1 qualifying source row retains an unresolved historical position family; details are in catalog metadata.

RECENT DRAFTED PROFILE COMPARISONS
- Quentin Johnston — 93% similar, drafted #21 in 2023, 6 shared measurements/metrics
- George Pickens — 90% similar, drafted #52 in 2022, 6 shared measurements/metrics
- Jordan Lasley — 89% similar, drafted #162 in 2018, 6 shared measurements/metrics
These support the pick range; they are not necessarily elite NFL players.

POSSIBLE NFL TEAM FITS
1. PIT — 60/100 | evidence: moderate (57% coverage) | listed room: 9 vs NFL median 12; veteran-room pressure: age 26.7, exp 3.3y
2. SF — 60/100 | evidence: moderate (54% coverage) | veteran-room pressure: age 26.9, exp 3.9y; recent investment: 0 WR pick(s) in 2024–2026
3. ATL — 59/100 | evidence: moderate (57% coverage) | starter-quality opportunity 83/100; listed room: 10 vs NFL median 12
Team fits show roster opportunity, not a prediction of who will draft the player.

When the weekly updater is run (or its optional schedule is installed), new statistics can change the draft chance and board position.
Add --full to the command for validation metrics and every underlying table.
```

The probability is intentionally low because its denominator is every
supported-position player on the FBS roster at the same checkpoint, not only
declared prospects or media-board candidates. The report keeps that probability,
the broader scouting grade, similarity comparisons, comparable pick range, and
team-fit context separate.
