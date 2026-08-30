from __future__ import annotations

import argparse
import json
import math
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from evaluation_summary import load_evaluation_summary
from simulate import calculate_value_details, minimum_budget_for_value_stake, round_ratio
from utils import (
    STATISTICAL_PREDICTION_METHOD,
    TRADITIONAL_PREDICTION_METHOD,
    evaluation_variants,
    ensure_dir,
    find_variant,
    list_race_files,
    load_config,
    load_race_json,
    log_job,
    now_jst,
    parse_jst_datetime,
    parse_target_date,
    prediction_variants,
    public_dir,
    race_html_path,
    race_result_html_path,
    race_start_datetime,
    repo_root,
    stage_dir,
    simulation_variants,
    track_name_from_race_id,
)


STATUS_LABELS = {
    "prediction_only": "予想公開",
    "result_published": "結果公開",
}
STATUS_CLASSES = {
    "prediction_only": "status-prediction",
    "result_published": "status-result",
}
INDEX_STATUS_PRIORITIES = {
    "prediction_only": 0,
    "result_published": 2,
}
STATUS_COLORS = {
    "prediction": {"background": "#dde9e4", "color": "#32594d", "border": "#b8cec5"},
    "result": {"background": "#e4eef3", "color": "#315b78", "border": "#b7cbd7"},
    "pending": {"background": "#ececeb", "color": "#5f625f", "border": "#d0d1cf"},
}
SITE_BACKGROUND = "#f2f2f0"

PREDICTION_METHOD_LABELS = {
    TRADITIONAL_PREDICTION_METHOD: "総合AI予想",
    STATISTICAL_PREDICTION_METHOD: "統計重視予想",
}
PREDICTION_METHOD_DESCRIPTIONS = {
    TRADITIONAL_PREDICTION_METHOD: "過去成績や今回のレース条件、市場評価などを総合して1着確率を推定しています。",
    STATISTICAL_PREDICTION_METHOD: "市場情報を使用せず、過去成績や今回のレース条件などの客観データから1着確率を推定しています。",
}

TOOLTIPS = {
    "dutching_method": "AI予想上位の複数馬を対象に、どの馬が勝っても払戻額が近くなるよう購入額を配分する方式です。",
    "value_method": "AIが推定した1着確率と単勝オッズから各馬の期待値を計算し、最低EVを満たす馬についてKelly基準で予算に対する購入割合を算出する方式です。Kelly係数で購入割合を抑え、購入単位未満の金額は購入対象から除外します。",
    "coverage_probability": "選択した馬の1着確率を合計した値です。",
    "group_expected_value": "選択馬全体の期待払戻額を合計購入額で割った値です。1.0が損益分岐の目安です。",
    "minimum_ev": "1着確率と単勝オッズから計算した期待値について、購入対象とする最低ラインです。1.0が損益分岐の目安です。1.0未満も入力できますが、Kelly基準で購入割合が0以下になる馬には購入額を割り当てません。",
    "kelly_fraction": "Kelly基準は、予測確率とオッズから、資金を長期的に効率よく増やすための購入割合を算出する方法です。Kelly係数は、その算出額を実際に何割使うかを示します。0.5なら算出額の半分を使用する「ハーフケリー」、0.25なら4分の1を使用する「クォーターケリー」です。",
    "ev": "1着確率×単勝オッズで計算する期待値です。1.0が損益分岐の目安です。",
    "full_kelly": "予測確率と単勝オッズからKelly基準で算出した、予算に対する購入割合です。",
    "fractional_kelly": "Full KellyにKelly係数を掛けて抑制した購入割合です。係数0.5ならハーフケリーとなります。",
    "applied_kelly": "Full KellyへKelly係数を掛けた、実際のシミュレーションで使用する購入割合です。",
    "theoretical_stake": "現在の予算に適用Kellyを掛けた、購入単位へ丸める前の購入額です。",
    "minimum_budget": "現在の設定条件で、購入額が初めて1購入単位以上になる予算です。",
    "minimum_payout": "選択した馬のうち、最も払戻額が低い馬が的中した場合の払戻額です。",
    "minimum_profit": "選択した馬のうち、最も利益が低い馬が的中した場合の利益です。",
    "minimum_profit_rate": "購入金額に対する最低限の利益率を設定します。20%なら合計3,000円購入時に最低600円以上の利益が必要です。",
    "expected_return": "各馬の予測確率を考慮した、平均的な払戻見込み額です。",
}

REJECTION_REASON_LABELS = {
    "coverage_probability_below_threshold": "カバー確率が最低基準未満",
    "group_expected_value_below_threshold": "グループ期待値が最低基準未満",
    "minimum_profit_rate_below_threshold": "最低利益率が最低基準未満",
    "minimum_profit_not_positive": "的中時の最低利益を確保できない",
    "insufficient_budget_units": "予算が購入単位または選択頭数に対して不足",
}
UNKNOWN_REJECTION_REASON_LABEL = "条件を満たしていません"


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, "処理中")


def status_class(status: str) -> str:
    return STATUS_CLASSES.get(status, "status-pending")


def index_row_sort_key(row: dict[str, Any]) -> tuple[int, float, str, str]:
    start = race_start_datetime(row.get("date"), row.get("start_time") or "00:00")
    timestamp = start.timestamp() if start else float("-inf")
    return (
        INDEX_STATUS_PRIORITIES.get(row.get("status"), 1),
        -timestamp,
        row.get("track") or "",
        row.get("prediction_href") or row.get("href") or "",
    )


def format_jst_datetime(value: str | None) -> str:
    parsed = parse_jst_datetime(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S") if parsed else "-"


def is_created_this_week(
    created_at: str | None,
    reference: datetime | None = None,
) -> bool:
    created = parse_jst_datetime(created_at)
    current = parse_jst_datetime(reference.isoformat()) if reference else now_jst()
    if created is None or current is None:
        return False
    week_start = current.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=current.weekday()
    )
    return week_start <= created < week_start + timedelta(days=7)


def rejection_reason_text(reasons: list[str]) -> str:
    if not reasons:
        return "-"
    return "、".join(
        REJECTION_REASON_LABELS.get(reason, UNKNOWN_REJECTION_REASON_LABEL)
        for reason in reasons
    )


def comparable_finish_position(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    return None


def rank_comparison(prediction_rank: int | None, finish_position: Any) -> tuple[str, str]:
    finish = comparable_finish_position(finish_position)
    if prediction_rank is None or finish is None:
        return "-", "comparison-neutral"
    if finish < prediction_rank:
        return f"{prediction_rank - finish}着上", "comparison-up"
    if finish > prediction_rank:
        return f"{finish - prediction_rank}着下", "comparison-down"
    return "差なし", "comparison-neutral"


def build_odds_timing(race: dict[str, Any]) -> tuple[str, bool]:
    start = race_start_datetime(race.get("date"), race.get("start_time"))
    captured = parse_jst_datetime(race.get("odds_captured_at"))
    if start is None or captured is None:
        return "-", False

    seconds_from_start = (captured - start).total_seconds()
    minutes = int((abs(seconds_from_start) / 60.0) + 0.5)
    if minutes == 0:
        return "発走時点", seconds_from_start > 0
    if seconds_from_start < 0:
        return f"発走{minutes}分前", False
    return f"発走{minutes}分後", True


def finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def build_expected_value_rows(
    payload: dict[str, Any],
    horse_rows: list[dict[str, Any]],
    value_pre: dict[str, Any],
    prediction: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    budget = int(value_pre["budget"])
    stake_unit = int(value_pre["stake_unit"])
    settings = value_pre["settings"]
    details = calculate_value_details(payload, budget, stake_unit, settings, prediction)
    detail_lookup = {item["horse_number"]: item for item in details}
    selection_lookup = {
        item["horse_number"]: item
        for item in value_pre.get("selections", [])
    }
    rows = []
    for horse in horse_rows:
        probability = finite_float((horse.get("prediction") or {}).get("win_probability"))
        odds = finite_float(horse.get("win_odds"))
        detail = detail_lookup.get(horse["horse_number"])
        selection = selection_lookup.get(horse["horse_number"])
        if detail is None:
            purchase_status = "unavailable"
            purchase_decision = "算出不可"
        elif not detail["meets_threshold"]:
            purchase_status = "ev_below"
            purchase_decision = "EV基準未満"
        elif detail["full_kelly"] <= 0 or detail["fractional_kelly"] <= 0:
            purchase_status = "zero_kelly"
            purchase_decision = "Kelly割合が0"
        elif selection is None:
            purchase_status = "below_unit"
            purchase_decision = "購入単位未満"
        else:
            purchase_status = "purchase"
            purchase_decision = f"購入：{selection['stake']}円"

        rows.append(
            {
                "horse_number": horse["horse_number"],
                "horse_name": horse["horse_name"],
                "win_probability": probability,
                "win_odds": odds,
                "expected_value": detail["expected_value"] if detail else None,
                "meets_threshold": detail["meets_threshold"] if detail else None,
                "full_kelly": detail["full_kelly"] if detail else None,
                "fractional_kelly": detail["fractional_kelly"] if detail else None,
                "theoretical_stake": detail["theoretical_stake"] if detail else None,
                "minimum_budget": (
                    minimum_budget_for_value_stake(
                        payload,
                        stake_unit,
                        settings,
                        horse["horse_number"],
                        prediction,
                    )
                    if detail and detail["eligible"]
                    else None
                ),
                "purchase_status": purchase_status,
                "purchase_decision": purchase_decision,
            }
        )

    rows.sort(
        key=lambda item: (
            item["expected_value"] is None,
            -(item["expected_value"] or 0.0),
            item["horse_number"],
        )
    )
    for rank, row in enumerate((item for item in rows if item["expected_value"] is not None), start=1):
        row["ev_rank"] = rank
    for row in rows:
        row.setdefault("ev_rank", None)
    return rows


def build_value_selection_rows(value_pre: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not value_pre:
        return []
    rows = []
    for selection in value_pre.get("selections", []):
        stake = finite_float(selection.get("stake"))
        expected_value = finite_float(selection.get("expected_value"))
        expected_return = (
            round_ratio(stake * expected_value)
            if stake is not None and expected_value is not None
            else None
        )
        rows.append({**selection, "expected_return": expected_return})
    return rows


def value_no_purchase_message(
    rows: list[dict[str, Any]],
    stake_unit: int,
    kelly_fraction: float,
) -> str:
    if any(row["purchase_status"] == "purchase" for row in rows):
        return ""
    calculable = [row for row in rows if row["expected_value"] is not None]
    if not calculable:
        return "オッズまたは予測確率を算出できる馬がありません。"
    above_threshold = [row for row in calculable if row["meets_threshold"]]
    if not above_threshold:
        return "最低EVを満たす馬がありません。"
    if any(row["purchase_status"] == "below_unit" for row in above_threshold):
        return (
            "EV基準以上の馬はありますが、現在の予算ではKelly基準の購入額が"
            f"{stake_unit}円未満となるため、購入対象はありません。"
        )
    if kelly_fraction <= 0:
        return "Kelly係数が0のため、購入対象はありません。"
    return "EV基準以上の馬はありますが、Kelly割合が0のため、購入対象はありません。"


def build_environment(root: Path | None = None) -> Environment:
    root = root or repo_root()
    return Environment(
        loader=FileSystemLoader(root / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )


def rendered_html_bytes(content: str) -> bytes:
    return content.encode("utf-8")


def result_final_win_odds(
    payload: dict[str, Any],
    result: dict[str, Any] | None,
) -> dict[int, float]:
    expected_numbers = {
        int(horse["horse_number"])
        for horse in payload.get("horses", [])
    }
    entries = (result or {}).get("final_win_odds")
    if not expected_numbers or not isinstance(entries, list) or len(entries) != len(expected_numbers):
        return {}

    odds_by_horse: dict[int, float] = {}
    for entry in entries:
        if not isinstance(entry, dict) or isinstance(entry.get("horse_number"), bool):
            return {}
        try:
            horse_number = int(entry["horse_number"])
        except (KeyError, TypeError, ValueError):
            return {}
        odds = finite_float(entry.get("win_odds"))
        if odds is None or odds <= 0 or horse_number in odds_by_horse:
            return {}
        odds_by_horse[horse_number] = odds

    return odds_by_horse if set(odds_by_horse) == expected_numbers else {}


def build_prediction_horse_rows(
    payload: dict[str, Any],
    prediction: dict[str, Any] | None,
    result: dict[str, Any] | None,
    final_win_odds: dict[int, float],
) -> list[dict[str, Any]]:
    prediction_horses = (prediction or {}).get("horses", [])
    prediction_lookup = {item["horse_number"]: item for item in prediction_horses}
    prediction_ranks = {
        item["horse_number"]: rank
        for rank, item in enumerate(
            sorted(
                prediction_horses,
                key=lambda item: (-float(item["win_probability"]), item["horse_number"]),
            ),
            start=1,
        )
    }
    result_lookup = {
        item["horse_number"]: item["finish_position"]
        for item in (result or {}).get("horses", [])
    }
    payout_lookup = {
        item["horse_number"]: item["payout_per_100"]
        for item in (result or {}).get("payouts", {}).get("win", [])
    }
    return [
        {
            **horse,
            "prediction": prediction_lookup.get(horse["horse_number"]),
            "prediction_rank": prediction_ranks.get(horse["horse_number"]),
            "finish_position": result_lookup.get(horse["horse_number"]),
            "payout_per_100": payout_lookup.get(horse["horse_number"]),
            "final_win_odds": final_win_odds.get(horse["horse_number"]),
        }
        for horse in sorted(payload.get("horses", []), key=lambda item: item["horse_number"])
    ]


def build_result_rows(horse_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for horse in horse_rows:
        if horse.get("finish_position") is None:
            continue
        finish_position = horse["finish_position"]
        numeric_finish = comparable_finish_position(finish_position)
        comparison_text, comparison_class = rank_comparison(
            horse["prediction_rank"],
            finish_position,
        )
        comparison_sort_value = (
            numeric_finish - horse["prediction_rank"]
            if numeric_finish is not None and horse["prediction_rank"] is not None
            else None
        )
        rows.append(
            {
                "horse_number": horse["horse_number"],
                "horse_name": horse["horse_name"],
                "prediction_rank": horse["prediction_rank"],
                "win_probability": (horse.get("prediction") or {}).get("win_probability"),
                "finish_position": finish_position,
                "finish_position_label": (
                    f"{numeric_finish}着" if numeric_finish is not None else (finish_position or "-")
                ),
                "finish_position_sort_value": numeric_finish,
                "comparison_text": comparison_text,
                "comparison_class": comparison_class,
                "comparison_sort_value": comparison_sort_value,
                "prediction_rank_class": (
                    "rank-prediction-hit"
                    if horse["prediction_rank"] == 1 and numeric_finish == 1
                    else ("rank-prediction-top" if horse["prediction_rank"] == 1 else "")
                ),
                "finish_rank_class": (
                    "rank-prediction-hit"
                    if horse["prediction_rank"] == 1 and numeric_finish == 1
                    else ("rank-result-winner" if numeric_finish == 1 else "")
                ),
                "payout_per_100": horse.get("payout_per_100"),
                "final_win_odds": horse.get("final_win_odds"),
            }
        )
    return rows


def build_race_context(payload: dict[str, Any]) -> dict[str, Any]:
    race = payload.get("race", {})
    prediction = payload.get("prediction")
    statistical_prediction = find_variant(
        prediction_variants(payload),
        STATISTICAL_PREDICTION_METHOD,
    )
    simulation = payload.get("simulation") or {}
    result = payload.get("result")
    final_win_odds = result_final_win_odds(payload, result)
    evaluation = payload.get("evaluation")
    statistical_evaluation = None
    if statistical_prediction is not None:
        statistical_evaluation = find_variant(
            evaluation_variants(payload),
            STATISTICAL_PREDICTION_METHOD,
            statistical_prediction.get("model_provider"),
            statistical_prediction.get("model_name"),
        )
    odds_timing_label, odds_recorded_after_start = build_odds_timing(race)
    has_recorded_odds = (
        parse_jst_datetime(race.get("odds_captured_at")) is not None
        and any(
            (odds := finite_float(horse.get("win_odds"))) is not None and odds > 0
            for horse in payload.get("horses", [])
        )
    )

    statistical_simulation = None
    if statistical_prediction is not None:
        statistical_simulation = find_variant(
            simulation_variants(payload),
            STATISTICAL_PREDICTION_METHOD,
            statistical_prediction.get("model_provider"),
            statistical_prediction.get("model_name"),
        )

    prediction_specs = [
        (
            TRADITIONAL_PREDICTION_METHOD,
            prediction,
            simulation,
            evaluation,
        )
    ]
    if statistical_prediction is not None:
        prediction_specs.append(
            (
                STATISTICAL_PREDICTION_METHOD,
                statistical_prediction,
                statistical_simulation or {},
                statistical_evaluation,
            )
        )

    ai_views = []
    custom_simulation_methods = {}
    for method, method_prediction, method_simulation, method_evaluation in prediction_specs:
        horse_rows = build_prediction_horse_rows(
            payload,
            method_prediction,
            result,
            final_win_odds,
        )
        value_simulation = method_simulation.get("value") or {}
        dutching_simulation = method_simulation.get("dutching") or {}
        value_pre = value_simulation.get("pre")
        dutching_pre = dutching_simulation.get("pre")
        expected_value_rows = (
            build_expected_value_rows(
                payload,
                horse_rows,
                value_pre,
                method_prediction,
            )
            if value_pre
            else []
        )
        value_no_purchase_reason = (
            value_no_purchase_message(
                expected_value_rows,
                int(value_pre["stake_unit"]),
                float(value_pre["settings"]["kelly_fraction"]),
            )
            if value_pre and not value_pre.get("selections")
            else ""
        )
        custom_horses = [
            {
                "horse_number": horse["horse_number"],
                "win_probability": float(horse["prediction"]["win_probability"]),
                "win_odds": float(horse["win_odds"]),
            }
            for horse in horse_rows
            if horse.get("prediction") and horse.get("win_odds") is not None
        ]
        custom_simulation_methods[method] = {"horses": custom_horses}
        result_rows = build_result_rows(horse_rows)
        ai_views.append(
            {
                "method": method,
                "label": PREDICTION_METHOD_LABELS[method],
                "description": PREDICTION_METHOD_DESCRIPTIONS[method],
                "prediction": method_prediction,
                "horse_rows": horse_rows,
                "result_rows": result_rows,
                "prediction_top_hit": any(
                    row["prediction_rank"] == 1
                    and row["finish_position_sort_value"] == 1
                    for row in result_rows
                ),
                "evaluation": method_evaluation,
                "value_pre": value_pre,
                "value_post": value_simulation.get("post"),
                "dutching_pre": dutching_pre,
                "dutching_post": dutching_simulation.get("post"),
                "expected_value_rows": expected_value_rows,
                "value_selection_rows": build_value_selection_rows(value_pre),
                "value_no_purchase_reason": value_no_purchase_reason,
            }
        )

    traditional_view = ai_views[0]
    statistical_view = next(
        (view for view in ai_views if view["method"] == STATISTICAL_PREDICTION_METHOD),
        None,
    )
    value_pre = traditional_view["value_pre"]
    dutching_pre = traditional_view["dutching_pre"]
    custom_simulation_horses = custom_simulation_methods.get(
        TRADITIONAL_PREDICTION_METHOD,
        {"horses": []},
    )["horses"]
    custom_simulation_data = {
        "stake_unit": int((value_pre or dutching_pre or {}).get("stake_unit") or 100),
        "horses": custom_simulation_horses,
        "methods": custom_simulation_methods,
        "display": {
            "rejection_reason_labels": REJECTION_REASON_LABELS,
            "unknown_rejection_reason_label": UNKNOWN_REJECTION_REASON_LABEL,
        },
    }

    has_result_page = bool(result and evaluation)
    status = "result_published" if has_result_page else "prediction_only"
    return {
        "race": race,
        "prediction": prediction,
        "statistical_prediction": statistical_prediction,
        "ai_views": ai_views,
        "simulation_value_pre": traditional_view["value_pre"],
        "simulation_value_post": traditional_view["value_post"],
        "simulation_dutching_pre": traditional_view["dutching_pre"],
        "simulation_dutching_post": traditional_view["dutching_post"],
        "result": result,
        "final_win_odds_by_horse": final_win_odds,
        "evaluation": evaluation,
        "statistical_evaluation": statistical_evaluation,
        "horse_rows": traditional_view["horse_rows"],
        "statistical_horse_rows": statistical_view["horse_rows"] if statistical_view else [],
        "result_rows": traditional_view["result_rows"],
        "statistical_result_rows": statistical_view["result_rows"] if statistical_view else [],
        "expected_value_rows": traditional_view["expected_value_rows"],
        "value_no_purchase_reason": traditional_view["value_no_purchase_reason"],
        "has_result_page": has_result_page,
        "status": status,
        "status_label": status_label(status),
        "status_class": status_class(status),
        "status_colors": STATUS_COLORS,
        "site_background": SITE_BACKGROUND,
        "tooltips": TOOLTIPS,
        "rejection_reason_text": rejection_reason_text,
        "odds_captured_at_label": format_jst_datetime(race.get("odds_captured_at")),
        "result_fetched_at_label": format_jst_datetime((result or {}).get("fetched_at")),
        "odds_timing_label": odds_timing_label,
        "odds_recorded_after_start": odds_recorded_after_start,
        "has_recorded_odds": has_recorded_odds,
        "custom_simulation_data": custom_simulation_data,
    }


def render_site(
    config: dict[str, Any],
    job_name: str,
    race_date: str | None = None,
    root: Path | None = None,
) -> Path:
    root = root or repo_root()
    env = build_environment(root)
    race_template = env.get_template("race.html.j2")
    index_template = env.get_template("index.html.j2")

    output_dir = stage_dir(config, root)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    current_public_dir = public_dir(config, root)
    if current_public_dir.exists():
        shutil.copytree(current_public_dir, output_dir)
    else:
        ensure_dir(output_dir)

    managed_races_dir = output_dir / "races"
    if managed_races_dir.exists():
        for managed_html in managed_races_dir.rglob("*.html"):
            managed_html.unlink()
    ensure_dir(managed_races_dir)

    race_files = list_race_files(config, race_date, root)
    index_rows = []
    for path in race_files:
        persisted_payload = json.loads(path.read_text(encoding="utf-8"))
        persisted_created_at = (persisted_payload.get("meta") or {}).get("created_at")
        payload = load_race_json(path)
        if not payload or not payload.get("prediction"):
            continue
        context = build_race_context(payload)
        race = payload["race"]
        prediction_path = race_html_path(race["date"], race["track"], race["race_number"])
        result_path = race_result_html_path(race["date"], race["track"], race["race_number"])
        page_links = {
            "prediction_page_name": prediction_path.name,
            "result_page_name": result_path.name if context["has_result_page"] else None,
        }
        prediction_context = {
            **context,
            **page_links,
            "page_kind": "prediction",
            "status": "prediction_only",
            "status_label": status_label("prediction_only"),
            "status_class": status_class("prediction_only"),
        }
        prediction_target = output_dir / prediction_path
        ensure_dir(prediction_target.parent)
        prediction_target.write_bytes(rendered_html_bytes(race_template.render(**prediction_context)))
        if context["has_result_page"]:
            result_context = {
                **context,
                **page_links,
                "page_kind": "result",
                "status": "result_published",
                "status_label": status_label("result_published"),
                "status_class": status_class("result_published"),
            }
            result_target = output_dir / result_path
            ensure_dir(result_target.parent)
            result_target.write_bytes(rendered_html_bytes(race_template.render(**result_context)))
        index_rows.append(
            {
                "date": race["date"],
                "start_time": race.get("start_time"),
                "track": race["track"],
                "race_name": race["race_name"],
                "is_new": (
                    is_created_this_week(persisted_created_at)
                    and context["status"] == "prediction_only"
                ),
                "status": context["status"],
                "status_label": context["status_label"],
                "status_class": context["status_class"],
                "href": prediction_path.as_posix(),
                "prediction_href": prediction_path.as_posix(),
                "result_href": result_path.as_posix() if context["has_result_page"] else None,
            }
        )

    index_rows.sort(key=index_row_sort_key)
    index_html = index_template.render(
        races=index_rows,
        evaluation_summary=load_evaluation_summary(config, root),
        site_background=SITE_BACKGROUND,
        status_colors=STATUS_COLORS,
        prediction_method_descriptions=PREDICTION_METHOD_DESCRIPTIONS,
    )
    (output_dir / "index.html").write_bytes(rendered_html_bytes(index_html))
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    config = load_config()
    target_date = parse_target_date(args.date)
    render_site(config, "render", target_date)


if __name__ == "__main__":
    main()
