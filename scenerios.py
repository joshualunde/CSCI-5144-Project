"""
Deterministic traffic scenarios for trading-bot workloads.

This module has NO Locust dependency so the same timelines can be:
  * driven by Locust (see locustfile.py -> ScenarioShape),
  * exported as an "economic calendar" that the predictive autoscaler may read. Can set scenerios of anticipated activity
    i.e. ipo or quarterly finances

Every scenario is a pure function of time (plus a seed), so runs are repeatable:
the same SCENARIO + SCENARIO_SEED + TIME_SCALE always yields the same user curve.

Environment variables
---------------------
SCENARIO          low | normal | scheduled_surge | unscheduled_surge   (default: normal)
SCENARIO_SEED     integer seed for noise and the unscheduled shock time   (default: 42)
TIME_SCALE        multiply all durations (0.1 = 10x faster smoke test)   (default: 1.0)
LOW_USERS         concurrent bots in the low scenario                     (default: 10)
BASE_USERS        concurrent bots in normal trading                       (default: 50)
SURGE_MULTIPLIER  peak users = BASE_USERS * SURGE_MULTIPLIER              (default: 8)

CLI
---
python scenarios.py --list
python scenarios.py --dump scheduled_surge > scheduled_surge.csv
python scenarios.py --calendar scheduled_surge     # known events, JSON
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


SEED = _env_int("SCENARIO_SEED", 42)
TIME_SCALE = _env_float("TIME_SCALE", 1.0)
LOW_USERS = _env_int("LOW_USERS", 10)
BASE_USERS = _env_int("BASE_USERS", 50)
SURGE_MULTIPLIER = _env_float("SURGE_MULTIPLIER", 8.0)

# Phases that bots treat as "news-driven": they trade more aggressively.
SURGE_PHASES = {"spike", "aftershock"}
ELEVATED_PHASES = {"pre_event", "decay"}


@dataclass(frozen=True)
class Sample:
    users: int          # target concurrent bots
    spawn_rate: float   # bots started/stopped per second to reach target
    phase: str          # label used for logging, annotations and bot behaviour
    intensity: float    # users / BASE_USERS; bots use it to shorten think time


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    duration_s: float                                  # unscaled
    curve: Callable[[float], Tuple[float, str]]        # t (unscaled s) -> (users, phase)
    calendar: Callable[[], List[dict]] = lambda: []    # events a forecaster may know

    @property
    def scaled_duration(self) -> float:
        return self.duration_s * TIME_SCALE

    def at(self, real_elapsed_s: float) -> Sample | None:
        """Sample the scenario at a wall-clock offset from test start."""
        if real_elapsed_s >= self.scaled_duration:
            return None
        t = real_elapsed_s / TIME_SCALE
        users_f, phase = self.curve(t)
        users = max(1, int(round(users_f)))
        # Ramp fast enough to follow the curve. Spikes should hit in seconds,
        # not minutes, otherwise Locust itself smooths away the burst.
        if phase in SURGE_PHASES:
            rate = max(20.0, BASE_USERS * SURGE_MULTIPLIER / 5.0)
        else:
            rate = max(5.0, BASE_USERS / 10.0)
        return Sample(users, rate, phase, users / max(1, BASE_USERS))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _noise(t: float, amplitude: float, salt: int = 0) -> float:
    """Deterministic per-5-second jitter in [-amplitude, +amplitude]."""
    bucket = int(t // 5)
    rng = random.Random(SEED * 1_000_003 + salt * 7919 + bucket)
    return rng.uniform(-amplitude, amplitude)


def _decay(t_since: float, start: float, end: float, half_life_s: float) -> float:
    """Exponential decay from `start` toward `end`."""
    return end + (start - end) * math.pow(0.5, t_since / half_life_s)


def _ramp(t_since: float, start: float, end: float, ramp_s: float) -> float:
    frac = min(1.0, max(0.0, t_since / ramp_s))
    return start + (end - start) * frac


# ---------------------------------------------------------------------------
# 1. Low usage: overnight / weekend market. Flat, quiet, small jitter.
# ---------------------------------------------------------------------------

def _low(t: float) -> Tuple[float, str]:
    return LOW_USERS * (1 + _noise(t, 0.10, salt=1)), "low"


# ---------------------------------------------------------------------------
# 2. Normal traffic: a trading session compressed into 20 min. Busier at the
#    open and close (U-shaped intraday volume), quieter mid-day.
# ---------------------------------------------------------------------------

NORMAL_DURATION = 20 * 60


def _normal(t: float) -> Tuple[float, str]:
    x = t / NORMAL_DURATION                       # 0..1 across the "session"
    u_shape = 0.75 + 0.5 * (2 * x - 1) ** 2       # 1.25 at open/close, 0.75 midday
    users = BASE_USERS * u_shape * (1 + _noise(t, 0.08, salt=2))
    phase = "open" if x < 0.15 else "close" if x > 0.85 else "midday"
    return users, phase


# ---------------------------------------------------------------------------
# 3. Scheduled surge: a known release (CPI print, FOMC decision, earnings).
#    Traffic builds slightly before the release, spikes at it, then decays.
#    The release time is published via calendar() so a predictive autoscaler
#    is allowed to know about it in advance.
# ---------------------------------------------------------------------------

SCHED_EVENT_T = 10 * 60          # release at 10:00 into the run
SCHED_PREBUILD_S = 120           # positioning starts 2 min before
SCHED_SPIKE_HOLD_S = 180         # 3 min at peak
SCHED_DURATION = 25 * 60


def _scheduled(t: float) -> Tuple[float, str]:
    peak = BASE_USERS * SURGE_MULTIPLIER
    jitter = 1 + _noise(t, 0.05, salt=3)
    if t < SCHED_EVENT_T - SCHED_PREBUILD_S:
        return BASE_USERS * jitter, "baseline"
    if t < SCHED_EVENT_T:
        since = t - (SCHED_EVENT_T - SCHED_PREBUILD_S)
        return _ramp(since, BASE_USERS, BASE_USERS * 1.5, SCHED_PREBUILD_S) * jitter, "pre_event"
    if t < SCHED_EVENT_T + SCHED_SPIKE_HOLD_S:
        since = t - SCHED_EVENT_T
        return _ramp(since, BASE_USERS * 1.5, peak, 15) * jitter, "spike"
    since = t - (SCHED_EVENT_T + SCHED_SPIKE_HOLD_S)
    users = _decay(since, peak, BASE_USERS, half_life_s=90)
    phase = "decay" if users > BASE_USERS * 1.2 else "recovery"
    return users * jitter, phase


def _scheduled_calendar() -> List[dict]:
    return [{
        "event": "scheduled_macro_release",
        "t_offset_s": SCHED_EVENT_T * TIME_SCALE,
        "expected_peak_users": int(BASE_USERS * SURGE_MULTIPLIER),
        "expected_duration_s": SCHED_SPIKE_HOLD_S * TIME_SCALE,
    }]


# ---------------------------------------------------------------------------
# 4. Unscheduled surge: breaking news with no warning. The shock time is drawn
#    from the seed (repeatable, but not published in the calendar). The ramp is
#    near-instant, and a smaller aftershock follows as the story develops.
# ---------------------------------------------------------------------------

UNSCHED_DURATION = 30 * 60
UNSCHED_SHOCK_T = random.Random(SEED).uniform(8 * 60, 15 * 60)
UNSCHED_AFTERSHOCK_DELAY = 6 * 60


def _unscheduled(t: float) -> Tuple[float, str]:
    peak = BASE_USERS * SURGE_MULTIPLIER
    jitter = 1 + _noise(t, 0.06, salt=4)
    shock = UNSCHED_SHOCK_T
    after = shock + UNSCHED_AFTERSHOCK_DELAY
    if t < shock:
        return BASE_USERS * jitter, "baseline"
    if t < shock + 120:                                   # 2 min at peak, 5 s ramp
        return _ramp(t - shock, BASE_USERS, peak, 5) * jitter, "spike"
    if t < after:
        users = _decay(t - shock - 120, peak, BASE_USERS * 1.3, half_life_s=60)
        return users * jitter, "decay"
    if t < after + 60:                                    # aftershock at half the peak
        base = _decay(after - shock - 120, peak, BASE_USERS * 1.3, 60)
        return _ramp(t - after, base, peak * 0.5, 10) * jitter, "aftershock"
    users = _decay(t - after - 60, peak * 0.5, BASE_USERS, half_life_s=90)
    phase = "decay" if users > BASE_USERS * 1.2 else "recovery"
    return users * jitter, phase


SCENARIOS: Dict[str, Scenario] = {
    "low": Scenario(
        "low", "Quiet market: flat, low concurrency", 15 * 60, _low),
    "normal": Scenario(
        "normal", "Regular session with U-shaped intraday volume", NORMAL_DURATION, _normal),
    "scheduled_surge": Scenario(
        "scheduled_surge", "Known macro release at 10:00; build-up, spike, decay",
        SCHED_DURATION, _scheduled, _scheduled_calendar),
    "unscheduled_surge": Scenario(
        "unscheduled_surge", "Breaking news at a seeded random time; spike + aftershock",
        UNSCHED_DURATION, _unscheduled),
}


def get(name: str | None = None) -> Scenario:
    name = name or os.getenv("SCENARIO", "normal")
    try:
        return SCENARIOS[name]
    except KeyError:
        raise SystemExit(f"Unknown SCENARIO '{name}'. Choose one of: {', '.join(SCENARIOS)}")


def phase_intensity(phase: str) -> float:
    """How much faster bots act in a phase (divides think time)."""
    if phase in SURGE_PHASES:
        return 3.0
    if phase in ELEVATED_PHASES:
        return 1.5
    return 1.0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv: List[str]) -> None:
    if not argv or argv[0] == "--list":
        for s in SCENARIOS.values():
            print(f"{s.name:18s} {s.scaled_duration/60:5.1f} min  {s.description}")
        return
    if argv[0] == "--dump":
        sc = get(argv[1] if len(argv) > 1 else None)
        print("t_s,users,spawn_rate,phase")
        t = 0.0
        while (s := sc.at(t)) is not None:
            print(f"{t:.0f},{s.users},{s.spawn_rate:.1f},{s.phase}")
            t += max(1.0, TIME_SCALE)
        return
    if argv[0] == "--calendar":
        print(json.dumps(get(argv[1] if len(argv) > 1 else None).calendar(), indent=2))
        return
    raise SystemExit(__doc__)


if __name__ == "__main__":
    _main(sys.argv[1:])
