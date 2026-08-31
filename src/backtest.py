from __future__ import annotations

import argparse
import math
from collections import Counter
from pathlib import Path
from typing import Any

from simulate import calculate_dutching_pre, calculate_post, calculate_value_pre
from utils import (
    STATISTICAL_PREDICTION_METHOD,
    TRADITIONAL_PREDICTION_METHOD,
    list_race_files,
    load_config,
    load_race_json,
    prediction_variants,
)

METHOD_LABELS = {
    TRADITIONAL_PREDICTION_METHOD: "総合AI予想",
    STATISTICAL_PREDICTION_METHOD: "統計重視予想",
}
SIMULATION_LABELS = {
    "dutching": "単勝分配方式",
    "value": "期待値重視方式",
}
EXCLUSION_LABELS = {
    "invalid_json": "race JSONを読み込めない",
    "result_missing": "結果または単勝払戻が未取得・不完全",
    "prediction_missing": "予測が存在しない",
    "statistical_prediction_missing": "統計重視予想が存在しない",
    "prediction_odds_incomplete": "予測または予想時単勝オッズが不完全",
}


def empty_metrics() -> dict[str, Any]:
    return {
        "target_races": 0,
        "purchased_races": 0,
        "hit_races": 0,
        "total_stake": 0,
        "total_return": 0,
        "profit": 0,
        "return_rate": None,
    }


def prediction_for_method(
    payload: dict[str, Any],
    method: str,
) -> dict[str, Any] | None:
    if method == TRADITIONAL_PREDICTION_METHOD:
        prediction = payload.get("prediction")
        return prediction if isinstance(prediction, dict) else None
    return next(
        (
            item
            for item in prediction_variants(payload)
            if item.get("method") == method
        ),
        None,
    )


def has_complete_result(payload: dict[str, Any]) -> bool:
    result = payload.get("result")
    if not isinstance(result, dict):
        return False

    result_horses = result.get("horses")
    payout_groups = result.get("payouts")
    win_payouts = payout_groups.get("win") if isinstance(payout_groups, dict) else None
    if not isinstance(result_horses, list) or not isinstance(win_payouts, list):
        return False

    winners = set()
    payouts = {}
    try:
        for item in result_horses:
            if item.get("finish_position") == 1:
                winners.add(int(item["horse_number"]))
        for item in win_payouts:
            payouts[int(item["horse_number"])] = int(item["payout_per_100"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    return bool(winners) and all(payouts.get(horse_number, 0) > 0 for horse_number in winners)


def has_complete_prediction_odds(
    payload: dict[str, Any],
    prediction: dict[str, Any],
) -> bool:
    prediction_horses = prediction.get("horses")
    race_horses = payload.get("horses")
    if not isinstance(prediction_horses, list) or not prediction_horses:
        return False
    if not isinstance(race_horses, list) or not race_horses:
        return False

    odds_lookup: dict[int, float] = {}
    for horse in race_horses:
        try:
            horse_number = int(horse["horse_number"])
            odds = float(horse["win_odds"])
        except (KeyError, TypeError, ValueError):
            return False
        if horse_number in odds_lookup or not math.isfinite(odds) or odds <= 1:
            return False
        odds_lookup[horse_number] = odds

    prediction_numbers = []
    for item in prediction_horses:
        try:
            horse_number = int(item["horse_number"])
            probability = float(item["win_probability"])
        except (KeyError, TypeError, ValueError):
            return False
        if not math.isfinite(probability) or probability < 0:
            return False
        prediction_numbers.append(horse_number)

    return (
        len(prediction_numbers) == len(set(prediction_numbers))
        and set(prediction_numbers) == set(odds_lookup)
    )


def add_post_result(metrics: dict[str, Any], post: dict[str, Any]) -> None:
    total_stake = int(post["total_stake"])
    total_return = int(post["total_return"])
    metrics["target_races"] += 1
    metrics["total_stake"] += total_stake
    metrics["total_return"] += total_return
    if total_stake > 0:
        metrics["purchased_races"] += 1
    if total_return > 0:
        metrics["hit_races"] += 1


def finalize_metrics(metrics: dict[str, Any]) -> None:
    metrics["profit"] = metrics["total_return"] - metrics["total_stake"]
    metrics["return_rate"] = (
        metrics["total_return"] / metrics["total_stake"]
        if metrics["total_stake"] > 0
        else None
    )


def run_backtest(
    config: dict[str, Any],
    root: Path | None = None,
) -> dict[str, Any]:
    methods = {
        method: {
            "label": label,
            "dutching": empty_metrics(),
            "value": empty_metrics(),
            "excluded": Counter(),
        }
        for method, label in METHOD_LABELS.items()
    }
    paths = list_race_files(config, None, root)

    for path in paths:
        try:
            payload = load_race_json(path)
        except (OSError, ValueError):
            for method in methods.values():
                method["excluded"]["invalid_json"] += 1
            continue
        if not payload or not has_complete_result(payload):
            for method in methods.values():
                method["excluded"]["result_missing"] += 1
            continue

        for method_name, method in methods.items():
            prediction = prediction_for_method(payload, method_name)
            if prediction is None:
                reason = (
                    "statistical_prediction_missing"
                    if method_name == STATISTICAL_PREDICTION_METHOD
                    else "prediction_missing"
                )
                method["excluded"][reason] += 1
                continue
            if not has_complete_prediction_odds(payload, prediction):
                method["excluded"]["prediction_odds_incomplete"] += 1
                continue

            value_pre = calculate_value_pre(payload, config, prediction)
            dutching_pre = calculate_dutching_pre(payload, config, prediction)
            value_post = calculate_post(value_pre, payload["result"])
            dutching_post = calculate_post(dutching_pre, payload["result"])
            if value_post is None or dutching_post is None:
                raise RuntimeError(f"simulation calculation failed: {path}")

            add_post_result(method["value"], value_post)
            add_post_result(method["dutching"], dutching_post)

    for method in methods.values():
        finalize_metrics(method["dutching"])
        finalize_metrics(method["value"])
        method["excluded"] = dict(method["excluded"])

    return {
        "race_files": len(paths),
        "methods": methods,
    }


def format_money(value: int, *, signed: bool = False) -> str:
    if signed and value > 0:
        return f"+{value:,}円"
    return f"{value:,}円"


def format_backtest_report(report: dict[str, Any]) -> str:
    lines = [f"確認race JSON: {report['race_files']}件"]
    for method_name in METHOD_LABELS:
        method = report["methods"][method_name]
        lines.extend(["", method["label"]])
        for simulation_name in ("dutching", "value"):
            metrics = method[simulation_name]
            return_rate = metrics["return_rate"]
            lines.extend(
                [
                    "",
                    SIMULATION_LABELS[simulation_name],
                    f"対象レース: {metrics['target_races']}",
                    f"購入レース: {metrics['purchased_races']}",
                    f"的中レース: {metrics['hit_races']}",
                    f"総投資額: {format_money(metrics['total_stake'])}",
                    f"総払戻額: {format_money(metrics['total_return'])}",
                    f"収支: {format_money(metrics['profit'], signed=True)}",
                    "回収率: "
                    + (f"{return_rate * 100:.1f}%" if return_rate is not None else "-"),
                ]
            )

        exclusions = method["excluded"]
        lines.extend(["", f"対象外レース: {sum(exclusions.values())}"])
        for reason, count in exclusions.items():
            lines.append(f"- {EXCLUSION_LABELS[reason]}: {count}件")
    return "\n".join(lines)


def main() -> None:
    argparse.ArgumentParser(description="現行設定で保存済みレースを再計算します").parse_args()
    report = run_backtest(load_config())
    print(format_backtest_report(report))


if __name__ == "__main__":
    main()
