# 2027 scouting report template

Use this template only after recording the film sample in
[`scouting_evidence_template.json`](scouting_evidence_template.json). Replace
bracketed prompts with sourced observations. Leave unknown values blank; never
invent an observation, play reference, grade, or source.

## Player / position / school / checkpoint

- Player: [name]
- Position: [normalized position]
- School: [school]
- Checkpoint: [season and completed week]
- Projected draft year: 2027

## Evaluation status and sample

- Status: [complete / provisional / insufficient]
- Grader: [name]
- Grading date: [YYYY-MM-DD]
- Games reviewed: [count]
- Relevant snaps reviewed: [count]
- Game IDs: [stable readable identifiers]
- Opponent mix: [recent; strongest_available; adversity, lower_production, or
  different_game_script as applicable]
- Film source: [lawfully accessed source]
- Evidence notes: [portable relative path]

If the status is `provisional`, label all displayed grades provisional and keep
them out of the overall scouting-profile score and team-fit calculation. If the
status is `insufficient`, publish no numeric trait grades.

## Role projection

[Projected NFL role, role boundaries, and the film evidence supporting them.]

## Best NFL deployment

[Alignment, usage, and responsibilities that best fit the observed traits.]

## Bottom line

[Concise film conclusion, evidence confidence, and most important unresolved
question. Do not state draft probability here.]

## Trait grades

Use only the position's fields from the
[`SCOUTING_RUBRIC.md`](../SCOUTING_RUBRIC.md). Add one row for every graded
trait; grade and confidence stay blank when evidence is missing.

| Trait field | Grade (0–100) | Confidence | Supporting game/play evidence | Counter-evidence |
|---|---:|---|---|---|
| [`trait_...`] | [grade or blank] | [low / moderate / high, or blank] | [game ID, play reference, observation] | [game ID, play reference, observation] |

## Strengths

- [Observed repeatable strength with game/play reference.]

## Limitations

- [Observed repeatable limitation with game/play reference.]

## Development priorities

1. [Specific, observable improvement priority.]

## Scheme fits

- [Supported scheme or role fit and the evidence behind it.]

## Scheme limitations

- [Assignment or usage that exposes an observed limitation.]

## Statistical context

[Relevant production context, sample size, role, and limitations. Statistics
may contextualize film but do not replace film evidence.]

## DraftScope probability output — separate model result

- Probability population: [exact population]
- Probability checkpoint: [season / week]
- Draft probability: [model output or withheld]
- Model validation status: [passed / withheld and reason]
- Model release or snapshot: [identifier]

This probability is produced independently of all manual `trait_*` grades and
film metadata. It is not the scouting grade, role projection, or team fit.

## Film evidence by game

### [stable game ID] — [opponent] — [game date]

- Relevant snaps reviewed: [count]
- Sample role: [recent / strongest_available / adversity / lower_production /
  different_game_script]
- Positive evidence: [play reference and observation]
- Limiting or counter-evidence: [play reference and observation]
- Source: [lawfully accessed source]

Repeat this section for every complete game reviewed.

## Opponent-quality discussion

[Explain which assignments the opponent allowed the grader to test and how that
affects confidence. Do not raise or lower a trait merely because of conference
or opponent reputation.]

## Missing evidence

- [Ungraded trait, unavailable angle, missing assignment context, or other
  unresolved evidence.]

## Second-grader agreement

- Second grader: [name or blank]
- Agreement: [0–1 or blank]
- Agreement calculation: [traits within five raw-grade points / commonly graded
  traits, calculated before consensus]
- Consensus method: [independent_average / lead_grader /
  discussion_consensus / blank]
- Material disagreements: [traits, evidence, and disposition]

## Sources and grading date

- Film sources: [source for each game]
- Statistical sources: [source and checkpoint]
- Grading completed: [YYYY-MM-DD]
- Evidence JSON: [portable relative path]
