"""Bot traders.

Every bot has a *bias* (which side it trades) and a *strategy* (how it picks the
target price it trades around). All bots share one ``Bot`` class; each strategy
has its own ``target_*`` method, picked in ``Bot.update_price``.

Biases (``bot.bias``):
    bull   buys when the price is at or below its target
    bear   sells when the price is at or above its target
    both   two-sided market maker skewed towards its target

Strategies (``STRATEGIES``, ``bot.strategy``):
    uniform      fixed target, drawn uniformly around the price at creation
    normal       fixed target, drawn from a normal distribution around the price
    follow       target is the latest price
    history_avg  target is the average of the last ``window`` traded prices
    momentum     target extrapolates the recent trend in past prices

follow / history_avg / momentum add ``offset`` (bull > 0, bear < 0) on top of the price they
compute; it fades by ``OFFSET_DECAY`` on every update.

Use ``create_bot`` to build bots; it samples the bias, target and strategy parameters.
"""

import random

from main import _BOT_CFG, SHORT_MAINT_MARGIN_RATE, Player

STRATEGIES = ("uniform", "normal", "follow", "history_avg", "momentum")
BIASES = ("bull", "bear", "both")

# Share of bull/bear bots that are created as a random uniform bot instead.
RANDOM_FALLBACK_PROB = 0.01
# Bull/bear bots start this far above/below the current price.
BIAS_OFFSET_RANGE = (2.0, 10.0)
# Updating bots' offset is multiplied by this on every update (every tick).
OFFSET_DECAY = 0.998
# Momentum targets lead the latest price by at most this much.
MAX_TREND = 1.0
# Bots only look at this many recent prices.
MAX_LOOKBACK = 300


def round_price(p: float) -> float:
    return round(max(0.01, p), 2)


def price_history(market, s) -> list[float]:
    """Recent traded prices of stock s, oldest first."""
    return [p for _, p in market.history.get(s.name, [])[-MAX_LOOKBACK:]] or [s.price]


def update_prices(bots, market, s) -> None:
    """Call once per tick: update every bot's target price."""
    history = price_history(market, s)
    for bot in bots:
        bot.update_price(history)


class Bot(Player):
    """A bot trader. ``strategy`` picks which ``target_*`` method sets its target."""

    def __init__(
        self,
        *,
        strategy: str = "uniform",
        bias: str,
        target_price: float,
        offset: float = 0.0,
        window: int = 60,
        lookback: int = 30,
        gain: float = 0.5,
        **kwargs,
    ):
        super().__init__(is_bot=True, **kwargs)
        self.strategy = strategy
        self.bias = bias
        self.target_price = round_price(target_price)
        self.offset = offset
        self.window = window      # history_avg: number of prices to average
        self.lookback = lookback  # momentum: how many prices back to measure the trend
        self.gain = gain          # momentum: how strongly the trend is extrapolated

    # --- target price functions, one per strategy ---------------------------

    def target_follow(self, history: list[float]) -> float:
        return history[-1]

    def target_history_avg(self, history: list[float]) -> float:
        recent = history[-self.window:]
        return sum(recent) / len(recent)

    def target_momentum(self, history: list[float]) -> float:
        past = history[-min(self.lookback, len(history))]
        trend = max(-MAX_TREND, min(MAX_TREND, self.gain * (history[-1] - past)))
        return history[-1] + trend

    def update_price(self, history: list[float]) -> None:
        """Recompute the target from recent prices. Called every tick."""
        if self.strategy in ("uniform", "normal"):
            pass  # fixed target
        else:
            if self.strategy == "follow":
                price = self.target_follow(history)
            elif self.strategy == "history_avg":
                price = self.target_history_avg(history)
            elif self.strategy == "momentum":
                price = self.target_momentum(history)
            self.offset *= OFFSET_DECAY
            self.target_price = round_price(price + self.offset)

    # --- trading -------------------------------------------------------------

    def place_order(self, market, s, t: float) -> None:
        ref = market.reference_price(s)
        target = self.target_price
        spread = _BOT_CFG.get("price_spread", 2.0)
        amount = random.randint(_BOT_CFG.get("min_order_qty", 1), _BOT_CFG.get("max_order_qty", 10))

        if self.bias == "bull":
            if ref <= target:
                self.buy(market, s, round_price(ref + random.uniform(-spread * 0.6, spread)), amount, t=t)
            elif random.random() < 0.15:  # passive bid keeps the bid book alive
                self.buy(market, s, round_price((ref + target) / 2 + random.uniform(-spread, 0)), amount, t=t)
        elif self.bias == "bear":
            if ref >= target:
                self.sell(market, s, round_price(ref + random.uniform(-spread, spread * 0.6)), amount, t=t)
            elif random.random() < 0.15:
                self.sell(market, s, round_price((ref + target) / 2 + random.uniform(0, spread)), amount, t=t)
        else:
            buy_prob = max(0.15, min(0.85, 0.5 + (target - ref) / max(spread * 4, 1.0)))
            price = round_price(ref + random.uniform(-spread * 0.75, spread * 0.75))
            if random.random() < buy_prob:
                self.buy(market, s, price, amount, t=t)
            else:
                self.sell(market, s, price, amount, t=t)

    def cancel_order(self, market, t: float) -> None:
        """Drop this bot's stale orders and keep its book presence small."""
        market.prune_bot_orders(self, now_t=t)

    def act(self, market, s, t: float = 0.0) -> None:
        self.cancel_order(market, t)

        qty = self.get_qty(s)
        if qty < 0 and self.equity(s) < abs(qty) * s.price * SHORT_MAINT_MARGIN_RATE:
            # Near the maintenance margin: cover instead of trading.
            market.submitMarketOrder(s, "buy", min(abs(qty), _BOT_CFG.get("max_order_qty", 10)), player=self, t=t)
            return

        self.place_order(market, s, t)


def create_bot(
    strategy: str = "uniform",
    *,
    id,
    curr_price: float,
    bias: str = "random",
    order_freq: float | None = None,
    stock: list[dict] | None = None,
) -> Bot:
    """Create a bot with the given strategy.

    bias "bull" / "bear" makes a buyer / seller targeting above / below the
    current price (1% of those come out as a random uniform bot instead);
    "random" picks any bias with a small target offset.
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy {strategy!r}; expected one of {STRATEGIES}")
    if bias not in ("random", "bull", "bear"):
        raise ValueError(f"Unknown bias {bias!r}; expected random, bull or bear")

    if bias != "random" and random.random() < RANDOM_FALLBACK_PROB:
        strategy, bias = "uniform", "random"

    lo, hi = BIAS_OFFSET_RANGE
    if bias == "random":
        bias = random.choice(BIASES)
        if strategy == "normal":
            offset = random.gauss(0.0, _BOT_CFG.get("target_price_std", 3.0))
        else:
            spread = _BOT_CFG.get("target_price_range", 5.0)
            offset = random.uniform(-spread, spread)
    else:
        if strategy == "normal":
            magnitude = max(0.5, random.gauss((lo + hi) / 2, (hi - lo) / 4))
        else:
            magnitude = random.uniform(lo, hi)
        offset = magnitude if bias == "bull" else -magnitude

    kwargs = dict(id=id, stock=stock, order_freq=order_freq, strategy=strategy, bias=bias,
                  target_price=curr_price + offset, offset=offset)
    if strategy == "history_avg":
        kwargs["window"] = random.choice((30, 60, 120, 300))
    if strategy == "momentum":
        kwargs["lookback"] = random.choice((10, 30, 60))
        kwargs["gain"] = random.uniform(0.25, 1.0)
    return Bot(**kwargs)
