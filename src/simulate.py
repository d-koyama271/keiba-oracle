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


def round_ratio(value: float) -> float:
    return round(value, 6)


def build_pre_simulation(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    prediction = payload.get("prediction")
    if not prediction:
        return None

    horse_lookup = {horse["horse_number"]: horse for horse in payload.get("horses", [])}
    budget = int(config["race_budget"])
    ev_threshold = float(config["ev_threshold"])
    kelly_fraction = float(config["kelly_fraction"])
    stake_unit = int(config["stake_unit"])
    if budget <= 0:
        raise ValueError("race_budget must be positive")
    if stake_unit <= 0:
        raise ValueError("stake_unit must be positive")

    selections = []
    for item in prediction.get("horses", []):
        horse_number = item["horse_number"]
        horse = horse_lookup.get(horse_number)
        odds = horse.get("win_odds") if horse else None
        predicted_probability = float(item["win_probability"])
        if not odds or odds <= 1:
            continue
        implied_probability = 1.0 / odds
        expected_value = predicted_probability * odds
        b = odds - 1.0
        full_kelly = max(0.0, ((b * predicted_probability) - (1.0 - predicted_probability)) / b)
        kelly_weight_raw = full_kelly * kelly_fraction
        if expected_value + 1e-12 < ev_threshold or full_kelly <= 0 or kelly_weight_raw <= 0:
            continue
        selections.append(
            {
                "horse_number": horse_number,
                "implied_probability": round_ratio(implied_probability),
                "predicted_probability": round_ratio(predicted_probability),
                "expected_value": round_ratio(expected_value),
                "kelly_weight_raw": round_ratio(kelly_weight_raw),
                "_raw_stake": budget * kelly_weight_raw,
            }
        )

    selections.sort(key=lambda item: (-item["expected_value"], item["horse_number"]))
    if selections:
        total_raw_stake = sum(item["_raw_stake"] for item in selections)
        scale = (budget / total_raw_stake) if total_raw_stake > budget else 1.0
        rounded_selections = []
        for item in selections:
            adjusted_stake = item.pop("_raw_stake") * scale
            stake = math.floor((adjusted_stake / stake_unit) + 1e-12) * stake_unit
            if stake < stake_unit:
                continue
            item["allocation_ratio"] = round_ratio(stake / budget)
            item["stake"] = int(stake)
            rounded_selections.append(item)
        selections = rounded_selections

    return {
        "budget": budget,
        "ev_threshold": ev_threshold,
        "kelly_fraction": kelly_fraction,
        "stake_unit": stake_unit,
        "selections": selections,
    }


def build_post_simulation(payload: dict[str, Any]) -> dict[str, Any] | None:
    pre = (payload.get("simulation") or {}).get("pre")
    result = payload.get("result")
    if not pre or not result:
        return None

    payout_lookup = {item["horse_number"]: item["payout_per_100"] for item in result.get("payouts", {}).get("win", [])}
    finish_lookup = {item["horse_number"]: item["finish_position"] for item in result.get("horses", [])}

    selections = []
    total_stake = 0
    total_return = 0
    for item in pre.get("selections", []):
        horse_number = item["horse_number"]
        stake = int(item["stake"])
        payout = int(payout_lookup.get(horse_number, 0))
        hit = finish_lookup.get(horse_number) == 1
        return_amount = int((stake // 100) * payout) if hit else 0
        profit = return_amount - stake
        total_stake += stake
        total_return += return_amount
        selections.append(
            {
                "horse_number": horse_number,
                "stake": stake,
                "hit": hit,
                "payout": payout if payout else 0,
                "return_amount": return_amount,
                "profit": profit,
            }
        )

    profit = total_return - total_stake
    roi = round_ratio((profit / total_stake) if total_stake else 0.0)
    return {
        "total_stake": total_stake,
        "total_return": total_return,
        "profit": profit,
        "roi": roi,
        "selections": selections,
    }


def simulate_file(path: Path, config: dict[str, Any], mode: str, job_name: str, root: Path | None = None) -> bool:
    logger = setup_logger(job_name, config, root)
    payload = load_race_json(path)
    if not payload:
        return False
    race_id = payload["meta"].get("race_id")
    if mode == "pre":
        simulation_pre = build_pre_simulation(payload, config)
        if simulation_pre is None:
            log_job(logger, job_name, race_id, "simulation.pre skipped: prediction missing")
            return False
        payload["simulation"]["pre"] = simulation_pre
        save_race_json(path, payload)
        log_job(logger, job_name, race_id, "simulation.pre updated")
        return True

    simulation_post = build_post_simulation(payload)
    if simulation_post is None:
        log_job(logger, job_name, race_id, "simulation.post skipped: result missing")
        return False
    payload["simulation"]["post"] = simulation_post
    save_race_json(path, payload)
    log_job(logger, job_name, race_id, "simulation.post updated")
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
