"""Адаптеры источников гигов.

- ``base``       — протокол ``GigSource``;
- ``fixture``    — синтетический датасет P1 (тесты и regression);
- ``contra``     — реальный live-источник P2 (публичные страницы Contra);
- ``contra_auth``— авторизованная лента Contra P2.1;
- ``freelancer`` — публичная выдача Freelancer.com P3 (без логина).
"""
from __future__ import annotations

from .base import GigSource
from .contra import ContraGigSource, ContraUnavailable
from .fixture import FixtureGigSource
from .freelancer import FreelancerGigSource, FreelancerUnavailable
from .kwork import KworkGigSource, KworkUnavailable
from .telegram import TelegramGigSource

__all__ = [
    "GigSource",
    "ContraGigSource",
    "ContraUnavailable",
    "FixtureGigSource",
    "FreelancerGigSource",
    "FreelancerUnavailable",
    "KworkGigSource",
    "KworkUnavailable",
    "TelegramGigSource",
]

