"""Closed-candle market phase, liquidity event, and trigger sequence review."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import median

from .model import Candle, MarketDataFrame, TIMEFRAMES, validate_generation


ALLOWED_STATES = ("NO_TRADE", "WAIT", "WATCH", "ARMED")
HTF_TIMEFRAMES = ("D1", "H4")
TACTICAL_TIMEFRAMES = ("H1", "M15", "M5")
TRIGGER_TIMEFRAMES = ("M15", "M5")


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
class StructureEvent:
    direction: str
    price: float
    timestamp: int
    kind: str
    previous_state: str
    new_state: str
    evidence: str


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
    events: list[StructureEvent]


@dataclass(frozen=True)
class LiquidityLevel:
    price: float
    type: str
    timeframe: str
    formed_at: int
    status: str
    liquidity_id: str = ""


@dataclass(frozen=True)
class LiquidityEvent:
    level_price: float
    level_type: str
    timeframe: str
    event_type: str
    timestamp: int
    sweep_price: float | None
    penetration: float
    close_location: str


@dataclass(frozen=True)
class DisplacementEvent:
    direction: str
    strength: str
    timestamp: int
    structure_broken: str
    fvg_created: bool
    body_ratio: float
    range_ratio: float
    close_near_extreme: bool
    follow_through: bool


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
    setup_type: str = "GENERIC_FVG"
    related_displacement_id: str | None = None


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
    width_pct: float


@dataclass(frozen=True)
class SequenceTransition:
    previous_state: str
    new_state: str
    timestamp: int
    evidence: str


@dataclass
class Review:
    Symbol: str
    Persistence_Version: str
    State_Loaded_From: str
    Active_Draw_Selected_At: str
    Sequence_Started_At: str
    Sequence_ID: str
    Loaded_Sequence_ID: str
    Loaded_Sequence_State: str
    Loaded_Active_Target: str
    Current_Sequence_ID: str
    Current_Sequence_State: str
    Current_Active_Target: str
    Transition: str
    Transition_Reason: str
    Review_Timestamp: str
    Swing_Bias: str
    Current_Phase: str
    Pullback_Stage: str
    Market_Regime: str
    D1_Structure: str
    H4_Structure: str
    H1_Structure: str
    Last_Structure_Events: dict[str, list[str]]
    Structural_Invalidation: dict[str, str]
    Macro_Draw_on_Liquidity: str
    Tactical_Draw_on_Liquidity: str
    Active_Tactical_Draw: str
    Candidate_Tactical_Draw: str
    Active_Draw_Status: str
    Candidate_Draw_Status: str
    Target_Changed: str
    Target_Change_Reason: str
    Premium_Discount: str
    Liquidity_Event: str
    Displacement: str
    Contextual_MSS: str
    Setup_FVG: str
    Primary_POI: str
    Secondary_POI: str
    POI_Width: str
    POI_Conflict: str
    Sequence_State: str
    Sequence_Transitions: list[dict]
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
    Liquidity_Events: list[dict]
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

    state = "RANGE"
    high_cursor: Swing | None = None
    low_cursor: Swing | None = None
    protected_high: float | None = None
    protected_low: float | None = None
    last_bos: BreakEvent | None = None
    last_mss: BreakEvent | None = None
    events: list[StructureEvent] = []

    for candle in candles:
        for swing in swings_by_time.get(candle.timestamp, []):
            if swing.kind == "HIGH":
                high_cursor = swing
            else:
                low_cursor = swing

        bullish_break = high_cursor and candle.timestamp > high_cursor.formed_at and candle.close > high_cursor.price
        bearish_break = low_cursor and candle.timestamp > low_cursor.formed_at and candle.close < low_cursor.price

        if state == "BULLISH" and protected_low is not None and candle.close < protected_low:
            event = StructureEvent("BEARISH", protected_low, candle.timestamp, "MSS", state, "BEARISH", "closed below bullish protected low")
            events.append(event)
            last_mss = BreakEvent(event.direction, event.price, event.timestamp, event.kind)
            state = "BEARISH"
            protected_high = high_cursor.price if high_cursor else protected_high
            continue
        if state == "BEARISH" and protected_high is not None and candle.close > protected_high:
            event = StructureEvent("BULLISH", protected_high, candle.timestamp, "MSS", state, "BULLISH", "closed above bearish protected high")
            events.append(event)
            last_mss = BreakEvent(event.direction, event.price, event.timestamp, event.kind)
            state = "BULLISH"
            protected_low = low_cursor.price if low_cursor else protected_low
            continue
        if bullish_break:
            kind = "BOS" if state in {"RANGE", "BULLISH"} else "MSS"
            previous = state
            event = StructureEvent("BULLISH", high_cursor.price, candle.timestamp, kind, previous, "BULLISH", "closed above confirmed swing high")
            events.append(event)
            if kind == "BOS":
                last_bos = BreakEvent(event.direction, event.price, event.timestamp, event.kind)
            else:
                last_mss = BreakEvent(event.direction, event.price, event.timestamp, event.kind)
            state = "BULLISH"
            protected_low = low_cursor.price if low_cursor else protected_low
        elif bearish_break:
            kind = "BOS" if state in {"RANGE", "BEARISH"} else "MSS"
            previous = state
            event = StructureEvent("BEARISH", low_cursor.price, candle.timestamp, kind, previous, "BEARISH", "closed below confirmed swing low")
            events.append(event)
            if kind == "BOS":
                last_bos = BreakEvent(event.direction, event.price, event.timestamp, event.kind)
            else:
                last_mss = BreakEvent(event.direction, event.price, event.timestamp, event.kind)
            state = "BEARISH"
            protected_high = high_cursor.price if high_cursor else protected_high

    if state == "RANGE":
        recent = [s.label for s in swings[-4:]]
        if {"HH", "HL"}.issubset(recent):
            state = "BULLISH"
        elif {"LH", "LL"}.issubset(recent):
            state = "BEARISH"

    if state == "BULLISH":
        invalidation_price = protected_low
        invalidation_type = f"break below {invalidation_price:.2f}" if invalidation_price is not None else "NONE"
    elif state == "BEARISH":
        invalidation_price = protected_high
        invalidation_type = f"break above {invalidation_price:.2f}" if invalidation_price is not None else "NONE"
    else:
        invalidation_price = None
        invalidation_type = "NONE"

    return Structure(
        timeframe=frame.timeframe,
        state=state,
        last_bos=last_bos,
        last_mss=last_mss,
        protected_high=protected_high,
        protected_low=protected_low,
        structural_invalidation_price=invalidation_price,
        structural_invalidation_type=invalidation_type,
        last_swing_high=swing_highs[-1] if swing_highs else None,
        last_swing_low=swing_lows[-1] if swing_lows else None,
        swings=swings,
        events=events,
    )


def _close_location(candle: Candle, level: LiquidityLevel) -> str:
    if "Buy-side" in level.type or "Highs" in level.type:
        return "OUTSIDE" if candle.close > level.price else "INSIDE"
    return "OUTSIDE" if candle.close < level.price else "INSIDE"


def _liquidity_events_for_level(candles: list[Candle], level: LiquidityLevel) -> list[LiquidityEvent]:
    events: list[LiquidityEvent] = []
    tolerance = max(level.price * 0.001, 0.01)
    swept = False
    reclaimed = False
    for candle in [c for c in candles if c.timestamp > level.formed_at]:
        if "Buy-side" in level.type or "Highs" in level.type:
            distance = level.price - candle.high
            beyond = candle.high > level.price
            sweep_price = candle.high
            penetration = max(0.0, candle.high - level.price)
            reclaim = swept and candle.close < level.price
        else:
            distance = candle.low - level.price
            beyond = candle.low < level.price
            sweep_price = candle.low
            penetration = max(0.0, level.price - candle.low)
            reclaim = swept and candle.close > level.price
        if not swept and 0 <= distance <= tolerance:
            events.append(LiquidityEvent(level.price, level.type, level.timeframe, "APPROACHED", candle.timestamp, None, 0.0, _close_location(candle, level)))
        if beyond and not swept:
            swept = True
            events.append(LiquidityEvent(level.price, level.type, level.timeframe, "SWEPT", candle.timestamp, sweep_price, penetration, _close_location(candle, level)))
            continue
        if reclaim and not reclaimed:
            reclaimed = True
            events.append(LiquidityEvent(level.price, level.type, level.timeframe, "RECLAIMED", candle.timestamp, None, 0.0, _close_location(candle, level)))
    if swept and not reclaimed:
        last = candles[-1]
        events.append(LiquidityEvent(level.price, level.type, level.timeframe, "FAILED_RECLAIM", last.timestamp, None, 0.0, _close_location(last, level)))
    return events


def _level_status(events: list[LiquidityEvent]) -> str:
    event_types = [event.event_type for event in events]
    if "RECLAIMED" in event_types:
        return "RECLAIMED"
    if "SWEPT" in event_types:
        return "SWEPT"
    return "UNSWEPT"


def _liquidity_side(level_type: str) -> str:
    if "Equal Lows" in level_type:
        return "EQL"
    if "Equal Highs" in level_type:
        return "EQH"
    if "Sell-side" in level_type:
        return "SSL"
    if "Buy-side" in level_type:
        return "BSL"
    return level_type.upper().replace(" ", "_")


def _liquidity_id(level: LiquidityLevel | dict | None) -> str:
    if not level:
        return ""
    if isinstance(level, dict):
        existing = level.get("liquidity_id")
        if existing:
            return str(existing)
        timeframe = str(level.get("timeframe", ""))
        level_type = str(level.get("type", ""))
        formed_at = level.get("formed_at", 0)
    else:
        existing = level.liquidity_id
        if existing:
            return existing
        timeframe = level.timeframe
        level_type = level.type
        formed_at = level.formed_at
    try:
        formed = int(formed_at)
    except (TypeError, ValueError):
        formed = 0
    return f"{timeframe}-{_liquidity_side(level_type)}-{formed}"


def _with_liquidity_id(level: LiquidityLevel) -> LiquidityLevel:
    if level.liquidity_id:
        return level
    return LiquidityLevel(level.price, level.type, level.timeframe, level.formed_at, level.status, _liquidity_id(level))


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
    return [
        _with_liquidity_id(LiquidityLevel(level.price, level.type, level.timeframe, level.formed_at, _level_status(_liquidity_events_for_level(candles, level))))
        for level in levels
    ]


def find_liquidity_events(frame: MarketDataFrame, levels: list[LiquidityLevel]) -> list[LiquidityEvent]:
    candles = frame.closed_candles()
    return [event for level in levels for event in _liquidity_events_for_level(candles, level)]


def _median_body(candles: list[Candle]) -> float:
    return median([abs(c.close - c.open) for c in candles]) if candles else 0.0


def _median_range(candles: list[Candle]) -> float:
    return median([c.high - c.low for c in candles]) if candles else 0.0


def _has_fvg_at(candles: list[Candle], index: int, direction: str) -> bool:
    if index < 2:
        return False
    first = candles[index - 2]
    third = candles[index]
    return third.low > first.high if direction == "BULLISH" else third.high < first.low


def find_displacements(frame: MarketDataFrame, structure: Structure) -> list[DisplacementEvent]:
    candles = frame.closed_candles()
    events: list[DisplacementEvent] = []
    structure_events = {event.timestamp: event for event in structure.events}
    for index in range(20, len(candles)):
        candle = candles[index]
        direction = "BULLISH" if candle.close > candle.open else "BEARISH" if candle.close < candle.open else "NONE"
        if direction == "NONE":
            continue
        previous = candles[index - 20:index]
        median_body = _median_body(previous)
        median_range = _median_range(previous)
        body_ratio = abs(candle.close - candle.open) / median_body if median_body else 0.0
        range_ratio = (candle.high - candle.low) / median_range if median_range else 0.0
        close_near_extreme = (
            candle.close >= candle.low + (candle.high - candle.low) * 0.75
            if direction == "BULLISH"
            else candle.close <= candle.low + (candle.high - candle.low) * 0.25
        )
        structure_broken = structure_events.get(candle.timestamp)
        fvg_created = _has_fvg_at(candles, index, direction)
        follow = candles[index + 1:index + 3]
        follow_through = any(c.close > candle.close for c in follow) if direction == "BULLISH" else any(c.close < candle.close for c in follow)
        components = sum([body_ratio >= 1.5, range_ratio >= 1.2, close_near_extreme, structure_broken is not None, fvg_created, follow_through])
        if components < 3:
            continue
        strength = "STRONG" if components >= 5 else "VALID" if components >= 4 else "WEAK"
        broken = f"{structure_broken.kind} {structure_broken.price:.2f}" if structure_broken else "NONE"
        events.append(DisplacementEvent(direction, strength, candle.timestamp, broken, fvg_created, round(body_ratio, 3), round(range_ratio, 3), close_near_extreme, follow_through))
    return events


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


def find_fvgs(frame: MarketDataFrame, displacements: list[DisplacementEvent] | None = None) -> list[FairValueGap]:
    candles = frame.closed_candles()
    displacement_by_time = {event.timestamp: event for event in displacements or [] if event.strength in {"VALID", "STRONG"}}
    gaps: list[FairValueGap] = []
    for index in range(2, len(candles)):
        first = candles[index - 2]
        third = candles[index]
        related = displacement_by_time.get(third.timestamp)
        if third.low > first.high:
            status, touches = _zone_status(candles, "BULLISH", first.high, third.low, third.timestamp)
            setup_type = "SETUP_FVG" if related and related.direction == "BULLISH" else "GENERIC_FVG"
            gaps.append(FairValueGap(third.low, first.high, (third.low + first.high) / 2, "BULLISH", frame.timeframe, third.timestamp, status, touches, related.body_ratio if related else 0.0, setup_type, f"{frame.timeframe}:{third.timestamp}" if setup_type == "SETUP_FVG" else None))
        if third.high < first.low:
            status, touches = _zone_status(candles, "BEARISH", third.high, first.low, third.timestamp)
            setup_type = "SETUP_FVG" if related and related.direction == "BEARISH" else "GENERIC_FVG"
            gaps.append(FairValueGap(first.low, third.high, (first.low + third.high) / 2, "BEARISH", frame.timeframe, third.timestamp, status, touches, related.body_ratio if related else 0.0, setup_type, f"{frame.timeframe}:{third.timestamp}" if setup_type == "SETUP_FVG" else None))
    return gaps[-16:]


def find_order_blocks(frame: MarketDataFrame, structure: Structure, displacements: list[DisplacementEvent] | None = None) -> list[OrderBlock]:
    valid_displacements = [event for event in displacements or [] if event.strength in {"VALID", "STRONG"} and event.structure_broken != "NONE"]
    if not valid_displacements:
        return []
    candles = frame.closed_candles()
    candles_by_time = {c.timestamp: i for i, c in enumerate(candles)}
    blocks: list[OrderBlock] = []
    for event in valid_displacements:
        index = candles_by_time.get(event.timestamp)
        if index is None:
            continue
        if event.direction == "BULLISH":
            candidates = [c for c in candles[max(0, index - 10):index] if c.close < c.open]
        else:
            candidates = [c for c in candles[max(0, index - 10):index] if c.close > c.open]
        if not candidates:
            continue
        source = candidates[-1]
        status, touches = _zone_status(candles, event.direction, source.low, source.high, source.timestamp)
        blocks.append(OrderBlock(source.high, source.low, (source.high + source.low) / 2, event.direction, frame.timeframe, source.timestamp, status, touches, event.body_ratio))
    return blocks[-8:]


def _format_price(value: float | None) -> str:
    return "NONE" if value is None else f"{value:.2f}"


def _break_text(event: BreakEvent | None) -> str:
    return "NONE" if not event else f"{event.direction} {event.price:.2f} @ {event.timestamp}"


def _event_timeline_text(structure: Structure) -> list[str]:
    tail = structure.events[-4:]
    lines = [f"{event.kind} {event.direction} @ {event.timestamp} price {event.price:.2f} ({event.previous_state}->{event.new_state})" for event in tail]
    lines.append(f"CURRENT STRUCTURE = {structure.state}")
    return lines


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
    if not _protected_intact(bias, structures["H4"], price):
        return "REVERSAL_CANDIDATE"
    tactical_states = [structures[timeframe].state for timeframe in TACTICAL_TIMEFRAMES]
    if any(state not in {bias, "RANGE"} for state in tactical_states):
        return "PULLBACK"
    if all(state == bias for state in tactical_states):
        return "CONTINUATION"
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


def _level_text(level: LiquidityLevel | None, price: float) -> str:
    if not level:
        return "NONE"
    return f"{level.type} {level.price:.2f} on {level.timeframe}, formed_at={level.formed_at}, distance={_distance(price, level.price):.4f}"


def _liquidity_draws(bias: str, phase: str, liquidity: list[LiquidityLevel], price: float) -> tuple[str, str, LiquidityLevel | None]:
    unswept = [level for level in liquidity if level.status == "UNSWEPT"]
    if bias == "BULLISH":
        macro_candidates = [level for level in unswept if "External Buy-side" in level.type and level.price > price]
        tactical_candidates = [level for level in liquidity if "Sell-side" in level.type and level.price < price]
    elif bias == "BEARISH":
        macro_candidates = [level for level in unswept if "External Sell-side" in level.type and level.price < price]
        tactical_candidates = [level for level in liquidity if "Buy-side" in level.type and level.price > price]
    else:
        return "Macro Draw: NONE (no valid swing bias)", "Tactical Draw: NONE (no valid swing bias)", None
    macro = sorted(macro_candidates, key=lambda level: (0 if level.timeframe == "D1" else 1, _distance(price, level.price)))
    tactical = sorted(tactical_candidates, key=lambda level: (0 if level.timeframe == "H1" else 1, _distance(price, level.price)))
    tactical_level = tactical[0] if tactical else None
    return (
        _draw_text("Macro Draw", macro[0] if macro else None, price, f"aligned with {bias} swing bias"),
        _draw_text("Tactical Draw", tactical_level, price, f"{phase} phase tactical objective"),
        tactical_level,
    )


def _events_for_level(events: list[LiquidityEvent], level: LiquidityLevel | None) -> list[LiquidityEvent]:
    if not level:
        return []
    return [event for event in events if event.timeframe == level.timeframe and event.level_type == level.type and event.level_price == level.price]


def _find_matching_level(liquidity: list[LiquidityLevel], raw: dict | None) -> LiquidityLevel | None:
    if not raw:
        return None
    try:
        price = float(raw["price"])
        timeframe = str(raw["timeframe"])
        level_type = str(raw["type"])
        formed_at = int(raw.get("formed_at", 0))
    except (KeyError, TypeError, ValueError):
        return None
    raw_id = _liquidity_id(raw)
    if raw_id:
        for level in liquidity:
            if _liquidity_id(level) == raw_id:
                return level
    if formed_at:
        for level in liquidity:
            if level.timeframe == timeframe and level.type == level_type and level.formed_at == formed_at:
                return level
    for level in liquidity:
        if level.timeframe == timeframe and level.type == level_type and abs(level.price - price) < 0.005:
            return level
    return LiquidityLevel(price, level_type, timeframe, formed_at, "UNSWEPT", raw_id)


def _previous_active_target(previous_thesis: dict | None, liquidity: list[LiquidityLevel]) -> LiquidityLevel | None:
    if not previous_thesis:
        return None
    raw = previous_thesis.get("active_tactical_draw") or previous_thesis.get("previous_active_tactical_draw")
    return _find_matching_level(liquidity, raw)


def _retired_liquidity_ids(previous_thesis: dict | None) -> set[str]:
    retired: set[str] = set()
    for item in (previous_thesis or {}).get("retired_liquidity_instances", []):
        if isinstance(item, dict) and item.get("liquidity_id"):
            retired.add(str(item["liquidity_id"]))
    return retired


def _is_retired_for_sequence_genesis(previous_thesis: dict | None, level: LiquidityLevel | None) -> bool:
    return bool(level and _liquidity_id(level) in _retired_liquidity_ids(previous_thesis))


def _draw_status(events: list[LiquidityEvent]) -> str:
    event_types = [event.event_type for event in events]
    if "RECLAIMED" in event_types:
        return "RECLAIMED"
    if "SWEPT" in event_types:
        return "SWEPT"
    if "TOUCHED" in event_types:
        return "TOUCHED"
    if "APPROACHED" in event_types:
        return "APPROACHED"
    return "NONE"


def _active_target_selected_at(previous_thesis: dict | None) -> int:
    if not previous_thesis:
        return 0
    raw_target = previous_thesis.get("active_tactical_draw") or previous_thesis.get("previous_active_tactical_draw") or {}
    for key in ("selected_at", "active_tactical_draw_selected_at", "tactical_draw_selected_at", "previous_review_timestamp", "review_timestamp"):
        value = raw_target.get(key) if isinstance(raw_target, dict) and key in raw_target else previous_thesis.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _latest_closed_timestamp(frames: dict[str, MarketDataFrame]) -> int:
    return max(frame.latest_closed_candle_timestamp for frame in frames.values())


def _sequence_started_at(previous_thesis: dict | None, active_selected_at: int) -> int:
    if previous_thesis:
        for key in ("sequence_started_at", "previous_review_timestamp"):
            value = previous_thesis.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return active_selected_at


def _resolve_tactical_targets(
    previous_thesis: dict | None,
    liquidity: list[LiquidityLevel],
    candidate: LiquidityLevel | None,
    liquidity_events: list[LiquidityEvent],
    price: float,
) -> tuple[LiquidityLevel | None, LiquidityLevel | None, str, str, str, str]:
    previous = _previous_active_target(previous_thesis, liquidity)
    previous_state = str((previous_thesis or {}).get("previous_sequence_state") or (previous_thesis or {}).get("sequence_state") or "")
    candidate_status = _draw_status(_events_for_level(liquidity_events, candidate))
    reselection = (previous_thesis or {}).get("target_reselection")
    if reselection:
        new_target = _find_matching_level(liquidity, reselection.get("new_target"))
        if new_target and not _is_retired_for_sequence_genesis(previous_thesis, new_target):
            return new_target, candidate, _draw_status(_events_for_level(liquidity_events, new_target)), candidate_status, "YES", "TARGET_RESELECTED"
    terminal_states = {"EXPIRED_NO_TRIGGER", "INVALIDATED"}
    if previous_state in terminal_states and _is_retired_for_sequence_genesis(previous_thesis, candidate):
        return None, candidate, "NONE", candidate_status, "NO", "AWAITING_FRESH_TACTICAL_LIQUIDITY"
    active_states = {"", "SEEKING_LIQUIDITY", "LIQUIDITY_SWEPT", "DISPLACEMENT_CONFIRMED", "MSS_CONFIRMED", "SETUP_FVG_CREATED", "RETEST_PENDING"}
    if previous and previous_state in active_states:
        active_status = _draw_status(_events_for_level(liquidity_events, previous))
        return previous, candidate, active_status, candidate_status, "NO", "ACTIVE_DRAW_LOCKED"
    if previous and previous_state == "INVALIDATED":
        return candidate, candidate, candidate_status, candidate_status, "YES", "THESIS_INVALIDATED"
    if previous_thesis:
        return candidate, candidate, candidate_status, candidate_status, "YES", "ACTIVE_DRAW_INITIALIZED_FROM_LEGACY_STATE"
    return candidate, candidate, candidate_status, candidate_status, "NO", "NO_PREVIOUS_ACTIVE_TARGET"


def _events_for_active_target(
    events: list[LiquidityEvent],
    active_target: LiquidityLevel | None,
    selected_at: int,
) -> list[LiquidityEvent]:
    return [event for event in _events_for_level(events, active_target) if event.timestamp > selected_at]


def _add_active_target_events(
    frames: dict[str, MarketDataFrame],
    liquidity_events: list[LiquidityEvent],
    active_target: LiquidityLevel | None,
) -> list[LiquidityEvent]:
    if not active_target or active_target.timeframe not in frames:
        return liquidity_events
    active_events = find_liquidity_events(frames[active_target.timeframe], [active_target])
    seen = {
        (event.level_price, event.level_type, event.timeframe, event.event_type, event.timestamp)
        for event in liquidity_events
    }
    merged = list(liquidity_events)
    for event in active_events:
        key = (event.level_price, event.level_type, event.timeframe, event.event_type, event.timestamp)
        if key not in seen:
            merged.append(event)
    return merged


def _pullback_stage(phase: str, events: list[LiquidityEvent], displacements: list[DisplacementEvent], bias: str) -> str:
    if phase != "PULLBACK":
        return "SEEKING_LIQUIDITY"
    event_types = [event.event_type for event in events]
    if "FAILED_RECLAIM" in event_types and "RECLAIMED" not in event_types:
        return "FAILED"
    if "RECLAIMED" in event_types:
        latest_reclaim = max(event.timestamp for event in events if event.event_type == "RECLAIMED")
        if any(event.timestamp > latest_reclaim and event.direction == bias and event.strength in {"VALID", "STRONG"} for event in displacements):
            return "REACCELERATION"
        return "RECLAIMED"
    if "SWEPT" in event_types:
        return "LIQUIDITY_TAKEN"
    return "SEEKING_LIQUIDITY"


def _latest_liquidity_event(events: list[LiquidityEvent]) -> str:
    if not events:
        return "NONE"
    event = max(events, key=lambda item: item.timestamp)
    return (
        f"{event.event_type} {event.level_type} {event.level_price:.2f} on {event.timeframe} "
        f"@ {event.timestamp}; sweep_price={_format_price(event.sweep_price)}; "
        f"penetration={event.penetration:.4f}; close_location={event.close_location}"
    )


def _latest_displacement(displacements: list[DisplacementEvent], bias: str) -> DisplacementEvent | None:
    valid = [event for event in displacements if event.direction == bias and event.strength in {"VALID", "STRONG"}]
    return max(valid, key=lambda event: event.timestamp) if valid else None


def _displacement_text(event: DisplacementEvent | None) -> str:
    if not event:
        return "NONE"
    return (
        f"{event.direction} {event.strength} @ {event.timestamp}; structure_broken={event.structure_broken}; "
        f"fvg_created={event.fvg_created}; body_ratio={event.body_ratio}; range_ratio={event.range_ratio}; "
        f"close_near_extreme={event.close_near_extreme}; follow_through={event.follow_through}"
    )


def _contextual_mss(structures: dict[str, Structure], sweep: LiquidityEvent | None, displacement: DisplacementEvent | None, bias: str) -> BreakEvent | None:
    if not sweep or not displacement:
        return None
    for timeframe in TRIGGER_TIMEFRAMES:
        candidates = [
            event for event in structures[timeframe].events
            if event.kind == "MSS" and event.direction == bias and event.timestamp > sweep.timestamp and event.timestamp >= displacement.timestamp
        ]
        if candidates:
            event = candidates[-1]
            return BreakEvent(event.direction, event.price, event.timestamp, event.kind)
    return None


def _contextual_mss_text(event: BreakEvent | None, sweep: LiquidityEvent | None, displacement: DisplacementEvent | None) -> str:
    if not event:
        return "NONE"
    return f"{event.direction} @ {event.timestamp}; related_sweep_id={sweep.timeframe}:{sweep.timestamp}; related_displacement_id={displacement.direction}:{displacement.timestamp}"


def _setup_fvg(fvgs: list[FairValueGap], sweep: LiquidityEvent | None, displacement: DisplacementEvent | None, mss: BreakEvent | None, bias: str) -> FairValueGap | None:
    if not sweep or not displacement or not mss:
        return None
    candidates = [
        gap for gap in fvgs
        if (
            gap.setup_type == "SETUP_FVG"
            and gap.direction == bias
            and gap.formed_at >= displacement.timestamp
            and gap.formed_at >= sweep.timestamp
            and gap.formed_at >= mss.timestamp
        )
    ]
    return candidates[-1] if candidates else None


def _setup_fvg_text(gap: FairValueGap | None) -> str:
    if not gap:
        return "NONE"
    return f"{gap.direction} SETUP_FVG {gap.timeframe} {gap.lower:.2f}-{gap.upper:.2f} @ {gap.formed_at}; status={gap.status}"


def _zone_distance(price: float, lower: float, upper: float) -> float:
    if lower <= price <= upper:
        return 0.0
    return min(abs(price - lower), abs(price - upper)) / price


def _zone_width_pct(price: float, lower: float, upper: float) -> float:
    return abs(upper - lower) / price if price else 1.0


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
    setup_related: bool,
    price: float,
    bias: str,
    premium_discount: str,
    liquidity_events: list[LiquidityEvent],
) -> POI:
    width_pct = _zone_width_pct(price, lower, upper)
    distance = _zone_distance(price, lower, upper)
    bias_alignment = direction == bias
    pd_alignment = (direction == "BULLISH" and premium_discount == "DISCOUNT") or (direction == "BEARISH" and premium_discount == "PREMIUM")
    near_event = any(_zone_distance(event.level_price, lower, upper) <= 0.002 for event in liquidity_events)
    score = {"D1": 5, "H4": 4, "H1": 3, "M15": 2, "M5": 1}.get(timeframe, 0)
    score += 5 if setup_related else 0
    score += 4 if bias_alignment else -4
    score += 2 if pd_alignment else 0
    score += 2 if status == "FRESH" else 1 if status in {"TOUCHED", "PARTIALLY_MITIGATED"} else -5
    score += max(0.0, 2.0 - min(distance * 100, 2.0))
    score += min(displacement_strength, 3.0)
    score += 2 if near_event else 0
    score -= touch_count * 0.75
    if width_pct > 0.035:
        score -= 10
    quality = {
        "timeframe": timeframe,
        "direction": direction,
        "freshness": "FRESH" if status == "FRESH" else "USED",
        "touch_count": touch_count,
        "mitigation_status": status,
        "displacement_strength": round(displacement_strength, 3),
        "displacement_related": setup_related,
        "premium_discount_alignment": pd_alignment,
        "liquidity_event_proximity": near_event,
        "swing_bias_alignment": bias_alignment,
        "distance_to_price": round(distance, 5),
        "width_pct": round(width_pct, 5),
    }
    label = f"{direction} {zone_type} {timeframe} {lower:.2f}-{upper:.2f}"
    return POI(label, zone_type, direction, timeframe, lower, upper, midpoint, formed_at, status, quality, score, width_pct)


def _make_pois(
    price: float,
    bias: str,
    premium_discount: str,
    fvgs: list[FairValueGap],
    obs: list[OrderBlock],
    liquidity_events: list[LiquidityEvent],
) -> list[POI]:
    pois: list[POI] = []
    for zone in fvgs:
        setup_related = zone.setup_type == "SETUP_FVG"
        pois.append(_score_poi("FVG", zone.direction, zone.timeframe, zone.lower, zone.upper, zone.midpoint, zone.formed_at, zone.status, zone.touch_count, zone.displacement_strength, setup_related, price, bias, premium_discount, liquidity_events))
    for zone in obs:
        pois.append(_score_poi("OB", zone.direction, zone.timeframe, zone.low, zone.high, zone.midpoint, zone.formed_at, zone.status, zone.touch_count, zone.displacement_strength, True, price, bias, premium_discount, liquidity_events))
    valid = [poi for poi in pois if poi.status not in {"MITIGATED", "INVALIDATED"} and poi.width_pct <= 0.035]
    return sorted(valid, key=lambda poi: poi.score, reverse=True)


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
        lower = max(item.lower for item in overlaps)
        upper = min(item.upper for item in overlaps)
        if lower >= upper:
            best = max(overlaps, key=lambda item: item.score)
            clustered.append(best)
            continue
        best = max(overlaps, key=lambda item: item.score)
        width_pct = _zone_width_pct(best.midpoint, lower, upper)
        quality = dict(best.quality)
        quality["cluster_method"] = "intersection"
        quality["width_pct"] = round(width_pct, 5)
        clustered.append(POI(f"{best.direction} POI intersection {best.timeframe} {lower:.2f}-{upper:.2f}", "CLUSTER", best.direction, best.timeframe, lower, upper, (lower + upper) / 2, best.formed_at, best.status, quality, best.score + 1, width_pct))
    return sorted(clustered, key=lambda poi: poi.score, reverse=True)


def _poi_summary(pois: list[POI], bias: str) -> tuple[POI | None, POI | None, str]:
    clustered = _cluster_pois(pois)
    aligned = [poi for poi in clustered if poi.direction == bias]
    opposing = [poi for poi in clustered if poi.direction != bias]
    primary = aligned[0] if aligned else None
    secondary = aligned[1] if len(aligned) > 1 else opposing[0] if opposing else None
    conflict = "NONE"
    if primary:
        for other in opposing:
            if max(primary.lower, other.lower) <= min(primary.upper, other.upper):
                conflict = f"POI_CONFLICT: {primary.label} overlaps {other.label}"
                break
    return primary, secondary, conflict


def _poi_text(poi: POI | None) -> str:
    if not poi:
        return "NONE"
    quality = ", ".join(f"{key}={value}" for key, value in poi.quality.items())
    return f"{poi.label}; status={poi.status}; score={poi.score:.2f}; {quality}"


def _poi_width_text(poi: POI | None) -> str:
    if not poi:
        return "NONE"
    flag = "POI_TOO_WIDE" if poi.width_pct > 0.035 else "OK"
    return f"{poi.width_pct:.4%} {flag}"


def _sequence_state(
    swing_bias: str,
    phase: str,
    pullback_stage: str,
    sweep: LiquidityEvent | None,
    displacement: DisplacementEvent | None,
    mss: BreakEvent | None,
    setup_fvg: FairValueGap | None,
) -> tuple[str, list[SequenceTransition]]:
    transitions: list[SequenceTransition] = []

    def add(new_state: str, timestamp: int, evidence: str) -> None:
        previous = transitions[-1].new_state if transitions else "NONE"
        transitions.append(SequenceTransition(previous, new_state, timestamp, evidence))

    if swing_bias == "NONE" or phase == "REVERSAL_CANDIDATE":
        add("INVALIDATED", 0, "no valid swing bias or structural invalidation")
        return "INVALIDATED", transitions
    add("SEEKING_LIQUIDITY", 0, f"pullback_stage={pullback_stage}")
    if not sweep:
        return "SEEKING_LIQUIDITY", transitions
    add("LIQUIDITY_SWEPT", sweep.timestamp, f"{sweep.level_type} {sweep.level_price:.2f}")
    if not displacement:
        return "LIQUIDITY_SWEPT", transitions
    add("DISPLACEMENT_CONFIRMED", displacement.timestamp, _displacement_text(displacement))
    if not mss:
        return "DISPLACEMENT_CONFIRMED", transitions
    add("MSS_CONFIRMED", mss.timestamp, _break_text(mss))
    if not setup_fvg:
        return "MSS_CONFIRMED", transitions
    add("SETUP_FVG_CREATED", setup_fvg.formed_at, _setup_fvg_text(setup_fvg))
    add("RETEST_PENDING", setup_fvg.formed_at, "setup FVG awaits valid retest")
    return "RETEST_PENDING", transitions


def _state_model(
    swing_bias: str,
    phase: str,
    pullback_stage: str,
    primary_poi: POI | None,
    sequence_state: str,
    poi_conflict: str,
) -> tuple[str, list[str], list[str]]:
    missing: list[str] = []
    conflicts: list[str] = []
    if poi_conflict != "NONE":
        conflicts.append(poi_conflict)
    if swing_bias == "NONE":
        return "NO_TRADE", ["valid D1/H4 swing bias"], conflicts + ["no directional swing bias"]
    if phase == "REVERSAL_CANDIDATE" or sequence_state == "INVALIDATED":
        return "NO_TRADE", ["rebuild thesis after structural invalidation"], conflicts + ["structural invalidation"]
    if sequence_state == "EXPIRED_NO_TRIGGER":
        if not primary_poi:
            missing.append("high-quality thesis-aligned POI")
        missing.append("new pullback sequence")
        return "WAIT", missing, conflicts
    if sequence_state == "SEEKING_LIQUIDITY":
        missing.append("tactical liquidity sweep")
    elif sequence_state == "LIQUIDITY_SWEPT":
        missing.extend(["valid displacement", "contextual MSS/BOS confirmation", "SETUP_FVG or valid OB"])
    elif sequence_state == "DISPLACEMENT_CONFIRMED":
        missing.extend(["contextual MSS/BOS confirmation", "SETUP_FVG or valid OB"])
    elif sequence_state == "MSS_CONFIRMED":
        missing.append("SETUP_FVG or valid OB")
    if not primary_poi:
        missing.append("high-quality thesis-aligned POI")
    if sequence_state == "RETEST_PENDING":
        return "ARMED", missing, conflicts
    if sequence_state in {"LIQUIDITY_SWEPT", "MSS_CONFIRMED", "DISPLACEMENT_CONFIRMED"} or pullback_stage in {"LIQUIDITY_TAKEN", "RECLAIMED", "REACCELERATION"}:
        return "WATCH", missing, conflicts
    return "WAIT", missing, conflicts


def _is_v2_state(previous_thesis: dict | None) -> bool:
    return bool(previous_thesis and (previous_thesis.get("state_schema") == "review-state.v2" or previous_thesis.get("persistence_version") == 2))


def _previous_sequence_state(previous_thesis: dict | None) -> str:
    return str((previous_thesis or {}).get("sequence_state") or (previous_thesis or {}).get("previous_sequence_state") or "")

def _sequence_id(symbol: str, previous_thesis: dict | None) -> str:
    if previous_thesis and previous_thesis.get("sequence_id"):
        return str(previous_thesis["sequence_id"])
    return f"{symbol}-seq-0001"


def _next_sequence_id(symbol: str, previous_thesis: dict | None) -> str:
    current = _sequence_id(symbol, previous_thesis)
    prefix = f"{symbol}-seq-"
    if current.startswith(prefix):
        try:
            return f"{prefix}{int(current.removeprefix(prefix)) + 1:04d}"
        except ValueError:
            pass
    return f"{prefix}0001"


def _has_htf_continuation_evidence(
    structures: dict[str, Structure],
    swing_bias: str,
    after_timestamp: int,
    displacement: DisplacementEvent | None = None,
) -> bool:
    if structures["H1"].state != swing_bias:
        return False
    tactical_aligned = structures["M15"].state == swing_bias and structures["M5"].state == swing_bias
    if not tactical_aligned:
        return False
    for timeframe in ("H1", "H4", "D1"):
        for event in (structures[timeframe].last_bos, structures[timeframe].last_mss):
            if event and event.direction == swing_bias and event.timestamp > after_timestamp:
                return True
    if (
        displacement
        and displacement.direction == swing_bias
        and displacement.timestamp > after_timestamp
        and displacement.fvg_created
        and displacement.strength in {"VALID", "STRONG"}
    ):
        return True
    return False


def _should_expire_sequence(
    previous_thesis: dict | None,
    phase: str,
    active_target: LiquidityLevel | None,
    latest_sweep: LiquidityEvent | None,
    structures: dict[str, Structure],
    swing_bias: str,
    sequence_started_at: int,
    displacement: DisplacementEvent | None = None,
) -> bool:
    if not previous_thesis:
        return False
    previous_phase = previous_thesis.get("current_phase") or previous_thesis.get("previous_phase")
    previous_sequence = previous_thesis.get("sequence_state") or previous_thesis.get("previous_sequence_state")
    return (
        previous_phase == "PULLBACK"
        and previous_sequence == "SEEKING_LIQUIDITY"
        and active_target is not None
        and latest_sweep is None
        and phase == "CONTINUATION"
        and _has_htf_continuation_evidence(structures, swing_bias, sequence_started_at, displacement)
    )


def _expired_transition(active_target: LiquidityLevel, phase: str, timestamp: int) -> SequenceTransition:
    evidence = f"old_active_target={active_target.type} {active_target.price:.2f} on {active_target.timeframe}; phase={phase}; continuation confirmed without active sweep"
    return SequenceTransition("SEEKING_LIQUIDITY", "EXPIRED_NO_TRIGGER", timestamp, evidence)

SEQUENCE_ORDER = {
    "SEEKING_LIQUIDITY": 1,
    "LIQUIDITY_SWEPT": 2,
    "DISPLACEMENT_CONFIRMED": 3,
    "MSS_CONFIRMED": 4,
    "SETUP_FVG_CREATED": 5,
    "RETEST_PENDING": 6,
}
TERMINAL_SEQUENCE_STATES = {"EXPIRED_NO_TRIGGER", "INVALIDATED"}


def _last_transition_for_state(transitions: list[SequenceTransition], state: str) -> SequenceTransition | None:
    for transition in reversed(transitions):
        if transition.new_state == state:
            return transition
    return None


def _resolve_sequence_lifecycle(
    previous_thesis: dict | None,
    sequence_id: str,
    computed_state: str,
    computed_transitions: list[SequenceTransition],
    new_sequence_started: bool,
) -> tuple[str, list[SequenceTransition], str, str]:
    previous_state = _previous_sequence_state(previous_thesis)
    if not previous_state or not _is_v2_state(previous_thesis) or new_sequence_started:
        transition = _last_transition_for_state(computed_transitions, computed_state)
        reason = transition.evidence if transition else "new sequence initialized"
        return computed_state, computed_transitions, f"NEW_SEQUENCE {sequence_id}", reason
    if previous_state in TERMINAL_SEQUENCE_STATES:
        return previous_state, [], "NO_TRANSITION", f"terminal persisted state {previous_state}"
    if computed_state == "INVALIDATED":
        transition = _last_transition_for_state(computed_transitions, "INVALIDATED") or SequenceTransition(previous_state, "INVALIDATED", 0, "structural invalidation")
        return "INVALIDATED", [transition], f"{previous_state} -> INVALIDATED", transition.evidence
    if previous_state == "SEEKING_LIQUIDITY" and computed_state == "EXPIRED_NO_TRIGGER":
        transition = _last_transition_for_state(computed_transitions, "EXPIRED_NO_TRIGGER") or SequenceTransition(previous_state, "EXPIRED_NO_TRIGGER", 0, "sequence expired before active liquidity sweep")
        return "EXPIRED_NO_TRIGGER", [transition], "SEEKING_LIQUIDITY -> EXPIRED_NO_TRIGGER", transition.evidence
    previous_rank = SEQUENCE_ORDER.get(previous_state, 0)
    computed_rank = SEQUENCE_ORDER.get(computed_state, 0)
    if computed_rank > previous_rank:
        transition = _last_transition_for_state(computed_transitions, computed_state) or SequenceTransition(previous_state, computed_state, 0, "forward lifecycle evidence")
        if transition.previous_state != previous_state:
            transition = SequenceTransition(previous_state, transition.new_state, transition.timestamp, transition.evidence)
        return computed_state, [transition], f"{previous_state} -> {computed_state}", transition.evidence
    return previous_state, [], "NO_TRANSITION", f"persisted state {previous_state} remains authoritative"


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


def review_symbol(frames: dict[str, MarketDataFrame], previous_thesis: dict | None = None, state_loaded_from: str = "NONE") -> Review:
    validate_generation(frames)
    structures = {timeframe: analyze_structure(frames[timeframe]) for timeframe in TIMEFRAMES}
    liquidity_by_tf = {timeframe: find_liquidity(frames[timeframe], structures[timeframe]) for timeframe in TIMEFRAMES}
    liquidity = [level for levels in liquidity_by_tf.values() for level in levels]
    liquidity_events = [event for timeframe, levels in liquidity_by_tf.items() for event in find_liquidity_events(frames[timeframe], levels)]
    displacements_by_tf = {timeframe: find_displacements(frames[timeframe], structures[timeframe]) for timeframe in TIMEFRAMES}
    displacements = [event for events in displacements_by_tf.values() for event in events]
    fvgs = [gap for timeframe in TIMEFRAMES for gap in find_fvgs(frames[timeframe], displacements_by_tf[timeframe])]
    order_blocks = [block for timeframe in TIMEFRAMES for block in find_order_blocks(frames[timeframe], structures[timeframe], displacements_by_tf[timeframe])]
    price = _current_price(frames)
    swing_bias = _swing_bias(structures)
    phase = _current_phase(swing_bias, structures, price)
    premium_discount = _premium_discount(frames["H4"], structures["H4"])
    macro_draw, candidate_tactical_draw, candidate_tactical_level = _liquidity_draws(swing_bias, phase, liquidity, price)
    active_tactical_level, candidate_tactical_level, active_status, candidate_status, target_changed, target_reason = _resolve_tactical_targets(
        previous_thesis,
        liquidity,
        candidate_tactical_level,
        liquidity_events,
        price,
    )
    liquidity_events = _add_active_target_events(frames, liquidity_events, active_tactical_level)
    active_selected_at = _active_target_selected_at(previous_thesis)
    if active_tactical_level and active_selected_at == 0:
        active_selected_at = _latest_closed_timestamp(frames)
    sequence_started_at = _sequence_started_at(previous_thesis, active_selected_at)
    tactical_events = _events_for_active_target(liquidity_events, active_tactical_level, active_selected_at)
    active_status = _draw_status(tactical_events)
    tactical_draw = _draw_text("Tactical Draw", active_tactical_level, price, f"{phase} phase active sequence target")
    sequence_id = _sequence_id(frames["D1"].symbol, previous_thesis)
    loaded_sequence_id = str((previous_thesis or {}).get("sequence_id") or "NONE")
    loaded_sequence_state = _previous_sequence_state(previous_thesis) or "NONE"
    loaded_active_target = _previous_active_target(previous_thesis, liquidity)
    new_sequence_started = False
    if _is_v2_state(previous_thesis) and not _previous_active_target(previous_thesis, liquidity) and _previous_sequence_state(previous_thesis) == "EXPIRED_NO_TRIGGER":
        if phase == "PULLBACK" and active_tactical_level:
            sequence_id = _next_sequence_id(frames["D1"].symbol, previous_thesis)
            sequence_started_at = active_selected_at
            target_changed = "YES"
            target_reason = "NEW_SEQUENCE_STARTED"
            new_sequence_started = True
        else:
            active_tactical_level = None
            tactical_events = []
            active_status = "NONE"
            tactical_draw = _draw_text("Tactical Draw", None, price, "awaiting new pullback sequence")
            target_changed = "NO"
            target_reason = "AWAITING_NEW_PULLBACK"
    pullback_stage = _pullback_stage(phase, tactical_events, displacements, swing_bias)
    latest_tactical_sweep = max([event for event in tactical_events if event.event_type == "SWEPT"], key=lambda event: event.timestamp, default=None)
    displacement = _latest_displacement([event for event in displacements if not latest_tactical_sweep or event.timestamp > latest_tactical_sweep.timestamp], swing_bias)
    contextual_mss = _contextual_mss(structures, latest_tactical_sweep, displacement, swing_bias)
    setup_fvg = _setup_fvg(fvgs, latest_tactical_sweep, displacement, contextual_mss, swing_bias)
    sequence_state, transitions = _sequence_state(swing_bias, phase, pullback_stage, latest_tactical_sweep, displacement, contextual_mss, setup_fvg)
    if _should_expire_sequence(previous_thesis, phase, active_tactical_level, latest_tactical_sweep, structures, swing_bias, sequence_started_at, displacement):
        expired_target = active_tactical_level
        sequence_state = "EXPIRED_NO_TRIGGER"
        transitions = [SequenceTransition("NONE", "SEEKING_LIQUIDITY", 0, "pullback sequence expired before active liquidity sweep"), _expired_transition(expired_target, phase, _latest_closed_timestamp(frames))]
        active_tactical_level = None
        tactical_events = []
        active_status = "NONE"
        tactical_draw = _draw_text("Tactical Draw", None, price, "expired before active liquidity sweep")
        target_changed = "YES"
        target_reason = "EXPIRED_NO_TRIGGER"
    sequence_state, transitions, transition, transition_reason = _resolve_sequence_lifecycle(
        previous_thesis,
        sequence_id,
        sequence_state,
        transitions,
        new_sequence_started,
    )
    if sequence_state == "EXPIRED_NO_TRIGGER" and not new_sequence_started:
        active_tactical_level = None
        tactical_events = []
        active_status = "NONE"
        tactical_draw = _draw_text("Tactical Draw", None, price, "expired sequence awaiting new pullback genesis")
        if target_reason not in {"EXPIRED_NO_TRIGGER", "AWAITING_NEW_PULLBACK"}:
            target_changed = "NO"
            target_reason = "AWAITING_NEW_PULLBACK"
    pois = _make_pois(price, swing_bias, premium_discount, fvgs, order_blocks, liquidity_events)
    primary_poi, secondary_poi, poi_conflict = _poi_summary(pois, swing_bias)
    state, missing, conflicts = _state_model(swing_bias, phase, pullback_stage, primary_poi, sequence_state, poi_conflict)
    d1_bias = structures["D1"].state if structures["D1"].state in {"BULLISH", "BEARISH"} else "RANGE"
    return Review(
        Symbol=frames["D1"].symbol,
        Persistence_Version="review-state.v2",
        State_Loaded_From=state_loaded_from,
        Active_Draw_Selected_At=str(active_selected_at) if active_tactical_level else "NONE",
        Sequence_Started_At=str(sequence_started_at) if active_tactical_level or sequence_state == "EXPIRED_NO_TRIGGER" else "NONE",
        Sequence_ID=sequence_id,
        Loaded_Sequence_ID=loaded_sequence_id,
        Loaded_Sequence_State=loaded_sequence_state,
        Loaded_Active_Target=_level_text(loaded_active_target, price),
        Current_Sequence_ID=sequence_id,
        Current_Sequence_State=sequence_state,
        Current_Active_Target=_level_text(active_tactical_level, price),
        Transition=transition,
        Transition_Reason=transition_reason,
        Review_Timestamp=str(_latest_closed_timestamp(frames)),
        Swing_Bias=swing_bias,
        Current_Phase=phase,
        Pullback_Stage=pullback_stage,
        Market_Regime=_market_regime(phase),
        D1_Structure=_structure_text(structures["D1"]),
        H4_Structure=_structure_text(structures["H4"]),
        H1_Structure=_structure_text(structures["H1"]),
        Last_Structure_Events={tf: _event_timeline_text(structures[tf]) for tf in TIMEFRAMES},
        Structural_Invalidation={tf: structures[tf].structural_invalidation_type for tf in TIMEFRAMES},
        Macro_Draw_on_Liquidity=macro_draw,
        Tactical_Draw_on_Liquidity=tactical_draw,
        Active_Tactical_Draw=_level_text(active_tactical_level, price),
        Candidate_Tactical_Draw=_level_text(candidate_tactical_level, price),
        Active_Draw_Status=active_status,
        Candidate_Draw_Status=candidate_status,
        Target_Changed=target_changed,
        Target_Change_Reason=target_reason,
        Premium_Discount=premium_discount,
        Liquidity_Event=_latest_liquidity_event(tactical_events),
        Displacement=_displacement_text(displacement),
        Contextual_MSS=_contextual_mss_text(contextual_mss, latest_tactical_sweep, displacement),
        Setup_FVG=_setup_fvg_text(setup_fvg),
        Primary_POI=_poi_text(primary_poi),
        Secondary_POI=_poi_text(secondary_poi),
        POI_Width=_poi_width_text(primary_poi),
        POI_Conflict=poi_conflict,
        Sequence_State=sequence_state,
        Sequence_Transitions=[asdict(transition) for transition in transitions],
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
        Liquidity_Events=[asdict(event) for event in liquidity_events[-24:]],
        FVG=[asdict(gap) for gap in fvgs[-24:]],
        Order_Blocks=[asdict(block) for block in order_blocks[-12:]],
    )
