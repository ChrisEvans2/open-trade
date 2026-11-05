"""Persist the intended Hermes profile with a notification, without another gateway."""

import re

from trading_agent.config import AppConfig

PREFIX = "hermes-profile:"


def prediction_target(config: AppConfig) -> str | None:
    target = config.notification.prediction_target
    if target is None:
        return config.notification.target  # existing installations keep their old destination
    return f"{PREFIX}{config.hermes.trading_profile}:{target}"


def destination(config: AppConfig, value: str) -> tuple[str, str]:
    profile = config.hermes.controller_profile
    target = value
    if value.startswith(PREFIX):
        profile, separator, target = value[len(PREFIX) :].partition(":")
        if not separator or profile not in {
            config.hermes.controller_profile,
            config.hermes.trading_profile,
        }:
            raise ValueError("NOTIFICATION_ROUTE_INVALID")
        if profile == config.hermes.trading_profile and not config.hermes.trading_telegram_enabled:
            raise ValueError("PREDICTION_NOTIFICATION_NOT_CONFIGURED")
    if not profile or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", profile) or not target:
        raise ValueError("NOTIFICATION_NOT_CONFIGURED")
    return profile, target
