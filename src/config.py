from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    ORIGIN: str
    DESTINATION: str
    DEPARTURE_DATE: str  # YYYY-MM-DD
    ARRIVAL_DATE: str  # YYYY-MM-DD
    ADULTS: int = 1

    REFRESH_INTERVAL_DAYTIME_MINUTES: int = 30
    REFRESH_INTERVAL_NIGHTTIME_MINUTES: int = 120
    USER_AGENT: str = "Mozilla/5.0 (compatible; VietnamTicketsScraper/1.0)"
    HEADLESS: bool = True

    # API Keys (optional - only needed for API-based providers)
    SKYSCANNER_API_KEY: str | None = None

    # Discord webhook (optional - for notifications)
    DISCORD_WEBHOOK_URL: str | None = None

    DATA_DIR: str = "./data"
    SEEN_FILE: str = "./data/seen_offers.txt"

    model_config = SettingsConfigDict(env_file=(".env.local", ".env"), env_file_encoding="utf-8")

    def get_origins(self) -> list[str]:
        """Parse pipe-separated ORIGIN values. E.g., 'PRG|VIE|BRQ' -> ['PRG', 'VIE', 'BRQ']"""
        return [o.strip() for o in self.ORIGIN.split("|") if o.strip()]

    def get_destinations(self) -> list[str]:
        """Parse pipe-separated DESTINATION values. E.g., 'SGN|HAN|DAD' -> ['SGN', 'HAN', 'DAD']"""
        return [d.strip() for d in self.DESTINATION.split("|") if d.strip()]


def get_settings() -> Settings:
    return Settings()
