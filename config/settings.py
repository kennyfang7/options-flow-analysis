from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, model_validator


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

    # Greeks Engine
    risk_free_rate: float = Field(
        default=0.05,
        description="Annualized risk-free rate used for Black-Scholes fallback (e.g. 0.05 = 5%)",
        ge=0.0,
    )

    # Flow Classifier
    sweep_window_seconds: float = Field(
        default=2.0, description="Seconds window for sweep detection"
    )
    sweep_min_legs: int = Field(
        default=3, description="Minimum prints in sweep window to qualify as sweep"
    )
    split_window_seconds: float = Field(
        default=5.0, description="Seconds window for split detection"
    )
    split_min_legs: int = Field(
        default=3, description="Minimum prints in split window to qualify as split"
    )
    split_size_tolerance: float = Field(
        default=0.20, description="Max deviation from median size to qualify as split (0.20 = ±20%)"
    )
    classifier_window_seconds: float = Field(
        default=30.0, description="Max age of ticks kept in classifier in-memory window"
    )
    aggressor_buy_threshold: float = Field(
        default=0.70, description="Spread position >= this → BUY aggressor"
    )
    aggressor_sell_threshold: float = Field(
        default=0.30, description="Spread position <= this → SELL aggressor"
    )

    # Unusual Activity Detector
    unusual_premium_threshold: float = Field(
        default=250_000.0,
        description="Minimum single-trade premium ($) to flag as PREMIUM_SIZE",
    )
    unusual_oi_ratio_threshold: float = Field(
        default=0.50,
        description="Minimum volume_delta/open_interest ratio to flag as OI_RATIO",
    )
    unusual_signal_threshold: float = Field(
        default=5.0,
        description="Minimum signal_strength score to flag as SIGNAL_STRENGTH",
    )
    otm_delta_threshold: float = Field(
        default=0.30,
        description="Maximum |delta| to consider a contract OTM for OTM_PREMIUM check",
    )
    otm_premium_threshold: float = Field(
        default=100_000.0,
        description="Minimum premium ($) for an OTM contract to flag as OTM_PREMIUM",
    )

    # Sentiment Aggregator
    sentiment_window_seconds: float = Field(
        default=3600.0,
        description="Rolling window (seconds) for sentiment aggregation (default 1 hour)",
        gt=0,
    )

    # Smart Money Detector
    near_expiry_days: int = Field(
        default=7,
        description="Days-to-expiry threshold for NEAR_EXPIRY_OTM smart money signal",
        ge=1,
        le=90,
    )
    smart_money_min_confidence: float = Field(
        default=0.30,
        description="Minimum confidence score [0, 1] to emit a SmartMoneySignal. 0.0 emits all signals.",
        ge=0,
        le=1.0,
    )

    # Alert Endpoints
    discord_webhook_url: str = Field(default="", description="Discord webhook URL for alerts")
    alert_email: str = Field(default="", description="Email address for alert notifications")

    @field_validator("min_premium")
    @classmethod
    def min_premium_must_be_positive(cls, v: float) -> float:
        """Ensure min_premium > 0 to prevent division-by-zero in signal_strength."""
        if v <= 0:
            raise ValueError("min_premium must be greater than 0")
        return v

    @model_validator(mode="after")
    def aggressor_thresholds_are_ordered(self) -> Settings:
        """Ensure buy threshold > sell threshold to prevent inverted classifier logic."""
        if self.aggressor_buy_threshold <= self.aggressor_sell_threshold:
            raise ValueError(
                "aggressor_buy_threshold must be greater than aggressor_sell_threshold"
            )
        return self

    @model_validator(mode="after")
    def unusual_premium_above_min_premium(self) -> Settings:
        """Ensure unusual_premium_threshold > min_premium to avoid dead PREMIUM_SIZE condition."""
        if self.unusual_premium_threshold <= self.min_premium:
            raise ValueError(
                f"unusual_premium_threshold ({self.unusual_premium_threshold}) "
                f"must exceed min_premium ({self.min_premium})"
            )
        return self

    @field_validator("unusual_oi_ratio_threshold")
    @classmethod
    def oi_ratio_threshold_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("unusual_oi_ratio_threshold must be greater than 0")
        return v

    @field_validator("otm_delta_threshold")
    @classmethod
    def otm_delta_threshold_must_be_in_range(cls, v: float) -> float:
        if not (0 < v < 1):
            raise ValueError("otm_delta_threshold must be between 0 and 1 (exclusive)")
        return v

    @field_validator("unusual_signal_threshold")
    @classmethod
    def unusual_signal_threshold_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("unusual_signal_threshold must be greater than 0")
        return v


# Single shared instance — import this everywhere
settings = Settings()


if __name__ == "__main__":
    print(settings.model_dump())
