"""
Reputation-based credit calculation.

Maps GitHub profile data to initial credit allocation (100–5000 range).
All data comes from the GitHub GET /user response — no extra API calls needed.
"""

from datetime import datetime, timezone
from decimal import Decimal


def calculate_credits(created_at: str, public_repos: int,
                      followers: int) -> Decimal:
    """
    Calculate initial credits based on GitHub reputation signals.

    Args:
        created_at: ISO 8601 timestamp of GitHub account creation.
        public_repos: Number of public repositories.
        followers: Number of followers.

    Returns:
        Credit amount as Decimal, in range [100, 5000].
    """
    now = datetime.now(timezone.utc)
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    account_age_years = (now - created).days / 365.25

    score = (
        min(account_age_years, 10) * 20       # 0–200 pts for age (10yr cap)
        + min(public_repos, 100) * 1           # 0–100 pts for repos (100 cap)
        + min(followers, 500) * 0.4            # 0–200 pts for followers (500 cap)
    )
    # score range: 0–500, credit range: 100–5000
    credits = 100 + score * 9.8
    return Decimal(str(round(credits)))
