from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from llm_client import LLMClient
from utils import (
    list_race_files,
    load_config,
    load_race_json,
    log_job,
    now_jst_iso,
    parse_target_date,
    read_text,
    repo_root,
    save_race_json,
    setup_logger,
)


def build_feedback_stats(payload: dict[str, Any]) -> dict[str, Any] | None:
    prediction = payload.get("prediction")
    result = payload.get("result")
    simulation = payload.get("simulation") or {}
    if not prediction or not result:
        return None

    predicted_horses = sorted(prediction["horses"], key=lambda item: item["win_probability"], reverse=True)
    actual_order = result.get("finish_order", [])
    if not predicted_horses or not actual_order:
        return None

    actual_winner = actual_order[0]
    pred_rank_lookup = {item["horse_number"]: index + 1 for index, item in enumerate(predicted_horses)}
    actual_rank_lookup = {horse_number: index + 1 for index, horse_number in enumerate(actual_order)}

    squared_errors = []
    ranking_errors = []
    for item in prediction["horses"]:
        horse_number = item["horse_number"]
        predicted_probability = float(item["win_probability"])
        actual_value = 1.0 if horse_number == actual_winner else 0.0
        squared_errors.append((predicted_probability - actual_value) ** 2)
        if horse_number in actual_rank_lookup:
            ranking_errors.append(abs(pred_rank_lookup[horse_number] - actual_rank_lookup[horse_number]))

    post = simulation.get("post") or {}
    top_pick = predicted_horses[0]["horse_number"]
    brier = round(sum(squared_errors) / len(squared_errors), 6) if squared_errors else None
    mean_rank_error = round(sum(ranking_errors) / len(ranking_errors), 4) if ranking_errors else None

    return {
        "race": payload["race"],
        "predicted_top3": [item["horse_number"] for item in predicted_horses[:3]],
        "actual_top3": actual_order[:3],
        "top_pick": top_pick,
        "actual_winner": actual_winner,
        "brier_score": brier,
        "mean_rank_error": mean_rank_error,
        "profit": post.get("profit"),
        "roi": post.get("roi"),
        "selection_count": len((simulation.get("pre") or {}).get("selections", [])),
    }


def fallback_feedback(stats: dict[str, Any]) -> dict[str, Any]:
    top_hit = "一致" if stats["top_pick"] == stats["actual_winner"] else "不一致"
    roi = stats["roi"]
    return {
        "probability_error_summary": f"Brier {stats['brier_score']:.4f}、本命 {stats['top_pick']} と勝ち馬 {stats['actual_winner']} は {top_hit}",
        "ranking_error_summary": f"平均順位誤差 {stats['mean_rank_error']:.2f}、予想上位 {stats['predicted_top3']} / 実着順上位 {stats['actual_top3']}",
        "profit_summary": f"損益 {stats['profit']} 円、ROI {(roi * 100):.2f}%" if roi is not None else "収支情報なし",
        "calibration_notes": "上位馬の確率を出し過ぎたかを本命と勝ち馬のズレで確認",
        "next_prediction_adjustment_summary": "本命と勝ち馬がズレた条件差を次回の理由欄へ明示する",
    }


def build_mock_feedback(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "probability_error_summary": f"mock: Brier {stats['brier_score']:.4f}",
        "ranking_error_summary": f"mock: predicted {stats['predicted_top3']} / actual {stats['actual_top3']}",
        "profit_summary": f"mock: profit {stats['profit']} / roi {stats['roi']}",
        "calibration_notes": "mock: top pick bias should be checked next time",
        "next_prediction_adjustment_summary": "mock: compare pace and distance fit more explicitly",
    }


def build_feedback_prompt(stats: dict[str, Any], root: Path | None = None) -> str:
    root = root or repo_root()
    template = read_text(root / "config" / "prompt_feedback.txt")
    return template.replace("{{SUMMARY_CONTEXT}}", json.dumps(stats, ensure_ascii=False, indent=2))


def build_feedback_chat_input(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "meta": {
            "race_id": payload["meta"].get("race_id"),
            "kind": "feedback",
            "generated_at": now_jst_iso(),
        },
        "race": payload["race"],
        "horses": payload["horses"],
        "prediction": payload["prediction"],
        "simulation": payload["simulation"],
        "result": payload["result"],
    }


def normalize_feedback_response(response: dict[str, Any]) -> dict[str, str]:
    required = (
        "probability_error_summary",
        "ranking_error_summary",
        "profit_summary",
        "calibration_notes",
        "next_prediction_adjustment_summary",
    )
    normalized = {}
    for key in required:
        value = str(response.get(key, "")).strip()
        if not value:
            raise ValueError(f"feedback missing field: {key}")
        normalized[key] = value
    return normalized


def feedback_file(path: Path, config: dict[str, Any], job_name: str, root: Path | None = None) -> bool:
    logger = setup_logger(job_name, config, root)
    payload = load_race_json(path)
    if not payload:
        return False

    race_id = payload["meta"].get("race_id")
    stats = build_feedback_stats(payload)
    if not stats:
        log_job(logger, job_name, race_id, "feedback skipped: prediction/result missing")
        return False

    try:
        if config["llm_provider"] == "mock":
            normalized = build_mock_feedback(stats)
        else:
            client = LLMClient.from_config(config)
            prompt = build_feedback_prompt(stats, root)
            response = client.invoke_json(prompt, max_retries=2)
            normalized = normalize_feedback_response(response)
    except Exception as exc:  # noqa: BLE001
        log_job(logger, job_name, race_id, f"feedback llm fallback: {exc}")
        normalized = fallback_feedback(stats)

    normalized["generated_at"] = now_jst_iso()
    payload["feedback"] = normalized
    save_race_json(path, payload)
    log_job(logger, job_name, race_id, "feedback updated")
    return True


def feedback_paths(
    paths: list[Path],
    config: dict[str, Any],
    job_name: str,
    root: Path | None = None,
) -> list[Path]:
    updated = []
    for path in paths:
        if feedback_file(path, config, job_name, root):
            updated.append(path)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    config = load_config()
    target_date = parse_target_date(args.date)
    paths = list_race_files(config, target_date)
    feedback_paths(paths, config, "post")


if __name__ == "__main__":
    main()
