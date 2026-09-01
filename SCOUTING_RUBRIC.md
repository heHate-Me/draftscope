# DraftScope manual scouting rubric

This rubric governs the manually entered `trait_*` fields defined in
`src/draftscope/schema.py`. It creates an auditable scouting layer; it does not
change or train DraftScope's draft-probability model.

## Grade anchors

| Grade | Meaning |
|---|---|
| 90–100 | Rare, dominant NFL-caliber trait |
| 80–89 | High-impact NFL strength |
| 70–79 | Clear NFL strength |
| 60–69 | Functional NFL trait |
| 50–59 | Adequate college trait with a meaningful NFL limitation |
| 40–49 | Below draftable NFL standard |
| 20–39 | Consistent liability |
| 0–19 | Critically deficient observed trait |

- Missing evidence is blank or `null`, never zero.
- Grades describe observed traits, not character, medical status, career
  outcome, or draft probability.
- A score between anchors requires supporting evidence showing why it belongs
  there.
- A prospect comparison does not justify copying another player's grade.
- Production statistics may add context, but cannot substitute for film
  evidence.
- Grade the observable execution, accounting for assignment and opportunity.
  Do not award or remove points merely because a play succeeded or failed.

## Film-sampling protocol

A **complete** evaluation requires at least three complete games and
approximately 75 position-relevant snaps. Include:

1. one recent game;
2. one game against the strongest available opponent; and
3. one game containing adversity, lower production, or a meaningfully different
   game script.

Record stable, readable game identifiers—not local file paths—plus the opponent,
game date, relevant snaps, source, and notes reference. Use these statuses:

- `complete`: all sample requirements are met;
- `provisional`: useful evidence exists, but the sample is below the complete
  requirement; and
- `insufficient`: the evidence cannot support numeric trait grades.

Provisional grades must be labeled and may be displayed, but they do not enter
the overall scouting-profile score or team-fit calculation. With an
`insufficient` sample, leave every numeric `trait_*` field blank. Opponent
quality changes confidence and the questions a grader can answer; it does not
automatically change a grade. Conference membership alone never raises a grade.

## Trait guide

In the tables below, “observe” names the repeatable behavior to isolate. A
positive or limiting example is evidence to record, not an automatic score.

### Quarterback (`QB`)

| Field | Definition and what to observe | Positive evidence | Limiting evidence | Common mistake |
|---|---|---|---|---|
| `trait_accuracy_short` | Placement and timing on throws generally under 10 air yards; watch target leverage and catch location. | Repeatedly leads receivers away from contact and enables yards after catch. | Behind, high, or late placement disrupts routine concepts. | Substituting completion percentage or screen volume for placement. |
| `trait_accuracy_intermediate` | Placement and timing at roughly 10–19 air yards, especially into layered windows. | Anticipates breaks and locates throws beyond underneath defenders. | Needs a wide window or routinely exposes the receiver. | Grading only completed throws and ignoring catch difficulty. |
| `trait_accuracy_deep` | Placement, trajectory, and timing at roughly 20-plus air yards. | Drops throws into stride with usable arc and boundary control. | Persistent underthrows, overthrows, or flat trajectories erase space. | Grading receiver adjustments or touchdowns as perfect placement. |
| `trait_arm_strength` | Usable velocity and distance from multiple platforms. | Drives field-side throws and accesses deep areas without excessive setup. | Ball dies outside the numbers or velocity collapses off platform. | Equating throw distance, effort, or aggressiveness with functional velocity. |
| `trait_processing` | Speed and accuracy of pre- and post-snap coverage identification and progression work. | Confirms rotation, reaches answers on time, and adjusts after disguise. | Predetermines, stalls after the first read, or repeatedly misses rotation. | Assuming the called concept or an open receiver proves the read. |
| `trait_decision_making` | Risk, timing, and situational quality of choices with ball and down context. | Protects the ball while selecting available high-value answers. | Forces leverage-lost throws or creates avoidable negative plays. | Using interception totals without judging responsibility and alternatives. |
| `trait_pocket_presence` | Spatial awareness, movement, and throwing posture around pressure. | Climbs, slides, and resets while preserving the concept. | Drifts into pressure, drops eyes, or abandons clean pockets. | Treating scrambling frequency as pocket presence. |
| `trait_playmaking` | Ability to create after structure changes while retaining sound choices. | Extends, changes platform, or runs to convert without inviting needless risk. | Turns manageable plays into sacks, turnovers, or unstable mechanics. | Building the grade from a few highlights. |

### Running back (`RB`)

| Field | Definition and what to observe | Positive evidence | Limiting evidence | Common mistake |
|---|---|---|---|---|
| `trait_vision` | Recognition of blocking leverage, fronts, and developing lanes. | Presses landmarks, manipulates defenders, and finds cutback space on time. | Predetermines lanes or misses available daylight. | Crediting blocking quality or box-score efficiency to vision alone. |
| `trait_burst` | Immediate acceleration through a crease or after a cut. | Reaches the second level before angles close. | Slow restart lets pursuit recover. | Grading long speed instead of first-step acceleration. |
| `trait_contact_balance` | Ability to retain feet, path, and power through contact. | Absorbs glancing or square contact and continues productively. | Routine contact stops momentum or topples the runner. | Treating body size or broken-tackle totals as direct proof. |
| `trait_elusiveness` | Ability to make defenders miss with pace, angle, and movement. | Creates misses in confined and open space without losing track. | Extra movement produces no advantage or narrows escape options. | Counting only dramatic jukes. |
| `trait_receiving` | Route execution, hands, adjustment, and transition after the catch. | Separates on assigned routes, catches outside frame, and transitions cleanly. | Limited route detail, unstable hands, or poor ball adjustment. | Using reception totals dominated by screens and checkdowns. |
| `trait_pass_protection` | Recognition, positioning, leverage, and finish in protection. | Identifies threats, squares them, and sustains a viable pocket. | Late scan, poor base, or avoidance creates immediate pressure. | Grading willingness or one collision instead of complete assignments. |
| `trait_ball_security` | Carry mechanics and protection through traffic, contact, and transition. | Maintains proper points of pressure and switches hands appropriately. | Loose carriage or late securing creates repeated exposure. | Treating zero recorded fumbles as complete film evidence. |

### Wide receiver (`WR`)

| Field | Definition and what to observe | Positive evidence | Limiting evidence | Common mistake |
|---|---|---|---|---|
| `trait_release` | Plan, footwork, and hand use to defeat leverage at the line. | Varies tempo and wins clean access against press and off leverage. | Repeats one release or yields chest and timing. | Grading free releases as press wins. |
| `trait_route_running` | Precision, tempo, leverage manipulation, and break efficiency. | Sells stems, stays on timing, and creates repeatable throwing windows. | Rounds breaks, declares early, or drifts from landmarks. | Treating a broad route tree as proof of quality. |
| `trait_separation` | Ability to create usable space through the route, not just raw speed. | Wins leverage at breakpoints and maintains space at the catch point. | Remains covered despite favorable concepts or alignment. | Using target rate or cushion as separation evidence. |
| `trait_ball_skills` | Tracking, hands, body control, and adjustment to the pass. | Locates early and finishes outside the frame across trajectories. | Late tracking, double catches, or poor adjustment shrinks the window. | Counting catch percentage without throw difficulty. |
| `trait_contested_catch` | Technique and finishing ability when a defender affects the catch point. | Establishes position, attacks the ball, and survives contact. | Passive hands or weak positioning loses genuinely contested chances. | Calling every nearby-defender reception contested. |
| `trait_yac` | Creation after the catch through transition, vision, pace, and contact response. | Gets vertical quickly and defeats credible pursuit angles. | Hesitation or poor path leaves available yards. | Crediting scheme-created open grass to the receiver. |
| `trait_blocking` | Assignment, positioning, sustain, and effort as a perimeter blocker. | Fits leverage and stays engaged without penalties. | Missed assignment or poor angle directly compromises the play. | Rewarding aggression when technique and assignment fail. |

### Tight end (`TE`)

| Field | Definition and what to observe | Positive evidence | Limiting evidence | Common mistake |
|---|---|---|---|---|
| `trait_release` | Ability to enter routes cleanly from inline, slot, or detached alignments. | Defeats jams and traffic without losing route timing. | Contact redirects the route or delays the concept. | Grading uncovered releases as wins. |
| `trait_route_running` | Precision, tempo, leverage use, and spacing across tight-end routes. | Manipulates second-level defenders and arrives on schedule. | Telegraphs breaks or crowds adjacent routes. | Letting athleticism or target volume replace route detail. |
| `trait_separation` | Creation of a usable window against relevant coverage matchups. | Wins with stem, body positioning, or break efficiency. | Cannot uncover without a designed free release. | Penalizing expected tight-window usage as automatic failure. |
| `trait_ball_skills` | Tracking, hands, catch radius, and body control. | Adjusts beyond frame and completes catches through legal disruption. | Late eyes or unstable hands waste accessible throws. | Treating height as catch skill. |
| `trait_yac` | Transition, path, power, and evasion after the catch. | Turns upfield efficiently and defeats realistic first contact. | Slow transition or poor angles erase space. | Assigning all schemed catch-and-run yardage to the player. |
| `trait_inline_blocking` | Assignment, leverage, hand placement, sustain, and finish from inline alignments. | Creates or preserves the intended lane against appropriate fronts. | Loses leverage, whiffs, or abandons the block early. | Overweighting size or one pancake. |
| `trait_pass_protection` | Recognition and execution on assigned pass-protection snaps. | Identifies threats, anchors, and hands off movement correctly. | Late recognition or poor base exposes the quarterback. | Grading chips or releases as full protection reps. |

### Offensive tackle (`OT`)

| Field | Definition and what to observe | Positive evidence | Limiting evidence | Common mistake |
|---|---|---|---|---|
| `trait_pass_protection` | Overall ability to preserve pocket width and depth across rush types. | Controls the arc and counters inside moves through the rep. | Gives direct access or loses late after initial contact. | Using sacks allowed without assigning responsibility. |
| `trait_run_blocking` | Ability to execute assigned drive, reach, zone, and combination blocks. | Creates displacement or seals leverage on schedule. | Misses landmark or loses contact before the runner clears. | Grading only visible knockdowns. |
| `trait_footwork` | Balance, set angles, cadence, and redirect ability. | Gains depth without crossing over and mirrors secondary moves. | Opens early, narrows base, or cannot redirect. | Treating quick feet in drills as game footwork. |
| `trait_anchor` | Ability to absorb and stop power while maintaining pocket shape. | Drops weight, re-fits hands, and halts compression. | Gives repeated ground or collapses at contact. | Assuming body weight guarantees an anchor. |
| `trait_hand_usage` | Timing, placement, independent use, grip, and recovery of hands. | Lands inside, varies strikes, and replaces losing hands. | Wide, late, or static hands concede control. | Counting initial contact without evaluating control. |
| `trait_recovery` | Ability to restore leverage after losing the first phase. | Re-centers, redirects, or rides a rusher beyond the pocket. | One early loss becomes immediate access. | Rewarding holds or quarterback escapes as successful recovery. |
| `trait_awareness` | Recognition and communication against games, pressure, and changing threats. | Passes off movement and prioritizes threats correctly. | Chases low-priority movement or leaves a free runner. | Assigning every line bust to the nearest blocker. |

### Interior offensive line (`IOL`)

| Field | Definition and what to observe | Positive evidence | Limiting evidence | Common mistake |
|---|---|---|---|---|
| `trait_pass_protection` | Overall pocket protection against power, quickness, and games inside. | Maintains depth, mirrors, and completes exchanges. | Allows rapid interior displacement or leakage. | Using team sack totals instead of assignment evidence. |
| `trait_run_blocking` | Execution of base, zone, combination, reach, and second-level assignments. | Generates movement or leverage at the required landmark. | Falls off, overshoots, or arrives late. | Grading only blocks at the point of the camera. |
| `trait_anchor` | Ability to absorb interior power without losing pocket integrity. | Sits down and re-fits through contact. | Upright base or backward momentum compresses the pocket. | Confusing mass with functional anchor. |
| `trait_leverage` | Pad level, hip position, and center-of-gravity control through contact. | Gets underneath and maintains an advantageous base. | Plays tall or loses hip relationship. | Awarding a grade from listed height alone. |
| `trait_hand_usage` | Timing, placement, control, and replacement of hands. | Fits inside and independently recovers control. | Late or wide strikes allow immediate access. | Treating force without placement as good hands. |
| `trait_pull_mobility` | Timing, balance, path, and target fit while pulling or working in space. | Clears traffic and connects squarely at the landmark. | Takes wasteful paths or cannot adjust to movement. | Grading straight-line speed rather than block arrival. |
| `trait_awareness` | Recognition and communication on fronts, stunts, and pressure. | Sorts exchanges and finds work without abandoning priority. | Misses movement or helps away from the true threat. | Assuming a visible free rusher identifies the responsible player. |

### Edge defender (`EDGE`)

| Field | Definition and what to observe | Positive evidence | Limiting evidence | Common mistake |
|---|---|---|---|---|
| `trait_get_off` | Reaction and first-step advantage from varied alignments and counts. | Consistently threatens blocker set points without guessing. | Late or false starts surrender initiative. | Grading only obvious passing downs. |
| `trait_bend` | Ability to reduce surface, turn the corner, and finish while retaining balance. | Flattens the arc with ankle/hip flexion and minimal drift. | Runs beyond the pocket or loses balance when cornering. | Equating speed around an untouched edge with bend. |
| `trait_pass_rush_plan` | Sequencing, setup, counters, and adaptation across rushes. | Creates reactions and deploys a timely counter. | Repeats a stalled move without adjustment. | Giving full credit for cleanup sacks. |
| `trait_power` | Force generation and conversion of speed to displacement. | Compresses the edge with leverage and hand placement. | Initial force stalls without control or extension. | Inferring power from body size. |
| `trait_run_defense` | Diagnosis, block response, gap control, and finish versus the run. | Keys action, stays square, and constricts the lane. | Runs past the mesh or yields an assigned gap. | Using tackle totals without grading structure. |
| `trait_setting_edge` | Ability to establish outside leverage and control the perimeter. | Locks out, stays square, and forces the ball to help. | Gets reached, kicked out, or dives inside. | Treating an unblocked contain rep as an edge win. |
| `trait_motor` | Repeatable pursuit and finish effort within assignment across the sample. | Re-enters plays and sustains chase without compromising contain. | Regularly stops after the first action. | Using one chase-down highlight or judging character. |

### Interior defensive line (`IDL`)

| Field | Definition and what to observe | Positive evidence | Limiting evidence | Common mistake |
|---|---|---|---|---|
| `trait_get_off` | Snap reaction and first-step advantage from interior alignments. | Enters the blocker's frame or gap before the fit develops. | Consistently late out of stance. | Ignoring assignment when rewarding penetration. |
| `trait_hand_usage` | Strike timing, placement, extension, shed, and counter ability. | Controls the blocker's frame and disengages on time. | Chest remains exposed or hands stall after contact. | Grading activity instead of effective control. |
| `trait_power` | Ability to generate displacement through leverage and force. | Walks blockers back or resets the line without losing balance. | Force rises or dissipates on contact. | Inferring power from weight or a single collision. |
| `trait_pass_rush_plan` | Rush setup, sequencing, counters, and finish from inside. | Links moves and attacks protection weaknesses. | Rush ends when the first move stalls. | Crediting schemed free rushes as plan evidence. |
| `trait_run_defense` | Diagnosis, block control, lane reduction, and tackling versus the run. | Holds or changes the point and finishes in assigned space. | Displacement or late recognition opens the lane. | Using tackles for loss without grading all run snaps. |
| `trait_gap_discipline` | Consistency maintaining assigned gap and block relationship. | Controls space while tracking the ball and exchange rules. | Vacates responsibility for low-probability penetration. | Rewarding disruption that breaks the defense's structure. |
| `trait_motor` | Repeatable pursuit and second-effort execution within assignment. | Works through sustained blocks and pursues after the ball declares. | Frequent early shutdown removes the player from recoverable plays. | Turning effort observations into a character claim. |

### Linebacker (`LB`)

| Field | Definition and what to observe | Positive evidence | Limiting evidence | Common mistake |
|---|---|---|---|---|
| `trait_instincts` | Recognition, key discipline, and speed to the correct response. | Diagnoses action without false steps and anticipates route/run development. | Bites on misdirection or arrives after the lane forms. | Assuming fast movement is a correct read. |
| `trait_range` | Functional field coverage using angles, acceleration, and play recognition. | Reaches perimeter or depth landmarks under control. | Poor angles or delayed trigger shrink effective range. | Equating 40 time with game range. |
| `trait_tackling` | Approach, leverage, contact balance, wrap, and finish. | Arrives square and completes tackles across space and traffic. | Overruns, lunges, or fails to secure routine contact. | Using tackle totals without missed opportunities. |
| `trait_block_destruction` | Ability to avoid, stack, shed, or work through blockers. | Keeps frame clean and separates in time to affect the ball. | Gets engulfed or chooses paths that abandon the fit. | Rewarding avoidance that creates a gap. |
| `trait_coverage` | Route recognition, spacing, leverage, transition, and ball response. | Carries threats and closes windows within assignment. | Loses landmarks or cannot transition with the route. | Using interceptions as the whole coverage grade. |
| `trait_blitzing` | Timing, path, disguise, block attack, and finish as a blitzer. | Hits the proper lane and defeats protection with a plan. | Declares early or runs into available blockers. | Crediting free rushes or sacks created by others. |
| `trait_pursuit` | Angles, tempo, leverage, and finish away from the initial action. | Maintains cutback leverage and closes without overrunning. | Takes shallow angles or trails without affecting the play. | Treating raw speed or effort alone as pursuit quality. |

### Cornerback (`CB`)

| Field | Definition and what to observe | Positive evidence | Limiting evidence | Common mistake |
|---|---|---|---|---|
| `trait_man_coverage` | Leverage, mirror, transition, and route response in man assignments. | Stays in phase or recovers to the proper hip across route types. | Loses leverage at release or breakpoint with no recovery path. | Treating an unthrown target as a win without viewing the route. |
| `trait_zone_coverage` | Spacing, route distribution, eyes, and trigger within zone rules. | Balances threats and closes the intended window on time. | Chases one route and exposes assigned space. | Blaming the nearest defender without knowing the coverage. |
| `trait_press` | Footwork, leverage, jam timing, patience, and recovery at the line. | Controls release access without opening the gate early. | Lunges, misses, or yields immediate leverage. | Counting any contact as a successful press rep. |
| `trait_ball_skills` | Locate, track, position, and finish at the catch point. | Finds the ball without losing the receiver and attacks cleanly. | Panics, plays through the receiver, or fails to finish. | Using interception totals without opportunity context. |
| `trait_recovery_speed` | Functional ability to restore phase after initial separation. | Closes space while preserving a legal play on the ball. | Separation compounds once the receiver gains a step. | Substituting a timed 40 for recovery evidence. |
| `trait_tackling` | Approach, leverage, willingness to fit assignment, wrap, and finish. | Controls space and completes tackles without avoidable leakage. | Poor angle, lunge, or failed wrap gives up extra yards. | Treating collision force as tackling consistency. |
| `trait_instincts` | Recognition, anticipation, and trigger within coverage/run responsibility. | Reads route combinations and acts before the window fully forms. | False steps or eye discipline repeatedly creates openings. | Rewarding gambling when it violates assignment. |

### Safety (`S`)

| Field | Definition and what to observe | Positive evidence | Limiting evidence | Common mistake |
|---|---|---|---|---|
| `trait_man_coverage` | Leverage, transition, and route response in assigned man coverage. | Matches tight ends, backs, or slots without losing phase. | Gives up leverage early or cannot transition. | Grading only favorable matchup reps. |
| `trait_zone_coverage` | Depth, spacing, route distribution, eyes, and trigger in zone. | Maintains structure while closing the correct window. | Drifts, overcommits, or arrives after the throw. | Assuming every deep completion belongs to the safety. |
| `trait_range` | Functional ground covered from alignment to the play using recognition and angles. | Reaches sidelines or seams in time to affect the catch point. | Late trigger or inefficient path leaves unreachable space. | Equating timed speed with game range. |
| `trait_ball_skills` | Tracking, positioning, hands, and finish at the catch point. | Locates through traffic and converts legitimate opportunities. | Misjudges flight or cannot complete the takeaway/play. | Using interception count without chance quality. |
| `trait_tackling` | Approach, leverage, control, wrap, and finish at multiple depths. | Breaks down and finishes in space while protecting leverage. | Overruns or misses as the last line. | Grading hit power instead of reliability. |
| `trait_instincts` | Recognition, anticipation, and disciplined trigger versus run and pass. | Identifies concepts and arrives without abandoning responsibility. | False keys or delayed decisions create explosive space. | Rewarding freelancing solely because the result was positive. |
| `trait_versatility` | Demonstrated execution across distinct alignments and responsibilities. | Performs credibly deep, in the box, over the slot, or on special teams. | Alignment variety does not include assignment quality. | Counting positions listed rather than successful responsibilities. |

### Kicker (`K`)

| Field | Definition and what to observe | Positive evidence | Limiting evidence | Common mistake |
|---|---|---|---|---|
| `trait_accuracy` | Repeatable directional and distance control on field goals and extra points. | Consistent start line and finish across hashes and ranges. | Recurring directional miss or unstable strike pattern. | Using make percentage without distance, conditions, and operation context. |
| `trait_leg_strength` | Usable field-goal range and ball speed without sacrificing control. | Clears from extended range with repeatable trajectory. | Maximum-effort mechanics degrade trajectory or direction. | Grading only a career-long make. |
| `trait_kickoff` | Distance, hang, location, and tactical repeatability on kickoffs. | Reaches called depth/location with coverage-friendly flight. | Short, low, or poorly located balls stress coverage. | Using touchback rate without venue and intent. |
| `trait_pressure_performance` | Technique stability in clearly defined high-leverage situations. | Operation and strike remain repeatable across multiple pressure samples. | Process consistently changes or deteriorates in those samples. | Making character claims or grading one make/miss. |

### Punter (`P`)

| Field | Definition and what to observe | Positive evidence | Limiting evidence | Common mistake |
|---|---|---|---|---|
| `trait_distance` | Functional punt distance relative to field position and call. | Produces usable length without outkicking coverage. | Cannot change field position when distance is required. | Using gross average without field and return context. |
| `trait_hang_time` | Flight time and trajectory that support coverage. | Repeatably pairs sufficient hang with the called distance. | Low flight creates return space. | Treating maximum hang on a short punt as complete evidence. |
| `trait_placement` | Directional and boundary control appropriate to the call. | Pins return space or lands inside target areas. | Middle-field or uncontrolled placement creates favorable returns/touchbacks. | Grading outcome without accounting for bounce and coverage. |
| `trait_consistency` | Repeatability of catch, operation, strike, flight, and placement. | Avoids major mishits across varied field positions and conditions. | Volatile contact produces unusable punts. | Letting one exceptional punt dominate the sample. |

### Long snapper (`LS`)

| Field | Definition and what to observe | Positive evidence | Limiting evidence | Common mistake |
|---|---|---|---|---|
| `trait_snap_accuracy` | Location of punt and placement snaps relative to the intended target. | Repeatably reaches the target window without adjustment. | High, low, or lateral misses disrupt operation timing. | Treating a completed kick or punt as proof of an accurate snap. |
| `trait_snap_velocity` | Repeatable delivery time and ball travel without loss of control. | Reaches the target on schedule with stable rotation. | Slow or variable delivery compresses the operation. | Inferring velocity from broadcast appearance without timing where possible. |
| `trait_blocking` | Assignment, base, hand use, and transition after the snap. | Protects the interior lane and releases according to the call. | Immediate leakage or poor transition compromises the operation. | Ignoring protection because the snap was accurate. |
| `trait_coverage` | Release, lane discipline, block defeat, and tackling on punt coverage. | Enters the correct lane and affects the return under control. | Loses lane or cannot navigate a block. | Grading only tackle totals or straight-line speed. |

## Provenance and review fields

Whenever any numeric `trait_*` grade is present, retain the following portable
fields with the player row:

- `film_grade_status`, `film_grader`, `film_graded_at`, `film_game_ids`,
  `film_games_reviewed`, `film_snaps_reviewed`, `film_opponent_mix`,
  `film_grade_source`, and `film_notes_path`;
- when a second review exists: `film_second_grader`,
  `film_second_grader_agreement`, and `film_consensus_method`.

Use an ISO `YYYY-MM-DD` grading date. A notes path must be a relative POSIX path
without `..`, a URL, a home-directory shortcut, or a machine-specific absolute
path. The evidence file should preserve per-game and per-trait observations,
including counter-evidence. Record trait confidence as `low`, `moderate`, or
`high` based on sample size, view quality, assignment clarity, and the variety
of situations actually observed. Agreement describes the recorded comparison
between two independent grades; it is not a confidence substitute. Calculate
`film_second_grader_agreement` before consensus as the number of commonly graded
position traits whose two raw grades are within five points, divided by the
number of commonly graded traits. Round to three decimals and retain both raw
grades in the evidence JSON. Use `independent_average` when the final trait
grade is the arithmetic mean of the two grades, `lead_grader` when the named
primary grader makes the final call, or `discussion_consensus` when both graders
agree on a final grade after reviewing their differences.

In CSV rows, separate game identifiers and opponent-mix tags with semicolons;
JSON may use lists. A game identifier must be 3–80 characters, begin with a
letter or number, and otherwise use only letters, numbers, `.`, `_`, `:`, or
`-`. Opponent-mix tags are `recent`, `strongest_available`, `adversity`,
`lower_production`, and `different_game_script`.

Manual film metadata and `trait_*` grades remain outside model training,
calibration, validation, retrospective probability replay, and draft
probability. Complete grades may affect only the separately labeled scouting
profile and appropriately supported prototype/team-fit context.
