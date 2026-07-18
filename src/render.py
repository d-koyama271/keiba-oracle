from __future__ import annotations

import argparse
import math
import shutil
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from simulate import calculate_value_details, minimum_budget_for_value_stake
from utils import (
    ensure_dir,
    list_race_files,
    load_config,
    load_race_json,
    log_job,
    parse_jst_datetime,
    parse_target_date,
    public_dir,
    race_html_path,
    race_start_datetime,
    repo_root,
    stage_dir,
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
STATUS_COLORS = {
    "prediction": {"background": "#dde9e4", "color": "#32594d", "border": "#b8cec5"},
    "result": {"background": "#e4eef3", "color": "#315b78", "border": "#b7cbd7"},
    "pending": {"background": "#ececeb", "color": "#5f625f", "border": "#d0d1cf"},
}
SITE_BACKGROUND = "#f2f2f0"

TOOLTIPS = {
    "dutching_method": "AIの予測上位馬を複数選び、どの馬が勝っても払戻額が近くなるよう購入額を配分する方式です。",
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
    "expected_return": "各馬の予測確率を考慮した、平均的な払戻見込み額です。",
}

REJECTION_REASON_LABELS = {
    "coverage_probability_below_threshold": "カバー確率が最低基準未満",
    "group_expected_value_below_threshold": "グループ期待値が最低基準未満",
    "minimum_profit_not_positive": "的中時の最低利益を確保できない",
    "insufficient_budget_units": "予算が購入単位または選択頭数に対して不足",
}
UNKNOWN_REJECTION_REASON_LABEL = "条件を満たしていません"


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, "処理中")


def status_class(status: str) -> str:
    return STATUS_CLASSES.get(status, "status-pending")


def format_jst_datetime(value: str | None) -> str:
    parsed = parse_jst_datetime(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S") if parsed else "-"


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


def result_highlight_class(prediction_rank: int | None, finish_position: Any) -> str:
    finish = comparable_finish_position(finish_position)
    if prediction_rank == 1 and finish == 1:
        return "prediction-hit"
    if prediction_rank == 1:
        return "prediction-top"
    if finish == 1:
        return "result-winner"
    return ""


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
) -> list[dict[str, Any]]:
    budget = int(value_pre["budget"])
    stake_unit = int(value_pre["stake_unit"])
    settings = value_pre["settings"]
    details = calculate_value_details(payload, budget, stake_unit, settings)
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


def build_race_context(payload: dict[str, Any]) -> dict[str, Any]:
    race = payload.get("race", {})
    prediction = payload.get("prediction")
    simulation = payload.get("simulation") or {}
    result = payload.get("result")
    evaluation = payload.get("evaluation")
    odds_timing_label, odds_recorded_after_start = build_odds_timing(race)

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
    result_lookup = {item["horse_number"]: item["finish_position"] for item in (result or {}).get("horses", [])}
    payout_lookup = {item["horse_number"]: item["payout_per_100"] for item in (result or {}).get("payouts", {}).get("win", [])}

    horse_rows = []
    for horse in sorted(payload.get("horses", []), key=lambda item: item["horse_number"]):
        horse_rows.append(
            {
                **horse,
                "prediction": prediction_lookup.get(horse["horse_number"]),
                "prediction_rank": prediction_ranks.get(horse["horse_number"]),
                "finish_position": result_lookup.get(horse["horse_number"]),
                "payout_per_100": payout_lookup.get(horse["horse_number"]),
            }
        )

    result_rows = []
    for horse in horse_rows:
        if horse["horse_number"] not in result_lookup:
            continue
        finish_position = horse["finish_position"]
        numeric_finish = comparable_finish_position(finish_position)
        comparison_text, comparison_class = rank_comparison(
            horse["prediction_rank"],
            finish_position,
        )
        result_rows.append(
            {
                "horse_number": horse["horse_number"],
                "horse_name": horse["horse_name"],
                "prediction_rank": horse["prediction_rank"],
                "win_probability": (horse["prediction"] or {}).get("win_probability"),
                "finish_position": finish_position,
                "finish_position_label": f"{numeric_finish}着" if numeric_finish is not None else (finish_position or "-"),
                "comparison_text": comparison_text,
                "comparison_class": comparison_class,
                "row_class": result_highlight_class(horse["prediction_rank"], finish_position),
                "payout_per_100": horse["payout_per_100"],
            }
        )

    value_simulation = simulation.get("value") or {}
    dutching_simulation = simulation.get("dutching") or {}
    value_pre = value_simulation.get("pre")
    dutching_pre = dutching_simulation.get("pre")
    expected_value_rows = build_expected_value_rows(payload, horse_rows, value_pre) if value_pre else []
    value_no_purchase_reason = (
        value_no_purchase_message(
            expected_value_rows,
            int(value_pre["stake_unit"]),
            float(value_pre["settings"]["kelly_fraction"]),
        )
        if value_pre and not value_pre.get("selections")
        else ""
    )
    custom_simulation_horses = [
        {
            "horse_number": horse["horse_number"],
            "win_probability": float(horse["prediction"]["win_probability"]),
            "win_odds": float(horse["win_odds"]),
        }
        for horse in horse_rows
        if horse.get("prediction") and horse.get("win_odds") is not None
    ]
    custom_simulation_data = {
        "stake_unit": int((value_pre or dutching_pre or {}).get("stake_unit") or 100),
        "horses": custom_simulation_horses,
        "display": {
            "rejection_reason_labels": REJECTION_REASON_LABELS,
            "unknown_rejection_reason_label": UNKNOWN_REJECTION_REASON_LABEL,
        },
    }

    status = "result_published" if result and evaluation else "prediction_only"
    return {
        "race": race,
        "prediction": prediction,
        "simulation_value_pre": value_pre,
        "simulation_value_post": value_simulation.get("post"),
        "simulation_dutching_pre": dutching_pre,
        "simulation_dutching_post": dutching_simulation.get("post"),
        "result": result,
        "evaluation": evaluation,
        "horse_rows": horse_rows,
        "result_rows": result_rows,
        "expected_value_rows": expected_value_rows,
        "value_no_purchase_reason": value_no_purchase_reason,
        "status": status,
        "status_label": status_label(status),
        "status_class": status_class(status),
        "status_colors": STATUS_COLORS,
        "site_background": SITE_BACKGROUND,
        "tooltips": TOOLTIPS,
        "rejection_reason_text": rejection_reason_text,
        "odds_captured_at_label": format_jst_datetime(race.get("odds_captured_at")),
        "odds_timing_label": odds_timing_label,
        "odds_recorded_after_start": odds_recorded_after_start,
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
        payload = load_race_json(path)
        if not payload or not payload.get("prediction"):
            continue
        context = build_race_context(payload)
        race = payload["race"]
        html = race_template.render(**context)
        relative_path = race_html_path(race["date"], race["track"], race["race_number"])
        target_path = output_dir / relative_path
        ensure_dir(target_path.parent)
        target_path.write_text(html, encoding="utf-8")
        index_rows.append(
            {
                "date": race["date"],
                "track": race["track"],
                "race_name": race["race_name"],
                "status_label": context["status_label"],
                "status_class": context["status_class"],
                "href": relative_path.as_posix(),
            }
        )

    index_rows.sort(key=lambda item: (item["date"], item["track"]))
    index_html = index_template.render(
        races=index_rows,
        site_background=SITE_BACKGROUND,
        status_colors=STATUS_COLORS,
    )
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")
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
