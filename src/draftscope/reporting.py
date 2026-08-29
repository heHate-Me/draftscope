from __future__ import annotations

from typing import Any, Iterable, Mapping

from .analysis import Evaluation
from .records import parse_number
from .schema import all_specs, normalize_position


def _number(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def _percent(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{100.0 * value:.{digits}f}%"


def _table(headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    materialized = [[str(value) for value in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in materialized:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    header_line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    rule = "  ".join("-" * width for width in widths)
    body = ["  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in materialized]
    return "\n".join([header_line, rule, *body])


def render_player_summary(
    result: Evaluation | Mapping[str, Any],
    *,
    board_row: Mapping[str, Any] | None = None,
) -> str:
    """Render the decision-level player report used by normal CLI commands."""
    data = result.to_dict() if isinstance(result, Evaluation) else dict(result)
    player = _mapping(data.get("player"))
    prediction = _mapping(data.get("draft_prediction"))
    name = str(player.get("name") or "Unknown player")
    position = str(player.get("position") or "—")
    school = str(player.get("school") or "—")
    known_drafted = str(player.get("drafted") or "").strip().lower() in {
        "1",
        "1.0",
        "true",
        "yes",
        "drafted",
    } and _float_or_none(player.get("draft_ovr") or player.get("draft_pick")) is not None
    projected_year = _float_or_none(
        player.get("draft_year") if known_drafted else player.get("projected_draft_year")
    )
    if projected_year is None and not known_drafted:
        season = _float_or_none(player.get("season"))
        projected_year = season + 1 if season is not None else None
    draft_label = f" | {int(projected_year)} NFL Draft" if projected_year else ""

    lines = [
        "DRAFTSCOPE PLAYER SUMMARY",
        f"{name} | {position} | {school}{draft_label}",
        f"Updated {data.get('as_of_date') or 'date unavailable'}{_week(player.get('as_of_week'))}",
        "",
        "BOTTOM LINE",
    ]

    probability = _float_or_none(prediction.get("probability"))
    if known_drafted:
        actual_pick = round(
            _float_or_none(player.get("draft_ovr") or player.get("draft_pick")) or 0
        )
        actual_round = _float_or_none(player.get("draft_round"))
        actual_year = _float_or_none(player.get("draft_year") or player.get("season"))
        actual_team = str(player.get("nfl_team") or player.get("draft_team") or "team unavailable")
        result_parts = [f"selected #{actual_pick}"]
        if actual_round is not None:
            result_parts.append(f"Round {round(actual_round)}")
        result_parts.append(f"by {actual_team}")
        year_text = f"{round(actual_year)} result" if actual_year is not None else "Draft result"
        lines.append(f"{year_text}: " + " | ".join(result_parts))
        lines.append("Draft chance: not shown because the draft outcome is already known.")
    elif bool(prediction.get("available")) and probability is not None:
        lines.append(f"Draft chance: {_percent(probability)} for the next NFL Draft")
        lines.append(
            f"Plain English: about {round(probability * 100):d} of 100 comparable checkpoint profiles "
            "were drafted in the immediately following draft."
        )
        validation = _mapping(prediction.get("validation"))
        prevalence = _float_or_none(validation.get("prevalence"))
        if prevalence is not None:
            lines.append(
                f"Historical position baseline: {_percent(prevalence, 1)} at the same checkpoint"
            )
    else:
        warnings = [str(item) for item in (prediction.get("warnings") or ()) if item]
        reason = warnings[0] if warnings else str(
            prediction.get("label") or "not enough reliable data"
        )
        if not warnings and _looks_like_available_projection(reason):
            reason = "the model did not return a publishable probability"
        lines.append(f"Draft chance: unavailable — {reason}")

    if board_row:
        rank = _float_or_none(board_row.get("rank"))
        rank_delta = _float_or_none(board_row.get("rank_delta"))
        probability_delta = _float_or_none(board_row.get("draft_probability_delta"))
        board_parts = [f"#{int(rank)} overall" if rank is not None else "not ranked"]
        if rank_delta is None or rank_delta == 0:
            board_parts.append("unchanged")
        elif rank_delta > 0:
            board_parts.append(f"up {int(rank_delta)} spot{_plural(rank_delta)}")
        else:
            board_parts.append(f"down {abs(int(rank_delta))} spot{_plural(rank_delta)}")
        if probability_delta is not None:
            board_parts.append(f"draft chance {_signed_percent_words(probability_delta)}")
        lines.append("Draft board: " + " | ".join(board_parts))

    pick_range = data.get("projected_pick_range")
    if not known_drafted and isinstance(pick_range, (list, tuple)) and len(pick_range) == 3:
        low = _float_or_none(pick_range[0])
        middle = _float_or_none(pick_range[1])
        high = _float_or_none(pick_range[2])
        if low is not None and middle is not None and high is not None:
            lines.append(
                f"If drafted: comparable range #{round(low)}–#{round(high)} "
                f"(middle comparable #{round(middle)})"
            )

    model_feature_count = len(tuple(prediction.get("features_used") or ()))
    model_coverage = _float_or_none(prediction.get("feature_coverage"))
    profile_coverage = _float_or_none(data.get("evidence_coverage"))
    if not known_drafted and model_feature_count and model_coverage is not None:
        observed_count = round(model_feature_count * model_coverage)
        lines.append(
            f"Data status: model inputs {observed_count}/{model_feature_count}; "
            f"broader scouting profile {_percent(profile_coverage)} complete"
        )
    elif profile_coverage is not None:
        lines.append(f"Data status: broader scouting profile {_percent(profile_coverage)} complete")

    profile_score = _float_or_none(data.get("profile_score"))
    if profile_score is not None and (profile_coverage or 0.0) >= 0.5:
        lines.append(
            f"Broader profile: {_number(profile_score, 0)}/100 — "
            f"{data.get('profile_tier') or 'unrated'}"
        )
    elif profile_score is not None:
        lines.append(
            "Overall scouting grade: withheld because the broader profile is less than 50% complete"
        )

    validation = _mapping(prediction.get("validation"))
    if validation and not known_drafted:
        lines.append(
            "Historical model check: "
            + ("passed" if validation.get("passes_quality_gate") else "did not pass")
        )
    outlier_score = _float_or_none(prediction.get("out_of_distribution_score"))
    if outlier_score is not None and outlier_score > 3.0:
        lines.append("Reliability flag: CAUTION — this profile is outside the usual historical range.")

    categories = [
        _mapping(item)
        for item in (data.get("categories") or ())
        if isinstance(item, Mapping)
    ]
    lines.extend(["", f"SCOUTING SNAPSHOT — {_percent(profile_coverage)} COMPLETE"])
    for item in categories:
        category = str(item.get("category") or "Unknown").title()
        score = _float_or_none(item.get("score"))
        observed = _float_or_none(item.get("observed_metrics"))
        expected = _float_or_none(item.get("expected_metrics"))
        if score is None:
            lines.append(f"- {category}: no reliable data")
        else:
            input_text = ""
            if observed is not None and expected is not None:
                input_text = f" ({round(observed)}/{round(expected)} inputs)"
            lines.append(f"- {category}: {score:.0f}/100{input_text}")

    benchmarks = [
        _mapping(item)
        for item in (data.get("benchmarks") or ())
        if isinstance(item, Mapping)
    ]
    scored = [
        item for item in benchmarks if _float_or_none(item.get("favorable_score")) is not None
    ]
    strengths = sorted(
        (item for item in scored if (_float_or_none(item.get("favorable_score")) or 0.0) >= 60.0),
        key=lambda item: _float_or_none(item.get("favorable_score")) or 0.0,
        reverse=True,
    )[:3]
    concerns = sorted(
        (item for item in scored if (_float_or_none(item.get("favorable_score")) or 0.0) < 50.0),
        key=lambda item: _float_or_none(item.get("favorable_score")) or 0.0,
    )[:2]

    lines.extend(["", "WHY THE MODEL LIKES THE PLAYER"])
    if strengths:
        lines.extend(f"- {_benchmark_summary(item)}" for item in strengths)
    else:
        lines.append("- No observed metric is clearly above the recent drafted-player norm yet.")

    lines.extend(["", "CONCERNS / MISSING INFORMATION"])
    if concerns:
        lines.extend(f"- {_benchmark_summary(item)}" for item in concerns)
    missing_categories = [
        str(item.get("category") or "").title()
        for item in categories
        if _float_or_none(item.get("score")) is None
    ]
    if missing_categories:
        lines.append("- Not graded because data are missing: " + ", ".join(missing_categories))
    if outlier_score is not None and outlier_score > 3.0:
        lines.append("- The combination of inputs is unusual compared with the training history.")
    if not concerns and not missing_categories and not (outlier_score is not None and outlier_score > 3.0):
        lines.append("- No major concern appears in the currently observed benchmark fields.")

    overall_comps = [
        _mapping(item)
        for item in (data.get("overall_comps") or ())
        if isinstance(item, Mapping)
    ]
    physical_comps = [
        _mapping(item)
        for item in (data.get("physical_comps") or ())
        if isinstance(item, Mapping)
    ]
    comps = overall_comps or physical_comps
    heading = "ACTIVE NFL COMPARISONS" if overall_comps else "ACTIVE NFL PHYSICAL COMPARISONS"
    lines.extend(["", heading])
    if comps:
        lines.extend(f"- {_comparable_summary(comp)}" for comp in comps[:5])
    else:
        lines.append("- No trustworthy active-player comparison yet; more verified measurements are needed.")

    historical_elite_comps = [
        _mapping(item)
        for item in (data.get("historical_elite_comps") or ())
        if isinstance(item, Mapping)
    ]
    benchmark_context = _mapping(data.get("benchmark_context"))
    elite_scope = str(
        benchmark_context.get("historical_career_elite_scope")
        or "provided career-elite references; exact scope metadata unavailable"
    )
    if benchmark_context.get(
        "historical_career_elite_full_position_coverage_complete"
    ):
        elite_heading = "ALL NFL HISTORY"
    elif benchmark_context.get(
        "historical_career_elite_full_history_criteria_available"
    ):
        elite_heading = "ALL-HISTORY SOURCES — PARTIAL POSITION COVERAGE"
    elif benchmark_context.get(
        "historical_career_elite_full_history_sources_available"
    ):
        elite_heading = "ALL-HISTORY SOURCES — PARTIAL SOURCE COVERAGE"
    elif benchmark_context.get(
        "historical_career_elite_hof_all_era_available"
    ):
        elite_heading = "ALL-ERA HOF + LIMITED HISTORICAL HONORS"
    else:
        elite_heading = "SEPARATE HISTORICAL CATALOG"
    lines.extend(["", f"CAREER-ELITE NFL COMPARISONS — {elite_heading}"])
    if historical_elite_comps:
        lines.extend(
            f"- {_comparable_summary(comp, show_accolades=True)}"
            for comp in historical_elite_comps[:5]
        )
        lines.append(
            "Elite means Hall of Fame, at least one first-team All-Pro, or at least three Pro Bowls."
        )
        lines.append(f"Coverage: {elite_scope}.")
    else:
        if "unavailable" in elite_scope.lower():
            lines.append("- The career-elite comparison catalog is unavailable.")
        else:
            lines.append("- No career-elite player has enough overlapping measurements yet.")
        lines.append(f"Coverage: {elite_scope}.")
    coverage_note = str(
        benchmark_context.get("historical_career_elite_coverage_note") or ""
    ).strip()
    if coverage_note:
        lines.append(f"Coverage boundary: {coverage_note}")

    pick_comps = [
        _mapping(item)
        for item in (data.get("pick_comps") or ())
        if isinstance(item, Mapping)
    ]
    if pick_comps:
        lines.extend(["", "RECENT DRAFTED PROFILE COMPARISONS"])
        lines.extend(f"- {_comparable_summary(comp)}" for comp in pick_comps[:3])
        lines.append("These support the pick range; they are not necessarily elite NFL players.")

    team_fits = [
        _mapping(item)
        for item in (data.get("team_fits") or ())
        if isinstance(item, Mapping)
    ]
    lines.extend(["", "POSSIBLE NFL TEAM FITS"])
    if team_fits:
        for index, fit in enumerate(team_fits[:3], start=1):
            score = _float_or_none(fit.get("score"))
            confidence = str(fit.get("confidence_label") or "unrated")
            fit_coverage = _float_or_none(fit.get("evidence_coverage"))
            reasons = [str(reason) for reason in (fit.get("reasons") or ()) if reason]
            why = "; ".join(reasons[:2]) or "limited supported evidence"
            score_text = f"{score:.0f}/100" if score is not None else "unscored"
            evidence_text = confidence
            if fit_coverage is not None:
                evidence_text += f" ({_percent(fit_coverage)} coverage)"
            lines.append(
                f"{index}. {fit.get('team') or '—'} — {score_text} | evidence: {evidence_text} | {why}"
            )
        lines.append("Team fits show roster opportunity, not a prediction of who will draft the player.")
    else:
        lines.append("No supported team-fit result is available.")

    lines.append("")
    if not known_drafted:
        lines.append(
            "When the weekly updater is run (or its optional schedule is installed), new "
            "statistics can change the draft chance and board position."
        )
    lines.append("Add --full to the command for validation metrics and every underlying table.")
    return "\n".join(lines) + "\n"


def render_evaluation(result: Evaluation) -> str:
    player = result.player
    lines = [
        "DRAFTSCOPE PROSPECT REPORT",
        f"{player.get('name', '')} | {player.get('position', '')} | {player.get('school', '')}",
        f"As of {result.as_of_date}{_week(player.get('as_of_week'))} | benchmark drafts {_window(result.benchmark_window)}",
        "",
    ]
    benchmark_context = result.benchmark_context
    if benchmark_context.get("explicit_active_cohort"):
        active_drafted = benchmark_context.get("active_drafted_window_rows")
        denominator = f"/{active_drafted}" if active_drafted is not None else ""
        snapshot = f"{benchmark_context.get('active_roster_season') or 'current'} roster"
        if benchmark_context.get("active_roster_snapshot_week") is not None:
            snapshot += f" Week {benchmark_context['active_roster_snapshot_week']}"
        source_as_of = benchmark_context.get("source_as_of") or "timestamp unavailable"
        contributor_outcome = benchmark_context.get("contributor_outcome") or {}
        performance_kind = benchmark_context.get("elite_kind")
        if performance_kind == "active_contributor":
            performance_evidence = (
                f"established active-contributor cohort n={benchmark_context.get('elite_rows', 0)}"
            )
        else:
            performance_evidence = (
                f"active top-64 fallback cohort n={benchmark_context.get('elite_rows', 0)}"
            )
        lines.extend(
            [
                f"Benchmark cohort: {benchmark_context.get('population')}",
                (
                    f"Benchmark evidence: {benchmark_context.get('rows', 0)}{denominator} active drafted players "
                    f"({_percent(benchmark_context.get('coverage'))} coverage); "
                    f"{performance_evidence}"
                ),
                f"Benchmark source: {benchmark_context.get('source')} | {snapshot} | source retrieved {source_as_of}",
                "Eligible roster statuses: "
                + ", ".join(benchmark_context.get("included_statuses") or [])
                + "; conflicted or ambiguous identities are excluded",
                "",
            ]
        )
        if performance_kind == "active_contributor":
            lines.extend(
                [
                    "Performance comparison: "
                    + str(
                        contributor_outcome.get("outcome_definition")
                        or benchmark_context.get("elite_reference")
                    ),
                    (
                        "NFL outcome labels: "
                        f"known n={contributor_outcome.get('known_outcomes', '—')}, "
                        f"contributors n={contributor_outcome.get('contributors', '—')}, "
                        f"right-censored n={contributor_outcome.get('right_censored_outcomes', '—')} "
                        f"(excluded), incomplete-source n={contributor_outcome.get('incomplete_source_outcomes', '—')} "
                        f"(excluded); complete through {contributor_outcome.get('completed_nfl_season', '—')}"
                    ),
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "Performance comparison fallback: active players selected in the first 64 picks; "
                    "the fixed three-year active-contributor cohort is unavailable for this comparison.",
                    "",
                ]
            )
    prediction = result.draft_prediction
    if prediction.available:
        interval = ""
        if prediction.interval_80:
            interval = f" (80% draft-year-bootstrap interval {_percent(prediction.interval_80[0])}–{_percent(prediction.interval_80[1])})"
        if prediction.probability is not None:
            lines.append(f"Next-draft likelihood: {_percent(prediction.probability)}{interval}")
        else:
            lines.append("Next-draft likelihood: unavailable under the stated historical risk-set contract")
        conditional_interval = interval if prediction.probability is None else ""
        if prediction.model_stage == "college_precombine":
            lines.extend(
                [
                    "Model stage: college pre-combine",
                    f"Probability scope: {prediction.probability_scope}",
                    f"Projection: {prediction.label}",
                    f"Model population: {prediction.population}",
                ]
            )
        else:
            lines.extend(
                [
                    f"Model-conditional draft probability: {_percent(prediction.conditional_probability)}{conditional_interval}",
                    f"Conditional scope: {prediction.conditional_scope}",
                    f"Draft entry probability: {_percent(prediction.entry_probability)}",
                    f"Probability scope: {prediction.probability_scope}",
                    f"Conditional projection: {prediction.label}",
                    f"Model population: {prediction.population}",
                ]
            )
    else:
        lines.extend(
            [
                f"Next-draft likelihood: unavailable — {prediction.label}",
                "Model-conditional draft probability: unavailable",
                f"Conditional scope: {prediction.conditional_scope}",
                f"Draft entry probability: {_percent(prediction.entry_probability)}",
                f"Model population: {prediction.population}",
            ]
        )
    lines.extend(
        [
            f"Comprehensive scouting profile: {_number(result.profile_score)}/100 — {result.profile_tier}",
            f"Comprehensive profile coverage: {_percent(result.evidence_coverage)}",
        ]
    )
    if result.projected_pick_range:
        low, middle, high = result.projected_pick_range
        lines.append(f"Full-history comparable pick band: {low:.0f}–{high:.0f} (median {middle:.0f}; conditional on being drafted)")

    features = tuple(prediction.features_used)
    observed_features = tuple(feature for feature in features if _feature_observed(player, feature))
    missing_features = tuple(feature for feature in features if feature not in observed_features)
    lines.extend(
        [
            "",
            "MODEL EVIDENCE",
            f"Features ({len(features)}): {', '.join(features) if features else 'none'}",
            (
                f"Feature coverage: {len(observed_features)}/{len(features)} "
                f"({_percent(prediction.feature_coverage)}); observed: "
                f"{', '.join(observed_features) if observed_features else 'none'}"
            ),
            f"Missing model features: {', '.join(missing_features) if missing_features else 'none'}",
        ]
    )
    if prediction.out_of_distribution_score is not None:
        lines.append(f"Out-of-distribution score: {_number(prediction.out_of_distribution_score, 3)} (warning threshold >3.000)")
    validation = prediction.validation
    if validation is None:
        lines.append("Quality gate: unavailable — no validation result")
    else:
        validation_scheme = str(
            getattr(validation, "validation_scheme", "unspecified") or "unspecified"
        )
        evaluated_years = tuple(getattr(validation, "evaluated_years", ()) or ())
        metric_label = (
            "Past-only forward metrics"
            if "expanding-window" in validation_scheme
            else "Held-out metrics"
        )
        decision_text = (
            "binary draft threshold: not published for the full-roster college probability"
            if prediction.model_stage == "college_precombine"
            else (
                f"conditional threshold={_number(validation.threshold, 3)}, "
                f"F1={_number(validation.threshold_f1, 3)}"
            )
        )
        lines.extend(
            [
                f"Quality gate: {'PASS' if validation.passes_quality_gate else 'FAIL'} — {validation.quality_message}",
                (
                    f"Validation design: {validation_scheme}; evaluated years="
                    f"{_years(evaluated_years) if evaluated_years else _years(validation.years)}"
                ),
                (
                    f"{metric_label}: "
                    f"n={validation.rows}, positives={validation.positives}, years={_years(validation.years)}, "
                    f"prevalence={_percent(validation.prevalence, 1)}, "
                    f"Brier={_number(validation.brier, 3)} vs baseline={_number(validation.baseline_brier, 3)}, "
                    f"log loss={_number(validation.log_loss, 3)}, ROC-AUC={_number(validation.roc_auc, 3)}, "
                    f"AP={_number(validation.average_precision, 3)}, "
                    f"ECE={_number(getattr(validation, 'expected_calibration_error', None), 3)}, "
                    f"{decision_text}"
                ),
            ]
        )
        per_year = tuple(getattr(validation, "per_year", ()) or ())
        if per_year:
            lines.extend(
                [
                    "Forward metrics by evaluated draft year:",
                    _table(
                        ["Year", "Train years", "N", "Pos", "Brier", "Base", "ROC-AUC", "AP", "Cal n"],
                        (
                            (
                                row.get("year", "—"),
                                _compact_years(row.get("train_years") or ()),
                                row.get("rows", "—"),
                                row.get("positives", "—"),
                                _number(_float_or_none(row.get("brier")), 3),
                                _number(_float_or_none(row.get("baseline_brier")), 3),
                                _number(_float_or_none(row.get("roc_auc")), 3),
                                _number(_float_or_none(row.get("average_precision")), 3),
                                row.get("calibrator_rows", "—"),
                            )
                            for row in per_year
                        ),
                    ),
                ]
            )

        board_metrics = tuple(getattr(validation, "board_metrics", ()) or ())
        if board_metrics:
            lines.extend(
                [
                    "Draft-board ranking checks:",
                    (
                        "Scope: "
                        + str(getattr(validation, "board_scope", "within-position held-out ranking"))
                        + " These are not cross-position overall-board cutoffs."
                    ),
                    _table(
                        ["Top K", "Selected", "Hits", "Precision", "Recall", "NDCG", "R1-3 recall", "Round cov."],
                        (
                            (
                                row.get("cutoff", "—"),
                                row.get("selected_rows", "—"),
                                row.get("drafted_hits", "—"),
                                _percent(_float_or_none(row.get("precision")), 1),
                                _percent(_float_or_none(row.get("recall")), 1),
                                _number(_float_or_none(row.get("mean_binary_ndcg")), 3),
                                _percent(_float_or_none(row.get("early_round_recall")), 1),
                                _percent(
                                    _float_or_none(row.get("draft_round_outcome_coverage")),
                                    1,
                                ),
                            )
                            for row in board_metrics
                        ),
                    ),
                    (
                        "Round 1-3 recall is withheld unless draft-round outcomes are complete "
                        "for every drafted player in the evaluated rows."
                    ),
                ]
            )

        simple_baselines = tuple(getattr(validation, "simple_baselines", ()) or ())
        show_simple_baselines = bool(simple_baselines)
        if show_simple_baselines:
            lines.extend(
                [
                    "Simple past-only baselines:",
                    (
                        "Each logistic baseline is refit on strictly earlier draft classes. "
                        "Direct comparisons require 'Same set' to be yes."
                    ),
                    _table(
                        ["Baseline", "Features", "N", "Same set", "Brier", "ROC-AUC", "AP", "ECE", "P@10"],
                        (
                            (
                                row.get("label", row.get("name", "—")),
                                len(tuple(row.get("features_used") or ())),
                                row.get("rows", 0),
                                "yes" if row.get("evaluation_set_matches_primary") else "no",
                                _number(_float_or_none(row.get("brier")), 3),
                                _number(_float_or_none(row.get("roc_auc")), 3),
                                _number(_float_or_none(row.get("average_precision")), 3),
                                _number(
                                    _float_or_none(row.get("expected_calibration_error")),
                                    3,
                                ),
                                _percent(_baseline_precision_at(row, 10), 1),
                            )
                            for row in simple_baselines
                        ),
                    ),
                ]
            )
            for baseline in simple_baselines:
                reason = str(baseline.get("unavailable_reason") or "").strip()
                if reason:
                    lines.append(f"{baseline.get('label', baseline.get('name', 'Baseline'))}: {reason}")

    combine = result.combine_prediction
    if combine is not None and combine is not prediction:
        lines.extend(["", "COMBINE-STAGE CROSS-CHECK"])
        if combine.available:
            lines.append(
                f"Conditional combine-population probability: {_percent(combine.conditional_probability)}"
            )
        else:
            lines.append(f"Unavailable: {combine.label}")
            for warning in dict.fromkeys(combine.warnings):
                lines.append(f"Reason: {warning}")
        lines.extend(
            [
                f"Scope: {combine.conditional_scope}",
                f"Feature coverage: {_percent(combine.feature_coverage)}",
                (
                    f"Quality gate: {'PASS' if combine.validation and combine.validation.passes_quality_gate else 'FAIL'}"
                    if combine.validation
                    else "Quality gate: unavailable"
                ),
                "This cross-check is conditional on reaching the combine population and is not a second next-draft probability.",
            ]
        )
        if combine.validation is not None:
            cross_validation = combine.validation
            cross_scheme = str(
                getattr(cross_validation, "validation_scheme", "unspecified")
                or "unspecified"
            )
            cross_years = tuple(
                getattr(cross_validation, "evaluated_years", ()) or ()
            )
            lines.append(
                "Cross-check validation: "
                f"{cross_scheme}; evaluated years={_years(cross_years)}; "
                f"n={cross_validation.rows}, positives={cross_validation.positives}, "
                f"Brier={_number(cross_validation.brier, 3)} vs "
                f"baseline={_number(cross_validation.baseline_brier, 3)}"
            )
            cross_per_year = tuple(
                getattr(cross_validation, "per_year", ()) or ()
            )
            if cross_per_year:
                lines.extend(
                    [
                        "Cross-check forward metrics by evaluated draft year:",
                        _table(
                            ["Year", "Train years", "N", "Pos", "Brier", "Base", "ROC-AUC", "AP"],
                            (
                                (
                                    row.get("year", "—"),
                                    _compact_years(row.get("train_years") or ()),
                                    row.get("rows", "—"),
                                    row.get("positives", "—"),
                                    _number(_float_or_none(row.get("brier")), 3),
                                    _number(_float_or_none(row.get("baseline_brier")), 3),
                                    _number(_float_or_none(row.get("roc_auc")), 3),
                                    _number(_float_or_none(row.get("average_precision")), 3),
                                )
                                for row in cross_per_year
                            ),
                        ),
                    ]
                )

    category_rows = []
    for category in result.categories:
        category_rows.append(
            (
                category.category.title(),
                _number(category.score),
                _percent(category.coverage),
                f"{category.observed_metrics}/{category.expected_metrics}",
                category.reference,
            )
        )
    lines.extend(["", "CATEGORY SCORES", _table(["Category", "Score", "Coverage", "Inputs", "Reference"], category_rows)])

    benchmark_rows = []
    for item in result.benchmarks:
        value = _number(item.value, 2) + (f" {item.unit}" if item.unit else "")
        benchmark_rows.append(
            (
                item.label,
                value,
                _number(item.drafted_mean, 2),
                _number(item.elite_mean, 2),
                _number(item.drafted_percentile, 0),
                _number(item.favorable_score, 0),
                f"{item.drafted_n}/{item.elite_n}",
                item.reference,
                item.elite_reference,
            )
        )
    if benchmark_rows:
        active_headers = bool(result.benchmark_context.get("explicit_active_cohort"))
        elite_kinds = {item.elite_kind for item in result.benchmarks}
        if active_headers and elite_kinds == {"active_contributor"}:
            elite_header = "Established active contributor avg"
        elif active_headers and elite_kinds == {"top64_fallback"}:
            elite_header = "Elite active top-64 avg"
        elif active_headers:
            elite_header = "Performance cohort avg"
        else:
            elite_header = "Elite avg"
        lines.extend(
            [
                "",
                "POSITION BENCHMARKS",
                _table(
                    [
                        "Metric",
                        "Player",
                        "Active drafted avg" if active_headers else "Drafted avg",
                        elite_header,
                        "Dist pct",
                        "Fit pct",
                        "N avg/elite",
                        "Average reference",
                        "Elite/performance cohort",
                    ],
                    benchmark_rows,
                ),
            ]
        )

    benchmarked = {item.metric for item in result.benchmarks}
    unbenchmarked = []
    for spec in all_specs(normalize_position(player.get("position"))):
        value = parse_number(player.get(spec.key))
        if value is None or spec.key in benchmarked:
            continue
        unbenchmarked.append((spec.label, spec.category.title(), _number(value, 2) + (f" {spec.unit}" if spec.unit else ""), "No equivalent historical field"))
    if unbenchmarked:
        lines.extend(["", "OTHER PLAYER INPUTS", _table(["Metric", "Category", "Value", "Benchmark"], unbenchmarked)])

    if result.overall_comps or result.physical_comps:
        lines.extend(["", "CURRENT-ACTIVE COMPARABLE DRAFTED PLAYERS"])
        comps = result.overall_comps or result.physical_comps
        lines.append(
            _table(
                ["Player", "School", "Year", "Pick", "Similarity", "Overlap", "Reference"],
                (
                    (
                        comp.name,
                        comp.school,
                        comp.draft_year or "—",
                        comp.draft_pick or "—",
                        f"{comp.similarity:.0f}",
                        _percent(comp.feature_overlap),
                        comp.reference,
                    )
                    for comp in comps
                ),
            )
        )

    historical_elite_comps = tuple(
        getattr(result, "historical_elite_comps", ()) or ()
    )
    lines.extend(["", "HISTORICAL CAREER-ELITE NFL COMPARABLES"])
    elite_scope = str(
        result.benchmark_context.get("historical_career_elite_scope")
        or "provided career-elite history"
    )
    lines.append(f"Coverage: {elite_scope}.")
    coverage_note = str(
        result.benchmark_context.get(
            "historical_career_elite_coverage_note"
        )
        or ""
    ).strip()
    if coverage_note:
        lines.append(f"Coverage boundary: {coverage_note}")
    if historical_elite_comps:
        lines.append(
            _table(
                ["Player", "Year", "Pick", "Similarity", "Compared", "Measurement source", "All-Pro", "Pro Bowls", "HOF"],
                (
                    (
                        comp.name,
                        comp.draft_year or "—",
                        comp.draft_pick or "—",
                        f"{comp.similarity:.0f}",
                        comp.features_compared,
                        _measurement_source_label(
                            getattr(comp, "measurement_source", "")
                        )
                        or "—",
                        getattr(comp, "nfl_all_pro_selections", None) or "—",
                        getattr(comp, "nfl_pro_bowls", None) or "—",
                        "yes" if getattr(comp, "nfl_hof", False) else "no",
                    )
                    for comp in historical_elite_comps
                ),
            )
        )
        lines.append(
            "Career elite means Hall of Fame, at least one first-team All-Pro, or at least three Pro Bowls. "
            "This outcome-defined catalog is comparison-only and never enters model training."
        )
    elif "unavailable" in elite_scope.lower():
        lines.append("The career-elite catalog is unavailable; no historical comp was attempted.")
    else:
        lines.append(
            "No career-elite player has enough overlapping verified or biographical measurements."
        )

    if result.pick_comps:
        lines.extend(["", "RECENT DRAFTED COMPARABLES SUPPORTING PICK BAND"])
        lines.append(
            _table(
                ["Player", "School", "Year", "Pick", "Similarity", "Overlap", "Compared"],
                (
                    (
                        comp.name,
                        comp.school,
                        comp.draft_year or "—",
                        comp.draft_pick or "—",
                        f"{comp.similarity:.0f}",
                        _percent(comp.feature_overlap),
                        comp.features_compared,
                    )
                    for comp in result.pick_comps
                ),
            )
        )

    if result.team_fits:
        lines.extend(["", "POSSIBLE NFL TEAM FITS"])
        lines.append(
            _table(
                [
                    "Team",
                    "Fit",
                    "Evidence",
                    "Confidence",
                    "Need",
                    "Starter",
                    "Contract",
                    "Room",
                    "Recent picks",
                    "Scheme",
                    "Access",
                    "Coach",
                    "As of",
                    "Why",
                ],
                (
                    (
                        fit.team,
                        _number(fit.score, 0),
                        _percent(fit.evidence_coverage),
                        f"{fit.confidence_label} ({_percent(fit.confidence)})",
                        _number(fit.need_score, 0),
                        _number(fit.starter_quality_need_score, 0),
                        _number(fit.contract_need_score, 0),
                        _team_room(fit.room_count, fit.league_room_median),
                        _recent_team_picks(
                            fit.recent_position_picks,
                            fit.best_recent_position_pick,
                            fit.recent_draft_window,
                        ),
                        _number(fit.scheme_score, 0),
                        _number(fit.draft_access_score, 0),
                        _number(fit.coaching_stability_score, 0),
                        fit.profile_as_of_date or "—",
                        "; ".join(fit.reasons[:5]) or fit.scheme or "supported inputs only",
                    )
                    for fit in result.team_fits
                ),
            )
        )
        automatic_fits = [
            fit for fit in result.team_fits if "nflverse" in fit.profile_source.lower()
        ]
        if automatic_fits:
            windows = sorted(
                {
                    fit.recent_draft_window
                    for fit in automatic_fits
                    if fit.recent_draft_window and fit.recent_draft_window != "unavailable"
                }
            )
            window_text = f" ({', '.join(windows)})" if windows else ""
            supplemental = {
                "starter quality": any(
                    fit.starter_quality_need_score is not None for fit in automatic_fits
                ),
                "contracts": any(fit.contract_need_score is not None for fit in automatic_fits),
                "scheme": any(fit.scheme_score is not None for fit in automatic_fits),
                "coaching stability": any(
                    fit.coaching_stability_score is not None for fit in automatic_fits
                ),
                "future draft access": any(
                    fit.draft_access_score is not None for fit in automatic_fits
                ),
            }
            supported = [label for label, present in supplemental.items() if present]
            missing = [label for label, present in supplemental.items() if not present]
            evidence_text = (
                "Automatic evidence: current nflverse listed room size and age/experience, "
                f"plus original-team recent draft investment{window_text}."
            )
            if supported:
                evidence_text += " Dated supplemental evidence shown: " + ", ".join(supported) + "."
            if missing:
                evidence_text += " Unsupported components omitted: " + ", ".join(missing) + "."
            lines.append(evidence_text)
        lines.append("Team-fit scores are evidence-weighted compatibility rankings, not predicted selections.")

    warnings = list(dict.fromkeys([*result.warnings, *prediction.warnings]))
    if warnings:
        lines.extend(["", "LIMITS / WARNINGS"])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines) + "\n"


def render_board(rows: Iterable[Mapping[str, Any]]) -> str:
    materialized = list(rows)
    return _table(
        ["Rank", "Move", "Player", "Pos", "School", "Next draft", "Model P", "Entry P", "Profile", "Profile Δ", "Prob Δ", "Coverage"],
        (
            (
                row.get("rank", "—"),
                _movement(row.get("rank_delta")),
                row.get("name", ""),
                row.get("position", ""),
                row.get("school", ""),
                _percent(_float_or_none(row.get("draft_probability"))),
                _percent(_first_float(row, "conditional_probability", "draft_conditional_probability")),
                _percent(_first_float(row, "entry_probability", "draft_entry_probability")),
                _number(_float_or_none(row.get("profile_score"))),
                _signed(_float_or_none(row.get("profile_delta")), digits=1),
                _signed_percent(_float_or_none(row.get("draft_probability_delta"))),
                _percent(_float_or_none(row.get("evidence_coverage"))),
            )
            for row in materialized
        ),
    ) + "\n"


def render_board_summary(rows: Iterable[Mapping[str, Any]]) -> str:
    materialized = list(rows)
    lines = [
        "DRAFTSCOPE DRAFT BOARD",
        _table(
            ["Rank", "Move", "Player", "Pos", "School", "Draft chance", "Weekly change"],
            (
                (
                    row.get("rank", "—"),
                    _movement(row.get("rank_delta")),
                    row.get("name", ""),
                    row.get("position", ""),
                    row.get("school", ""),
                    _percent(_float_or_none(row.get("draft_probability"))),
                    _signed_percent(_float_or_none(row.get("draft_probability_delta"))),
                )
                for row in materialized
            ),
        ),
        "Move is the change in board position; weekly change is the draft-chance change in percentage points.",
    ]
    return "\n".join(lines) + "\n"


def _feature_observed(player: Mapping[str, Any], feature: str) -> bool:
    if parse_number(player.get(feature)) is not None:
        return True
    if feature == "class_year_numeric":
        return player.get("class_year") not in (None, "")
    if feature == "context_school_draft_rate":
        return player.get("school") not in (None, "")
    if feature == "context_conference_draft_rate":
        return player.get("conference") not in (None, "")
    if feature == "bmi":
        height = parse_number(player.get("height_in"))
        return bool(height and parse_number(player.get("weight_lb")) is not None)
    if feature == "has_recorded_stats":
        return any(
            key.startswith("prod_") and parse_number(value) is not None
            for key, value in player.items()
        )
    return False


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _plural(value: float) -> str:
    return "" if abs(int(value)) == 1 else "s"


def _signed_percent_words(value: float) -> str:
    points = abs(100.0 * value)
    if points < 0.05:
        return "unchanged"
    direction = "up" if value > 0 else "down"
    return f"{direction} {points:.1f} percentage points"


def _metric_value(value: object, unit: object = "") -> str:
    parsed = _float_or_none(value)
    if parsed is None:
        return "—"
    if abs(parsed) >= 100:
        rendered = f"{parsed:,.0f}"
    elif float(parsed).is_integer():
        rendered = f"{parsed:.0f}"
    else:
        rendered = f"{parsed:.2f}".rstrip("0").rstrip(".")
    suffix = str(unit or "").strip()
    return f"{rendered} {suffix}" if suffix else rendered


def _benchmark_summary(item: Mapping[str, Any]) -> str:
    label = str(item.get("label") or item.get("metric") or "Metric")
    unit = item.get("unit") or ""
    parts = [
        f"player {_metric_value(item.get('value'), unit)}",
        f"active drafted avg {_metric_value(item.get('drafted_mean'), unit)}",
    ]
    elite_mean = _float_or_none(item.get("elite_mean"))
    if elite_mean is not None:
        parts.append(f"elite avg {_metric_value(elite_mean, unit)}")
    fit = _float_or_none(item.get("favorable_score"))
    if fit is not None:
        parts.append(f"position fit {fit:.0f}/100")
    return f"{label}: " + " | ".join(parts)


def _comparable_summary(
    comp: Mapping[str, Any],
    *,
    show_accolades: bool = False,
) -> str:
    detail: list[str] = []
    similarity = _float_or_none(comp.get("similarity"))
    if similarity is not None:
        detail.append(f"{similarity:.0f}% similar")
    pick = _float_or_none(comp.get("draft_pick"))
    year = _float_or_none(comp.get("draft_year"))
    if pick is not None:
        drafted = f"drafted #{round(pick)}"
        if year is not None:
            drafted += f" in {round(year)}"
        detail.append(drafted)
    compared = _float_or_none(comp.get("features_compared"))
    if compared is not None:
        detail.append(f"{round(compared)} shared measurements/metrics")
    if show_accolades:
        accolades: list[str] = []
        if bool(comp.get("nfl_hof")):
            accolades.append("Hall of Fame")
        all_pro = _float_or_none(comp.get("nfl_all_pro_selections"))
        pro_bowls = _float_or_none(comp.get("nfl_pro_bowls"))
        if all_pro:
            accolades.append(f"{round(all_pro)}x first-team All-Pro")
        if pro_bowls:
            accolades.append(f"{round(pro_bowls)}x Pro Bowl")
        if accolades:
            detail.append(", ".join(accolades))
        measurement_source = _measurement_source_label(
            comp.get("measurement_source")
        )
        if measurement_source:
            detail.append(f"measurements: {measurement_source}")
    return f"{comp.get('name') or 'Unknown'}" + (f" — {', '.join(detail)}" if detail else "")


def _measurement_source_label(value: object) -> str:
    source = str(value or "").strip().casefold()
    labels = {
        "nfl_combine": "NFL Combine",
        "pro_day_nonstandard": "2021 pro-day protocol",
        "nfl_combine_with_biographical_fallback": (
            "NFL Combine plus biographical size fallback"
        ),
        "pro_day_nonstandard_with_biographical_fallback": (
            "2021 pro-day protocol plus biographical size fallback"
        ),
        "nflverse_player_registry": "nflverse player biography",
        "nflverse_player_registry_with_wikidata_fallback": (
            "nflverse player biography plus Wikidata size fallback"
        ),
        "wikidata_biographical_measurement": "Wikidata biography",
    }
    return labels.get(source, source.replace("_", " ") if source != "unavailable" else "")


def _window(window: tuple[int, int] | None) -> str:
    return f"{window[0]}–{window[1]}" if window else "unavailable"


def _years(years: Iterable[int]) -> str:
    values = tuple(years)
    return ",".join(str(value) for value in values) if values else "unavailable"


def _compact_years(years: Iterable[int]) -> str:
    values = tuple(int(value) for value in years)
    if not values:
        return "—"
    if len(values) == 1:
        return str(values[0])
    return f"{values[0]}–{values[-1]}"


def _week(value: Any) -> str:
    parsed = _float_or_none(value)
    return f" (Week {int(parsed)})" if parsed is not None else ""


def _movement(value: Any) -> str:
    parsed = _float_or_none(value)
    if parsed is None or parsed == 0:
        return "—"
    return f"+{int(parsed)}" if parsed > 0 else str(int(parsed))


def _looks_like_available_projection(value: str) -> bool:
    normalized = value.strip().casefold()
    return any(
        phrase in normalized
        for phrase in (
            "likely draft pick",
            "draftable range",
            "borderline draft range",
            "below the historical draft line",
        )
    )


def _baseline_precision_at(row: Mapping[str, Any], cutoff: int) -> float | None:
    for metric in row.get("board_metrics") or ():
        if isinstance(metric, Mapping) and _float_or_none(metric.get("cutoff")) == cutoff:
            return _float_or_none(metric.get("precision"))
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _first_float(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float_or_none(row.get(key))
        if value is not None:
            return value
    return None


def _team_room(count: int | None, league_median: float | None) -> str:
    if count is None:
        return "—"
    if league_median is None:
        return str(count)
    return f"{count} / med {league_median:g}"


def _recent_team_picks(count: int | None, best_pick: int | None, window: str) -> str:
    if count is None:
        return "—"
    detail = f"{count}"
    if best_pick is not None:
        detail += f" (best #{best_pick})"
    if window and window != "unavailable":
        detail += f" {window}"
    return detail


def _signed(value: float | None, *, digits: int = 1) -> str:
    if value is None or abs(value) < 10 ** (-(digits + 1)):
        return "—"
    return f"{value:+.{digits}f}"


def _signed_percent(value: float | None) -> str:
    if value is None or abs(value) < 0.0005:
        return "—"
    return f"{100.0 * value:+.1f}pp"
