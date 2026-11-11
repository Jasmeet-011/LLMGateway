"""Provider prices and what an attempt costs (D-096, D-098).

Why it exists: a usage record carries its cost (D-095), and a cost needs a price that
says where it came from and when it applies. This module reads the packaged price
table, checks at startup that every configured model has a price, and turns token
counts into an exact cost.

Key design choices:

- Money is an integer number of nano-dollars, never a float (D-096). A price is
  written as a decimal string in USD per million tokens with at most three decimal
  places, so its per-token value is a whole number of nano-dollars ($1.00 / MTok is
  1,000), and a cost is an exact integer product. The string is turned into an
  integer from its digits; no float or `Decimal` is involved.
- The table lives in the package (`prices.toml`), not in the gateway config: prices
  are facts about providers, changed by a reviewed commit, each with its source page
  and the day it was read (D-098).
- Each model has dated periods, so a price change announced in advance is a row and
  not a deploy on the day. A record is priced by the period with the latest `from`
  on or before its UTC date, and a long-prompt tier applies to both input and output
  when the prompt is over its threshold, as Anthropic prices Haiku 5.5.
- The mock costs 0 by rule and has no entry; its counts are words (D-016), and its
  rows say `mock` so they are never taken for real tokens.
- `check` runs at startup: a configured Claude or Gemini model with no price in
  effect stops the gateway, naming the config location and never the model (D-044).
  Dates only move forward, so a model priced at startup is priced at billing time.
"""

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from importlib import resources
from typing import Any, Final

from llmgateway.config import ConfigError, GatewayConfig
from llmgateway.schema import Usage

_PRICED_TYPES: Final = ("claude", "gemini")
_PRICE: Final = re.compile(r"(0|[1-9][0-9]*)(?:\.([0-9]{1,3}))?")
_REQUIRED: Final = ("from", "input", "output", "source", "checked")
_LONG: Final = ("long_prompt_over", "long_input", "long_output")
_KEYS: Final = frozenset(_REQUIRED + _LONG)
# Nano-dollars per token in $1 per million tokens: 10**9 / 10**6.
_NANO_PER_USD_PER_MTOK: Final = 1000


@dataclass(frozen=True)
class PricePeriod:
    """One model's prices from `start` on, in nano-dollars per token."""

    start: date
    input_nanousd: int
    output_nanousd: int
    long_prompt_over: int | None
    long_input_nanousd: int | None
    long_output_nanousd: int | None
    source: str
    checked: date


def _error(location: str, rule: str) -> ConfigError:
    return ConfigError(f"prices.toml: {location}: {rule}")


def _date(value: object, location: str) -> date:
    # tomllib gives a datetime for a date-time; that is not a day.
    if not isinstance(value, date) or isinstance(value, datetime):
        raise _error(location, "must be a TOML date such as 2026-11-11")
    return value


def _nanousd(value: object, location: str) -> int:
    if not isinstance(value, str):
        raise _error(location, 'must be a string of USD per million tokens, such as "0.10"')
    match = _PRICE.fullmatch(value)
    if match is None:
        raise _error(location, "must be a non-negative decimal with at most three decimal places")
    whole, fraction = match.group(1), match.group(2) or ""
    return int(whole) * _NANO_PER_USD_PER_MTOK + int(fraction.ljust(3, "0"))


def _period(raw: object, location: str) -> PricePeriod:
    if not isinstance(raw, dict):
        raise _error(location, "must be a table")
    for key in raw:
        if key not in _KEYS:
            raise _error(f"{location}.{key}", "is not a known key")
    for key in _REQUIRED:
        if key not in raw:
            raise _error(f"{location}.{key}", "is required")
    missing = [key for key in _LONG if key not in raw]
    present = len(missing) < len(_LONG)
    if present and missing:
        raise _error(
            f"{location}.{missing[0]}", "long_prompt_over, long_input and long_output go together"
        )
    long_prompt_over: int | None = None
    long_input: int | None = None
    long_output: int | None = None
    if present:
        threshold = raw["long_prompt_over"]
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
            raise _error(f"{location}.long_prompt_over", "must be an integer of at least 1")
        long_prompt_over = threshold
        long_input = _nanousd(raw["long_input"], f"{location}.long_input")
        long_output = _nanousd(raw["long_output"], f"{location}.long_output")
    source = raw["source"]
    if not isinstance(source, str) or not source.startswith("https://"):
        raise _error(f"{location}.source", "must be an https:// URL")
    return PricePeriod(
        start=_date(raw["from"], f"{location}.from"),
        input_nanousd=_nanousd(raw["input"], f"{location}.input"),
        output_nanousd=_nanousd(raw["output"], f"{location}.output"),
        long_prompt_over=long_prompt_over,
        long_input_nanousd=long_input,
        long_output_nanousd=long_output,
        source=source,
        checked=_date(raw["checked"], f"{location}.checked"),
    )


def _periods(raw: object, location: str) -> tuple[PricePeriod, ...]:
    if not isinstance(raw, list) or not raw:
        raise _error(location, "must be a non-empty array of tables")
    periods = tuple(_period(item, f"{location}[{index}]") for index, item in enumerate(raw))
    for index in range(1, len(periods)):
        if periods[index].start <= periods[index - 1].start:
            raise _error(f"{location}[{index}].from", "must be later than the period before")
    return periods


class PriceTable:
    def __init__(self, prices: Mapping[tuple[str, str], tuple[PricePeriod, ...]]) -> None:
        self._prices = dict(prices)

    def price_on(self, provider_type: str, model: str, day: date) -> PricePeriod | None:
        """The period in effect on `day`: the latest that starts on or before it."""
        found: PricePeriod | None = None
        for period in self._prices.get((provider_type, model), ()):
            if period.start <= day:
                found = period
        return found

    def cost_nanousd(self, provider_type: str, model: str, usage: Usage, on: date) -> int:
        """Exact cost in nano-dollars; raises LookupError when no price is in effect."""
        if provider_type == "mock":
            return 0
        period = self.price_on(provider_type, model, on)
        if period is None:
            raise LookupError("no price in effect for this model")
        input_price, output_price = period.input_nanousd, period.output_nanousd
        if (
            period.long_prompt_over is not None
            and period.long_input_nanousd is not None
            and period.long_output_nanousd is not None
            and usage.input_tokens > period.long_prompt_over
        ):
            input_price, output_price = period.long_input_nanousd, period.long_output_nanousd
        return usage.input_tokens * input_price + usage.output_tokens * output_price

    def check(self, config: GatewayConfig, today: date) -> None:
        """Stops startup if a configured Claude or Gemini model has no price (D-098)."""
        for alias, entries in config.aliases.items():
            for index, entry in enumerate(entries):
                provider_type = config.providers[entry.provider].type
                if provider_type not in _PRICED_TYPES:
                    continue
                if self.price_on(provider_type, entry.model, today) is None:
                    # The location, never the model (D-044).
                    raise ConfigError(
                        f"aliases.{alias}[{index}].model: no price in effect for this "
                        "model in prices.toml"
                    )


def parse_prices(text: str) -> PriceTable:
    """Parse and validate price-table text; raises ConfigError on any mistake."""
    try:
        data: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ConfigError("prices.toml: the file is not valid TOML") from None
    prices: dict[tuple[str, str], tuple[PricePeriod, ...]] = {}
    for provider_type, models in data.items():
        if provider_type not in _PRICED_TYPES:
            raise _error(provider_type, "only claude and gemini are priced; mock costs 0")
        if not isinstance(models, dict):
            raise _error(provider_type, "must be a table of models")
        for model, raw in models.items():
            prices[(provider_type, model)] = _periods(raw, f"{provider_type}.{model}")
    return PriceTable(prices)


def load_prices() -> PriceTable:
    """The table shipped in the package."""
    text = resources.files("llmgateway").joinpath("prices.toml").read_text(encoding="utf-8")
    return parse_prices(text)
