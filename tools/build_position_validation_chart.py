#!/usr/bin/env python3
"""Render the DraftScope position-validation chart for publication.

This publication-only helper reads the canonical values from MODEL_CARD.md so
the graphic cannot silently drift from the documented evaluation. It requires
Pillow, but DraftScope's runtime and modeling code remain standard-library only.
"""

from __future__ import annotations

import argparse
import math
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH = 1600
HEIGHT = 2000
HELD_OUT_DRAFT_YEARS = tuple(range(2019, 2027))

COLORS = {
    "background": "#F7F8FA",
    "paper": "#FFFFFF",
    "ink": "#172033",
    "muted": "#657080",
    "faint": "#98A2B1",
    "grid": "#DCE1E8",
    "baseline": "#596474",
    "blue": "#2F6FED",
    "blue_dark": "#194AA8",
    "blue_light": "#E8F0FF",
    "withheld_fill": "#EEF1F5",
}


@dataclass(frozen=True)
class PositionResult:
    position: str
    status: str
    evaluated: int
    drafted: int
    roc_auc: float
    average_precision: float
    brier: float
    prevalence_brier: float

    @property
    def draft_rate(self) -> float:
        return self.drafted / self.evaluated


def parse_position_results(model_card: Path) -> list[PositionResult]:
    text = model_card.read_text(encoding="utf-8")
    marker = "### Position-level results"
    if marker not in text:
        raise ValueError(f"Missing {marker!r} in {model_card}")

    section = text.split(marker, 1)[1]
    rows: list[PositionResult] = []
    row_pattern = re.compile(r"^\|\s*(.*?)\s*\|$")
    for line in section.splitlines():
        match = row_pattern.match(line)
        if not match:
            if rows and line.startswith("## "):
                break
            continue
        cells = [cell.strip() for cell in match.group(1).split("|")]
        if len(cells) != 8 or cells[0] in {"Position", "---"}:
            continue
        if set(cells[0]) == {"-"}:
            continue
        try:
            rows.append(
                PositionResult(
                    position=cells[0],
                    status=cells[1],
                    evaluated=int(cells[2].replace(",", "")),
                    drafted=int(cells[3].replace(",", "")),
                    roc_auc=float(cells[4]),
                    average_precision=float(cells[5]),
                    brier=float(cells[6]),
                    prevalence_brier=float(cells[7]),
                )
            )
        except ValueError as exc:
            raise ValueError(f"Could not parse model-card row: {line}") from exc

    expected = {"QB", "RB", "WR", "TE", "EDGE", "IDL", "LB", "CB", "S", "K", "P", "LS"}
    observed = {row.position for row in rows}
    if not expected.issubset(observed) or not any("OT / IOL" in row.position for row in rows):
        raise ValueError(f"Incomplete position table: found {sorted(observed)}")
    return rows


def load_font(size: int, *, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    candidates: list[str]
    if mono:
        candidates = [
            "/System/Library/Fonts/SFNSMono.ttf",
            "/System/Library/Fonts/Menlo.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        ]
    elif bold:
        candidates = [
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/HelveticaNeue.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    else:
        candidates = [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/HelveticaNeue.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default(size=size)


def text_width(draw: ImageDraw.ImageDraw, value: str, font: ImageFont.ImageFont) -> float:
    left, _, right, _ = draw.textbbox((0, 0), value, font=font)
    return right - left


def draw_right(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    value: str,
    *,
    font: ImageFont.ImageFont,
    fill: str,
) -> None:
    draw.text((xy[0] - text_width(draw, value, font), xy[1]), value, font=font, fill=fill)


def draw_center(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    value: str,
    *,
    font: ImageFont.ImageFont,
    fill: str,
) -> None:
    draw.text((xy[0] - text_width(draw, value, font) / 2, xy[1]), value, font=font, fill=fill)


def log_x(percent: float, x0: float, x1: float) -> float:
    lower = 1.0
    upper = 64.0
    bounded = min(max(percent, lower), upper)
    return x0 + (math.log2(bounded / lower) / math.log2(upper / lower)) * (x1 - x0)


def render(rows: list[PositionResult], output: Path) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), COLORS["background"])
    draw = ImageDraw.Draw(image)

    font_eyebrow = load_font(24, bold=True)
    font_title = load_font(68, bold=True)
    font_subtitle = load_font(29)
    font_stat = load_font(26, bold=True)
    font_small = load_font(23)
    font_small_bold = load_font(23, bold=True)
    font_axis = load_font(23, mono=True)
    font_position = load_font(35, bold=True)
    font_value_bold = load_font(32, mono=True)
    font_panel = load_font(29, bold=True)
    font_note = load_font(25)
    font_note_bold = load_font(25, bold=True)
    font_footer = load_font(24)

    validated = sorted(
        (row for row in rows if row.status.casefold() == "pass"),
        key=lambda row: row.average_precision,
        reverse=True,
    )
    evaluated_total = sum(row.evaluated for row in rows)
    drafted_total = sum(row.drafted for row in rows)

    # Header
    draw.text((100, 82), "DRAFTSCOPE  /  MODEL VALIDATION", font=font_eyebrow, fill=COLORS["blue_dark"])
    draw.text((100, 132), "Held-out NFL draft prediction", font=font_title, fill=COLORS["ink"])
    draw.text((100, 208), "performance by position", font=font_title, fill=COLORS["ink"])
    draw.text(
        (100, 304),
        f"{len(HELD_OUT_DRAFT_YEARS)} draft classes "
        f"({HELD_OUT_DRAFT_YEARS[0]}–{HELD_OUT_DRAFT_YEARS[-1]}) • every class scored using only earlier seasons",
        font=font_subtitle,
        fill=COLORS["muted"],
    )
    draw.text(
        (100, 348),
        "Blue farther right = actual picks were more concentrated at the top than in a no-skill ranking.",
        font=font_note,
        fill=COLORS["ink"],
    )
    draw.text(
        (100, 397),
        f"{evaluated_total:,} unique held-out player-seasons   •   {drafted_total:,} selections   •   "
        f"{len(rows)} cohorts × {len(HELD_OUT_DRAFT_YEARS)} drafts",
        font=font_stat,
        fill=COLORS["ink"],
    )

    # Legend and chart headers
    legend_y = 468
    draw.polygon(
        [(111, legend_y - 14), (125, legend_y), (111, legend_y + 14), (97, legend_y)],
        fill=COLORS["background"],
        outline=COLORS["baseline"],
        width=5,
    )
    draw.text((145, legend_y - 16), "actual draft rate", font=font_small, fill=COLORS["baseline"])
    draw.ellipse((388, legend_y - 14, 416, legend_y + 14), fill=COLORS["blue"], outline=COLORS["blue_dark"], width=2)
    draw.text((432, legend_y - 16), "average precision", font=font_small, fill=COLORS["ink"])
    draw.text((650, legend_y - 16), "LOG SCALE • GRIDLINES DOUBLE", font=font_small_bold, fill=COLORS["ink"])
    draw_right(draw, (1260, legend_y - 16), "AVG. PRECISION", font=font_small_bold, fill=COLORS["muted"])
    draw_right(draw, (1490, legend_y - 16), "× NO-SKILL", font=font_small_bold, fill=COLORS["muted"])

    chart_x0 = 350
    chart_x1 = 1000
    chart_top = 535
    row_gap = 83
    chart_bottom = chart_top + row_gap * len(validated)
    ticks = [1, 2, 4, 8, 16, 32, 64]
    for tick in ticks:
        x = log_x(tick, chart_x0, chart_x1)
        draw.line((x, chart_top - 8, x, chart_bottom - 22), fill=COLORS["grid"], width=2)
        draw_center(draw, (x, chart_top - 40), f"{tick}%", font=font_axis, fill=COLORS["muted"])

    for index, row in enumerate(validated):
        y = chart_top + index * row_gap + 26
        base_pct = row.draft_rate * 100
        ap_pct = row.average_precision * 100
        x_base = log_x(base_pct, chart_x0, chart_x1)
        x_ap = log_x(ap_pct, chart_x0, chart_x1)
        label = "OT / IOL" if row.position.startswith("OT / IOL") else row.position

        if index:
            draw.line((100, y - 39, 1500, y - 39), fill="#E9ECF1", width=1)
        draw.text((100, y - 25), label, font=font_position, fill=COLORS["ink"])

        draw.line((x_base, y, x_ap, y), fill="#9EB9EE", width=8)
        draw.polygon(
            [(x_base, y - 14), (x_base + 14, y), (x_base, y + 14), (x_base - 14, y)],
            fill=COLORS["background"],
            outline=COLORS["baseline"],
            width=5,
        )
        draw.ellipse((x_ap - 15, y - 15, x_ap + 15, y + 15), fill=COLORS["blue"], outline=COLORS["blue_dark"], width=2)
        draw_center(draw, (x_base, y - 34), f"{base_pct:.2f}%", font=font_axis, fill=COLORS["baseline"])
        draw_right(draw, (1260, y - 18), f"{ap_pct:.1f}%", font=font_value_bold, fill=COLORS["blue_dark"])
        lift = row.average_precision / row.draft_rate
        draw_right(draw, (1490, y - 18), f"{lift:.1f}×", font=font_value_bold, fill=COLORS["ink"])

    # Withheld panel
    panel_top = 1415
    panel_bottom = 1585
    draw.rounded_rectangle((100, panel_top, 1500, panel_bottom), radius=20, fill=COLORS["withheld_fill"])
    draw.text((130, panel_top + 23), "PROBABILITIES WITHHELD", font=font_panel, fill=COLORS["ink"])
    draw.text(
        (130, panel_top + 59),
        "K  •  P  •  LS     Too few drafted outcomes; P and LS also missed the probability-error baseline.",
        font=font_note,
        fill=COLORS["muted"],
    )
    draw.text(
        (130, panel_top + 107),
        "Their comparisons remain available, but DraftScope emits no draft probability.",
        font=font_note_bold,
        fill=COLORS["ink"],
    )

    # Footer and provenance
    footer_y = 1635
    draw.line((100, footer_y, 1500, footer_y), fill=COLORS["grid"], width=2)
    draw.text(
        (100, footer_y + 28),
        "Population: supported-position players in selected FBS roster snapshots—not only declared prospects.",
        font=font_footer,
        fill=COLORS["ink"],
    )
    draw.text(
        (100, footer_y + 63),
        "Average precision is a ranking metric, not a player probability. OT/IOL share one model.",
        font=font_footer,
        fill=COLORS["muted"],
    )
    draw.text(
        (100, footer_y + 105),
        "Sources: SportsDataverse FBS releases + nflverse NFL Draft results",
        font=font_footer,
        fill=COLORS["muted"],
    )
    draw.text(
        (100, footer_y + 162),
        "github.com/heHate-Me/draftscope",
        font=load_font(36, bold=True),
        fill=COLORS["blue_dark"],
    )
    draw.text(
        (100, footer_y + 217),
        "2026 preseason evaluation • research alpha • results are within-position • not a draft guarantee",
        font=font_small,
        fill=COLORS["baseline"],
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-card",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "MODEL_CARD.md",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "docs"
        / "figures"
        / "draftscope_position_validation.png",
    )
    args = parser.parse_args()
    rows = parse_position_results(args.model_card)
    render(rows, args.out)
    print(args.out.resolve())


if __name__ == "__main__":
    main()
