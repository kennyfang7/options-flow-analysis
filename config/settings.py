from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # IBKR Connection
    ibkr_host: str = Field(default="127.0.0.1", description="TWS/Gateway host")
    ibkr_port: int = Field(default=7497, description="7497=paper, 7496=live")
    ibkr_client_id: int = Field(default=1, description="Unique client ID per connection")
    ibkr_timeout: int = Field(default=10, description="Connection timeout in seconds")
    ibkr_max_retries: int = Field(default=5, description="Max reconnection attempts before giving up")
    ibkr_readonly: bool = Field(default=True, description="Connect in read-only mode to prevent accidental orders")

    # Database
    database_url: str = Field(
        default="sqlite:///options_flow.db",
        description="SQLAlchemy connection string",
    )

    # Watchlist
    watchlist_path: str = Field(
        default="config/watchlist.txt",
        description="Path to newline-separated ticker watchlist",
    )

    # Scanning Thresholds
    unusual_volume_multiplier: float = Field(
        default=3.0,
        description="Flag trades where volume exceeds X * average daily volume",
    )
    min_block_size: int = Field(
        default=500,
        description="Minimum contracts to qualify as a block trade",
    )
    min_premium: float = Field(
        default=50_000.0,
        description="Minimum dollar premium to track (e.g. 50000 = $50k)",
    )

    # Alert Endpoints
    discord_webhook_url: str = Field(default="", description="Discord webhook URL for alerts")
    alert_email: str = Field(default="", description="Email address for alert notifications")


# Single shared instance — import this everywhere
settings = Settings()


if __name__ == "__main__":
    print(settings.model_dump())
