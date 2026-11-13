"""Tests for the Redis service's command in `compose.yaml` (M17, D-112, D-117), with no Docker.

D-112: "Compose runs Redis with `--maxmemory 256mb --maxmemory-policy volatile-lru`". The running
stack's `CONFIG GET` is the integration test's (`test_compose_stack_cache.py`); this file pins the
file itself, so a change that drops or alters the command fails in the default run too. The file is
read as text (no YAML library is installed): the `redis` service's block, and in it a `command` that
is either a flow sequence on one line (valid JSON when its strings are double quoted) or a block
sequence of `- item` lines. It must be exactly `redis-server --maxmemory 256mb --maxmemory-policy
volatile-lru`, as a list, so Redis is started directly and no shell is involved.

Also pinned, because they are what the memory cap and the limiter's keys rely on: the service still
runs the pinned image (tag plus digest, D-079) and keeps its healthcheck, and no other service has
the command.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "compose.yaml"
EXPECTED = ["redis-server", "--maxmemory", "256mb", "--maxmemory-policy", "volatile-lru"]


def service_block(name: str) -> list[str]:
    """The lines of one service under `services:`, from its name line to the next sibling."""
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if re.fullmatch(rf"  {name}:\s*", line))
    block = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("    ") and not line.lstrip().startswith("#"):
            break
        block.append(line)
    return block


def command_of(block: list[str]) -> list[str] | None:
    for index, line in enumerate(block):
        match = re.fullmatch(r"    command:\s*(.*?)\s*", line)
        if match is None:
            continue
        rest = match.group(1)
        if rest.startswith("["):
            parsed = json.loads(rest)
            assert isinstance(parsed, list)
            return [str(item) for item in parsed]
        items = []
        for following in block[index + 1 :]:
            item = re.fullmatch(r"\s{4,}-\s+(.*?)\s*", following)
            if item is None:
                break
            items.append(item.group(1).strip("\"'"))
        return items
    return None


def test_the_redis_service_starts_redis_with_a_256_mb_cap_and_volatile_lru_eviction() -> None:
    assert command_of(service_block("redis")) == EXPECTED


def test_the_command_is_a_list_so_redis_is_started_directly_and_not_through_a_shell() -> None:
    block = service_block("redis")
    commands = [line for line in block if line.strip().startswith("command")]
    assert len(commands) == 1, commands
    assert not re.search(r"command:\s*[\"']?redis-server", commands[0]), commands[0]
    text = "\n".join(block)
    assert "sh -c" not in text
    assert "/bin/sh" not in text


def test_only_the_redis_service_has_the_memory_settings() -> None:
    for name in ("api", "migrate", "postgres"):
        assert command_of(service_block(name)) != EXPECTED


def test_the_redis_service_keeps_its_pinned_image_and_its_healthcheck() -> None:
    block = "\n".join(service_block("redis"))
    assert re.search(r"image:\s*redis:\S+@sha256:[0-9a-f]{64}", block), block
    assert "healthcheck:" in block
    assert "redis-cli" in block


def test_redis_still_publishes_no_port() -> None:
    # D-078: the integration tests reach Redis with `docker compose exec`, never a published port.
    assert "ports:" not in "\n".join(service_block("redis"))
