from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Any

from utils import (
    list_race_files,
    load_config,
    load_race_json,
    log_job,
    parse_target_date,
    save_race_json,
    setup_logger,
)

EPSILON = 1e-12


def round_ratio(value: float) -> float:
    return round(value, 6)


def calculate_expected_value(probability: float, odds: float) -> float:
    return probability * odds


def meets_ev_threshold(expected_value: float, threshold: float) -> bool:
    return expected_value + EPSILON >= threshold


def simulation_settings(config: dict[str, Any]) -> tuple[int, int, dict[str, Any], dict[str, Any]]:
    simulation = config["simulation"]
    budget = int(simulation["budget"])
    stake_unit = int(simulation["stake_unit"])
    if budget <= 0:
        raise ValueError("simulation.budget must be positive")
    if stake_unit <= 0:
        raise ValueError("simulation.stake_unit must be positive")
    return budget, stake_unit, simulation["value"], simulation["dutching"]


def prediction_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    prediction = payload.get("prediction") or {}
    horse_lookup = {horse["horse_number"]: horse for horse in payload.get("horses", [])}
    rows = []
    for item in prediction.get("horses", []):
        horse_number = int(item["horse_number"])
        horse = horse_lookup.get(horse_number)
        odds = horse.get("win_odds") if horse else None
        probability = float(item["win_probability"])
        if probability < 0 or odds is None or float(odds) <= 1:
            continue
        rows.append(
            {
                "horse_number": horse_number,
                "predicted_probability": probability,
                "win_odds": float(odds),
            }
        )
    return rows


def calculate_value_details(
    payload: dict[str, Any],
    budget: int,
    stake_unit: int,
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    ev_threshold = float(settings["ev_threshold"])
    kelly_fraction = float(settings["kelly_fraction"])
    details = []

    for row in prediction_rows(payload):
        probability = row["predicted_probability"]
        odds = row["win_odds"]
        expected_value = calculate_expected_value(probability, odds)
        b = odds - 1.0
        full_kelly = max(0.0, (expected_value - 1.0) / b)
        fractional_kelly = full_kelly * kelly_fraction
        meets_threshold = meets_ev_threshold(expected_value, ev_threshold)
        eligible = meets_threshold and full_kelly > 0 and fractional_kelly > 0
        details.append(
            {
                "horse_number": row["horse_number"],
                "predicted_probability": probability,
                "win_odds": odds,
                "expected_value": expected_value,
                "full_kelly": full_kelly,
                "fractional_kelly": fractional_kelly,
                "meets_threshold": meets_threshold,
                "eligible": eligible,
                "_raw_stake": budget * fractional_kelly if eligible else 0.0,
            }
        )

    total_raw_stake = sum(item["_raw_stake"] for item in details)
    scale = budget / total_raw_stake if total_raw_stake > budget else 1.0
    for item in details:
        if item["eligible"]:
            theoretical_stake = item["_raw_stake"] * scale
            stake = math.floor((theoretical_stake / stake_unit) + EPSILON) * stake_unit
            item["theoretical_stake"] = theoretical_stake
            item["stake"] = int(stake)
        else:
            item["theoretical_stake"] = 0.0 if item["meets_threshold"] else None
            item["stake"] = 0
        del item["_raw_stake"]
    return details


def minimum_budget_for_value_stake(
    payload: dict[str, Any],
    stake_unit: int,
    settings: dict[str, Any],
    horse_number: int,
) -> int | None:
    def target_detail(budget: int) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in calculate_value_details(payload, budget, stake_unit, settings)
                if item["horse_number"] == horse_number
            ),
            None,
        )

    initial = target_detail(1)
    if initial is None or not initial["eligible"]:
        return None

    low = 0
    high = max(1, stake_unit)
    while (target_detail(high) or {}).get("stake", 0) < stake_unit:
        low = high
        high *= 2

    while low + 1 < high:
        middle = (low + high) // 2
        if (target_detail(middle) or {}).get("stake", 0) >= stake_unit:
            high = middle
        else:
            low = middle
    return high


def calculate_value_pre(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    if not payload.get("prediction"):
        return None

    budget, stake_unit, settings, _ = simulation_settings(config)
    ev_threshold = float(settings["ev_threshold"])
    kelly_fraction = float(settings["kelly_fraction"])
    details = calculate_value_details(payload, budget, stake_unit, settings)
    selections = []
    for item in sorted(
        details,
        key=lambda detail: (-round_ratio(detail["expected_value"]), detail["horse_number"]),
    ):
        if item["stake"] < stake_unit:
            continue
        selections.append(
            {
                "horse_number": item["horse_number"],
                "predicted_probability": round_ratio(item["predicted_probability"]),
                "win_odds": item["win_odds"],
                "expected_value": round_ratio(item["expected_value"]),
                "full_kelly": round_ratio(item["full_kelly"]),
                "fractional_kelly": round_ratio(item["fractional_kelly"]),
                "stake": item["stake"],
            }
        )

    total_stake = sum(item["stake"] for item in selections)
    return {
        "budget": budget,
        "stake_unit": stake_unit,
        "settings": {
            "ev_threshold": ev_threshold,
            "kelly_fraction": kelly_fraction,
        },
        "total_stake": total_stake,
        "unused_budget": budget - total_stake,
        "selections": selections,
    }


def allocate_dutching_stakes(
    rows: list[dict[str, Any]],
    budget: int,
    stake_unit: int,
) -> list[dict[str, Any]]:
    budget_units = budget // stake_unit
    if budget_units < len(rows):
        return []

    units = {row["horse_number"]: 1 for row in rows}
    for _ in range(budget_units - len(rows)):
        target = min(
            rows,
            key=lambda row: (
                round_ratio(units[row["horse_number"]] * stake_unit * row["win_odds"]),
                row["horse_number"],
            ),
        )
        units[target["horse_number"]] += 1

    total_stake = budget_units * stake_unit
    selections = []
    for row in rows:
        stake = units[row["horse_number"]] * stake_unit
        estimated_payout = stake * row["win_odds"]
        selections.append(
            {
                "horse_number": row["horse_number"],
                "predicted_probability": round_ratio(row["predicted_probability"]),
                "win_odds": row["win_odds"],
                "stake": stake,
                "estimated_payout": round_ratio(estimated_payout),
                "estimated_profit": round_ratio(estimated_payout - total_stake),
            }
        )
    return selections


def evaluate_dutching_count(
    rows: list[dict[str, Any]],
    budget: int,
    stake_unit: int,
    settings: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    selections = allocate_dutching_stakes(rows, budget, stake_unit)
    coverage_probability = round_ratio(sum(row["predicted_probability"] for row in rows))
    total_stake = sum(item["stake"] for item in selections)
    expected_return = round_ratio(
        sum(item["predicted_probability"] * item["estimated_payout"] for item in selections)
    )
    group_expected_value = round_ratio(expected_return / total_stake) if total_stake else 0.0
    minimum_payout = min((item["estimated_payout"] for item in selections), default=0.0)
    minimum_profit = round_ratio(minimum_payout - total_stake) if total_stake else 0.0

    rejection_reasons = []
    if coverage_probability + EPSILON < float(settings["min_coverage_probability"]):
        rejection_reasons.append("coverage_probability_below_threshold")
    if group_expected_value + EPSILON < float(settings["min_group_expected_value"]):
        rejection_reasons.append("group_expected_value_below_threshold")
    if not selections:
        rejection_reasons.append("insufficient_budget_units")
    if bool(settings["require_profit_if_hit"]) and minimum_profit <= 0:
        rejection_reasons.append("minimum_profit_not_positive")

    evaluation = {
        "selection_count": len(rows),
        "horse_numbers": [row["horse_number"] for row in rows],
        "coverage_probability": coverage_probability,
        "expected_return": expected_return,
        "group_expected_value": group_expected_value,
        "minimum_payout": round_ratio(minimum_payout),
        "minimum_profit": minimum_profit,
        "eligible": not rejection_reasons,
        "rejection_reasons": rejection_reasons,
    }
    return evaluation, selections


def select_best_dutching(
    evaluations: list[tuple[dict[str, Any], list[dict[str, Any]]]],
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    eligible = [item for item in evaluations if item[0]["eligible"]]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda item: (
            item[0]["group_expected_value"],
            item[0]["coverage_probability"],
            -item[0]["selection_count"],
        ),
    )


def calculate_dutching_pre(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    if not payload.get("prediction"):
        return None

    budget, stake_unit, _, settings = simulation_settings(config)
    max_selection_count = int(settings["max_selection_count"])
    if max_selection_count <= 0:
        raise ValueError("simulation.dutching.max_selection_count must be positive")

    rows = sorted(
        prediction_rows(payload),
        key=lambda item: (-item["predicted_probability"], item["horse_number"]),
    )
    evaluations = [
        evaluate_dutching_count(rows[:count], budget, stake_unit, settings)
        for count in range(1, min(max_selection_count, len(rows)) + 1)
    ]
    selected = select_best_dutching(evaluations)

    result = {
        "budget": budget,
        "stake_unit": stake_unit,
        "settings": {
            "max_selection_count": max_selection_count,
            "min_coverage_probability": float(settings["min_coverage_probability"]),
            "min_group_expected_value": float(settings["min_group_expected_value"]),
            "require_profit_if_hit": bool(settings["require_profit_if_hit"]),
        },
        "selected_count": 0,
        "coverage_probability": 0.0,
        "expected_return": 0.0,
        "group_expected_value": 0.0,
        "minimum_payout": 0.0,
        "minimum_profit": 0.0,
        "total_stake": 0,
        "unused_budget": budget,
        "evaluated_counts": [evaluation for evaluation, _ in evaluations],
        "selections": [],
    }
    if selected is None:
        return result

    evaluation, selections = selected
    result.update(
        {
            "selected_count": evaluation["selection_count"],
            "coverage_probability": evaluation["coverage_probability"],
            "expected_return": evaluation["expected_return"],
            "group_expected_value": evaluation["group_expected_value"],
            "minimum_payout": evaluation["minimum_payout"],
            "minimum_profit": evaluation["minimum_profit"],
            "total_stake": sum(item["stake"] for item in selections),
            "unused_budget": budget - sum(item["stake"] for item in selections),
            "selections": selections,
        }
    )
    return result


def calculate_post(pre: dict[str, Any] | None, result: dict[str, Any] | None) -> dict[str, Any] | None:
    if pre is None or result is None:
        return None

    payout_lookup = {
        int(item["horse_number"]): int(item["payout_per_100"])
        for item in result.get("payouts", {}).get("win", [])
    }
    winner_numbers = {
        int(item["horse_number"])
        for item in result.get("horses", [])
        if item.get("finish_position") == 1
    }
    selections = []
    total_stake = 0
    total_return = 0
    for item in pre.get("selections", []):
        horse_number = int(item["horse_number"])
        stake = int(item["stake"])
        hit = horse_number in winner_numbers
        payout = payout_lookup.get(horse_number, 0)
        return_amount = (stake * payout // 100) if hit else 0
        total_stake += stake
        total_return += return_amount
        selections.append(
            {
                "horse_number": horse_number,
                "stake": stake,
                "hit": hit,
                "return": return_amount,
            }
        )

    profit = total_return - total_stake
    return {
        "total_stake": total_stake,
        "total_return": total_return,
        "profit": profit,
        "roi": round_ratio(profit / total_stake) if total_stake else 0.0,
        "selections": selections,
    }


def calculate_value_post(payload: dict[str, Any]) -> dict[str, Any] | None:
    value = ((payload.get("simulation") or {}).get("value") or {})
    return calculate_post(value.get("pre"), payload.get("result"))


def calculate_dutching_post(payload: dict[str, Any]) -> dict[str, Any] | None:
    dutching = ((payload.get("simulation") or {}).get("dutching") or {})
    return calculate_post(dutching.get("pre"), payload.get("result"))


def calculate_pre_simulation(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    value_pre = calculate_value_pre(payload, config)
    dutching_pre = calculate_dutching_pre(payload, config)
    if value_pre is None or dutching_pre is None:
        return None
    return {
        "value": {"pre": value_pre, "post": None},
        "dutching": {"pre": dutching_pre, "post": None},
    }


def simulate_file(path: Path, config: dict[str, Any], mode: str, job_name: str, root: Path | None = None) -> bool:
    logger = setup_logger(job_name, config, root)
    payload = load_race_json(path)
    if not payload:
        return False
    race_id = payload["meta"].get("race_id")

    if mode == "pre":
        simulation = calculate_pre_simulation(payload, config)
        if simulation is None:
            log_job(logger, job_name, race_id, "simulation pre skipped: prediction missing")
            return False
        payload["simulation"] = simulation
        save_race_json(path, payload)
        log_job(logger, job_name, race_id, "simulation value/dutching pre updated")
        return True

    value_post = calculate_value_post(payload)
    dutching_post = calculate_dutching_post(payload)
    if value_post is None or dutching_post is None:
        log_job(logger, job_name, race_id, "simulation post skipped: pre/result missing")
        return False
    payload["simulation"]["value"]["post"] = value_post
    payload["simulation"]["dutching"]["post"] = dutching_post
    save_race_json(path, payload)
    log_job(logger, job_name, race_id, "simulation value/dutching post updated")
    return True


def simulate_paths(
    paths: list[Path],
    config: dict[str, Any],
    mode: str,
    job_name: str,
    root: Path | None = None,
) -> list[Path]:
    updated = []
    for path in paths:
        if simulate_file(path, config, mode, job_name, root):
            updated.append(path)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    parser.add_argument("--mode", choices=("pre", "post"), default="pre")
    args = parser.parse_args()

    config = load_config()
    target_date = parse_target_date(args.date)
    paths = list_race_files(config, target_date)
    simulate_paths(paths, config, args.mode, args.mode)


if __name__ == "__main__":
    main()
