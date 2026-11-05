"""Seeded simulation behind D-057's trip rule numbers. Arithmetic, not a measurement.

Why it exists: D-057 chose 15 failures in the last 20 outcomes by comparing how often
each rule opens by chance at measurement 4's failure rate, and how fast it opens in a
brownout. Those numbers should be reproducible, so the rule can be defended.

Key design choices:

- Failures are independent, which real failures are not, so this ranks the rules and
  predicts nothing about a real provider.
- An opening starts the count again from an empty window, and time spent open is not
  modelled: the rates are per attempt made while closed.
- The consecutive rules have an exact answer, printed beside the simulated one as a
  check on the simulation: (1 - p^k) / ((1 - p) * p^k) attempts on average to open.

Run with `uv run python scripts/breaker_trip_sim.py`. It can take over ten minutes.
"""

from random import Random

SEED = 57
ATTEMPTS = 40_000_000
CHANCE_RATE = 0.3
BROWNOUT_RATES = (0.7, 0.8)
# (name, window size, failures needed); consecutive k is k failures in a window of k.
RULES = (
    ("5 consecutive", 5, 5),
    ("10 consecutive", 10, 10),
    ("12 of 20", 20, 12),
    ("14 of 20", 20, 14),
    ("15 of 20", 20, 15),
)


def openings(rate: float, size: int, needed: int, attempts: int, rng: Random) -> int:
    """How many times the rule opens over `attempts` attempts."""
    bits = 0
    failures = 0
    count = 0
    oldest = 1 << (size - 1)
    mask = (1 << size) - 1
    for _ in range(attempts):
        failures -= 1 if bits & oldest else 0
        failed = rng.random() < rate
        bits = ((bits << 1) | failed) & mask
        failures += failed
        if failures >= needed:
            count += 1
            bits = 0
            failures = 0
    return count


def exact_mean(rate: float, run: int) -> float:
    """Mean attempts until `run` failures in a row."""
    return (1 - rate**run) / ((1 - rate) * rate**run)


def main() -> None:
    rng = Random(SEED)  # noqa: S311
    print(f"seed {SEED}; chance openings per 100,000 attempts at {CHANCE_RATE:.0%}")
    for name, size, needed in RULES:
        count = openings(CHANCE_RATE, size, needed, ATTEMPTS, rng)
        rate = count / ATTEMPTS * 100_000
        exact = ""
        if size == needed:
            exact = f"  exact {100_000 / exact_mean(CHANCE_RATE, needed):.3f}"
        print(f"  {name}: {rate:.3f} ({count} openings){exact}")
    for failure_rate in BROWNOUT_RATES:
        print(f"mean attempts to open at {failure_rate:.0%}")
        for name, size, needed in RULES:
            if name not in ("10 consecutive", "15 of 20"):
                continue
            count = openings(failure_rate, size, needed, ATTEMPTS, rng)
            exact = ""
            if size == needed:
                exact = f"  exact {exact_mean(failure_rate, needed):.1f}"
            print(f"  {name}: {ATTEMPTS / count:.1f}{exact}")


if __name__ == "__main__":
    main()
