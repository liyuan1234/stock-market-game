"""Bot traders.

Every bot has a *side* (what it trades) and a *price model* (how it picks the
target price it trades around).

Sides (stored in ``strategy["type"]``):
    buy_below   bullish: buys when the price is at or below its target
    sell_above  bearish: sells when the price is at or above its target
    buy_sell    two-sided market maker skewed towards its target

Price models (``BOT_TYPES``, stored in ``strategy["model"]``):
    uniform      fixed target, drawn uniformly around the price at creation
    normal       fixed target, drawn from a normal distribution around the price
    follow       target tracks the current price (exponential moving average)
    history_avg  target is the average of historical prices over a time window
    momentum     target extrapolates the recent trend in past prices

Use ``create_bot`` to build bots; it picks the class and samples parameters.
"""

import bisect
import math
import random

from main import _BOT_CFG, SHORT_MAINT_MARGIN_RATE, Player

BOT_TYPES = ("uniform", "normal", "follow", "history_avg", "momentum")
BIASES = ("random", "bull", "bear")
SIDES = ("buy_below", "sell_above", "buy_sell")

# Share of bull/bear bots that are created as a random uniform bot instead.
RANDOM_FALLBACK_PROB = 0.01

# Bull/bear bots start this far above/below the current price.
BIAS_OFFSET_RANGE = (2.0, 10.0)

# Momentum targets lead the price by at most this many price_spreads.
MOMENTUM_CAP_SPREADS = 0.5

# Price-updating bots' bull/bear offset fades with this time constant (sim seconds),
# so a bullish follow bot pushes the price for a while instead of buying forever.
OFFSET_DECAY_TAU = 30.0


def _price(p: float) -> float:
    return round(max(0.01, p), 2)


def _history(market, s) -> list[tuple[float, float]]:
    return market.history.get(s.name) or [(0.0, s.price)]


def price_at(history: list[tuple[float, float]], t: float) -> float:
    """Last traded price at or before sim time t."""
    idx = bisect.bisect_right(history, t, key=lambda pt: pt[0]) - 1
    return history[max(idx, 0)][1]


def mean_price_since(history: list[tuple[float, float]], t0: float, max_samples: int = 100) -> float:
    """Average traded price since sim time t0 (sampled to keep it cheap)."""
    lo = bisect.bisect_left(history, t0, key=lambda pt: pt[0])
    pts = history[lo:] or history[-1:]
    stride = max(1, len(pts) // max_samples)
    sample = pts[::stride]
    return sum(p for _, p in sample) / len(sample)


class Bot(Player):
    """Base bot with a fixed target price."""

    bot_type = "uniform"

    def __init__(self, *, side: str, target_price: float, offset: float = 0.0, **kwargs):
        super().__init__(
            strategy={"type": side, "model": self.bot_type, "target_price": _price(target_price)},
            is_bot=True,
            **kwargs,
        )
        # Distance from the model's anchor price (bull > 0, bear < 0); used by updating bots.
        self.offset = offset

    def update_target(self, market, s, ref: float, t: float) -> float:
        """Fixed target. Only drift after extreme shocks so the market can't freeze."""
        target = self.strategy["target_price"]
        p_spread = _BOT_CFG.get("price_spread", 2.0)
        if abs(ref - target) > p_spread * 2.5:
            target = round(max(0.05, target + 0.02 * (ref - target)), 2)
            self.strategy["target_price"] = target
        return target

    def act(self, market, s, t=0.0):
        """Submit an order around the reference price according to side and target."""
        market.prune_bot_orders(self, now_t=t)

        qty = self.get_qty(s)
        # Automatic margin cover if deeply short and approaching maintenance threshold
        if qty < 0 and self.equity(s) < abs(qty) * s.price * SHORT_MAINT_MARGIN_RATE:
            cover_qty = max(1, min(abs(qty), _BOT_CFG.get("max_order_qty", 10)))
            market.submitMarketOrder(s, "buy", cover_qty, player=self, t=t)
            return

        ref = market.reference_price(s)
        target = self.update_target(market, s, ref, t)
        stype = self.strategy["type"]
        p_spread = _BOT_CFG.get("price_spread", 2.0)

        amount = random.randint(
            _BOT_CFG.get("min_order_qty", 1),
            _BOT_CFG.get("max_order_qty", 10),
        )

        if stype == "buy_below":
            if ref <= target:
                self.buy(market, s, _price(ref + random.uniform(-p_spread * 0.6, p_spread)), amount, t=t)
            elif random.random() < 0.15:
                # Passive value bid near target keeps bid book alive
                self.buy(market, s, _price((ref + target) * 0.5 + random.uniform(-p_spread, 0)), amount, t=t)
        elif stype == "sell_above":
            if ref >= target:
                self.sell(market, s, _price(ref + random.uniform(-p_spread, p_spread * 0.6)), amount, t=t)
            elif random.random() < 0.15:
                self.sell(market, s, _price((ref + target) * 0.5 + random.uniform(0, p_spread)), amount, t=t)
        else:
            # buy_sell: two-sided market maker skewed by target
            diff = target - ref
            buy_prob = max(0.15, min(0.85, 0.5 + diff / max(p_spread * 4.0, 1.0)))
            price = _price(ref + random.uniform(-p_spread * 0.75, p_spread * 0.75))
            if random.random() < buy_prob:
                self.buy(market, s, price, amount, t=t)
            else:
                self.sell(market, s, price, amount, t=t)


class UpdatingBot(Bot):
    """Base for bots whose target is recomputed from the market on every action."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._offset_t = None

    def current_offset(self, t: float) -> float:
        if self._offset_t is not None and self.offset:
            self.offset *= math.exp(-max(0.0, t - self._offset_t) / OFFSET_DECAY_TAU)
        self._offset_t = t
        return self.offset

    def anchor(self, market, s, ref: float, t: float) -> float:
        raise NotImplementedError

    def update_target(self, market, s, ref, t):
        self.strategy["target_price"] = _price(self.anchor(market, s, ref, t) + self.current_offset(t))
        return self.strategy["target_price"]


class UniformBot(Bot):
    bot_type = "uniform"


class NormalBot(Bot):
    bot_type = "normal"


class FollowBot(UpdatingBot):
    """Target follows the current price with an exponential moving average."""

    bot_type = "follow"

    def __init__(self, *, alpha: float, **kwargs):
        super().__init__(**kwargs)
        self.alpha = alpha
        self._ema = self.strategy["target_price"] - self.offset

    def anchor(self, market, s, ref, t):
        self._ema += self.alpha * (ref - self._ema)
        return self._ema


class HistoryAvgBot(UpdatingBot):
    """Target is the average traded price over the last `window` sim seconds."""

    bot_type = "history_avg"

    def __init__(self, *, window: float, **kwargs):
        super().__init__(**kwargs)
        self.window = window

    def anchor(self, market, s, ref, t):
        return mean_price_since(_history(market, s), t - self.window)


class MomentumBot(UpdatingBot):
    """Target extrapolates the price trend over the last `lookback` sim seconds.

    The trend is added to the recent average (not the current price) and capped,
    so a market full of momentum bots trends without running away.
    """

    bot_type = "momentum"

    def __init__(self, *, lookback: float, gain: float, **kwargs):
        super().__init__(**kwargs)
        self.lookback = lookback
        self.gain = gain

    def anchor(self, market, s, ref, t):
        hist = _history(market, s)
        since = t - self.lookback
        cap = _BOT_CFG.get("price_spread", 2.0) * MOMENTUM_CAP_SPREADS
        trend = max(-cap, min(cap, self.gain * (ref - price_at(hist, since))))
        return mean_price_since(hist, since) + trend


_CLASSES = {cls.bot_type: cls for cls in (UniformBot, NormalBot, FollowBot, HistoryAvgBot, MomentumBot)}


def _side_and_offset(bot_type: str, bias: str) -> tuple[str, float]:
    spread = _BOT_CFG.get("target_price_range", 5.0)
    std = _BOT_CFG.get("target_price_std", 3.0)
    lo, hi = BIAS_OFFSET_RANGE
    if bias == "bull":
        side, sign = "buy_below", 1.0
    elif bias == "bear":
        side, sign = "sell_above", -1.0
    else:
        side = random.choice(SIDES)
        offset = random.gauss(0.0, std) if bot_type == "normal" else random.uniform(-spread, spread)
        return side, offset

    if bot_type == "normal":
        magnitude = max(0.5, random.gauss((lo + hi) / 2, (hi - lo) / 4))
    else:
        magnitude = random.uniform(lo, hi)
    return side, sign * magnitude


def create_bot(
    bot_type: str = "uniform",
    *,
    id,
    curr_price: float,
    bias: str = "random",
    order_freq: float | None = None,
    stock: list[dict] | None = None,
) -> Bot:
    """Create a bot of the given price model.

    bias "bull" / "bear" makes a buyer / seller targeting above / below the
    current price; 1% of those come out as a random uniform bot instead.
    """
    if bot_type not in _CLASSES:
        raise ValueError(f"Unknown bot type {bot_type!r}; expected one of {BOT_TYPES}")
    if bias not in BIASES:
        raise ValueError(f"Unknown bias {bias!r}; expected one of {BIASES}")

    if bias != "random" and random.random() < RANDOM_FALLBACK_PROB:
        bot_type, bias = "uniform", "random"

    side, offset = _side_and_offset(bot_type, bias)
    common = dict(
        id=id,
        stock=stock,
        order_freq=order_freq,
        side=side,
        target_price=curr_price + offset,
        offset=offset,
    )
    if bot_type == "follow":
        return FollowBot(alpha=random.uniform(0.05, 0.3), **common)
    if bot_type == "history_avg":
        return HistoryAvgBot(window=random.choice((30.0, 60.0, 120.0, 300.0)), **common)
    if bot_type == "momentum":
        return MomentumBot(lookback=random.choice((10.0, 30.0, 60.0)), gain=random.uniform(0.25, 1.0), **common)
    return _CLASSES[bot_type](**common)
