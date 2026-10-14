from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./wcm.db"

    # Wikimedia's User-Agent policy requires a way to contact the operator.
    wcm_contact: str = "unset@example.org"
    wiki_lang: str = "en"

    # Claude settings. The judge decides whether a source supports a claim, so it
    # runs on the most capable model at high effort; precision matters more than cost.
    wcm_judge_model: str = "claude-opus-5-5"
    wcm_judge_effort: str = "high"
    wcm_judge_max_tokens: int = 32000

    # Sources longer than this are trimmed to the passages most relevant to the
    # claims (see sources.select_passages) instead of being sent whole.
    wcm_max_source_chars: int = 400_000

    # Characters of source text kept around each quoted passage, so reviewers can
    # see context without the full copyrighted text being stored.
    wcm_excerpt_context_chars: int = 1500

    wcm_fetch_timeout: float = 30.0
    wcm_fetch_max_bytes: int = 25_000_000

    # Built web UI. Defaults to web/dist in a source checkout.
    wcm_web_dist: str = str(Path(__file__).resolve().parents[2] / "web" / "dist")

    # name:token pairs, comma-separated.
    wcm_reviewer_tokens: str = ""

    @property
    def user_agent(self) -> str:
        return f"WikiCitationMonitor/0.1 (research tool; {self.wcm_contact})"

    def reviewers(self) -> dict[str, str]:
        """Map of token -> reviewer name."""
        out: dict[str, str] = {}
        for pair in filter(None, (p.strip() for p in self.wcm_reviewer_tokens.split(","))):
            name, _, token = pair.partition(":")
            if name and token:
                out[token] = name
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
