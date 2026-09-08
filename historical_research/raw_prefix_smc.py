"""Outcome-blind, uncapped raw-prefix research; never a production authority."""
import ast
import hashlib
import inspect
import json
import textwrap
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import asdict

from market_reviewer import reviewer as rv
from market_reviewer.model import TIMEFRAME_SECONDS as TF
from .smc_ancestry_v11 import visible, WINDOW_PIVOT_SPAN

VERSION = "smc-provenance-raw-prefix.v1"
# Research bridge candidates, not a change to v1.1 accepted ancestry.
SWEEP_TFS = {"D1": ("D1", "H4"), "H4": ("H4", "H1"),
             "H1": ("H1", "M15", "M5"), "M15": ("M15", "M5"), "M5": ("M5",)}
STRUCTURE_TFS = {"M5": ("M5", "M15", "H1"), "M15": ("M15", "H1"),
                 "H1": ("H1", "H4"), "H4": ("H4", "D1"), "D1": ("D1",)}


def uid(kind, *parts):
    body = json.dumps([VERSION, kind, *parts], sort_keys=True, separators=(",", ":"))
    return kind + "-" + hashlib.sha256(body.encode()).hexdigest()[:24]


def legacy_id(kind, symbol, tf, direction, timestamp, anchor):
    body = json.dumps([kind, symbol, tf, direction, timestamp, anchor], sort_keys=True, separators=(",", ":"))
    return kind + "-" + hashlib.sha256(body.encode()).hexdigest()[:24]


def uncapped(function, variable, cap):
    """Compile a private copy with exactly one known display return slice removed.

    No globals are patched. Fail closed if the detector source contract changes.
    All detector predicates and intermediate calculations remain byte-for-byte AST.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    matches = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Subscript):
            continue
        val = node.value
        if (isinstance(val.value, ast.Name) and val.value.id == variable
                and isinstance(val.slice, ast.Slice) and val.slice.upper is None
                and val.slice.step is None and isinstance(val.slice.lower, ast.UnaryOp)
                and isinstance(val.slice.lower.op, ast.USub)
                and isinstance(val.slice.lower.operand, ast.Constant)
                and val.slice.lower.operand.value == cap):
            matches.append(node)
    if len(matches) != 1:
        raise ValueError("uncapped detector contract changed: " + function.__name__)
    matches[0].value = ast.Name(id=variable, ctx=ast.Load())
    ast.fix_missing_locations(tree)
    namespace = dict(function.__globals__)
    exec(compile(tree, "<research-uncapped-" + function.__name__ + ">", "exec"), namespace)
    return namespace[function.__name__]


RAW_FVG = uncapped(rv.find_fvgs, "gaps", 16)
RAW_OB = uncapped(rv.find_order_blocks, "blocks", 8)
RAW_EQUAL = uncapped(rv._equal_levels, "levels", 3)


class Prefix:
    def __init__(self, frames, checkpoint):
        self.frames = {tf: visible(f, checkpoint) for tf, f in frames.items()}
        if any(f is None for f in self.frames.values()):
            raise ValueError("empty legal prefix")
        self.checkpoint = checkpoint
        self.cache = {}
        self.candles = {tf: f.closed_candles() for tf, f in self.frames.items()}
        self.by_open = {tf: {c.timestamp: c for c in cs} for tf, cs in self.candles.items()}

    def at(self, tf, at):
        at = min(at, self.checkpoint)
        key = (tf, at)
        if key not in self.cache:
            f = visible(self.frames[tf], at)
            st = rv.analyze_structure(f) if f else None
            self.cache[key] = (f, st, rv.find_displacements(f, st) if f else [])
        return self.cache[key]


def event(symbol, tf, kind, direction, timestamp, available_at, evidence, source, checkpoint, anchor):
    if available_at > checkpoint or timestamp + TF[tf] > available_at:
        raise ValueError("event not closed/available at origin")
    return {"event_id": legacy_id(kind, symbol, tf, direction, timestamp, anchor),
            "symbol": symbol, "timeframe": tf, "kind": kind, "direction": direction,
            "timestamp": timestamp, "open_timestamp": timestamp, "available_at": available_at,
            "evidence": evidence, "status": evidence.get("status", evidence.get("strength", kind)),
            "source": dict(source), "authority": "RAW_PREFIX_RESEARCH",
            "decision_time_context": {"checkpoint": checkpoint, "attributes_known_at": checkpoint}}


def extract(frames, symbol, checkpoint, source):
    p = Prefix(frames, checkpoint)
    events = {}; exact = []; levels = []
    def add(tf, kind, di, ts, at, evidence, anchor, mode="EXACT_CONFIRMATION"):
        e = event(symbol, tf, kind, di, ts, at, evidence, source, checkpoint, anchor)
        e["availability_provenance"] = mode
        events[e["event_id"]] = e
        return e
    def edge(a, b, typ, proof):
        exact.append(make_relation(a, b, typ, proof, exact=True))
    for tf, frame in p.frames.items():
        cs = p.candles[tf]; pos = {c.timestamp: i for i, c in enumerate(cs)}
        _, structure, displacements = p.at(tf, checkpoint)
        dmap = {}
        for d in displacements:
            i = pos[d.timestamp]; known = checkpoint; mode = "ORIGIN_VERIFIED_UPPER_BOUND"
            first = None
            for n in range(i + 1, min(i + 3, len(cs)) + 1):
                at = cs[n - 1].timestamp + TF[tf]
                _, _, ds = p.at(tf, at)
                first = next((x for x in ds if x.timestamp == d.timestamp and x.direction == d.direction
                              and (x.strength in {"VALID", "STRONG"} if d.strength in {"VALID", "STRONG"} else True)), None)
                if first:
                    known = at; mode = "FIRST_ELIGIBLE_PREFIX"; break
            e = add(tf, "DISPLACEMENT", d.direction, d.timestamp, known, asdict(d), {}, mode)
            # Relation qualification consumes confirmation-time evidence, not origin-time strength upgrades.
            e["confirmation_evidence"] = asdict(first or d)
            dmap[d.timestamp] = e
        for s in structure.events:
            at = s.timestamp + TF[tf]; _, ss, _ = p.at(tf, at)
            verified = any((x.kind, x.direction, x.timestamp, x.price) ==
                           (s.kind, s.direction, s.timestamp, s.price) for x in ss.events)
            add(tf, s.kind, s.direction, s.timestamp, at if verified else checkpoint,
                asdict(s), {"price": s.price}, "EVENT_CLOSE_PREFIX" if verified else "ORIGIN_VERIFIED_UPPER_BOUND")
        # Every historically confirmed swing is a reference, not just the latest display cursor.
        raw_levels = []
        for s in structure.swings:
            scope = "External" if tf in rv.HTF_TIMEFRAMES else "Internal"
            typ = scope + (" Buy-side Liquidity" if s.kind == "HIGH" else " Sell-side Liquidity")
            raw_levels.append(rv.LiquidityLevel(s.price, typ, tf, s.formed_at, "UNSWEPT"))
        raw_levels.extend(RAW_EQUAL(frame, structure.swings))
        for l0 in raw_levels:
            l = rv._with_liquidity_id(l0); i = pos[l.formed_at]
            at = cs[i + 2].timestamp + TF[tf]
            pf, ps, _ = p.at(tf, at)
            known = any(x.price == l.price and x.type == l.type and x.formed_at == l.formed_at
                        for x in rv.find_liquidity(pf, ps) + RAW_EQUAL(pf, ps.swings))
            if not known:
                at = checkpoint
            di = "BEARISH" if "Buy-side" in l.type or "Highs" in l.type else "BULLISH"
            le = add(tf, "LIQUIDITY", di, l.formed_at, at, asdict(l),
                     {"liquidity_id": l.liquidity_id, "price": l.price},
                     "TWO_RIGHT_BARS_PREFIX" if known else "ORIGIN_VERIFIED_UPPER_BOUND")
            levels.append(le)
        for z in RAW_FVG(frame, displacements):
            ze = add(tf, "FVG", z.direction, z.formed_at, z.formed_at + TF[tf], asdict(z),
                     {"lower": z.lower, "upper": z.upper})
            d = dmap.get(z.formed_at)
            if d and d["direction"] == z.direction and d["confirmation_evidence"]["strength"] in {"VALID", "STRONG"}:
                edge(d, ze, "FVG_CREATED_BY", {"creation": "same third candle; exact three-candle geometry",
                                             "research_type": "CHAIN_CANDIDATE_FVG"})
        blocks = RAW_OB(frame, structure, displacements)
        causes = []
        for d in displacements:
            if d.strength not in {"VALID", "STRONG"} or d.structure_broken == "NONE":
                continue
            i = pos[d.timestamp]
            if any(c.close < c.open if d.direction == "BULLISH" else c.close > c.open
                   for c in cs[max(0, i - 10):i]):
                causes.append(d)
        if len(blocks) != len(causes):
            raise ValueError("OB detector ancestry mismatch")
        for z, cause in zip(blocks, causes):
            d = dmap[cause.timestamp]
            # OB proof may only appear later than the first qualifying D strength.
            at = d["available_at"]
            pf, ps, ds = p.at(tf, at)
            confirmed = any(x.timestamp == cause.timestamp and x.direction == cause.direction
                            and x.strength in {"VALID", "STRONG"} and x.structure_broken != "NONE" for x in ds)
            if not confirmed:
                at = checkpoint
            ze = add(tf, "OB", z.direction, z.formed_at, at, asdict(z),
                     {"low": z.low, "high": z.high, "displacement_id": d["event_id"]})
            edge(d, ze, "OB_ASSOCIATED_WITH", {"creation": "exact detector last-opposite source within ten bars",
                                            "research_type": "CHAIN_CANDIDATE_OB"})
    # A level cannot be swept before it is confirmed. Same candle as confirmation is excluded.
    for le in sorted(levels, key=lambda e: e["event_id"]):
        price = le["evidence"]["price"]; bull = le["direction"] == "BULLISH"
        for tf in SWEEP_TFS[le["timeframe"]]:
            cs = [c for c in p.candles.get(tf, []) if c.timestamp >= le["available_at"]]
            sw = None; rec = None; rejection = None
            for c in cs:
                at = c.timestamp + TF[tf]
                beyond = c.low < price if bull else c.high > price
                inside = c.close > price if bull else c.close < price
                if sw is None and beyond:
                    ev = {"level_price": price, "level_type": le["evidence"]["type"],
                          "liquidity_event_id": le["event_id"], "liquidity_timeframe": le["timeframe"],
                          "sweep_price": c.low if bull else c.high, "penetration": price-c.low if bull else c.high-price,
                          "close_location": "OUTSIDE" if (c.close < price if bull else c.close > price) else "INSIDE", "candle": asdict(c)}
                    sw = add(tf, "SWEPT", le["direction"], c.timestamp, at, ev, {"liquidity_id": le["event_id"]})
                    edge(le, sw, "SWEEP_OF", {"exact_reference": le["event_id"], "price": price})
                    continue
                if sw is None:
                    continue
                end = sw["available_at"] + WINDOW_PIVOT_SPAN * TF[le["timeframe"]]
                if rec is None and inside:
                    ev = {"level_price": price, "level_type": le["evidence"]["type"],
                          "close_location": "INSIDE", "candle": asdict(c)}
                    rec = add(tf, "RECLAIMED", le["direction"], c.timestamp, at, ev, {"liquidity_id": le["event_id"]})
                    edge(sw, rec, "RECLAIM_AFTER", {"same_reference": le["event_id"]})
                if at <= end and rejection is None and beyond and inside and (c.close > c.open if bull else c.close < c.open):
                    rejection = add(tf, "REJECTION", le["direction"], c.timestamp, at,
                                    {"level_price": price, "close_location": "INSIDE", "candle": asdict(c)},
                                    {"sweep_id": sw["event_id"]})
                    edge(sw, rejection, "REJECTION_AFTER", {"same_reference": le["event_id"]})
                if rec and (rejection or at > end):
                    break
            if tf == le["timeframe"]:
                le["status"] = "RECLAIMED" if rec else "SWEPT" if sw else "UNSWEPT"
                le["evidence"]["status"] = le["status"]
    return p, events, exact


def make_relation(parent, child, kind, proof, exact=False):
    if parent["symbol"] != child["symbol"] or parent["direction"] != child["direction"]:
        raise ValueError("relation direction/symbol")
    key = uid("REL", kind, parent["event_id"], child["event_id"])
    return {"relation_id": key, "parent": parent["event_id"], "child": child["event_id"],
            "type": kind, "available_at": max(parent["available_at"], child["available_at"]),
            "proof": proof, "ancestry_state": "EXACT" if exact else "UNAMBIGUOUS",
            "candidate_only": True}


class Index:
    def __init__(self, events):
        self.groups = defaultdict(list)
        for e in events.values():
            for direction in (None, e["direction"]):
                self.groups[(e["symbol"], e["timeframe"], e["kind"], direction)].append(e)
        self.times = {}
        for k, es in self.groups.items():
            es.sort(key=lambda e: (e["timestamp"], e["event_id"]))
            self.times[k] = [e["timestamp"] for e in es]

    def between(self, symbol, tf, kind, start, end, direction=None):
        k = symbol, tf, kind, direction
        times = self.times.get(k, [])
        return self.groups.get(k, [])[bisect_left(times, start):bisect_right(times, end)]


def candidate_reason(parent, child, end, proof):
    if parent["symbol"] != child["symbol"] or parent["direction"] != child["direction"]:
        return "DIRECTION_MISMATCH"
    if child["timestamp"] < parent["available_at"] or child["available_at"] > end:
        return "TEMPORAL_ORDER"
    if child["timeframe"] not in SWEEP_TFS[parent["timeframe"]]:
        return "TIMEFRAME_INCOMPATIBLE"
    if proof.get("context_break"):
        return "CONTEXT_BREAK"
    if not proof.get("price_local"):
        return "PRICE_UNRELATED"
    if child.get("confirmation_evidence", child["evidence"]).get("strength") not in {"VALID", "STRONG"}:
        return "WEAK_DISPLACEMENT"
    return "PASS"


def locality(prefix, index, sweep, parent, child):
    tf = child["timeframe"]; bull = child["direction"] == "BULLISH"
    c = prefix.by_open[tf][child["timestamp"]]
    price = sweep["evidence"]["level_price"]
    rc = prefix.by_open[parent["timeframe"]].get(parent["timestamp"])
    interacts = ((c.low <= rc.high and c.high >= rc.low) if parent["kind"] != "SWEPT" and rc else
                 (c.open <= price < c.close if bull else c.open >= price > c.close))
    _, pre, _ = prefix.at(tf, sweep["timestamp"])
    ref = (pre.last_swing_high if bull else pre.last_swing_low) if pre else None
    protected = (pre.protected_low if bull else pre.protected_high) if pre else None
    start = sweep["available_at"]; end = child["timestamp"]
    broken = set()
    for kind in ("DISPLACEMENT", "BOS", "MSS"):
        for e in index.between(child["symbol"], tf, kind, start, end - 1):
            if e["available_at"] > child["available_at"] or e["direction"] == child["direction"]:
                continue
            if kind != "DISPLACEMENT" or e["confirmation_evidence"]["strength"] in {"VALID", "STRONG"}:
                broken.add("OPPOSING_" + kind)
    for x in prefix.candles[tf]:
        if not start <= x.timestamp < end:
            continue
        extreme = sweep["evidence"]["sweep_price"]
        if x.close < extreme if bull else x.close > extreme:
            broken.add("SWEEP_EXTREME_INVALIDATED")
        if parent["kind"] != "SWEPT" and x.timestamp >= parent["available_at"] and (x.close < price if bull else x.close > price):
            broken.add("LIQUIDITY_THESIS_INVALIDATED")
        if pre and pre.state == child["direction"] and protected is not None and (x.close < protected if bull else x.close > protected):
            broken.add("PROTECTED_LEVEL_LOSS")
    return {"price_local": bool(interacts), "reference_checkpoint": sweep["timestamp"],
            "confirmed_reference": asdict(ref) if ref else None,
            "structure_penetrating": bool(ref and (c.close > ref.price if bull else c.close < ref.price)),
            "context_break": sorted(broken), "candle": asdict(c)}


def ambiguity(relations, events):
    parents = defaultdict(set); children = defaultdict(set); direct_children = defaultdict(set)
    for r in relations:
        if r["type"] == "DISPLACEMENT_CANDIDATE":
            # Reclaim and rejection of one sweep are not independent parent ancestries.
            root = r["proof"]["sweep_id"]
            parents[r["child"]].add(root); children[root].add(r["child"])
            direct_children[r["parent"]].add(r["child"])
    for r in relations:
        if r["type"] == "DISPLACEMENT_CANDIDATE":
            np = len(parents[r["child"]]); nc = len(children[r["proof"]["sweep_id"]])
            r.update(candidate_parent_count=np, candidate_child_count=nc,
                     ancestry_state="UNAMBIGUOUS" if np == nc == 1 else "AMBIGUOUS")
    for e in events.values():
        if e["kind"] == "DISPLACEMENT":
            n = len(parents[e["event_id"]])
            e["candidate_parent_count"] = n
            e["parent_state"] = "NO_PLAUSIBLE_PARENT" if not n else "UNAMBIGUOUS_PARENT" if n == 1 else "MULTIPLE_PLAUSIBLE_PARENTS"
        if e["kind"] == "SWEPT":
            e["candidate_child_count"] = len(children[e["event_id"]])
        elif e["kind"] in {"RECLAIMED", "REJECTION"}:
            e["candidate_child_count"] = len(direct_children[e["event_id"]])


def reconstruct(graph, frames):
    cp = graph["origin_checkpoint"]
    first = next(iter(graph["events"].values()))
    p, events, relations = extract(frames, first["symbol"], cp, first["source"])
    index = Index(events)
    sweeps = {r["child"]: r["parent"] for r in relations if r["type"] == "SWEEP_OF"}
    reactions = defaultdict(list)
    for r in relations:
        if r["type"] in {"RECLAIM_AFTER", "REJECTION_AFTER"}:
            reactions[r["parent"]].append(events[r["child"]])
    diagnostics = []; peak = 0
    for swid, lid in sorted(sweeps.items()):
        sw = events[swid]; level = events[lid]
        end = min(cp, sw["available_at"] + WINDOW_PIVOT_SPAN * TF[level["timeframe"]])
        for parent in [sw] + sorted(reactions[swid], key=lambda e: e["event_id"]):
            considered = []
            for tf in TF:
                for d in index.between(sw["symbol"], tf, "DISPLACEMENT", parent["available_at"], end):
                    proof = locality(p, index, sw, parent, d)
                    reason = candidate_reason(parent, d, end, proof)
                    classified = {"candidate_relation_id": uid("REL", "DISPLACEMENT_CANDIDATE", parent["event_id"], d["event_id"]),
                                  "parent": parent["event_id"], "sweep_id": swid, "child": d["event_id"],
                                  "reason": reason, "expected_direction": d["direction"] == sw["direction"],
                                  "tf_relation": "SAME_TF" if tf == parent["timeframe"] else "LOWER_TF" if TF[tf] < TF[parent["timeframe"]] else "HIGHER_TF",
                                  "structure_penetrating": proof["structure_penetrating"],
                                  "with_fvg": d["confirmation_evidence"]["fvg_created"],
                                  "context_break": proof["context_break"], "price_local": proof["price_local"],
                                  "ancestry_state": "INVALID" if reason != "PASS" else "UNAMBIGUOUS"}
                    considered.append(classified)
                    if reason == "PASS":
                        relations.append(make_relation(parent, d, "DISPLACEMENT_CANDIDATE",
                                                       dict(proof, sweep_id=swid, liquidity_id=lid, tf_relation=classified["tf_relation"])))
            peak = max(peak, len(considered)); diagnostics.extend(considered)
    ambiguity(relations, events)
    relation_states = {r["relation_id"]: r["ancestry_state"] for r in relations}
    per_parent = defaultdict(lambda: defaultdict(int))
    for d in diagnostics:
        d["ancestry_state"] = relation_states.get(d["candidate_relation_id"], "INVALID")
        counts = per_parent[d["parent"]]
        for label in (d["tf_relation"], "EXPECTED_DIRECTION" if d["expected_direction"] else "OPPOSITE_DIRECTION",
                      "STRUCTURE_PENETRATING" if d["structure_penetrating"] else "NON_STRUCTURE_PENETRATING",
                      "WITH_FVG" if d["with_fvg"] else "WITHOUT_FVG"):
            counts[label] += 1
        counts["raw_candidate_count"] += 1
    for e in events.values():
        if e["kind"] in {"RECLAIMED", "REJECTION"}:
            e["raw_displacement_candidate_classes"] = dict(per_parent[e["event_id"]])
    # Structure bridge requires a concrete reference known at D open and D-body penetration.
    # Temporal proximity alone never establishes structural ancestry.
    for d in sorted((e for e in events.values() if e["kind"] == "DISPLACEMENT"), key=lambda e: e["event_id"]):
        if d["confirmation_evidence"]["strength"] not in {"VALID", "STRONG"}:
            continue
        c = p.by_open[d["timeframe"]][d["timestamp"]]
        for tf in STRUCTURE_TFS[d["timeframe"]]:
            end = min(cp, d["available_at"] + WINDOW_PIVOT_SPAN * TF[tf])
            for kind in ("BOS", "MSS"):
                for s in index.between(d["symbol"], tf, kind, d["timestamp"], end, direction=d["direction"]):
                    if s["direction"] != d["direction"] or s["available_at"] > end:
                        continue
                    exact = tf == d["timeframe"] and s["timestamp"] == d["timestamp"]
                    if not exact and s["timestamp"] < d["available_at"]:
                        continue
                    _, pre, _ = p.at(tf, d["timestamp"])
                    ref = (pre.last_swing_high if d["direction"] == "BULLISH" else pre.last_swing_low) if pre else None
                    price = s["evidence"]["price"]
                    protected = (pre.protected_high if d["direction"] == "BULLISH" else pre.protected_low) if pre else None
                    if not pre or not ((ref and ref.price == price) or protected == price):
                        continue
                    crosses = c.open <= price < c.close if d["direction"] == "BULLISH" else c.open >= price > c.close
                    if not crosses:
                        continue
                    contrary = any(e["direction"] != d["direction"] and e["available_at"] <= s["available_at"]
                                   for k in ("BOS", "MSS", "DISPLACEMENT")
                                   for e in index.between(d["symbol"], tf, k, d["timestamp"] + 1, s["timestamp"] - 1)
                                   if k != "DISPLACEMENT" or e["confirmation_evidence"]["strength"] in {"VALID", "STRONG"})
                    if contrary:
                        continue
                    relations.append(make_relation(d, s, "STRUCTURE_CONSEQUENCE_CANDIDATE",
                        {"reference": price, "reference_known_at": d["timestamp"], "exact_reference_break": exact,
                         "confirmed_swing_penetration": bool(ref and ref.price == price),
                         "protected_structure_penetration": protected == price,
                         "tf_relation": "SAME_TF" if tf == d["timeframe"] else "HIGHER_TF"}, exact=exact))
    structure_parents = defaultdict(set)
    for r in relations:
        if r["type"] == "STRUCTURE_CONSEQUENCE_CANDIDATE":
            structure_parents[r["child"]].add(r["parent"])
    for r in relations:
        if r["type"] == "STRUCTURE_CONSEQUENCE_CANDIDATE":
            r["candidate_parent_count"] = len(structure_parents[r["child"]])
            if r["candidate_parent_count"] > 1:
                r["ancestry_state"] = "AMBIGUOUS"
    context = contextual_candidates(events, relations)
    chains = chain_candidates(events, relations, graph)
    if any(e["available_at"] > cp for e in events.values()) or any(r["available_at"] > cp for r in relations):
        raise ValueError("post-checkpoint ancestry")
    return {"schema": VERSION, "episode_id": graph["episode_id"], "origin_checkpoint": cp,
            "events": dict(sorted(events.items())), "relations": sorted(relations, key=lambda r: r["relation_id"]),
            "diagnostic_candidates": diagnostics, "contextual_mss_candidates": context,
            "chains": chains, "peak_candidates_per_parent": peak,
            "authorities": {"ORIGIN_INVENTORY": "unchanged archived decision output",
                            "RAW_PREFIX_RESEARCH": "candidate-only legally available detector prefix"}}


def contextual_candidates(events, relations):
    ds = defaultdict(list); structs = defaultdict(list)
    for r in relations:
        if r["type"] == "DISPLACEMENT_CANDIDATE":
            ds[r["child"]].append(r)
        if r["type"] == "STRUCTURE_CONSEQUENCE_CANDIDATE":
            structs[r["child"]].append(r)
    output = []
    for e in events.values():
        if e["kind"] != "MSS":
            continue
        pr = structs[e["event_id"]]
        ancestry = [a for r in pr for a in ds[r["parent"]]]
        unambiguous = (len(pr) == 1 and bool(ancestry) and
                       all(a["ancestry_state"] == "UNAMBIGUOUS" for a in ancestry))
        output.append({"mss_id": e["event_id"], "displacement_parents": sorted({r["parent"] for r in pr}),
                       "sweep_ancestors": sorted({a["proof"]["sweep_id"] for a in ancestry}),
                       "state": "UNAMBIGUOUS" if unambiguous else "AMBIGUOUS" if ancestry else "INSUFFICIENT_EVIDENCE",
                       "classification": "CONTEXTUAL_MSS_CANDIDATE" if ancestry else "GENERIC_MSS"})
    return sorted(output, key=lambda e: e["mss_id"])


def chain_candidates(events, relations, origin):
    by_parent = defaultdict(list)
    for r in relations:
        by_parent[r["parent"]].append(r)
    inventory = origin.get("inventory_keys", {semantic_key(e) for e in origin["events"].values()})
    result = []
    for root in sorted((e for e in events.values() if e["kind"] == "LIQUIDITY"), key=lambda e: e["event_id"]):
        scope = root["evidence"]["type"]
        typ = "REVERSAL" if "External" in scope else "CONTINUATION" if "Internal" in scope else "UNCLASSIFIED"
        for sr in by_parent[root["event_id"]]:
            if sr["type"] != "SWEEP_OF":
                continue
            swid = sr["child"]; ids = {root["event_id"], swid}; rs = [sr]
            parents = [swid]
            for r in by_parent[swid]:
                if r["type"] in {"RECLAIM_AFTER", "REJECTION_AFTER"}:
                    parents.append(r["child"]); ids.add(r["child"]); rs.append(r)
            branch_complete = False
            for par in parents:
                for dr in by_parent[par]:
                    if dr["type"] != "DISPLACEMENT_CANDIDATE":
                        continue
                    rs.append(dr); ids.add(dr["child"])
                    descendants = [r for r in by_parent[dr["child"]] if r["type"] in
                                   {"STRUCTURE_CONSEQUENCE_CANDIDATE", "FVG_CREATED_BY", "OB_ASSOCIATED_WITH"}]
                    rs.extend(descendants); ids.update(r["child"] for r in descendants)
                    structures = [r for r in descendants if r["type"] == "STRUCTURE_CONSEQUENCE_CANDIDATE"
                                  and events[r["child"]]["kind"] == ("MSS" if typ == "REVERSAL" else "BOS")]
                    zones = [r for r in descendants if r["type"] in {"FVG_CREATED_BY", "OB_ASSOCIATED_WITH"}]
                    has_reaction = events[par]["kind"] in {"RECLAIMED", "REJECTION"}
                    branch_complete |= bool(structures and zones and (typ == "CONTINUATION" or has_reaction))
            ambiguous = any(r["ancestry_state"] == "AMBIGUOUS" for r in rs)
            # Retain the candidate DAG, never enumerate arbitrary Cartesian branch combinations.
            state = ("AMBIGUOUS_CHAIN_CANDIDATE" if ambiguous else "UNAMBIGUOUS_CHAIN_CANDIDATE") if branch_complete else "PARTIAL_CHAIN"
            visible_count = sum(semantic_key(events[k]) in inventory for k in ids)
            visibility = "FULLY_VISIBLE_IN_ORIGIN_INVENTORY" if visible_count == len(ids) else "PARTIALLY_VISIBLE_IN_ORIGIN_INVENTORY" if visible_count else "NOT_VISIBLE_IN_ORIGIN_INVENTORY"
            result.append({"candidate_chain_id": uid("CHAIN", typ, root["event_id"], swid),
                           "chain_type": typ, "state": state, "event_ids": sorted(ids),
                           "relation_ids": sorted({r["relation_id"] for r in rs}),
                           "inventory_visibility": visibility, "candidate_only": True,
                           "origin_timestamp": root["timestamp"],
                           "latest_timestamp": max(events[k]["timestamp"] for k in ids)})
    return result


def semantic_key(e):
    ev = e["evidence"]; kind = e["kind"]
    extra = ((ev.get("type"), ev.get("price")) if kind == "LIQUIDITY" else
             (ev.get("level_type"), ev.get("level_price")) if kind in {"SWEPT", "RECLAIMED"} else
             (ev.get("price"),) if kind in {"BOS", "MSS"} else
             (ev.get("lower"), ev.get("upper")) if kind == "FVG" else
             (ev.get("low"), ev.get("high")) if kind == "OB" else ())
    return kind, e["timeframe"], e["direction"], e["timestamp"], extra
