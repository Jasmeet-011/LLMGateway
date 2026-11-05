"""Gateway configuration: named provider instances and model aliases (D-044).

Why it exists: clients send an alias in `model`, and the gateway must know which
(provider, model) pairs stand behind it, in fallback order (D-010). This module
reads that mapping from a TOML file and rejects every mistake at startup, so a bad
config never turns into a stream of per-request errors.

Key design choices:

- TOML, parsed with the standard library's `tomllib`: no new dependency, comments
  are allowed, and integers, floats and booleans are distinct types.
- Provider instances are named, so one alias can hold two mocks with independent
  latency and failure settings, and fallback can be tested without keys.
- API keys never come from the file; the repo is public and the file is meant to
  be committed. `router.build_router` reads them from the environment.
- Every check is strict: unknown keys, unused instances, duplicate entries, names
  that could not be requested (D-030), and Gemini models that would change the
  request URL (D-046) all stop startup.
- Error messages name the location in the file and the rule, never a value from
  the file, the path, or the environment, so a key pasted into the wrong place
  is not printed to a console or CI log. A name that is itself invalid is shown
  as `<invalid name>`.
- `LLMGATEWAY_PROVIDER` (D-036) is rejected rather than ignored, so a stale
  variable cannot leave the gateway on a provider the owner did not expect.
- `LLMGATEWAY_FAULTS` (D-064) is the one setting not in the file, because the same
  config must serve a baseline run and a failure run. It can only name mock
  instances, which is what keeps it out of production: a config with no mock
  cannot start with it set, so it can never fail a real provider's traffic.
"""

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from llmgateway.providers.gemini import MODEL_ID_PATTERN
from llmgateway.providers.mock import FailureType, FaultWindow

CONFIG_ENV: Final = "LLMGATEWAY_CONFIG"
RETIRED_PROVIDER_ENV: Final = "LLMGATEWAY_PROVIDER"
FAULTS_ENV: Final = "LLMGATEWAY_FAULTS"

# Provider and alias names. 128 is ChatRequest.model's cap (D-030), so every alias
# can be requested.
NAME_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
MAX_NAME_LENGTH: Final = 128
MAX_MODEL_LENGTH: Final = 128

# One LLMGATEWAY_FAULTS entry: <instance>=<rate>, optionally @<start>+<duration>.
_NUMBER: Final = r"[0-9]+(?:\.[0-9]+)?"
_FAULT_ENTRY: Final = re.compile(
    rf"({NAME_PATTERN.pattern})=({_NUMBER})(?:@({_NUMBER})\+({_NUMBER}))?"
)

DEFAULT_CONFIG: Final = """\
# Built-in default (D-044): the mock provider only, so no keys are needed.
[providers.mock]
type = "mock"

[aliases]
mock = [{ provider = "mock", model = "mock-1" }]
"""


class ConfigError(Exception):
    """The configuration is invalid; the gateway must not start."""


class _Model(BaseModel):
    # Strict: TOML already has types, so "1" or true is never a number here.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MockProviderConfig(_Model):
    type: Literal["mock"] = "mock"
    latency_ms: Annotated[float, Field(ge=0.0, allow_inf_nan=False)] = 0.0
    failure_rate: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)] = 0.0
    failure_type: FailureType = "unavailable"
    seed: int | None = None


class ClaudeProviderConfig(_Model):
    type: Literal["claude"] = "claude"


class GeminiProviderConfig(_Model):
    type: Literal["gemini"] = "gemini"


ProviderConfig = MockProviderConfig | ClaudeProviderConfig | GeminiProviderConfig

_PROVIDER_TYPES: Final[dict[str, type[ProviderConfig]]] = {
    "mock": MockProviderConfig,
    "claude": ClaudeProviderConfig,
    "gemini": GeminiProviderConfig,
}


class AliasEntry(_Model):
    provider: str
    model: Annotated[str, Field(min_length=1, max_length=MAX_MODEL_LENGTH)]


class GatewayConfig(_Model):
    providers: Mapping[str, ProviderConfig]
    aliases: Mapping[str, tuple[AliasEntry, ...]]


def _is_name(value: str) -> bool:
    return len(value) <= MAX_NAME_LENGTH and NAME_PATTERN.fullmatch(value) is not None


def _shown(name: object) -> str:
    # Only names that pass the rules are printed; anything else could be text that
    # was never meant to be a name, such as a pasted key.
    return name if isinstance(name, str) and _is_name(name) else "<invalid name>"


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    if key not in data:
        raise ConfigError(f"{key}: the table is required")
    value = data[key]
    if not isinstance(value, dict):
        raise ConfigError(f"{key}: must be a table")
    if not value:
        raise ConfigError(f"{key}: must not be empty")
    return value


def _validated[M: _Model](model: type[M], raw: dict[str, Any], location: str) -> M:
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        # The first error only. pydantic's msg for these field types states the rule,
        # not the input; "input" and "ctx" are never used.
        error = exc.errors()[0]
        field = ".".join(_shown(part) for part in error["loc"])
        raise ConfigError(f"{location}.{field}: {error['msg']}") from None


def _provider(name: str, raw: object) -> ProviderConfig:
    location = f"providers.{_shown(name)}"
    if not _is_name(name):
        raise ConfigError(
            f"{location}: a provider name must match {NAME_PATTERN.pattern} "
            f"and have at most {MAX_NAME_LENGTH} characters"
        )
    if not isinstance(raw, dict):
        raise ConfigError(f"{location}: must be a table")
    if "type" not in raw:
        raise ConfigError(f"{location}.type: is required")
    provider_type = raw["type"]
    # Dispatched here, not with a pydantic discriminated union, whose error message
    # would repeat the unknown type value.
    if not isinstance(provider_type, str) or provider_type not in _PROVIDER_TYPES:
        raise ConfigError(f"{location}.type: must be 'mock', 'claude' or 'gemini'")
    return _validated(_PROVIDER_TYPES[provider_type], raw, location)


def _alias(
    name: str, raw: object, providers: Mapping[str, ProviderConfig]
) -> tuple[AliasEntry, ...]:
    location = f"aliases.{_shown(name)}"
    if not _is_name(name):
        raise ConfigError(
            f"{location}: an alias name must match {NAME_PATTERN.pattern} "
            f"and have at most {MAX_NAME_LENGTH} characters"
        )
    if not isinstance(raw, list):
        raise ConfigError(f"{location}: must be an array of {{ provider, model }} tables")
    if not raw:
        raise ConfigError(f"{location}: must not be empty")

    entries: list[AliasEntry] = []
    for index, item in enumerate(raw):
        entry_location = f"{location}[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{entry_location}: must be a {{ provider, model }} table")
        entry = _validated(AliasEntry, item, entry_location)
        provider = providers.get(entry.provider)
        if provider is None:
            raise ConfigError(f"{entry_location}.provider: names no defined provider")
        # The model goes into Gemini's URL path (D-042, D-046).
        if provider.type == "gemini" and MODEL_ID_PATTERN.fullmatch(entry.model) is None:
            raise ConfigError(
                f"{entry_location}.model: a Gemini model must match {MODEL_ID_PATTERN.pattern}"
            )
        if entry in entries:
            raise ConfigError(f"{entry_location}: repeats an earlier entry of this alias")
        entries.append(entry)
    return tuple(entries)


def parse_config(text: str) -> GatewayConfig:
    """Parse and validate config text; raises ConfigError on any mistake."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        # Only the position from tomllib's message, never a fragment of the file.
        position = re.search(r"\((at [^)]*)\)\s*$", str(exc))
        where = f" ({position.group(1)})" if position else ""
        raise ConfigError(f"the file is not valid TOML{where}") from None

    for key in data:
        if key not in ("providers", "aliases"):
            raise ConfigError(f"{_shown(key)}: is not a known top-level key")

    providers = {name: _provider(name, raw) for name, raw in _table(data, "providers").items()}
    aliases = {name: _alias(name, raw, providers) for name, raw in _table(data, "aliases").items()}

    used = {entry.provider for entries in aliases.values() for entry in entries}
    for name in providers:
        if name not in used:
            raise ConfigError(f"providers.{name}: is not used by any alias")

    return GatewayConfig(providers=providers, aliases=aliases)


def parse_faults(environ: Mapping[str, str], config: GatewayConfig) -> dict[str, FaultWindow]:
    """The fault windows in LLMGATEWAY_FAULTS, by instance; empty when unset (D-064)."""
    value = environ.get(FAULTS_ENV)
    if value is None:
        return {}
    if not value.strip():
        raise ConfigError(f"{FAULTS_ENV} is set but empty")
    faults: dict[str, FaultWindow] = {}
    for position, entry in enumerate(value.split(","), start=1):
        location = f"{FAULTS_ENV}: entry {position}"
        match = _FAULT_ENTRY.fullmatch(entry)
        if match is None:
            raise ConfigError(
                f"{location} must be <instance>=<rate> or <instance>=<rate>@<start>+<duration>"
            )
        name, rate, start, duration = match.groups()
        provider = config.providers.get(name)
        if provider is None:
            raise ConfigError(f"{location} names an instance that is not in the config")
        if provider.type != "mock":
            raise ConfigError(f"{location} must name a mock instance")
        if name in faults:
            raise ConfigError(f"{location} repeats an instance")
        try:
            faults[name] = FaultWindow(
                float(rate),
                float(start) if start is not None else 0.0,
                float(duration) if duration is not None else None,
            )
        except ValueError as exc:
            # FaultWindow's messages are fixed text that states the rule, never a value.
            raise ConfigError(f"{location}: {exc}") from None
    return faults


def load_config(environ: Mapping[str, str]) -> GatewayConfig:
    """The config named by LLMGATEWAY_CONFIG, or the built-in default when unset."""
    if RETIRED_PROVIDER_ENV in environ:
        raise ConfigError(
            f"{RETIRED_PROVIDER_ENV} is no longer read; "
            f"set {CONFIG_ENV} to a config file instead (D-044)"
        )
    path = environ.get(CONFIG_ENV)
    if path is None:
        return parse_config(DEFAULT_CONFIG)
    if not path.strip():
        raise ConfigError(f"{CONFIG_ENV} is set but empty")
    try:
        raw = Path(path).read_bytes()
    except OSError:
        # The OSError's own text includes the path, so it is not used.
        raise ConfigError(f"{CONFIG_ENV}: the file could not be read") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{CONFIG_ENV}: the file is not valid UTF-8") from None
    return parse_config(text)
