"""Closed-candle market phase, structure, liquidity, and POI review."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .model import Candle, MarketDataFrame, TIMEFRAMES, validate_generation


ALLOWED_STATES = ("NO_TRADE", "WAIT", "WATCH", "ARMED")
HTF_TIMEFRAMES = ("D1", "H4")
TACTICAL_TIMEFRAMES = ("H1", "M15", "M5")
TRIGGER_TIMEFRAMES = ("M15", "M5")
PHASES = ("CONTINUATION", "PULLBACK", "REVERSAL_CANDIDATE", "RANGE", "TRANSITION")


@dataclass(frozen=True)
class Swing:
    kind: str
    price: float
    formed_at: int
    label: str = "UNCLASSIFIED"


@dataclass(frozen=True)
class BreakEvent:
    direction: str
    price: float
    timestamp: int
    kind: str


@dataclass(frozen=True)
class Structure:
    timeframe: str
    state: str
    last_bos: BreakEvent | None
    last_mss: BreakEvent | None
    protected_high: float | None
    protected_low: float | None
    structural_invalidation_price: float | None
    structural_invalidation_type: str
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
    touch_count: int
    displacement_strength: float


@dataclass(frozen=True)
class OrderBlock:
    high: float
    low: float
    midpoint: float
    direction: str
    timeframe: str
    formed_at: int
    status: str
    touch_count: int
    displacement_strength: float


@dataclass(frozen=True)
class POI:
    label: str
    zone_type: str
    direction: str
    timeframe: str
    lower: float
    upper: float
    midpoint: float
    formed_at: int
    status: str
    quality: dict
    score: float


@dataclass
class Review:
    Symbol: str
    Swing_Bias: str
    Current_Phase: str
    Market_Regime: str
    D1_Structure: str
    H4_Structure: str
    H1_Structure: str
    Structural_Invalidation: dict[str, str]
    Macro_Draw_on_Liquidity: str
    Tactical_Draw_on_Liquidity: str
    Premium_Discount: str
    Primary_POI: str
    Secondary_POI: str
    POI_Conflict: str
    Preferred_Direction: str
    State: str
    Thesis_Status: str
    Confidence: str
    Missing_Evidence: list[str]
    Risk_Conflict: list[str]
    Structure_State: dict[str, str]
    Last_BOS: dict[str, str]
    Last_MSS: dict[str, str]
    Protected_High: dict[str, str]
    Protected_Low: dict[str, str]
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
    swings_by_time: dict[int, list[Swing]] = {}
    for swing in swings:
        swings_by_time.setdefault(swing.formed_at, []).append(swing)

    direction = "RANGE"
    high_cursor: Swing | None = None
    low_cursor: Swing | None = None
    protected_high: float | None = None
    protected_low: float | None = None
    last_bos: BreakEvent | None = None
    last_mss: BreakEvent | None = None

    for candle in candles:
        for swing in swings_by_time.get(candle.timestamp, []):
            if swing.kind == "HIGH":
                high_cursor = swing
            else:
                low_cursor = swing

        if direction == "BULLISH" and protected_low is not None and candle.close < protected_low:
            last_mss = BreakEvent("BEARISH", protected_low, candle.timestamp, "MSS")
            direction = "BEARISH"
            if high_cursor:
                protected_high = high_cursor.price
            continue
        if direction == "BEARISH" and protected_high is not None and candle.close > protected_high:
            last_mss = BreakEvent("BULLISH", protected_high, candle.timestamp, "MSS")
            direction = "BULLISH"
            if low_cursor:
                protected_low = low_cursor.price
            continue

        if high_cursor and candle.timestamp > high_cursor.formed_at and candle.close > high_cursor.price:
            if direction in {"RANGE", "BULLISH"}:
                last_bos = BreakEvent("BULLISH", high_cursor.price, candle.timestamp, "BOS")
            else:
                last_mss = BreakEvent("BULLISH", protected_high or high_cursor.price, candle.timestamp, "MSS")
            direction = "BULLISH"
            if low_cursor:
                protected_low = low_cursor.price
        elif low_cursor and candle.timestamp > low_cursor.formed_at and candle.close < low_cursor.price:
            if direction in {"RANGE", "BEARISH"}:
                last_bos = BreakEvent("BEARISH", low_cursor.price, candle.timestamp, "BOS")
            else:
                last_mss = BreakEvent("BEARISH", protected_low or low_cursor.price, candle.timestamp, "MSS")
            direction = "BEARISH"
            if high_cursor:
                protected_high = high_cursor.price

    if direction == "RANGE":
        recent = [s.label for s in swings[-4:]]
        if {"HH", "HL"}.issubset(recent):
            direction = "BULLISH"
        elif {"LH", "LL"}.issubset(recent):
            direction = "BEARISH"

    if direction == "BULLISH":
        invalidation_price = protected_low
        invalidation_type = f"break below {invalidation_price:.2f}" if invalidation_price is not None else "NONE"
    elif direction == "BEARISH":
        invalidation_price = protected_high
        invalidation_type = f"break above {invalidation_price:.2f}" if invalidation_price is not None else "NONE"
    else:
        invalidation_price = None
        invalidation_type = "NONE"

    return Structure(
        timeframe=frame.timeframe,
        state=direction,
        last_bos=last_bos,
        last_mss=last_mss,
        protected_high=protected_high,
        protected_low=protected_low,
        structural_invalidation_price=invalidation_price,
        structural_invalidation_type=invalidation_type,
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
                levels.append(LiquidityLevel((first.price + second.price) / 2, level_type, frame.timeframe, second.formed_at, "UNSWEPT"))
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
    return [LiquidityLevel(level.price, level.type, level.timeframe, level.formed_at, _status_for_level(candles, level)) for level in levels]


def _displacement_strength(candles: list[Candle], index: int) -> float:
    if index < 20:
        return 0.0
    average_range = sum(c.high - c.low for c in candles[index - 20:index]) / 20
    if average_range <= 0:
        return 0.0
    return abs(candles[index].close - candles[index].open) / average_range


def _is_displacement(candles: list[Candle], index: int, direction: str) -> bool:
    if _displacement_strength(candles, index) < 1.0:
        return False
    candle = candles[index]
    if direction == "BULLISH":
        return candle.close > candle.open and candle.close >= candle.low + (candle.high - candle.low) * 0.65
    return candle.close < candle.open and candle.close <= candle.low + (candle.high - candle.low) * 0.35


def _zone_status(candles: list[Candle], direction: str, lower: float, upper: float, formed_at: int) -> tuple[str, int]:
    later = [c for c in candles if c.timestamp > formed_at]
    if direction == "BULLISH":
        touches = [c for c in later if c.low <= upper and c.high >= lower]
        invalidated = any(c.close < lower for c in later)
        mitigated = any(c.low <= lower for c in later)
    else:
        touches = [c for c in later if c.high >= lower and c.low <= upper]
        invalidated = any(c.close > upper for c in later)
        mitigated = any(c.high >= upper for c in later)
    if invalidated:
        return "INVALIDATED", len(touches)
    if mitigated:
        return "MITIGATED", len(touches)
    if len(touches) > 1:
        return "PARTIALLY_MITIGATED", len(touches)
    if len(touches) == 1:
        return "TOUCHED", 1
    return "FRESH", 0


def find_fvgs(frame: MarketDataFrame) -> list[FairValueGap]:
    candles = frame.closed_candles()
    gaps: list[FairValueGap] = []
    for index in range(2, len(candles)):
        first = candles[index - 2]
        third = candles[index]
        if third.low > first.high:
            status, touches = _zone_status(candles, "BULLISH", first.high, third.low, third.timestamp)
            gaps.append(FairValueGap(third.low, first.high, (third.low + first.high) / 2, "BULLISH", frame.timeframe, third.timestamp, status, touches, _displacement_strength(candles, index)))
        if third.high < first.low:
            status, touches = _zone_status(candles, "BEARISH", third.high, first.low, third.timestamp)
            gaps.append(FairValueGap(first.low, third.high, (first.low + third.high) / 2, "BEARISH", frame.timeframe, third.timestamp, status, touches, _displacement_strength(candles, index)))
    return gaps[-12:]


def find_order_blocks(frame: MarketDataFrame, structure: Structure) -> list[OrderBlock]:
    if not structure.last_bos:
        return []
    candles = frame.closed_candles()
    blocks: list[OrderBlock] = []
    for index, candle in enumerate(candles):
        if not _is_displacement(candles, index, structure.last_bos.direction):
            continue
        if structure.last_bos.direction == "BULLISH":
            candidates = [c for c in candles[max(0, index - 10):index] if c.close < c.open]
        else:
            candidates = [c for c in candles[max(0, index - 10):index] if c.close > c.open]
        if not candidates:
            continue
        source = candidates[-1]
        status, touches = _zone_status(candles, structure.last_bos.direction, source.low, source.high, source.timestamp)
        blocks.append(OrderBlock(source.high, source.low, (source.high + source.low) / 2, structure.last_bos.direction, frame.timeframe, source.timestamp, status, touches, _displacement_strength(candles, index)))
    return blocks[-6:]


def _format_price(value: float | None) -> str:
    return "NONE" if value is None else f"{value:.2f}"


def _break_text(event: BreakEvent | None) -> str:
    if not event:
        return "NONE"
    return f"{event.direction} {event.price:.2f} @ {event.timestamp}"


def _swing_bias(structures: dict[str, Structure]) -> str:
    d1 = structures["D1"].state
    h4 = structures["H4"].state
    if d1 == h4 and d1 in {"BULLISH", "BEARISH"}:
        return d1
    if d1 in {"BULLISH", "BEARISH"} and h4 == "RANGE":
        return d1
    if h4 in {"BULLISH", "BEARISH"} and d1 == "RANGE":
        return h4
    return "NONE"


def _protected_intact(bias: str, structure: Structure, price: float) -> bool:
    if bias == "BULLISH":
        return structure.protected_low is None or price >= structure.protected_low
    if bias == "BEARISH":
        return structure.protected_high is None or price <= structure.protected_high
    return False


def _current_phase(bias: str, structures: dict[str, Structure], price: float) -> str:
    if bias == "NONE":
        return "RANGE"
    h4 = structures["H4"]
    if not _protected_intact(bias, h4, price):
        return "REVERSAL_CANDIDATE"
    tactical_states = [structures[timeframe].state for timeframe in TACTICAL_TIMEFRAMES]
    if any(state not in {bias, "RANGE"} for state in tactical_states):
        return "PULLBACK"
    if all(state == bias for state in tactical_states):
        return "CONTINUATION"
    if any(state == "RANGE" for state in tactical_states):
        return "TRANSITION"
    return "TRANSITION"


def _market_regime(phase: str) -> str:
    return {
        "CONTINUATION": "TREND_CONTINUATION",
        "PULLBACK": "TREND_PULLBACK",
        "REVERSAL_CANDIDATE": "REVERSAL_CANDIDATE",
        "RANGE": "RANGE",
    }.get(phase, "TRANSITION")


def _premium_discount(frame: MarketDataFrame, structure: Structure) -> str:
    if not structure.last_swing_high or not structure.last_swing_low:
        return "UNKNOWN"
    midpoint = (structure.last_swing_high.price + structure.last_swing_low.price) / 2
    return "PREMIUM" if frame.closed_candles()[-1].close >= midpoint else "DISCOUNT"


def _current_price(frames: dict[str, MarketDataFrame]) -> float:
    return frames["M5"].closed_candles()[-1].close


def _distance(price: float, level: float) -> float:
    return abs(level - price) / price if price else 1.0


def _draw_text(name: str, level: LiquidityLevel | None, price: float, reason: str) -> str:
    if not level:
        return f"{name}: NONE ({reason})"
    return f"{name}: {level.type} {level.price:.2f} on {level.timeframe}, distance={_distance(price, level.price):.4f}; {reason}"


def _liquidity_draws(bias: str, phase: str, liquidity: list[LiquidityLevel], price: float) -> tuple[str, str]:
    unswept = [level for level in liquidity if level.status == "UNSWEPT"]
    if bias == "BULLISH":
        macro_candidates = [level for level in unswept if "External Buy-side" in level.type and level.price > price]
        tactical_candidates = [level for level in unswept if "Sell-side" in level.type and level.price < price]
    elif bias == "BEARISH":
        macro_candidates = [level for level in unswept if "External Sell-side" in level.type and level.price < price]
        tactical_candidates = [level for level in unswept if "Buy-side" in level.type and level.price > price]
    else:
        return "Macro Draw: NONE (no valid swing bias)", "Tactical Draw: NONE (no valid swing bias)"
    macro = sorted(macro_candidates, key=lambda level: (0 if level.timeframe == "D1" else 1, _distance(price, level.price)))
    tactical = sorted(tactical_candidates, key=lambda level: (0 if level.timeframe == "H1" else 1, _distance(price, level.price)))
    tactical_reason = f"{phase} phase can draw into opposite-side internal liquidity before trend continuation"
    return (
        _draw_text("Macro Draw", macro[0] if macro else None, price, f"aligned with {bias} swing bias"),
        _draw_text("Tactical Draw", tactical[0] if tactical else None, price, tactical_reason),
    )


def _zone_distance(price: float, lower: float, upper: float) -> float:
    if lower <= price <= upper:
        return 0.0
    return min(abs(price - lower), abs(price - upper)) / price


def _make_pois(
    price: float,
    bias: str,
    premium_discount: str,
    fvgs: list[FairValueGap],
    obs: list[OrderBlock],
    structures: dict[str, Structure],
    liquidity: list[LiquidityLevel],
) -> list[POI]:
    pois: list[POI] = []
    for zone in fvgs:
        pois.append(_score_poi("FVG", zone.direction, zone.timeframe, zone.lower, zone.upper, zone.midpoint, zone.formed_at, zone.status, zone.touch_count, zone.displacement_strength, price, bias, premium_discount, structures, liquidity))
    for zone in obs:
        pois.append(_score_poi("OB", zone.direction, zone.timeframe, zone.low, zone.high, zone.midpoint, zone.formed_at, zone.status, zone.touch_count, zone.displacement_strength, price, bias, premium_discount, structures, liquidity))
    valid = [poi for poi in pois if poi.status not in {"MITIGATED", "INVALIDATED"}]
    return sorted(valid, key=lambda poi: poi.score, reverse=True)


def _score_poi(
    zone_type: str,
    direction: str,
    timeframe: str,
    lower: float,
    upper: float,
    midpoint: float,
    formed_at: int,
    status: str,
    touch_count: int,
    displacement_strength: float,
    price: float,
    bias: str,
    premium_discount: str,
    structures: dict[str, Structure],
    liquidity: list[LiquidityLevel],
) -> POI:
    distance = _zone_distance(price, lower, upper)
    freshness = "FRESH" if status == "FRESH" else "USED"
    bias_alignment = direction == bias
    pd_alignment = (direction == "BULLISH" and premium_discount == "DISCOUNT") or (direction == "BEARISH" and premium_discount == "PREMIUM")
    bos_relation = structures.get(timeframe).last_bos.direction if structures.get(timeframe) and structures[timeframe].last_bos else "NONE"
    mss_relation = structures.get(timeframe).last_mss.direction if structures.get(timeframe) and structures[timeframe].last_mss else "NONE"
    near_liquidity = any(_zone_distance(level.price, lower, upper) <= 0.002 for level in liquidity)
    score = 0.0
    score += {"D1": 5, "H4": 4, "H1": 3, "M15": 2, "M5": 1}.get(timeframe, 0)
    score += 4 if bias_alignment else -3
    score += 2 if pd_alignment else 0
    score += 2 if status == "FRESH" else 1 if status in {"TOUCHED", "PARTIALLY_MITIGATED"} else -4
    score += max(0.0, 2.0 - min(distance * 100, 2.0))
    score += min(displacement_strength, 3.0)
    score += 1 if near_liquidity else 0
    score -= touch_count * 0.5
    quality = {
        "timeframe": timeframe,
        "direction": direction,
        "freshness": freshness,
        "touch_count": touch_count,
        "mitigation_status": status,
        "displacement_strength": round(displacement_strength, 3),
        "bos_relation": bos_relation,
        "mss_relation": mss_relation,
        "fvg_overlap": zone_type == "FVG",
        "premium_discount_alignment": pd_alignment,
        "liquidity_proximity": near_liquidity,
        "swing_bias_alignment": bias_alignment,
        "distance_to_price": round(distance, 5),
    }
    label = f"{direction} {zone_type} {timeframe} {lower:.2f}-{upper:.2f}"
    return POI(label, zone_type, direction, timeframe, lower, upper, midpoint, formed_at, status, quality, score)


def _cluster_pois(pois: list[POI]) -> list[POI]:
    clustered: list[POI] = []
    used: set[int] = set()
    for index, poi in enumerate(pois):
        if index in used:
            continue
        overlaps = [poi]
        for other_index, other in enumerate(pois[index + 1:], start=index + 1):
            if other_index in used or other.direction != poi.direction:
                continue
            if max(poi.lower, other.lower) <= min(poi.upper, other.upper):
                overlaps.append(other)
                used.add(other_index)
        if len(overlaps) == 1:
            clustered.append(poi)
            continue
        lower = min(item.lower for item in overlaps)
        upper = max(item.upper for item in overlaps)
        best = max(overlaps, key=lambda item: item.score)
        clustered.append(POI(f"{best.direction} POI cluster {best.timeframe} {lower:.2f}-{upper:.2f}", "CLUSTER", best.direction, best.timeframe, lower, upper, (lower + upper) / 2, best.formed_at, best.status, best.quality, best.score + 1))
    return sorted(clustered, key=lambda poi: poi.score, reverse=True)


def _poi_summary(pois: list[POI], bias: str) -> tuple[str, str, str]:
    clustered = _cluster_pois(pois)
    aligned = [poi for poi in clustered if poi.direction == bias]
    opposing = [poi for poi in clustered if poi.direction != bias]
    primary = aligned[0] if aligned else clustered[0] if clustered else None
    secondary = aligned[1] if len(aligned) > 1 else opposing[0] if opposing else None
    conflict = "NONE"
    if primary:
        for other in opposing:
            if max(primary.lower, other.lower) <= min(primary.upper, other.upper):
                conflict = f"POI_CONFLICT: {primary.label} overlaps {other.label}"
                break
    return (
        _poi_text(primary),
        _poi_text(secondary),
        conflict,
    )


def _poi_text(poi: POI | None) -> str:
    if not poi:
        return "NONE"
    quality = ", ".join(f"{key}={value}" for key, value in poi.quality.items())
    return f"{poi.label}; status={poi.status}; score={poi.score:.2f}; {quality}"


def _state_model(
    swing_bias: str,
    phase: str,
    price: float,
    primary_poi: str,
    tactical_draw: str,
    structures: dict[str, Structure],
    liquidity: list[LiquidityLevel],
    frames: dict[str, MarketDataFrame],
) -> tuple[str, list[str], list[str]]:
    missing: list[str] = []
    conflicts: list[str] = []
    if swing_bias == "NONE":
        return "NO_TRADE", ["valid D1/H4 swing bias"], ["no directional swing bias"]
    if phase == "REVERSAL_CANDIDATE":
        return "NO_TRADE", ["rebuild thesis after structural invalidation"], ["H4 structural protection is broken"]
    has_primary = primary_poi != "NONE"
    near_tactical = "NONE" not in tactical_draw and "distance=0." in tactical_draw
    swept = any(level.status in {"SWEPT", "RECLAIMED"} for level in liquidity)
    displacement = _has_displacement(frames, swing_bias)
    trigger_break = any(
        (structures[tf].last_mss and structures[tf].last_mss.direction == swing_bias)
        or (structures[tf].last_bos and structures[tf].last_bos.direction == swing_bias)
        for tf in TRIGGER_TIMEFRAMES
    )
    if not has_primary:
        missing.append("thesis-aligned Primary POI")
    if not swept:
        missing.append("meaningful liquidity sweep")
    if not displacement:
        missing.append(f"{swing_bias.lower()} displacement")
    if not trigger_break:
        missing.append(f"{swing_bias.lower()} M15/M5 MSS or BOS confirmation")
    if swept and displacement and trigger_break and has_primary:
        return "ARMED", missing, conflicts
    if has_primary or near_tactical:
        return "WATCH", missing, conflicts
    return "WAIT", missing, conflicts


def _has_displacement(frames: dict[str, MarketDataFrame], direction: str) -> bool:
    for timeframe in TRIGGER_TIMEFRAMES:
        candles = frames[timeframe].closed_candles()
        start = max(0, len(candles) - 10)
        if any(_is_displacement(candles, index, direction) for index in range(start, len(candles))):
            return True
    return False


def _thesis_status(previous_thesis: dict | None, d1_bias: str) -> str:
    if previous_thesis is None:
        return "THESIS_INITIALIZED"
    if previous_thesis.get("previous_bias") == d1_bias:
        return "THESIS_MAINTAINED"
    return "THESIS_REVERSED"


def _structure_text(structure: Structure) -> str:
    return (
        f"{structure.state}; BOS={_break_text(structure.last_bos)}; "
        f"MSS={_break_text(structure.last_mss)}; protected_high={_format_price(structure.protected_high)}; "
        f"protected_low={_format_price(structure.protected_low)}"
    )


def review_symbol(frames: dict[str, MarketDataFrame], previous_thesis: dict | None = None) -> Review:
    validate_generation(frames)
    structures = {timeframe: analyze_structure(frames[timeframe]) for timeframe in TIMEFRAMES}
    liquidity = [level for timeframe in TIMEFRAMES for level in find_liquidity(frames[timeframe], structures[timeframe])]
    fvgs = [gap for timeframe in TIMEFRAMES for gap in find_fvgs(frames[timeframe])]
    order_blocks = [block for timeframe in TIMEFRAMES for block in find_order_blocks(frames[timeframe], structures[timeframe])]
    price = _current_price(frames)
    swing_bias = _swing_bias(structures)
    phase = _current_phase(swing_bias, structures, price)
    premium_discount = _premium_discount(frames["H4"], structures["H4"])
    macro_draw, tactical_draw = _liquidity_draws(swing_bias, phase, liquidity, price)
    pois = _make_pois(price, swing_bias, premium_discount, fvgs, order_blocks, structures, liquidity)
    primary_poi, secondary_poi, poi_conflict = _poi_summary(pois, swing_bias)
    state, missing, conflicts = _state_model(swing_bias, phase, price, primary_poi, tactical_draw, structures, liquidity, frames)
    if poi_conflict != "NONE":
        conflicts.append(poi_conflict)
    d1_bias = structures["D1"].state if structures["D1"].state in {"BULLISH", "BEARISH"} else "RANGE"
    return Review(
        Symbol=frames["D1"].symbol,
        Swing_Bias=swing_bias,
        Current_Phase=phase,
        Market_Regime=_market_regime(phase),
        D1_Structure=_structure_text(structures["D1"]),
        H4_Structure=_structure_text(structures["H4"]),
        H1_Structure=_structure_text(structures["H1"]),
        Structural_Invalidation={tf: structures[tf].structural_invalidation_type for tf in TIMEFRAMES},
        Macro_Draw_on_Liquidity=macro_draw,
        Tactical_Draw_on_Liquidity=tactical_draw,
        Premium_Discount=premium_discount,
        Primary_POI=primary_poi,
        Secondary_POI=secondary_poi,
        POI_Conflict=poi_conflict,
        Preferred_Direction="LONG" if swing_bias == "BULLISH" else "SHORT" if swing_bias == "BEARISH" else "NONE",
        State=state,
        Thesis_Status=_thesis_status(previous_thesis, d1_bias),
        Confidence="UNCALIBRATED",
        Missing_Evidence=missing,
        Risk_Conflict=conflicts,
        Structure_State={tf: structures[tf].state for tf in TIMEFRAMES},
        Last_BOS={tf: _break_text(structures[tf].last_bos) for tf in TIMEFRAMES},
        Last_MSS={tf: _break_text(structures[tf].last_mss) for tf in TIMEFRAMES},
        Protected_High={tf: _format_price(structures[tf].protected_high) for tf in TIMEFRAMES},
        Protected_Low={tf: _format_price(structures[tf].protected_low) for tf in TIMEFRAMES},
        Liquidity=[asdict(level) for level in liquidity[-24:]],
        FVG=[asdict(gap) for gap in fvgs[-24:]],
        Order_Blocks=[asdict(block) for block in order_blocks[-12:]],
    )
