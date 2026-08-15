from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Callable

from evaluation import normalized_market_probabilities, ranked_probabilities, round_metric
from utils import (
    atomic_write_json,
    data_dir,
    evaluation_variants,
    find_variant,
    list_race_files,
    load_config,
    load_race_json,
    log_job,
    now_jst_iso,
    prediction_variants,
    setup_logger,
    STATISTICAL_PREDICTION_METHOD,
    TRADITIONAL_PREDICTION_METHOD,
)

CALIBRATION_BUCKETS = (
    ("0-5%", 0.00, 0.05),
    ("5-10%", 0.05, 0.10),
    ("10-20%", 0.10, 0.20),
    ("20-30%", 0.20, 0.30),
    ("30-50%", 0.30, 0.50),
    ("50-100%", 0.50, 1.00),
)
UNKNOWN_SEGMENT = "不明"


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def positive_integer(value: Any) -> int | None:
    number = finite_number(value)
    if number is None or not number.is_integer() or number <= 0:
        return None
    return int(number)


def evaluation_record_from(evaluation: Any) -> dict[str, Any] | None:
    if not isinstance(evaluation, dict):
        return None
    winner = evaluation.get("winner")
    metrics = evaluation.get("metrics")
    if not isinstance(winner, dict) or not isinstance(metrics, dict):
        return None

    winner_number = positive_integer(winner.get("horse_number"))
    winner_rank = positive_integer(winner.get("predicted_rank"))
    log_loss = finite_number(metrics.get("log_loss"))
    brier_score = finite_number(metrics.get("brier_score"))
    hits = [metrics.get(key) for key in ("top1_hit", "top3_hit", "top5_hit")]
    if (
        winner_number is None
        or winner_rank is None
        or log_loss is None
        or brier_score is None
        or not all(isinstance(hit, bool) for hit in hits)
    ):
        return None
    return {
        "winner_number": winner_number,
        "winner_rank": winner_rank,
        "log_loss": log_loss,
        "brier_score": brier_score,
        "top1_hit": hits[0],
        "top3_hit": hits[1],
        "top5_hit": hits[2],
    }


def evaluation_record(payload: dict[str, Any]) -> dict[str, Any] | None:
    return evaluation_record_from(payload.get("evaluation"))


def aggregate_prediction_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    race_count = len(records)
    top1_hits = sum(record["top1_hit"] for record in records)
    top3_hits = sum(record["top3_hit"] for record in records)
    top5_hits = sum(record["top5_hit"] for record in records)
    return {
        "evaluated_races": race_count,
        "top1_hits": top1_hits,
        "top1_hit_rate": round_metric(top1_hits / race_count) if race_count else None,
        "top3_hits": top3_hits,
        "top3_hit_rate": round_metric(top3_hits / race_count) if race_count else None,
        "top5_hits": top5_hits,
        "top5_hit_rate": round_metric(top5_hits / race_count) if race_count else None,
        "average_winner_predicted_rank": (
            round_metric(sum(record["winner_rank"] for record in records) / race_count)
            if race_count
            else None
        ),
        "average_log_loss": (
            round_metric(sum(record["log_loss"] for record in records) / race_count)
            if race_count
            else None
        ),
        "average_brier_score": (
            round_metric(sum(record["brier_score"] for record in records) / race_count)
            if race_count
            else None
        ),
    }


def distance_band(payload: dict[str, Any]) -> str:
    distance = positive_integer((payload.get("race") or {}).get("distance"))
    if distance is None:
        return UNKNOWN_SEGMENT
    if distance <= 1400:
        return "～1400m"
    if distance <= 1800:
        return "1401～1800m"
    if distance <= 2200:
        return "1801～2200m"
    return "2201m～"


def field_size_band(payload: dict[str, Any]) -> str:
    field_size = len(payload.get("horses") or [])
    if field_size <= 0:
        return UNKNOWN_SEGMENT
    if field_size <= 9:
        return "～9頭"
    if field_size <= 13:
        return "10～13頭"
    if field_size <= 16:
        return "14～16頭"
    return "17頭以上"


def text_segment(payload: dict[str, Any], key: str) -> str:
    value = str((payload.get("race") or {}).get(key) or "").strip()
    return value or UNKNOWN_SEGMENT


def aggregate_segments(
    evaluated: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    classifiers: dict[str, Callable[[dict[str, Any]], str]] = {
        "surface": lambda payload: text_segment(payload, "surface"),
        "distance_band": distance_band,
        "class_grade": lambda payload: text_segment(payload, "class_grade"),
        "track": lambda payload: text_segment(payload, "track"),
        "field_size_band": field_size_band,
    }
    segments: dict[str, dict[str, dict[str, Any]]] = {}
    for name, classifier in classifiers.items():
        grouped: dict[str, list[dict[str, Any]]] = {}
        for payload, record in evaluated:
            grouped.setdefault(classifier(payload), []).append(record)
        segments[name] = {
            key: aggregate_prediction_metrics(grouped[key])
            for key in sorted(grouped)
        }
    return segments


def aggregate_calibration(
    evaluated: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    values = [{"probabilities": [], "wins": 0} for _ in CALIBRATION_BUCKETS]
    for _, record, prediction in evaluated:
        for horse in prediction.get("horses", []):
            horse_number = positive_integer(horse.get("horse_number"))
            probability = finite_number(horse.get("win_probability"))
            if horse_number is None or probability is None or probability < 0 or probability > 1:
                continue
            for index, (_, minimum, maximum) in enumerate(CALIBRATION_BUCKETS):
                if probability >= minimum and (probability < maximum or (maximum == 1 and probability <= 1)):
                    values[index]["probabilities"].append(probability)
                    values[index]["wins"] += horse_number == record["winner_number"]
                    break

    calibration = []
    for (label, _, _), bucket in zip(CALIBRATION_BUCKETS, values):
        probabilities = bucket["probabilities"]
        samples = len(probabilities)
        wins = bucket["wins"]
        calibration.append(
            {
                "range": label,
                "samples": samples,
                "wins": wins,
                "average_predicted_probability": (
                    round_metric(sum(probabilities) / samples) if samples else None
                ),
                "actual_win_rate": round_metric(wins / samples) if samples else None,
            }
        )
    return calibration


def method_evaluation(
    payload: dict[str, Any],
    method: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if method == TRADITIONAL_PREDICTION_METHOD:
        prediction = payload.get("prediction")
        record = evaluation_record(payload)
        if not isinstance(prediction, dict) or record is None:
            return None
        return record, prediction

    evaluation = find_variant(evaluation_variants(payload), method)
    if evaluation is None:
        return None
    prediction = find_variant(
        prediction_variants(payload),
        method,
        evaluation.get("model_provider"),
        evaluation.get("model_name"),
    )
    record = evaluation_record_from(evaluation)
    if prediction is None or record is None:
        return None
    return record, prediction


def collect_method_evaluations(
    payloads: list[dict[str, Any]],
    method: str,
) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    evaluated = []
    for payload in payloads:
        item = method_evaluation(payload, method)
        if item is not None:
            record, prediction = item
            evaluated.append((payload, record, prediction))
    return evaluated


def build_method_summary(
    evaluated: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "overall": aggregate_prediction_metrics([record for _, record, _ in evaluated]),
        "calibration": aggregate_calibration(evaluated),
        "segments": aggregate_segments([(payload, record) for payload, record, _ in evaluated]),
    }


def build_paired_comparison(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    traditional_records = []
    statistical_records = []
    for payload in payloads:
        traditional = method_evaluation(payload, TRADITIONAL_PREDICTION_METHOD)
        statistical = method_evaluation(payload, STATISTICAL_PREDICTION_METHOD)
        if traditional is None or statistical is None:
            continue
        traditional_records.append(traditional[0])
        statistical_records.append(statistical[0])

    traditional_metrics = aggregate_prediction_metrics(traditional_records)
    statistical_metrics = aggregate_prediction_metrics(statistical_records)
    count = len(traditional_records)
    return {
        "compared_races": count,
        "methods": {
            TRADITIONAL_PREDICTION_METHOD: traditional_metrics,
            STATISTICAL_PREDICTION_METHOD: statistical_metrics,
        },
        "differences": {
            "definition": "statistical minus traditional; negative values favor statistical",
            "average_log_loss": (
                round_metric(
                    statistical_metrics["average_log_loss"]
                    - traditional_metrics["average_log_loss"]
                )
                if count
                else None
            ),
            "average_brier_score": (
                round_metric(
                    statistical_metrics["average_brier_score"]
                    - traditional_metrics["average_brier_score"]
                )
                if count
                else None
            ),
        },
    }


def comparable_market_records(
    evaluated: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    comparable = []
    for payload, record in evaluated:
        market = (payload.get("evaluation") or {}).get("market_baseline")
        if not isinstance(market, dict):
            continue
        if market.get("available") is not True or market.get("odds_recorded_after_start") is True:
            continue
        required = (
            market.get("log_loss"),
            market.get("brier_score"),
            market.get("model_log_loss_difference"),
            market.get("model_brier_difference"),
        )
        if any(finite_number(value) is None for value in required):
            continue
        comparable.append((payload, record, market))
    return comparable


def aggregate_market_comparison(
    comparable: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    count = len(comparable)
    if not count:
        return {
            "comparable_races": 0,
            "model_average_log_loss": None,
            "market_average_log_loss": None,
            "average_log_loss_difference": None,
            "model_average_brier_score": None,
            "market_average_brier_score": None,
            "average_brier_score_difference": None,
        }
    return {
        "comparable_races": count,
        "model_average_log_loss": round_metric(
            sum(record["log_loss"] for _, record, _ in comparable) / count
        ),
        "market_average_log_loss": round_metric(
            sum(float(market["log_loss"]) for _, _, market in comparable) / count
        ),
        "average_log_loss_difference": round_metric(
            sum(float(market["model_log_loss_difference"]) for _, _, market in comparable)
            / count
        ),
        "model_average_brier_score": round_metric(
            sum(record["brier_score"] for _, record, _ in comparable) / count
        ),
        "market_average_brier_score": round_metric(
            sum(float(market["brier_score"]) for _, _, market in comparable) / count
        ),
        "average_brier_score_difference": round_metric(
            sum(float(market["model_brier_difference"]) for _, _, market in comparable)
            / count
        ),
    }


def aggregate_market_characteristics(
    comparable: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    records = []
    for payload, _, market in comparable:
        try:
            model_rows = ranked_probabilities(payload)
        except (KeyError, TypeError, ValueError):
            continue
        probabilities = normalized_market_probabilities(payload, model_rows)
        if probabilities is None:
            continue
        ranked = sorted(
            probabilities,
            key=lambda item: (-item["probability"], item["horse_number"]),
        )
        winner_rank = positive_integer(market.get("winner_rank"))
        winner_probability = finite_number(market.get("winner_probability"))
        if not ranked or winner_rank is None or winner_probability is None:
            continue
        records.append(
            {
                "race_id": str((payload.get("meta") or {}).get("race_id") or ""),
                "date": (payload.get("race") or {}).get("date"),
                "market_top1_probability": round_metric(ranked[0]["probability"]),
                "market_top3_probability": round_metric(
                    sum(item["probability"] for item in ranked[:3])
                ),
                "winner_market_rank": winner_rank,
                "winner_market_probability": round_metric(winner_probability),
            }
        )

    count = len(records)
    return {
        "race_count": count,
        "average_market_top1_probability": (
            round_metric(sum(item["market_top1_probability"] for item in records) / count)
            if count
            else None
        ),
        "average_market_top3_probability": (
            round_metric(sum(item["market_top3_probability"] for item in records) / count)
            if count
            else None
        ),
        "average_winner_market_rank": (
            round_metric(sum(item["winner_market_rank"] for item in records) / count)
            if count
            else None
        ),
        "average_winner_market_probability": (
            round_metric(sum(item["winner_market_probability"] for item in records) / count)
            if count
            else None
        ),
        "races": records,
    }


def aggregate_simulation(
    evaluated: list[tuple[dict[str, Any], dict[str, Any]]],
    method: str,
) -> dict[str, Any]:
    records = []
    for payload, _ in evaluated:
        result = ((payload.get("evaluation") or {}).get("simulation_results") or {}).get(method)
        if not isinstance(result, dict):
            continue
        stake = finite_number(result.get("total_stake"))
        returned = finite_number(result.get("total_return"))
        profit = finite_number(result.get("profit"))
        hit = result.get("hit")
        if (
            stake is None
            or returned is None
            or profit is None
            or stake < 0
            or returned < 0
            or not isinstance(hit, bool)
        ):
            continue
        records.append(
            {
                "stake": int(stake),
                "return": int(returned),
                "profit": int(profit),
                "hit": hit,
            }
        )

    total_stake = sum(record["stake"] for record in records)
    total_return = sum(record["return"] for record in records)
    return {
        "simulation_races": len(records),
        "purchase_races": sum(record["stake"] > 0 for record in records),
        "hit_races": sum(record["hit"] for record in records),
        "total_stake": total_stake,
        "total_return": total_return,
        "cumulative_profit": sum(record["profit"] for record in records),
        "overall_roi": round_metric(total_return / total_stake) if total_stake else None,
    }


def build_evaluation_summary(
    payloads: list[dict[str, Any]],
    generated_at: str | None = None,
) -> dict[str, Any]:
    traditional = collect_method_evaluations(payloads, TRADITIONAL_PREDICTION_METHOD)
    statistical = collect_method_evaluations(payloads, STATISTICAL_PREDICTION_METHOD)
    evaluated = [(payload, record) for payload, record, _ in traditional]
    comparable = comparable_market_records(evaluated)
    return {
        "generated_at": generated_at or now_jst_iso(),
        "overall": aggregate_prediction_metrics([record for _, record in evaluated]),
        "market_comparison": aggregate_market_comparison(comparable),
        "calibration": aggregate_calibration(traditional),
        "segments": aggregate_segments(evaluated),
        "market_characteristics": aggregate_market_characteristics(comparable),
        "simulation": {
            "value": aggregate_simulation(evaluated, "value"),
            "dutching": aggregate_simulation(evaluated, "dutching"),
        },
        "methods": {
            TRADITIONAL_PREDICTION_METHOD: build_method_summary(traditional),
            STATISTICAL_PREDICTION_METHOD: build_method_summary(statistical),
        },
        "paired_comparison": build_paired_comparison(payloads),
    }


def evaluation_summary_path(config: dict[str, Any], root: Path | None = None) -> Path:
    return data_dir(config, root) / "evaluation_summary.json"


def load_evaluation_summary(
    config: dict[str, Any],
    root: Path | None = None,
) -> dict[str, Any] | None:
    path = evaluation_summary_path(config, root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def generate_evaluation_summary(
    config: dict[str, Any],
    job_name: str = "evaluation_summary",
    root: Path | None = None,
) -> Path:
    logger = setup_logger(job_name, config, root)
    payloads = []
    for path in list_race_files(config, None, root):
        try:
            payload = load_race_json(path)
        except (OSError, ValueError):
            log_job(logger, job_name, None, f"summary skipped invalid JSON -> {path}")
            continue
        if payload:
            payloads.append(payload)
    summary = build_evaluation_summary(payloads)
    output_path = evaluation_summary_path(config, root)
    atomic_write_json(output_path, summary)
    log_job(
        logger,
        job_name,
        None,
        f"evaluation summary updated: races={summary['overall']['evaluated_races']} -> {output_path}",
    )
    return output_path


def main() -> None:
    argparse.ArgumentParser().parse_args()
    config = load_config()
    generate_evaluation_summary(config)


if __name__ == "__main__":
    main()
