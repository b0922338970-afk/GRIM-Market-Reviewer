"""Closed-candle structure, liquidity, and POI market review."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .model import Candle, MarketDataFrame, TIMEFRAMES, validate_generation


ALLOWED_STATES = ("NO_TRADE", "WAIT", "WATCH", "ARMED")
HTF_TIMEFRAMES = ("D1", "H4", "H1")
TRIGGER_TIMEFRAMES = ("M15", "M5")


@dataclass(frozen=True)
class Swing:
    kind: str
    price: float
    formed_at: int
    label: str = "UNCLASSIFIED"


@dataclass(frozen=True)
class Structure:
    timeframe: str
    state: str
    last_bos: str
    last_mss: str
    protected_high: float | None
    protected_low: float | None
    last_swing_high: Swing | None
    last_swing_low: Swing | None
    swings: list[Swing]


@dataclass(frozen=True)
class LiquidityLevel:
    price: float
    type: str
    timeframe: str
    formed_at: int
    status: str


@dataclass(frozen=True)
class FairValueGap:
    upper: float
    lower: float
    midpoint: float
    direction: str
    timeframe: str
    formed_at: int
    status: str


@dataclass(frozen=True)
class OrderBlock:
    high: float
    low: float
    midpoint: float
    direction: str
    timeframe: str
    formed_at: int
    status: str


@dataclass
class Review:
    Symbol: str
    Market_Regime: str
    D1_Bias: str
    H4_Bias: str
    H1_Structure: str
    Structure_State: dict[str, str]
    Last_BOS: dict[str, str]
    Last_MSS: dict[str, str]
    Protected_High: dict[str, str]
    Protected_Low: dict[str, str]
    Premium_Discount: str
    Major_Buy_side_Liquidity: str
    Major_Sell_side_Liquidity: str
    Primary_Draw_on_Liquidity: str
    Current_POI: str
    Preferred_Direction: str
    State: str
    Thesis_Status: str
    Confidence: str
    Missing_Evidence: list[str]
    Risk_Conflict: list[str]
    Liquidity: list[dict]
    FVG: list[dict]
    Order_Blocks: list[dict]

    def to_dict(self) -> dict:
        return asdict(self)


def _confirmed_swings(candles: list[Candle], window: int = 2) -> list[Swing]:
    swings: list[Swing] = []
    for index in range(window, len(candles) - window):
        candle = candles[index]
        peers = candles[index - window:index] + candles[index + 1:index + 1 + window]
        if all(candle.high > peer.high for peer in peers):
            swings.append(Swing("HIGH", candle.high, candle.timestamp))
        if all(candle.low < peer.low for peer in peers):
            swings.append(Swing("LOW", candle.low, candle.timestamp))
    return _label_swings(swings)


def _label_swings(swings: list[Swing]) -> list[Swing]:
    labelled: list[Swing] = []
    last_high: Swing | None = None
    last_low: Swing | None = None
    for swing in swings:
        if swing.kind == "HIGH":
            label = "HH" if last_high and swing.price > last_high.price else "LH" if last_high else "HIGH"
            last_high = swing
        else:
            label = "HL" if last_low and swing.price > last_low.price else "LL" if last_low else "LOW"
            last_low = swing
        labelled.append(Swing(swing.kind, swing.price, swing.formed_at, label))
    return labelled


def analyze_structure(frame: MarketDataFrame) -> Structure:
    candles = frame.closed_candles()
    swings = _confirmed_swings(candles)
    swing_highs = [s for s in swings if s.kind == "HIGH"]
    swing_lows = [s for s in swings if s.kind == "LOW"]
    last_bos = "NONE"
    last_mss = "NONE"
    active_direction = "RANGE"
    high_cursor: Swing | None = None
    low_cursor: Swing | None = None
    protected_high: float | None = None
    protected_low: float | None = None
    swings_by_time = {}
    for swing in swings:
        swings_by_time.setdefault(swing.formed_at, []).append(swing)

    for candle in candles:
        for swing in swings_by_time.get(candle.timestamp, []):
            if swing.kind == "HIGH":
                high_cursor = swing
            else:
                low_cursor = swing
        if high_cursor and candle.timestamp > high_cursor.formed_at and candle.close > high_cursor.price:
            last_bos = "BULLISH"
            if active_direction == "BEARISH":
                last_mss = "BULLISH"
            active_direction = "BULLISH"
            if low_cursor:
                protected_low = low_cursor.price
        if low_cursor and candle.timestamp > low_cursor.formed_at and candle.close < low_cursor.price:
            last_bos = "BEARISH"
            if active_direction == "BULLISH":
                last_mss = "BEARISH"
            active_direction = "BEARISH"
            if high_cursor:
                protected_high = high_cursor.price

    if last_bos == "NONE":
        recent = [s.label for s in swings[-4:]]
        if {"HH", "HL"}.issubset(recent):
            active_direction = "BULLISH"
        elif {"LH", "LL"}.issubset(recent):
            active_direction = "BEARISH"
        else:
            active_direction = "RANGE"

    return Structure(
        timeframe=frame.timeframe,
        state=active_direction,
        last_bos=last_bos,
        last_mss=last_mss,
        protected_high=protected_high,
        protected_low=protected_low,
        last_swing_high=swing_highs[-1] if swing_highs else None,
        last_swing_low=swing_lows[-1] if swing_lows else None,
        swings=swings,
    )


def _status_for_level(candles: list[Candle], level: LiquidityLevel) -> str:
    later = [c for c in candles if c.timestamp > level.formed_at]
    if not later:
        return "UNSWEPT"
    if "Buy-side" in level.type or "Highs" in level.type:
        swept = [c for c in later if c.high > level.price]
        if not swept:
            return "UNSWEPT"
        return "RECLAIMED" if any(c.close < level.price for c in swept) else "SWEPT"
    swept = [c for c in later if c.low < level.price]
    if not swept:
        return "UNSWEPT"
    return "RECLAIMED" if any(c.close > level.price for c in swept) else "SWEPT"


def _equal_levels(frame: MarketDataFrame, swings: list[Swing]) -> list[LiquidityLevel]:
    levels: list[LiquidityLevel] = []
    tolerance = max(frame.closed_candles()[-1].close * 0.001, 0.01)
    for kind, level_type in (("HIGH", "Equal Highs"), ("LOW", "Equal Lows")):
        side = [s for s in swings if s.kind == kind]
        for first, second in zip(side, side[1:]):
            if abs(first.price - second.price) <= tolerance:
                price = (first.price + second.price) / 2
                levels.append(LiquidityLevel(price, level_type, frame.timeframe, second.formed_at, "UNSWEPT"))
    return levels[-3:]


def find_liquidity(frame: MarketDataFrame, structure: Structure) -> list[LiquidityLevel]:
    candles = frame.closed_candles()
    levels: list[LiquidityLevel] = []
    if structure.last_swing_high:
        level_type = "External Buy-side Liquidity" if frame.timeframe in HTF_TIMEFRAMES else "Internal Buy-side Liquidity"
        levels.append(LiquidityLevel(structure.last_swing_high.price, level_type, frame.timeframe, structure.last_swing_high.formed_at, "UNSWEPT"))
    if structure.last_swing_low:
        level_type = "External Sell-side Liquidity" if frame.timeframe in HTF_TIMEFRAMES else "Internal Sell-side Liquidity"
        levels.append(LiquidityLevel(structure.last_swing_low.price, level_type, frame.timeframe, structure.last_swing_low.formed_at, "UNSWEPT"))
    levels.extend(_equal_levels(frame, structure.swings))
    return [
        LiquidityLevel(level.price, level.type, level.timeframe, level.formed_at, _status_for_level(candles, level))
        for level in levels
    ]


def find_fvgs(frame: MarketDataFrame) -> list[FairValueGap]:
    candles = frame.closed_candles()
    gaps: list[FairValueGap] = []
    for first, _, third in zip(candles, candles[1:], candles[2:]):
        if third.low > first.high:
            gaps.append(_fvg_status(frame, "BULLISH", first.high, third.low, third.timestamp))
        if third.high < first.low:
            gaps.append(_fvg_status(frame, "BEARISH", third.high, first.low, third.timestamp))
    return gaps[-10:]


def _fvg_status(frame: MarketDataFrame, direction: str, lower: float, upper: float, formed_at: int) -> FairValueGap:
    later = [c for c in frame.closed_candles() if c.timestamp > formed_at]
    midpoint = (upper + lower) / 2
    if direction == "BULLISH":
        touched = [c for c in later if c.low <= upper]
        filled = any(c.low <= lower for c in later)
        invalidated = any(c.close < lower for c in later)
    else:
        touched = [c for c in later if c.high >= lower]
        filled = any(c.high >= upper for c in later)
        invalidated = any(c.close > upper for c in later)
    status = "INVALIDATED" if invalidated else "FILLED" if filled else "PARTIALLY_FILLED" if touched else "OPEN"
    return FairValueGap(upper, lower, midpoint, direction, frame.timeframe, formed_at, status)


def _is_displacement(candles: list[Candle], index: int, direction: str) -> bool:
    if index < 20:
        return False
    candle = candles[index]
    average_range = sum(c.high - c.low for c in candles[index - 20:index]) / 20
    body = abs(candle.close - candle.open)
    if average_range <= 0 or body < average_range:
        return False
    if direction == "BULLISH":
        return candle.close > candle.open and candle.close >= candle.low + (candle.high - candle.low) * 0.65
    return candle.close < candle.open and candle.close <= candle.low + (candle.high - candle.low) * 0.35


def find_order_blocks(frame: MarketDataFrame, structure: Structure) -> list[OrderBlock]:
    if structure.last_bos == "NONE":
        return []
    candles = frame.closed_candles()
    blocks: list[OrderBlock] = []
    for index, _ in enumerate(candles):
        if not _is_displacement(candles, index, structure.last_bos):
            continue
        if structure.last_bos == "BULLISH":
            candidates = [c for c in candles[max(0, index - 10):index] if c.close < c.open]
        else:
            candidates = [c for c in candles[max(0, index - 10):index] if c.close > c.open]
        if not candidates:
            continue
        source = candidates[-1]
        blocks.append(OrderBlock(source.high, source.low, (source.high + source.low) / 2, structure.last_bos, frame.timeframe, source.timestamp, "OPEN"))
    return blocks[-5:]


def _bias_from_structure(structure: Structure) -> str:
    return structure.state if structure.state in {"BULLISH", "BEARISH"} else "RANGE"


def _format_price(value: float | None) -> str:
    return "NONE" if value is None else f"{value:.2f}"


def _premium_discount(frame: MarketDataFrame, structure: Structure) -> str:
    if not structure.last_swing_high or not structure.last_swing_low:
        return "UNKNOWN"
    midpoint = (structure.last_swing_high.price + structure.last_swing_low.price) / 2
    return "PREMIUM" if frame.closed_candles()[-1].close >= midpoint else "DISCOUNT"


def _current_price(frames: dict[str, MarketDataFrame]) -> float:
    return frames["M5"].closed_candles()[-1].close


def _active_pois(price: float, fvgs: list[FairValueGap], obs: list[OrderBlock]) -> list[str]:
    pois: list[str] = []
    for gap in fvgs:
        if gap.status in {"OPEN", "PARTIALLY_FILLED"} and gap.lower <= price <= gap.upper:
            pois.append(f"{gap.direction} FVG {gap.timeframe} {gap.lower:.2f}-{gap.upper:.2f}")
    for block in obs:
        if block.status == "OPEN" and block.low <= price <= block.high:
            pois.append(f"{block.direction} OB {block.timeframe} {block.low:.2f}-{block.high:.2f}")
    return pois


def _primary_draw(bias: str, liquidity: list[LiquidityLevel], price: float) -> tuple[str, list[str]]:
    unswept = [level for level in liquidity if level.status == "UNSWEPT"]
    if bias == "BULLISH":
        candidates = [level for level in unswept if "Buy-side" in level.type and level.price > price]
        other_side = [level for level in unswept if "Sell-side" in level.type and level.price < price]
        missing = "unswept buy-side liquidity above price"
    elif bias == "BEARISH":
        candidates = [level for level in unswept if "Sell-side" in level.type and level.price < price]
        other_side = [level for level in unswept if "Buy-side" in level.type and level.price > price]
        missing = "unswept sell-side liquidity below price"
    else:
        return "undetermined: HTF structure is not directional", ["HTF structural bias"]
    if not candidates:
        return f"undetermined: no {missing}", [missing]
    ranked = sorted(candidates, key=lambda level: (0 if level.timeframe in HTF_TIMEFRAMES else 1, abs(level.price - price)))
    chosen = ranked[0]
    reason = (
        f"{chosen.type} at {chosen.price:.2f} on {chosen.timeframe}; "
        f"favored over opposite-side unswept count {len(other_side)} because it aligns with {bias} HTF structure"
    )
    return reason, []


def _liquidity_interaction(price: float, liquidity: list[LiquidityLevel]) -> bool:
    for level in liquidity:
        tolerance = max(price * 0.002, 0.01)
        if abs(level.price - price) <= tolerance or level.status in {"SWEPT", "RECLAIMED"}:
            return True
    return False


def _has_displacement(frames: dict[str, MarketDataFrame], direction: str) -> bool:
    for timeframe in TRIGGER_TIMEFRAMES:
        candles = frames[timeframe].closed_candles()
        start = max(0, len(candles) - 10)
        if any(_is_displacement(candles, index, direction) for index in range(start, len(candles))):
            return True
    return False


def _state_and_missing(
    htf_bias: str,
    price: float,
    liquidity: list[LiquidityLevel],
    pois: list[str],
    structures: dict[str, Structure],
    frames: dict[str, MarketDataFrame],
) -> tuple[str, list[str], list[str]]:
    missing: list[str] = []
    conflicts: list[str] = []
    if htf_bias == "RANGE":
        return "NO_TRADE", ["D1/H4/H1 structural thesis"], ["HTF structure is not directional"]
    if any(structures[tf].state not in {htf_bias, "RANGE"} for tf in TRIGGER_TIMEFRAMES):
        conflicts.append("Trigger timeframe structure conflicts with HTF thesis")
    _, draw_missing = _primary_draw(htf_bias, liquidity, price)
    missing.extend(draw_missing)
    if not pois:
        missing.append("price inside valid FVG or OB POI")
    if not _liquidity_interaction(price, liquidity):
        missing.append("meaningful liquidity interaction")
    swept = any(level.status in {"SWEPT", "RECLAIMED"} for level in liquidity)
    if not swept:
        missing.append("liquidity sweep")
    if not _has_displacement(frames, htf_bias):
        missing.append(f"{htf_bias.lower()} displacement")
    trigger_break = any(structures[tf].last_mss == htf_bias or structures[tf].last_bos == htf_bias for tf in TRIGGER_TIMEFRAMES)
    if not trigger_break:
        missing.append(f"{htf_bias.lower()} M15/M5 MSS or BOS")
    if not missing and swept:
        return "ARMED", missing, conflicts
    if pois or _liquidity_interaction(price, liquidity):
        return "WATCH", missing, conflicts
    return "WAIT", missing, conflicts


def _thesis_status(previous_thesis: dict | None, d1_bias: str) -> str:
    if previous_thesis is None:
        return "THESIS_INITIALIZED"
    if previous_thesis.get("previous_bias") == d1_bias:
        return "THESIS_MAINTAINED"
    return "THESIS_REVERSED"


def review_symbol(frames: dict[str, MarketDataFrame], previous_thesis: dict | None = None) -> Review:
    validate_generation(frames)
    structures = {timeframe: analyze_structure(frames[timeframe]) for timeframe in TIMEFRAMES}
    liquidity = [level for timeframe in TIMEFRAMES for level in find_liquidity(frames[timeframe], structures[timeframe])]
    fvgs = [gap for timeframe in TIMEFRAMES for gap in find_fvgs(frames[timeframe])]
    order_blocks = [block for timeframe in TIMEFRAMES for block in find_order_blocks(frames[timeframe], structures[timeframe])]
    d1_bias = _bias_from_structure(structures["D1"])
    h4_bias = _bias_from_structure(structures["H4"])
    h1_bias = _bias_from_structure(structures["H1"])
    htf_aligned = d1_bias == h4_bias == h1_bias and d1_bias != "RANGE"
    htf_bias = d1_bias if htf_aligned else "RANGE"
    price = _current_price(frames)
    pois = _active_pois(price, fvgs, order_blocks)
    primary_draw, draw_missing = _primary_draw(htf_bias, liquidity, price)
    state, missing, conflicts = _state_and_missing(htf_bias, price, liquidity, pois, structures, frames)
    for item in draw_missing:
        if item not in missing:
            missing.append(item)
    major_buy = next((level for level in liquidity if "External Buy-side" in level.type and level.status == "UNSWEPT"), None)
    major_sell = next((level for level in liquidity if "External Sell-side" in level.type and level.status == "UNSWEPT"), None)
    return Review(
        Symbol=frames["D1"].symbol,
        Market_Regime="TREND" if htf_aligned else "RANGE_OR_TRANSITION",
        D1_Bias=d1_bias,
        H4_Bias=h4_bias,
        H1_Structure=h1_bias,
        Structure_State={tf: structures[tf].state for tf in TIMEFRAMES},
        Last_BOS={tf: structures[tf].last_bos for tf in TIMEFRAMES},
        Last_MSS={tf: structures[tf].last_mss for tf in TIMEFRAMES},
        Protected_High={tf: _format_price(structures[tf].protected_high) for tf in TIMEFRAMES},
        Protected_Low={tf: _format_price(structures[tf].protected_low) for tf in TIMEFRAMES},
        Premium_Discount=_premium_discount(frames["H4"], structures["H4"]),
        Major_Buy_side_Liquidity=_format_price(major_buy.price if major_buy else None),
        Major_Sell_side_Liquidity=_format_price(major_sell.price if major_sell else None),
        Primary_Draw_on_Liquidity=primary_draw,
        Current_POI="; ".join(pois) if pois else "NONE",
        Preferred_Direction="LONG" if htf_bias == "BULLISH" else "SHORT" if htf_bias == "BEARISH" else "NONE",
        State=state,
        Thesis_Status=_thesis_status(previous_thesis, d1_bias),
        Confidence="UNCALIBRATED",
        Missing_Evidence=missing,
        Risk_Conflict=conflicts,
        Liquidity=[asdict(level) for level in liquidity[-20:]],
        FVG=[asdict(gap) for gap in fvgs[-20:]],
        Order_Blocks=[asdict(block) for block in order_blocks[-10:]],
    )
