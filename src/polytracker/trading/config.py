"""Environment-backed configuration for authenticated trading."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import polymarket
from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when trading configuration is missing or unsafe."""


def load_local_env() -> None:
    """Load the repository-local secret file without overriding shell values."""
    load_dotenv(dotenv_path=Path(".env"), override=False)


def _decimal_env(name: str, default: str) -> Decimal:
    raw = os.environ.get(name, default)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigurationError(f"{name} must be a decimal number") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _float_env(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Hard execution limits shared by paper and live modes."""

    max_live_spend: Decimal = Decimal("5")
    min_seconds_remaining: float = 120.0
    max_book_age_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> "RiskConfig":
        load_local_env()
        max_spend = _decimal_env("POLYTRACKER_MAX_LIVE_SPEND", "5")
        if max_spend > Decimal("5"):
            raise ConfigurationError("POLYTRACKER_MAX_LIVE_SPEND cannot exceed 5")
        return cls(
            max_live_spend=max_spend,
            min_seconds_remaining=_float_env("POLYTRACKER_MIN_SECONDS_REMAINING", "120"),
            max_book_age_seconds=_float_env("POLYTRACKER_MAX_BOOK_AGE_SECONDS", "5"),
        )


@dataclass(frozen=True, slots=True)
class AuthConfig:
    """Secrets and public wallet identity used by ``AsyncSecureClient``."""

    private_key: str = field(repr=False)
    wallet_address: str
    relayer_api_key: str | None = field(default=None, repr=False)
    relayer_api_key_address: str | None = None

    @classmethod
    def from_env(cls, *, require_relayer: bool = False) -> "AuthConfig":
        load_local_env()
        private_key = (
            os.environ.get("POLYMARKET_PRIVATE_KEY")
            or os.environ.get("WALLET_PRIVATE_KEY")
            or ""
        ).strip()
        wallet = (
            os.environ.get("POLYMARKET_WALLET_ADDRESS")
            or os.environ.get("ACCOUNT_ADDRESS")
            or ""
        ).strip()
        if not private_key:
            raise ConfigurationError(
                "POLYMARKET_PRIVATE_KEY or WALLET_PRIVATE_KEY is required"
            )
        if not wallet:
            raise ConfigurationError(
                "POLYMARKET_WALLET_ADDRESS or ACCOUNT_ADDRESS is required"
            )
        if re.fullmatch(r"0x[0-9a-fA-F]{64}", private_key) is None:
            raise ConfigurationError(
                "private key must be 0x followed by 64 hexadecimal characters"
            )
        if re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet) is None:
            raise ConfigurationError(
                "wallet address must be 0x followed by 40 hexadecimal characters"
            )

        relayer_key = os.environ.get("POLYMARKET_RELAYER_API_KEY", "").strip() or None
        relayer_address = (
            os.environ.get("POLYMARKET_RELAYER_API_KEY_ADDRESS", "").strip() or None
        )
        if (relayer_key is None) != (relayer_address is None):
            raise ConfigurationError(
                "POLYMARKET_RELAYER_API_KEY and POLYMARKET_RELAYER_API_KEY_ADDRESS "
                "must be provided together"
            )
        if require_relayer and relayer_key is None:
            raise ConfigurationError("relayer API credentials are required for this command")
        if relayer_address is not None and re.fullmatch(
            r"0x[0-9a-fA-F]{40}", relayer_address
        ) is None:
            raise ConfigurationError(
                "relayer API key address must be 0x followed by 40 hexadecimal characters"
            )
        return cls(
            private_key=private_key,
            wallet_address=wallet,
            relayer_api_key=relayer_key,
            relayer_api_key_address=relayer_address,
        )

    def secure_client_kwargs(self) -> dict:
        kwargs: dict = {
            "private_key": self.private_key,
            "wallet": self.wallet_address,
        }
        if self.relayer_api_key is not None and self.relayer_api_key_address is not None:
            kwargs["api_key"] = polymarket.RelayerApiKey(
                key=self.relayer_api_key,
                address=self.relayer_api_key_address,
            )
        return kwargs


def live_trading_enabled() -> bool:
    load_local_env()
    return os.environ.get("POLYTRACKER_LIVE_ENABLED", "").strip().lower() == "true"
