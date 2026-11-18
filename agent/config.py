"""Settings. Everything secret comes from the environment, never from code."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
FAQ_PATH = ROOT / "docs" / "brief" / "faq.md"

# USD per 1 million tokens (input, output), then the source and the date checked.
# An unknown model costs 0 in the summary.
PRICES = {
    # This rate is for prompts up to 100,000 tokens. Ours are a few thousand.
    "claude-haiku-5-5": (
        0.10, 0.50, "https://platform.claude.com/docs/en/about-claude/pricing", "2026-11-17",
    ),
}
USD_TO_EUR = 0.92  # rough rate, only used for the cost line in the summary


@dataclass
class Settings:
    helpdesk_url: str
    helpdesk_key: str
    email: str
    name: str
    provider: str  # which model provider to use. Empty means: no model.
    model: str
    view_model: str  # model for the English back-translation
    llm_key: str
    threshold: float  # below this confidence the agent hands off
    max_tokens: int
    spend_cap_eur: float  # the run stops when the model cost passes this


def load(env_file=ROOT / ".env"):
    """Read the settings. Pass env_file=None to skip the .env file (tests)."""
    if env_file:
        load_dotenv(env_file)
    env = os.environ.get
    model = env("LLM_MODEL", "").strip()
    return Settings(
        helpdesk_url=env("HELPDESK_URL", "").strip().rstrip("/"),
        helpdesk_key=env("HELPDESK_KEY", "").strip(),
        email=env("CANDIDATE_EMAIL", "").strip(),
        name=env("CANDIDATE_NAME", "").strip(),
        provider=env("LLM_PROVIDER", "").strip().lower(),
        model=model,
        view_model=env("LLM_VIEW_MODEL", "").strip() or model,
        llm_key=env("LLM_API_KEY", "").strip(),
        threshold=float(env("CONFIDENCE_THRESHOLD", "0.75")),
        max_tokens=int(env("LLM_MAX_TOKENS", "4000")),
        spend_cap_eur=float(env("SPEND_CAP_EUR", "0.25")),
    )


def cost_eur(model, tokens_in, tokens_out):
    price_in, price_out = PRICES.get(model, (0.0, 0.0))[:2]
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000 * USD_TO_EUR
