"""Utilities for manual scouting evidence: template generation, validation, and report rendering."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .schema import (
    TRAITS,
    FILM_PROVENANCE_FIELDS,
    FILM_GRADE_STATUSES,
    FILM_OPPONENT_MIX_VALUES,
    FILM_CONSENSUS_METHODS,
    MIN_COMPLETE_FILM_GAMES,
    MIN_COMPLETE_FILM_SNAPS,
    normalize_position,
    trait_specs,
)
from .records import (
    _populated,
    parse_film_game_ids,
    parse_film_opponent_mix,
    _portable_notes_path,
    _portable_source,
    parse_film_opponent_mix as _parse_film_opponent_mix,
)
from .records import parse_film_game_ids as _parse_film_game_ids
from .records import film_grade_audit_findings


def load_evidence(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(path)
    return json.loads(p.read_text(encoding="utf-8"))


def evidence_template(position: str | None = None) -> Dict[str, Any]:
    """Return a machine-readable scouting evidence template for the given position.

    If position is None, the template remains generic; otherwise include only the
    position-specific trait keys in the trait_evidence placeholder.
    """
    position_norm = normalize_position(position or "") if position else None
    traits = []
    if position_norm and position_norm in TRAITS:
        for name in TRAITS[position_norm]:
            traits.append({
                "trait_key": f"trait_{name}",
                "grade": None,
                "confidence": None,
                "positive_evidence": [],
                "limiting_evidence": [],
                "game_play_references": [],
                "counter_evidence": [],
            })
    else:
        # Generic placeholder
        traits = [
            {
                "trait_key": None,
                "grade": None,
                "confidence": None,
                "positive_evidence": [],
                "limiting_evidence": [],
                "game_play_references": [],
                "counter_evidence": [],
            }
        ]
    template = {
        "schema_version": "1.0",
        "template_instructions": "Replace nulls and placeholder objects with observed facts; do not fabricate evidence.",
        "player": {"player_id": None, "name": None, "position": position_norm, "school": None, "season": None, "as_of_week": None, "projected_draft_year": None},
        "film_grade_status": None,
        "film_grader": None,
        "film_graded_at": None,
        "film_game_ids": None,
        "film_games_reviewed": None,
        "film_snaps_reviewed": None,
        "film_opponent_mix": None,
        "film_grade_source": None,
        "film_notes_path": None,
        "games_reviewed": [
            {"game_id": None, "opponent": None, "game_date": None, "relevant_snaps_reviewed": None, "complete_game_reviewed": None, "film_source": None, "sample_role": None, "notes": None}
        ],
        "trait_evidence": traits,
        "film_second_grader": None,
        "film_second_grader_agreement": None,
        "film_consensus_method": None,
        "second_grader_notes": None,
    }
    return template


def validate_evidence(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """Return audit-style findings for an evidence JSON payload.

    Uses existing film-grade audit functions where possible and also validates
    structural requirements of the evidence contract.
    """
    findings: List[Dict[str, str]] = []

    # Basic player identity checks
    player = payload.get("player") or {}
    if not player.get("name"):
        findings.append({"severity": "high", "field": "player.name", "message": "Missing player name"})
    if not player.get("position"):
        findings.append({"severity": "high", "field": "player.position", "message": "Missing player position"})
    else:
        try:
            _ = normalize_position(player.get("position"))
        except Exception:
            findings.append({"severity": "high", "field": "player.position", "message": "Invalid position"})

    # Film provenance checks (re-use film_grade_audit_findings by materializing a row-like dict)
    # Create a pseudo-row merging top-level film fields into a flat dict expected by film_grade_audit_findings
    row_like = dict(player)
    for key in FILM_PROVENANCE_FIELDS:
        if key in payload:
            row_like[key] = payload.get(key)
    # Also allow top-level trait entries to be present on a row-like object for auditing
    for item in payload.get("trait_evidence", []):
        key = item.get("trait_key")
        if key:
            row_like[key] = item.get("grade")
    findings.extend(film_grade_audit_findings(row_like, row_number=1))

    # Validate games_reviewed entries
    games = payload.get("games_reviewed") or []
    if not isinstance(games, list):
        findings.append({"severity": "high", "field": "games_reviewed", "message": "games_reviewed must be a list"})
    else:
        for idx, g in enumerate(games, start=1):
            if not g.get("game_id"):
                findings.append({"severity": "high", "field": f"games_reviewed[{idx}].game_id", "message": "Missing game_id"})
            if g.get("relevant_snaps_reviewed") is None:
                findings.append({"severity": "high", "field": f"games_reviewed[{idx}].relevant_snaps_reviewed", "message": "Missing relevant_snaps_reviewed"})
            if g.get("film_source") and not _portable_source(g.get("film_source")):
                findings.append({"severity": "high", "field": f"games_reviewed[{idx}].film_source", "message": "film_source must be a portable URL or short identifier"})

    # Trait evidence structure
    trait_keys_seen = set()
    for idx, trait in enumerate(payload.get("trait_evidence", []), start=1):
        key = trait.get("trait_key")
        if not key:
            findings.append({"severity": "high", "field": f"trait_evidence[{idx}].trait_key", "message": "Missing trait_key"})
            continue
        trait_keys_seen.add(key)
        # Validate trait belongs to position
        pos = normalize_position(player.get("position") or "")
        allowed = {f"trait_{name}" for name in TRAITS.get(pos, ())}
        if allowed and key not in allowed:
            findings.append({"severity": "high", "field": f"trait_evidence[{idx}].trait_key", "message": f"Trait {key} is not valid for position {pos}"})
        grade = trait.get("grade")
        if grade is not None:
            try:
                g = float(grade)
            except Exception:
                findings.append({"severity": "high", "field": f"trait_evidence[{idx}].grade", "message": "Grade must be numeric 0–100 or null"})
                continue
            if g < 0 or g > 100:
                findings.append({"severity": "high", "field": f"trait_evidence[{idx}].grade", "message": "Grade must be between 0 and 100"})
        # Evidence lists
        for listname in ("positive_evidence", "limiting_evidence", "game_play_references", "counter_evidence"):
            items = trait.get(listname) or []
            if not isinstance(items, list):
                findings.append({"severity": "high", "field": f"trait_evidence[{idx}].{listname}", "message": f"{listname} must be a list"})
                continue
            for j, ev in enumerate(items, start=1):
                if not ev.get("game_id"):
                    findings.append({"severity": "high", "field": f"trait_evidence[{idx}].{listname}[{j}].game_id", "message": "Missing game_id in evidence item"})
    # Ensure required film provenance fields present when any trait grades exist
    if trait_keys_seen:
        for key in FILM_PROVENANCE_FIELDS:
            if key not in payload or not _populated(payload.get(key)):
                findings.append({"severity": "high", "field": key, "message": "Required when trait evidence is present"})

    # High-level completeness assessment
    status = (payload.get("film_grade_status") or "").strip().lower()
    snaps = payload.get("film_snaps_reviewed")
    games_count = payload.get("film_games_reviewed")
    if status == "complete":
        if not snaps or snaps < MIN_COMPLETE_FILM_SNAPS:
            findings.append({"severity": "high", "field": "film_snaps_reviewed", "message": f"Complete film grades require >= {MIN_COMPLETE_FILM_SNAPS} snaps"})
        if not games_count or games_count < MIN_COMPLETE_FILM_GAMES:
            findings.append({"severity": "high", "field": "film_games_reviewed", "message": f"Complete film grades require >= {MIN_COMPLETE_FILM_GAMES} games"})
    elif status == "provisional":
        # provisional should have at least one game and one snap
        if not games_count or games_count < 1:
            findings.append({"severity": "high", "field": "film_games_reviewed", "message": "Provisional film grades require at least 1 reviewed game"})
        if not snaps or snaps < 1:
            findings.append({"severity": "high", "field": "film_snaps_reviewed", "message": "Provisional film grades require at least 1 snapshot"})

    return findings


def summarize_evidence(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a compact summary of an evidence payload: counts, completeness, and flags."""
    player = payload.get("player") or {}
    traits = payload.get("trait_evidence") or []
    games = payload.get("games_reviewed") or []
    snaps = payload.get("film_snaps_reviewed") or 0
    status = (payload.get("film_grade_status") or "").lower()
    trait_count = sum(1 for t in traits if t.get("grade") is not None)
    return {
        "player_name": player.get("name"),
        "position": player.get("position"),
        "school": player.get("school"),
        "season": player.get("season"),
        "as_of_week": player.get("as_of_week"),
        "status": status,
        "games_reviewed": len(games),
        "snaps_reviewed": snaps,
        "graded_traits": trait_count,
        "total_traits": len(traits),
    }


def write_template(path: str, position: str | None = None) -> None:
    payload = evidence_template(position)
    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_findings(findings: List[Dict[str, str]], path: str | None = None) -> None:
    text = json.dumps(findings, indent=2) + "\n"
    if path:
        Path(path).write_text(text, encoding="utf-8")
    else:
        print(text)
