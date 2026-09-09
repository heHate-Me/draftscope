#!/usr/bin/env python3
"""
Reddit-friendly visualization generator for DraftScope data.

Creates static PNG visualizations showing:
1. Team fit analysis (need, scheme, timeline, coaching stability)
2. Historical draft trends by position (draft rates over time)
3. Position-based draft probability distributions

Usage:
    python3 tools/reddit_visualizations.py <data_dir>
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import matplotlib
    matplotlib.use("Agg")  # Non-interactive backend for server/batch use
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    import numpy as np
except ImportError:
    print("Error: matplotlib and numpy are required for visualizations.")
    print("Install with: pip install matplotlib numpy")
    sys.exit(1)


def load_csv_data(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Load CSV data from file."""
    data = []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            data = list(reader)
    except FileNotFoundError:
        print(f"Warning: File not found: {path}")
    return data


def parse_number(value: str | None) -> float | None:
    """Parse numeric value from string."""
    if not value or value.strip().lower() in {"", "na", "n/a", "nan", "none", "null", "unknown", "-"}:
        return None
    try:
        text = str(value).strip().lower().replace("%", "").replace(",", "")
        return float(text)
    except ValueError:
        return None


def create_team_fit_heatmap(
    team_data: list[dict[str, Any]],
    output_path: str | os.PathLike[str],
) -> None:
    """
    Create a heatmap visualization of team fit metrics by position and team.
    
    Shows: need_score, timeline_score, coaching_stability_score
    """
    if not team_data:
        print("No team profile data available for heatmap.")
        return

    # Extract metrics
    teams_dict: dict[str, dict[str, float]] = {}
    
    for row in team_data:
        team = str(row.get("team", "")).strip()
        position = str(row.get("position", "")).strip()
        if not team or not position:
            continue
        
        if team not in teams_dict:
            teams_dict[team] = {}
        
        # Collect metrics for this team-position combo
        need = parse_number(row.get("need_score"))
        timeline = parse_number(row.get("timeline_score"))
        coaching = parse_number(row.get("coaching_stability_score"))
        
        # Calculate composite fit score (average of available metrics, 0-100)
        metrics = [v for v in [need, timeline, coaching] if v is not None]
        if metrics:
            composite = sum(metrics) / len(metrics)
            teams_dict[team][position] = min(100, max(0, composite))

    if not teams_dict:
        print("No team fit data found.")
        return

    # Prepare data for heatmap
    all_positions = sorted(set(
        pos for team_metrics in teams_dict.values() for pos in team_metrics.keys()
    ))
    sorted_teams = sorted(teams_dict.keys())
    
    # Create matrix
    matrix = np.zeros((len(sorted_teams), len(all_positions)))
    for i, team in enumerate(sorted_teams):
        for j, pos in enumerate(all_positions):
            matrix[i, j] = teams_dict[team].get(pos, 0)

    # Create figure
    fig, ax = plt.subplots(figsize=(14, 8), dpi=100)
    
    # Plot heatmap
    im = ax.imshow(matrix, cmap="RdYlGn", aspect="auto", vmin=0, vmax=100)
    
    # Set ticks and labels
    ax.set_xticks(np.arange(len(all_positions)))
    ax.set_yticks(np.arange(len(sorted_teams)))
    ax.set_xticklabels(all_positions, rotation=45, ha="right", fontsize=9)
    ax.set_yticklabels(sorted_teams, fontsize=9)
    
    # Add colorbar
    cbar = plt.colorbar(im, ax=ax, label="Team Fit Score (0-100)")
    
    # Add text annotations
    for i in range(len(sorted_teams)):
        for j in range(len(all_positions)):
            value = matrix[i, j]
            if value > 0:
                text_color = "white" if value < 50 else "black"
                ax.text(j, i, f"{value:.0f}", ha="center", va="center",
                       color=text_color, fontsize=8, fontweight="bold")
    
    ax.set_title("Team Fit Analysis by Position\n(Higher score = Better fit)", 
                fontsize=14, fontweight="bold", pad=20)
    ax.set_xlabel("Position", fontsize=11, fontweight="bold")
    ax.set_ylabel("Team", fontsize=11, fontweight="bold")
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=100, bbox_inches="tight")
    print(f"✓ Created team fit heatmap: {output_path}")
    plt.close()


def create_draft_trends_chart(
    historical_data: list[dict[str, Any]],
    output_path: str | os.PathLike[str],
) -> None:
    """
    Create a line chart showing historical draft rates by position over time.
    
    Shows how often each position gets drafted in recent years.
    """
    if not historical_data:
        print("No historical data available for trends chart.")
        return

    # Aggregate draft data by year and position
    draft_by_year_pos: dict[tuple[int, str], dict[str, int | float]] = {}
    
    for row in historical_data:
        draft_year = parse_number(row.get("draft_year"))
        position = str(row.get("position", "")).strip()
        drafted = str(row.get("drafted", "")).strip().lower() in {"1", "true", "yes", "drafted"}
        
        if draft_year is None or not position:
            continue
        
        year = int(draft_year)
        key = (year, position)
        
        if key not in draft_by_year_pos:
            draft_by_year_pos[key] = {"drafted": 0, "total": 0}
        
        draft_by_year_pos[key]["total"] += 1
        if drafted:
            draft_by_year_pos[key]["drafted"] += 1

    if not draft_by_year_pos:
        print("No historical draft data found.")
        return

    # Calculate draft rates by position
    positions = sorted(set(pos for _, pos in draft_by_year_pos.keys()))
    years = sorted(set(year for year, _ in draft_by_year_pos.keys()))
    
    # Create figure
    fig, ax = plt.subplots(figsize=(14, 8), dpi=100)
    
    # Color palette for positions
    colors = plt.cm.tab20(np.linspace(0, 1, len(positions)))
    
    # Plot line for each position
    for idx, position in enumerate(positions):
        rates = []
        plot_years = []
        
        for year in years:
            key = (year, position)
            if key in draft_by_year_pos:
                data = draft_by_year_pos[key]
                rate = (data["drafted"] / data["total"] * 100) if data["total"] > 0 else 0
                rates.append(rate)
                plot_years.append(year)
        
        if rates:
            ax.plot(plot_years, rates, marker="o", label=position, 
                   linewidth=2.5, markersize=6, color=colors[idx])
    
    ax.set_xlabel("Draft Year", fontsize=11, fontweight="bold")
    ax.set_ylabel("Draft Rate (%)", fontsize=11, fontweight="bold")
    ax.set_title("Historical Draft Rates by Position\n(Percentage of eligible players drafted)", 
                fontsize=14, fontweight="bold", pad=20)
    ax.legend(loc="best", ncol=2, fontsize=9)
    ax.grid(True, alpha=0.3, linestyle="--")
    ax.set_ylim([0, 105])
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=100, bbox_inches="tight")
    print(f"✓ Created draft trends chart: {output_path}")
    plt.close()


def create_probability_distribution(
    player_data: list[dict[str, Any]],
    output_path: str | os.PathLike[str],
) -> None:
    """
    Create histograms showing draft probability distributions by position.
    
    Shows the spread of predicted draft probabilities across positions.
    """
    if not player_data:
        print("No player data available for probability distribution.")
        return

    # Group probabilities by position
    probs_by_position: dict[str, list[float]] = {}
    
    for row in player_data:
        position = str(row.get("position", "")).strip()
        prob = parse_number(row.get("draft_entry_probability") or row.get("probability"))
        
        if not position or prob is None:
            continue
        
        if position not in probs_by_position:
            probs_by_position[position] = []
        
        probs_by_position[position].append(prob * 100)  # Convert to percentage

    if not probs_by_position:
        print("No draft probability data found.")
        return

    # Create subplots
    positions = sorted(probs_by_position.keys())
    n_positions = len(positions)
    cols = 3
    rows = (n_positions + cols - 1) // cols
    
    fig, axes = plt.subplots(rows, cols, figsize=(16, 4 * rows), dpi=100)
    if n_positions == 1:
        axes = [axes]
    else:
        axes = axes.flatten()

    for idx, position in enumerate(positions):
        ax = axes[idx]
        probs = probs_by_position[position]
        
        # Create histogram
        n, bins, patches = ax.hist(probs, bins=20, color="#2E86AB", alpha=0.7, edgecolor="black")
        
        # Color bars by probability range
        for i, patch in enumerate(patches):
            if bins[i] < 25:
                patch.set_facecolor("#E63946")  # Red: low probability
            elif bins[i] < 50:
                patch.set_facecolor("#F1FAEE")  # Light: medium-low
            elif bins[i] < 75:
                patch.set_facecolor("#A8DADC")  # Blue: medium-high
            else:
                patch.set_facecolor("#06A77D")  # Green: high probability
        
        # Statistics
        mean_prob = np.mean(probs)
        median_prob = np.median(probs)
        
        ax.axvline(mean_prob, color="red", linestyle="--", linewidth=2, label=f"Mean: {mean_prob:.1f}%")
        ax.axvline(median_prob, color="blue", linestyle=":", linewidth=2, label=f"Median: {median_prob:.1f}%")
        
        ax.set_title(f"{position} (n={len(probs)})", fontsize=11, fontweight="bold")
        ax.set_xlabel("Draft Probability (%)", fontsize=10)
        ax.set_ylabel("Count", fontsize=10)
        ax.set_xlim([0, 100])
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3, axis="y")

    # Hide unused subplots
    for idx in range(n_positions, len(axes)):
        axes[idx].set_visible(False)

    fig.suptitle("Draft Probability Distributions by Position\n(Current prospects)",
                fontsize=14, fontweight="bold", y=0.995)
    plt.tight_layout()
    plt.savefig(output_path, dpi=100, bbox_inches="tight")
    print(f"✓ Created probability distribution chart: {output_path}")
    plt.close()


def create_summary_infographic(
    player_data: list[dict[str, Any]],
    team_data: list[dict[str, Any]],
    historical_data: list[dict[str, Any]],
    output_path: str | os.PathLike[str],
) -> None:
    """
    Create a summary infographic with key statistics.
    """
    fig = plt.figure(figsize=(12, 10), dpi=100)
    ax = fig.add_subplot(111)
    ax.axis("off")

    # Title
    title_text = "DraftScope Summary Statistics"
    fig.text(0.5, 0.95, title_text, ha="center", fontsize=18, fontweight="bold")
    
    timestamp = datetime.now().strftime("%B %d, %Y")
    fig.text(0.5, 0.91, f"Generated: {timestamp}", ha="center", fontsize=10, style="italic", color="gray")

    # Calculate statistics
    y_pos = 0.85
    box_height = 0.08
    box_width = 0.28
    
    # Stats boxes
    stats = []
    
    # Player count by position
    positions = {}
    for row in player_data:
        pos = str(row.get("position", "")).strip()
        if pos:
            positions[pos] = positions.get(pos, 0) + 1
    
    if positions:
        pos_text = ", ".join(f"{p}: {c}" for p, c in sorted(positions.items())[:5])
        stats.append(("Players by Position", f"{pos_text}..."))

    # Average draft probability
    probs = []
    for row in player_data:
        prob = parse_number(row.get("draft_entry_probability") or row.get("probability"))
        if prob is not None:
            probs.append(prob * 100)
    
    if probs:
        stats.append(("Avg Draft Probability", f"{np.mean(probs):.1f}%"))

    # Teams analyzed
    teams = set(str(row.get("team", "")).strip() for row in team_data if row.get("team"))
    if teams:
        stats.append(("Teams Analyzed", str(len(teams))))

    # Historical draft data
    draft_years = set()
    for row in historical_data:
        year = parse_number(row.get("draft_year"))
        if year is not None:
            draft_years.add(int(year))
    
    if draft_years:
        stats.append(("Historical Years", f"{min(draft_years)}-{max(draft_years)}"))

    # Draw stat boxes
    cols = 2
    for idx, (label, value) in enumerate(stats):
        row = idx // cols
        col = idx % cols
        
        x = 0.1 + col * 0.5
        y = y_pos - row * 0.15
        
        # Background box
        rect = Rectangle((x, y - box_height/2), box_width, box_height,
                         facecolor="#E8F4F8", edgecolor="#2E86AB", linewidth=2, transform=fig.transFigure)
        fig.patches.append(rect)
        
        # Label
        fig.text(x + 0.01, y + box_height/4, label, fontsize=10, fontweight="bold", color="#2E86AB")
        # Value
        fig.text(x + 0.01, y - box_height/6, str(value), fontsize=11, fontweight="bold")

    # Data source info
    fig.text(0.5, 0.08, "Data sources: SportsDataverse, nflverse, CollegeFootballData",
            ha="center", fontsize=9, color="gray", style="italic")
    fig.text(0.5, 0.03, "DraftScope: Auditable NCAA-to-NFL prospect benchmarking",
            ha="center", fontsize=10, fontweight="bold")

    plt.savefig(output_path, dpi=100, bbox_inches="tight", facecolor="white")
    print(f"✓ Created summary infographic: {output_path}")
    plt.close()


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate Reddit-friendly visualizations from DraftScope data"
    )
    parser.add_argument(
        "data_dir",
        type=str,
        help="Directory containing players, team profiles, and historical data CSVs",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Output directory for PNG files (default: data_dir/visualizations)",
    )
    parser.add_argument(
        "--players-file",
        type=str,
        default="players_2026.csv",
        help="Players CSV filename",
    )
    parser.add_argument(
        "--teams-file",
        type=str,
        default="team_profiles_2026.csv",
        help="Team profiles CSV filename",
    )
    parser.add_argument(
        "--historical-file",
        type=str,
        default="historical_prospects.csv",
        help="Historical prospects CSV filename",
    )

    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir or data_dir / "visualizations")
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"📊 DraftScope Reddit Visualization Generator")
    print(f"   Data directory: {data_dir}")
    print(f"   Output directory: {output_dir}")
    print()

    # Load data
    print("Loading data files...")
    players = load_csv_data(data_dir / args.players_file)
    teams = load_csv_data(data_dir / args.teams_file)
    historical = load_csv_data(data_dir / args.historical_file)

    print(f"  ✓ Loaded {len(players)} players")
    print(f"  ✓ Loaded {len(teams)} team profiles")
    print(f"  ✓ Loaded {len(historical)} historical records")
    print()

    # Generate visualizations
    print("Generating visualizations...")
    
    create_team_fit_heatmap(teams, output_dir / "team_fit_analysis.png")
    create_draft_trends_chart(historical, output_dir / "draft_trends.png")
    create_probability_distribution(players, output_dir / "draft_probability_distribution.png")
    create_summary_infographic(players, teams, historical, output_dir / "summary_infographic.png")

    print()
    print("✨ All visualizations complete!")
    print(f"   Output files ready in: {output_dir}")


if __name__ == "__main__":
    main()
