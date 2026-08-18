"""Conservative closed-candle market review."""

from __future__ import annotations

from dataclasses import dataclass, asdict

from .model import MarketDataFrame, TIMEFRAMES, validate_generation


ALLOWED_STATES = ("NO_TRADE", "WAIT", "WATCH", "ARMED")


@dataclass
class Review:
    Symbol: str
    Market_Regime: str
    D1_Bias: str
    H4_Bias: str
    H1_Structure: str
    Premium_Discount: str
    Major_Buy_side_Liquidity: str
    Major_Sell_side_Liquidity: str
    Primary_Draw_on_Liquidity: str
    Current_POI: str
    Preferred_Direction: str
    State: str
    Thesis_Status: str
    Confidence_Score: int
    Missing_Evidence: list[str]
    Risk_Conflict: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _bias(frame: MarketDataFrame) -> str:
    candles = frame.closed_candles()
    recent = candles[-20:]
    if recent[-1].close > recent[0].close:
        return "BULLISH"
    if recent[-1].close < recent[0].close:
        return "BEARISH"
    return "RANGE"


def _premium_discount(frame: MarketDataFrame) -> str:
    candles = frame.closed_candles()[-50:]
    high = max(c.high for c in candles)
    low = min(c.low for c in candles)
    midpoint = (high + low) / 2
    return "PREMIUM" if candles[-1].close >= midpoint else "DISCOUNT"


def review_symbol(frames: dict[str, MarketDataFrame], previous_thesis: dict | None = None) -> Review:
    validate_generation(frames)
    ordered = [frames[timeframe] for timeframe in TIMEFRAMES]
    d1_bias = _bias(frames["D1"])
    h4_bias = _bias(frames["H4"])
    h1_bias = _bias(frames["H1"])
    aligned = d1_bias == h4_bias == h1_bias and d1_bias != "RANGE"
    missing = []
    conflicts = []
    if not aligned:
        missing.append("D1/H4/H1 alignment")
        conflicts.append("HTF context is not aligned")
    pd = _premium_discount(frames["H4"])
    preferred = "LONG" if d1_bias == "BULLISH" else "SHORT" if d1_bias == "BEARISH" else "NONE"
    state = "WATCH" if aligned else "WAIT"
    score = 70 if aligned else 35
    if previous_thesis is None:
        thesis_status = "THESIS_INITIALIZED"
    elif previous_thesis.get("previous_bias") == d1_bias:
        thesis_status = "THESIS_MAINTAINED"
    else:
        thesis_status = "THESIS_REVERSED"
    latest = ordered[-1].closed_candles()[-1]
    return Review(
        Symbol=ordered[0].symbol,
        Market_Regime="TREND" if aligned else "TRANSITION",
        D1_Bias=d1_bias,
        H4_Bias=h4_bias,
        H1_Structure=h1_bias,
        Premium_Discount=pd,
        Major_Buy_side_Liquidity=f"{max(c.high for c in frames['D1'].closed_candles()[-50:]):.2f}",
        Major_Sell_side_Liquidity=f"{min(c.low for c in frames['D1'].closed_candles()[-50:]):.2f}",
        Primary_Draw_on_Liquidity="buy-side" if preferred == "LONG" else "sell-side" if preferred == "SHORT" else "undetermined",
        Current_POI=f"{latest.close:.2f}",
        Preferred_Direction=preferred,
        State=state,
        Thesis_Status=thesis_status,
        Confidence_Score=score,
        Missing_Evidence=missing,
        Risk_Conflict=conflicts,
    )
