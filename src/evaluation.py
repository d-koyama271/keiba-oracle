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
    now_jst_iso,
    parse_jst_datetime,
    parse_target_date,
    race_start_datetime,
    save_race_json,
    setup_logger,
    prediction_variants,
)

PROBABILITY_FLOOR = 1e-12


def round_metric(value: float) -> float:
    return round(value, 6)


def ranked_prediction_probabilities(prediction: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "horse_number": int(item["horse_number"]),
            "probability": float(item["win_probability"]),
        }
        for item in prediction.get("horses", [])
    ]
    return sorted(rows, key=lambda item: (-item["probability"], item["horse_number"]))


def ranked_probabilities(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return ranked_prediction_probabilities(payload.get("prediction") or {})


def winner_number(result: dict[str, Any]) -> int | None:
    for item in result.get("horses", []):
        if item.get("finish_position") == 1:
            return int(item["horse_number"])
    finish_order = result.get("finish_order") or []
    return int(finish_order[0]) if finish_order else None


def brier_score(rows: list[dict[str, Any]], winner: int) -> float:
    errors = [
        (item["probability"] - (1.0 if item["horse_number"] == winner else 0.0)) ** 2
        for item in rows
    ]
    return round_metric(sum(errors) / len(errors))


def simulation_summary(post: dict[str, Any]) -> dict[str, Any]:
    total_stake = int(post.get("total_stake", 0))
    return {
        "total_stake": total_stake,
        "total_return": int(post.get("total_return", 0)),
        "profit": int(post.get("profit", 0)),
        "roi": float(post.get("roi", 0.0)) if total_stake else None,
        "hit": any(bool(item.get("hit")) for item in post.get("selections", [])),
    }


def odds_recorded_after_start(race: dict[str, Any]) -> bool | None:
    start = race_start_datetime(race.get("date"), race.get("start_time"))
    captured = parse_jst_datetime(race.get("odds_captured_at"))
    if start is None or captured is None:
        return None
    return captured > start


def normalized_market_probabilities(
    payload: dict[str, Any],
    model_rows: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    horse_lookup = {int(horse["horse_number"]): horse for horse in payload.get("horses", [])}
    implied_rows = []
    for item in model_rows:
        odds = horse_lookup.get(item["horse_number"], {}).get("win_odds")
        try:
            odds_value = float(odds)
        except (TypeError, ValueError):
            return None
        if odds_value <= 0:
            return None
        implied_rows.append(
            {
                "horse_number": item["horse_number"],
                "probability": 1.0 / odds_value,
            }
        )

    total = sum(item["probability"] for item in implied_rows)
    if not implied_rows or total <= 0:
        return None
    for item in implied_rows:
        item["probability"] /= total
    return implied_rows


def market_baseline(
    payload: dict[str, Any],
    model_rows: list[dict[str, Any]],
    winner: int,
    model_log_loss: float,
    model_brier_score: float,
) -> dict[str, Any]:
    implied_rows = normalized_market_probabilities(payload, model_rows)
    if implied_rows is None:
        return {"available": False}

    ranked_market = sorted(
        implied_rows,
        key=lambda item: (-item["probability"], item["horse_number"]),
    )
    rank_lookup = {item["horse_number"]: rank for rank, item in enumerate(ranked_market, 1)}
    probability_lookup = {item["horse_number"]: item["probability"] for item in implied_rows}
    if winner not in probability_lookup:
        return {"available": False}

    winner_probability = probability_lookup[winner]
    market_log_loss = round_metric(-math.log(max(winner_probability, PROBABILITY_FLOOR)))
    market_brier_score = brier_score(implied_rows, winner)
    recorded_after_start = odds_recorded_after_start(payload.get("race") or {})
    return {
        "available": True,
        "winner_probability": round_metric(winner_probability),
        "winner_rank": rank_lookup[winner],
        "log_loss": market_log_loss,
        "brier_score": market_brier_score,
        "model_log_loss_difference": round_metric(model_log_loss - market_log_loss),
        "model_brier_difference": round_metric(model_brier_score - market_brier_score),
        "odds_recorded_after_start": recorded_after_start,
        "comparison_note": (
            "Uses odds recorded after the start; this is not a formal pre-race performance comparison."
            if recorded_after_start
            else None
        ),
    }


def build_prediction_evaluation(
    payload: dict[str, Any],
    prediction: dict[str, Any],
) -> dict[str, Any] | None:
    result = payload.get("result")
    model_rows = ranked_prediction_probabilities(prediction)
    if not result or not model_rows:
        return None

    winner = winner_number(result)
    if winner is None:
        return None
    rank_lookup = {item["horse_number"]: rank for rank, item in enumerate(model_rows, 1)}
    probability_lookup = {item["horse_number"]: item["probability"] for item in model_rows}
    if winner not in probability_lookup:
        return None

    winner_probability = probability_lookup[winner]
    predicted_rank = rank_lookup[winner]
    log_loss = round_metric(-math.log(max(winner_probability, PROBABILITY_FLOOR)))
    model_brier_score = brier_score(model_rows, winner)
    return {
        "evaluated_at": now_jst_iso(),
        "winner": {
            "horse_number": winner,
            "predicted_probability": round_metric(winner_probability),
            "predicted_rank": predicted_rank,
        },
        "metrics": {
            "log_loss": log_loss,
            "brier_score": model_brier_score,
            "top1_hit": predicted_rank <= 1,
            "top3_hit": predicted_rank <= 3,
            "top5_hit": predicted_rank <= 5,
        },
        "market_baseline": market_baseline(
            payload,
            model_rows,
            winner,
            log_loss,
            model_brier_score,
        ),
    }


def build_evaluation(payload: dict[str, Any]) -> dict[str, Any] | None:
    simulation = payload.get("simulation") or {}
    value_post = (simulation.get("value") or {}).get("post")
    dutching_post = (simulation.get("dutching") or {}).get("post")
    if value_post is None or dutching_post is None:
        return None

    evaluation = build_prediction_evaluation(payload, payload.get("prediction") or {})
    if evaluation is None:
        return None
    evaluation["simulation_results"] = {
        "value": simulation_summary(value_post),
        "dutching": simulation_summary(dutching_post),
    }

    variant_evaluations = []
    identity_keys = (
        "method",
        "model_provider",
        "model_name",
        "predicted_at",
        "prompt_sha256",
        "prediction_input_sha256",
    )
    for prediction in prediction_variants(payload):
        variant_evaluation = build_prediction_evaluation(payload, prediction)
        if variant_evaluation is None:
            continue
        variant_evaluations.append(
            {
                **{key: prediction.get(key) for key in identity_keys if prediction.get(key) is not None},
                **variant_evaluation,
            }
        )
    if variant_evaluations:
        evaluation["variants"] = variant_evaluations
    return evaluation


def evaluate_file(
    path: Path,
    config: dict[str, Any],
    job_name: str,
    root: Path | None = None,
) -> bool:
    logger = setup_logger(job_name, config, root)
    payload = load_race_json(path)
    if not payload:
        return False
    race_id = payload["meta"].get("race_id")
    evaluation = build_evaluation(payload)
    if evaluation is None:
        log_job(logger, job_name, race_id, "evaluation skipped: prediction/result/post missing")
        return False
    payload["evaluation"] = evaluation
    save_race_json(path, payload)
    log_job(logger, job_name, race_id, "evaluation updated")
    return True


def evaluate_paths(
    paths: list[Path],
    config: dict[str, Any],
    job_name: str,
    root: Path | None = None,
) -> list[Path]:
    return [path for path in paths if evaluate_file(path, config, job_name, root)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    config = load_config()
    target_date = parse_target_date(args.date)
    evaluate_paths(list_race_files(config, target_date), config, "evaluation")


if __name__ == "__main__":
    main()
