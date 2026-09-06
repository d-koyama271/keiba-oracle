from __future__ import annotations

import copy
import math
from itertools import combinations
from typing import Any

from utils import now_jst, now_jst_iso, parse_jst_datetime, race_start_datetime


def pair_numbers(value: Any) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("invalid_pair")
    if any(type(number) is not int or number <= 0 for number in value):
        raise ValueError("invalid_pair")
    first, second = sorted(value)
    if first == second:
        raise ValueError("self_pair")
    return first, second


def validate_pair_odds(rows: list[dict], horse_numbers: list[int]) -> None:
    if len(horse_numbers) < 2 or any(type(n) is not int or n <= 0 for n in horse_numbers) or len(set(horse_numbers)) != len(horse_numbers):
        raise ValueError("invalid_horse_numbers")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid_pair_rows")
    expected = set(combinations(sorted(horse_numbers), 2))
    actual = set()
    for row in rows:
        pair = pair_numbers(row.get("horse_numbers"))
        if pair in actual:
            raise ValueError("duplicate_pair")
        actual.add(pair)
        odds = row.get("odds")
        if isinstance(odds, bool) or not isinstance(odds, (int, float)) or not math.isfinite(odds) or odds < 1:
            raise ValueError("invalid_odds")
    if actual != expected:
        raise ValueError("pair_set_mismatch")


def harville_probabilities(horses: list[dict], prediction: dict, exponent: float) -> list[dict]:
    if isinstance(exponent, bool) or not isinstance(exponent, (int, float)) or not math.isfinite(exponent) or exponent <= 0:
        raise ValueError("invalid_harville_lambda")
    numbers = [h.get("horse_number") for h in horses]
    if len(numbers) < 2 or any(type(n) is not int or n <= 0 for n in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError("invalid_horse_numbers")
    probabilities = {}
    for item in prediction.get("horses", []):
        number, probability = item.get("horse_number"), item.get("win_probability")
        if type(number) is not int or number in probabilities:
            raise ValueError("invalid_prediction_horses")
        if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("invalid_probability")
        probabilities[number] = float(probability)
    if set(probabilities) != set(numbers):
        raise ValueError("prediction_horse_mismatch")
    if not math.isclose(math.fsum(probabilities.values()), 1.0, rel_tol=0, abs_tol=1e-6):
        raise ValueError("invalid_probability_sum")
    powered = {n: p ** exponent for n, p in probabilities.items()}
    denominators = {n: math.fsum(v for k, v in powered.items() if k != n) for n in numbers}
    if any(value <= 0 for value in denominators.values()):
        raise ValueError("zero_harville_denominator")
    rows = [
        {"horse_numbers": [i, j], "probability": probabilities[i] * powered[j] / denominators[i] + probabilities[j] * powered[i] / denominators[j]}
        for i, j in combinations(sorted(numbers), 2)
    ]
    if not math.isclose(math.fsum(row["probability"] for row in rows), 1.0, rel_tol=0, abs_tol=1e-6):
        raise ValueError("invalid_pair_probability_sum")
    return rows


def calculate_quinella_pre(payload: dict, config: dict, prediction: dict) -> dict | None:
    settings = config["simulation"].get("quinella")
    race = payload.get("race") or {}
    start = race_start_datetime(race.get("date"), race.get("start_time"))
    if not settings or payload.get("result") is not None or start is None or now_jst() >= start:
        return None
    snapshot = copy.deepcopy(race.get("quinella_odds") or {})
    shared = {
        "status": "unavailable", "reason": None, "generated_at": now_jst_iso(),
        "harville_lambda": settings.get("harville_lambda"), "odds_snapshot": snapshot,
        "probabilities": [], "value": {"pre": None, "post": None}, "dutching": {"pre": None, "post": None},
        "post_status": "awaiting_result",
    }
    try:
        captured = parse_jst_datetime(snapshot.get("fetched_at"))
        official = parse_jst_datetime(snapshot.get("official_datetime"))
        if not snapshot.get("available"):
            raise ValueError(snapshot.get("reason") or "odds_unavailable")
        if captured is None or official is None or not official <= captured < start or snapshot.get("api_status") != "middle":
            raise ValueError("odds_not_pre_race")
        probabilities = harville_probabilities(payload.get("horses", []), prediction, settings["harville_lambda"])
        validate_pair_odds(snapshot.get("pairs", []), [h["horse_number"] for h in payload["horses"]])
        odds = {pair_numbers(row["horse_numbers"]): row["odds"] for row in snapshot["pairs"]}
        shared["probabilities"] = probabilities
        rows = [{**row, "odds": odds[tuple(row["horse_numbers"])]} for row in probabilities]
        for method in ("value", "dutching"):
            shared[method]["pre"] = calculate_quinella_purchase(rows, config["simulation"]["budget"], config["simulation"]["stake_unit"], settings[method], method)
        shared.update(status="ready", reason=None)
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        shared.update(status="unavailable", reason=str(exc))
        shared["value"]["pre"] = shared["dutching"]["pre"] = None
    return shared


def calculate_quinella_purchase(rows: list[dict], budget: int, stake_unit: int, settings: dict, method: str, fixed_count: int = 0) -> dict:
    # Numeric pair IDs are local adapters for the existing stake allocators, never horse IDs in JSON.
    from simulate import EPSILON, allocate_dutching_stakes, calculate_value_details, select_best_dutching

    if type(budget) is not int or type(stake_unit) is not int or budget <= 0 or stake_unit <= 0:
        raise ValueError("invalid_budget_or_unit")
    if method not in ("value", "dutching"):
        raise ValueError("invalid_purchase_method")
    for key, value in settings.items():
        if key == "require_profit_if_hit":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid_quinella_settings")
    if method == "dutching" and (type(settings["max_selection_count"]) is not int or settings["max_selection_count"] <= 0):
        raise ValueError("invalid_selection_count")
    adapters = []
    pairs = {}
    for index, row in enumerate(sorted(rows, key=lambda row: pair_numbers(row["horse_numbers"])), 1):
        pairs[index] = list(pair_numbers(row["horse_numbers"]))
        adapters.append({"horse_number": index, "predicted_probability": row["probability"], "win_odds": row["odds"]})

    def restore(item: dict) -> dict:
        item = dict(item)
        item["horse_numbers"] = pairs[item.pop("horse_number")]
        item["odds"] = item.pop("win_odds")
        item.setdefault("expected_value", item["predicted_probability"] * item["odds"])
        return item

    result = {"budget": budget, "stake_unit": stake_unit, "settings": copy.deepcopy(settings), "total_stake": 0, "unused_budget": budget, "selections": [], "status": "no_purchase", "reason": None}
    if method == "value":
        adapter_payload = {"horses": adapters}
        adapter_prediction = {"horses": [{"horse_number": r["horse_number"], "win_probability": r["predicted_probability"]} for r in adapters]}
        details = calculate_value_details(adapter_payload, budget, stake_unit, settings, adapter_prediction)
        # Odds of 1.0 cannot have positive Kelly; retain them as visible, unpurchased pairs.
        details += [{**r, "expected_value": r["predicted_probability"], "full_kelly": 0.0, "fractional_kelly": 0.0, "theoretical_stake": 0.0, "stake": 0, "eligible": False, "meets_threshold": r["predicted_probability"] + EPSILON >= settings["ev_threshold"]} for r in adapters if r["win_odds"] == 1]
        result["details"] = [restore(row) for row in sorted(details, key=lambda r: (-r["expected_value"], r["horse_number"]))]
        result["selections"] = [dict(row) for row in result["details"] if row["stake"] >= stake_unit]
        result["reason"] = "no_positive_kelly_stake" if not result["selections"] else None
    else:
        ordered = sorted(adapters, key=lambda r: (-r["predicted_probability"], r["horse_number"]))
        evaluations = []
        for count in range(1, min(int(settings["max_selection_count"]), len(ordered)) + 1):
            selected_rows = ordered[:count]
            selections = allocate_dutching_stakes(selected_rows, budget, stake_unit)
            for selection, row in zip(selections, selected_rows):
                selection["predicted_probability"] = row["predicted_probability"]
            stake = sum(s["stake"] for s in selections)
            expected_return = sum(s["predicted_probability"] * s["estimated_payout"] for s in selections)
            coverage = sum(r["predicted_probability"] for r in selected_rows)
            group_ev = expected_return / stake if stake else 0
            payout = min((s["estimated_payout"] for s in selections), default=0)
            profit = payout - stake
            reasons = []
            if coverage + EPSILON < settings["min_coverage_probability"]: reasons.append("coverage_probability_below_threshold")
            if group_ev + EPSILON < settings["min_group_expected_value"]: reasons.append("group_expected_value_below_threshold")
            if not selections: reasons.append("insufficient_budget_units")
            if profit + EPSILON < stake * settings["min_profit_rate"]: reasons.append("minimum_profit_rate_below_threshold")
            if settings["require_profit_if_hit"] and profit <= 0: reasons.append("minimum_profit_not_positive")
            evaluation = {"selection_count": count, "horse_pairs": [pairs[r["horse_number"]] for r in selected_rows], "coverage_probability": coverage, "expected_return": expected_return, "group_expected_value": group_ev, "minimum_payout": payout, "minimum_profit": profit, "eligible": not reasons, "rejection_reasons": reasons}
            evaluations.append((evaluation, [restore(s) for s in selections]))
        selected = next((entry for entry in evaluations if entry[0]["selection_count"] == fixed_count), None) if fixed_count else select_best_dutching(evaluations)
        result.update(selected_count=0, coverage_probability=0, expected_return=0, group_expected_value=0, minimum_payout=0, minimum_profit=0, evaluated_counts=[e for e, _ in evaluations])
        if selected:
            evaluation, selections = selected
            result.update({k: evaluation[k] for k in ("coverage_probability", "expected_return", "group_expected_value", "minimum_payout", "minimum_profit")})
            result.update(selected_count=evaluation["selection_count"], selections=selections)
        else:
            result["reason"] = "no_eligible_candidate"
    result["total_stake"] = sum(row["stake"] for row in result["selections"])
    result["unused_budget"] = budget - result["total_stake"]
    result["status"] = "purchased" if result["total_stake"] else "no_purchase"
    return result


def calculate_quinella_post(pre: dict | None, result: dict | None) -> dict | None:
    if not pre or pre.get("status") not in ("purchased", "no_purchase") or not result:
        return None
    settlement = result.get("quinella_settlement") or {}
    if settlement.get("status") != "complete":
        return None
    try:
        payouts = {}
        finish = {horse["horse_number"]: horse["finish_position"] for horse in result["horses"]}
        if len(finish) != len(result["horses"]) or any(type(n) is not int or n <= 0 for n in finish):
            return None
        if any(not (type(position) is int and position > 0) and position not in ("取消", "除外", "中止", "失格") for position in finish.values()):
            return None
        for item in result.get("payouts", {}).get("quinella", []):
            pair = pair_numbers(item["horse_numbers"])
            amount = item["payout_per_100"]
            if pair in payouts or not set(pair).issubset(finish) or type(amount) is not int or amount <= 0:
                return None
            payouts[pair] = amount
        refunds = settlement["refund_horse_numbers"]
        if not payouts or not isinstance(refunds, list) or any(type(n) is not int or n <= 0 for n in refunds):
            return None
        if len(refunds) != len(set(refunds)) or set(refunds) != {n for n, position in finish.items() if position in ("取消", "除外")}:
            return None
        selections = []
        seen = set()
        for item in pre["selections"]:
            pair, stake = pair_numbers(item["horse_numbers"]), item["stake"]
            if pair in seen or not set(pair).issubset(finish) or type(stake) is not int or stake <= 0:
                return None
            seen.add(pair)
            refunded = any(number in refunds for number in pair)
            if refunded and pair in payouts:
                return None
            hit = pair in payouts and not refunded
            payout = stake * payouts[pair] // 100 if hit else 0
            refund = stake if refunded else 0
            selections.append({"horse_numbers": list(pair), "stake": stake, "hit": hit, "payout": payout, "refund": refund, "return": payout + refund})
    except (KeyError, TypeError, ValueError):
        return None
    stake = sum(s["stake"] for s in selections)
    if pre.get("total_stake") != stake:
        return None
    returned = sum(s["return"] for s in selections)
    return {"status": "settled", "total_stake": stake, "total_refund": sum(s["refund"] for s in selections), "total_return": returned, "profit": returned - stake, "roi": round((returned - stake) / stake, 6) if stake else 0.0, "selections": selections}
