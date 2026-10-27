"""
requires locust file

Locust load generator that simulates trading bots hitting an exchange API.

Run (single process):
    SCENARIO=scheduled_surge locust -f locustfile.py --headless \
        --host http://<exchange-service> --csv results/scheduled_surge

Bots
----
MarketMakerBot  - streams quotes, posts two-sided limit orders, cancels/replaces.
MomentumBot     - polls prices; in a news phase it fires bursts of market orders.
ArbitrageBot    - compares two symbols and places paired orders on a "spread".

Bot behaviour follows the scenario phase (see scenarios.py): during "spike" and
"aftershock" bots think ~3x faster and shift toward order placement, which is how
real algos react to news. Load therefore rises from both more bots AND more
requests per bot.

Targets
-------
TARGET_API=exchange (default): generic REST exchange (see mock_exchange.py).
TARGET_API=hotel: DeathStarBench hotelReservation frontend, mapped as
    quote -> GET /hotels, book -> GET /recommendations,
    place order -> POST /reservation, auth/status -> POST /user.
"""
from __future__ import annotations

import itertools
import logging
import os
import random
import time

from locust import HttpUser, LoadTestShape, events, task

import scenarios #file with different bot scenerios

log = logging.getLogger("trading-bots")

SCENARIO = scenarios.get()
TARGET_API = os.getenv("TARGET_API", "exchange")
SYMBOLS = os.getenv("SYMBOLS", "AAPL,MSFT,NVDA,TSLA,SPY,QQQ").split(",")
PHASE_LOG = os.getenv("PHASE_LOG", f"phases_{SCENARIO.name}.csv")

_user_ids = itertools.count()
_test_start: float | None = None


# ---------------------------------------------------------------------------
# Scenario clock, shared by the shape (master) and bots (workers)
# ---------------------------------------------------------------------------

@events.test_start.add_listener
def _on_test_start(environment, **_):
    global _test_start
    _test_start = time.time()
    log.info("Scenario %s (%.1f min, seed %s, time scale %s) against %s API",
             SCENARIO.name, SCENARIO.scaled_duration / 60, scenarios.SEED,
             scenarios.TIME_SCALE, TARGET_API)


def current_phase() -> str:
    if _test_start is None:
        return "baseline"
    s = SCENARIO.at(time.time() - _test_start)
    return s.phase if s else "recovery"


# ---------------------------------------------------------------------------
# API adapters: one interface, two backends
# ---------------------------------------------------------------------------

class ExchangeAPI:
    """Generic REST exchange (matches mock_exchange.py)."""

    def __init__(self, client, rng: random.Random, bot_id: int):
        self.c, self.rng, self.bot_id = client, rng, bot_id

    def quote(self, sym):
        with self.c.get(f"/api/quote/{sym}", name="/api/quote/[sym]",
                        catch_response=True) as r:
            if r.ok:
                try:
                    return float(r.json()["price"])
                except (ValueError, KeyError):
                    r.failure("bad quote payload")
            return None

    def book(self, sym):
        self.c.get(f"/api/orderbook/{sym}", name="/api/orderbook/[sym]")

    def place(self, sym, side, qty, price=None):
        body = {"bot_id": self.bot_id, "symbol": sym, "side": side, "qty": qty,
                "type": "limit" if price else "market"}
        if price:
            body["price"] = round(price, 2)
        with self.c.post("/api/orders", json=body, name="/api/orders [POST]",
                         catch_response=True) as r:
            if r.ok:
                try:
                    return r.json()["order_id"]
                except (ValueError, KeyError):
                    r.failure("no order_id in response")
            return None

    def cancel(self, order_id):
        self.c.delete(f"/api/orders/{order_id}", name="/api/orders/[id] [DELETE]")

    def status(self, order_id):
        self.c.get(f"/api/orders/{order_id}", name="/api/orders/[id]")


class HotelReservationAPI:
    """DeathStarBench hotelReservation used as a stand-in transactional backend.

    Parameters follow the wrk2 mixed-workload script shipped with DeathStarBench
    (users Cornell_0..Cornell_500, password = id repeated 10 times, hotel ids
    1..80). Check them against your deployment's data before relying on them.
    """

    def __init__(self, client, rng: random.Random, bot_id: int):
        self.c, self.rng = client, rng
        uid = bot_id % 501
        self.user, self.pw = f"Cornell_{uid}", str(uid) * 10
        self._next_id = itertools.count(1)

    def _geo(self):
        return 38.0235 + self.rng.uniform(-0.25, 0.25), -122.095 + self.rng.uniform(-0.25, 0.25)

    def _dates(self):
        d = self.rng.randint(9, 23)
        return f"2015-04-{d:02d}", f"2015-04-{d + self.rng.randint(1, 5):02d}"

    def quote(self, sym):
        lat, lon = self._geo()
        i, o = self._dates()
        self.c.get("/hotels", params={"inDate": i, "outDate": o, "lat": lat, "lon": lon},
                   name="/hotels (quote)")
        return 100.0 + self.rng.uniform(-1, 1)     # synthetic price for bot logic

    def book(self, sym):
        lat, lon = self._geo()
        self.c.get("/recommendations",
                   params={"require": self.rng.choice(["dis", "rate", "price"]),
                           "lat": lat, "lon": lon},
                   name="/recommendations (orderbook)")

    def place(self, sym, side, qty, price=None):
        lat, lon = self._geo()
        i, o = self._dates()
        self.c.post("/reservation", params={
            "inDate": i, "outDate": o, "lat": lat, "lon": lon,
            "hotelId": str(self.rng.randint(1, 80)), "customerName": self.user,
            "username": self.user, "password": self.pw, "number": str(qty)},
            name="/reservation (order)")
        return next(self._next_id)

    def cancel(self, order_id):
        self.status(order_id)          # no cancel endpoint; issue a cheap auth call

    def status(self, order_id):
        self.c.post("/user", params={"username": self.user, "password": self.pw},
                    name="/user (status)")


ADAPTERS = {"exchange": ExchangeAPI, "hotel": HotelReservationAPI}


# ---------------------------------------------------------------------------
# Bots
# ---------------------------------------------------------------------------

class TradingBot(HttpUser):
    abstract = True
    base_wait = (1.0, 2.0)            # think time (s) in calm phases

    def on_start(self):
        self.bot_id = next(_user_ids)
        # Per-bot seeded RNG: each bot's decision sequence is reproducible.
        self.rng = random.Random(scenarios.SEED * 10_000 + self.bot_id)
        self.api = ADAPTERS[TARGET_API](self.client, self.rng, self.bot_id)
        self.symbols = self.rng.sample(SYMBOLS, k=min(3, len(SYMBOLS)))
        self.open_orders: list = []
        self.last_price: dict = {}

    def wait_time(self):
        lo, hi = self.base_wait
        return self.rng.uniform(lo, hi) / scenarios.phase_intensity(current_phase())

    @property
    def news_mode(self) -> bool:
        return current_phase() in scenarios.SURGE_PHASES

    def _track(self, oid):
        if oid is not None:
            self.open_orders.append(oid)
            del self.open_orders[:-20]      # cap memory per bot


class MarketMakerBot(TradingBot):
    """Quotes both sides around mid; cancels and replaces as the price moves."""
    weight = 3
    base_wait = (0.3, 0.8)

    @task(5)
    def requote(self):
        sym = self.rng.choice(self.symbols)
        mid = self.api.quote(sym)
        if mid is None:
            return
        spread = mid * (0.0015 if self.news_mode else 0.0005)   # widen on news
        self._track(self.api.place(sym, "buy", 100, mid - spread))
        self._track(self.api.place(sym, "sell", 100, mid + spread))

    @task(3)
    def cancel_stale(self):
        n = 4 if self.news_mode else 1
        for _ in range(min(n, len(self.open_orders))):
            self.api.cancel(self.open_orders.pop(0))

    @task(1)
    def check_book(self):
        self.api.book(self.rng.choice(self.symbols))


class MomentumBot(TradingBot):
    """Follows price moves; floods market orders when news hits."""
    weight = 5
    base_wait = (1.0, 3.0)

    @task(6)
    def watch(self):
        sym = self.rng.choice(self.symbols)
        price = self.api.quote(sym)
        if price is None:
            return
        prev = self.last_price.get(sym, price)
        self.last_price[sym] = price
        move = (price - prev) / prev if prev else 0.0
        if self.news_mode:
            side = "buy" if move >= 0 else "sell"
            for _ in range(self.rng.randint(2, 5)):
                self._track(self.api.place(sym, side, self.rng.choice([10, 50, 100])))
        elif abs(move) > 0.001 and self.rng.random() < 0.3:
            self._track(self.api.place(sym, "buy" if move > 0 else "sell", 10))

    @task(1)
    def check_fills(self):
        if self.open_orders:
            self.api.status(self.rng.choice(self.open_orders))


class ArbitrageBot(TradingBot):
    """Watches a pair of related symbols and trades the spread."""
    weight = 2
    base_wait = (0.5, 1.5)

    @task
    def scan_pair(self):
        a, b = self.symbols[0], self.symbols[1 % len(self.symbols)]
        pa, pb = self.api.quote(a), self.api.quote(b)
        if pa is None or pb is None:
            return
        threshold = 0.2 if self.news_mode else 0.6      # more opportunities in volatility
        if self.rng.random() > threshold:
            self._track(self.api.place(a, "sell", 50, pa))
            self._track(self.api.place(b, "buy", 50, pb))


# ---------------------------------------------------------------------------
# Load shape: drives bot count from the selected scenario
# ---------------------------------------------------------------------------

class ScenarioShape(LoadTestShape):
    """Selected by SCENARIO env var; stops the test when the scenario ends."""

    def __init__(self):
        super().__init__()
        self._last_phase = None
        self._phase_file = None

    def tick(self):
        s = SCENARIO.at(self.get_run_time())
        if s is None:
            if self._phase_file:
                self._phase_file.close()
            return None
        if s.phase != self._last_phase:
            self._record_phase(s)
        return s.users, s.spawn_rate

    def _record_phase(self, s):
        # Wall-clock phase boundaries, to line Locust/Prometheus data up with
        # the scenario (e.g. as Grafana annotations) during analysis.
        if self._phase_file is None:
            self._phase_file = open(PHASE_LOG, "w", buffering=1)
            self._phase_file.write("unix_ts,run_time_s,phase,target_users\n")
        self._phase_file.write(f"{time.time():.3f},{self.get_run_time():.1f},{s.phase},{s.users}\n")
        log.info("Phase -> %s (%d bots)", s.phase, s.users)
        self._last_phase = s.phase
