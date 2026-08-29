from __future__ import annotations

from datetime import date


def render_config_template(*, season: int | None = None) -> str:
    """Return a complete, credential-free configuration for a new installation."""

    active_season = int(season or date.today().year)
    return f'''[draftscope]
season = {active_season}
players_file = "data/players_{active_season}.csv"
team_profiles_file = "data/team_profiles_{active_season}.csv"
output_dir = "data"
cache_dir = ".draftscope-cache"
college_data_provider = "sportsdataverse"
cfbd_fallback = false
lookback_years = 10
bootstrap = 0
discover_candidates = true
discovery_per_position = 50
preseason_prior_year = true
build_weekly_history = true
strict_model_data = true
historical_raw_cache_dir = ".draftscope-cache/historical_college"
auto_team_needs = true

# The default SportsDataverse and nflverse workflow does not require an API key.
# Set college_data_provider to "cfbd" or cfbd_fallback to true only when you
# have configured a CollegeFootballData credential.
'''
