# keiba-oracle

中央競馬の各開催場 11R を対象に、`netkeiba` から必要情報を取得し、LLM で各馬の 1 着確率を予想し、購入シミュレーションを行い、静的 HTML を生成する最小構成のファイルベース実装です。

実装方針は次の通りです。

- 1 レース 1 JSON
- `prediction` と `simulation` を分離
- LLM は `predict.py` の予想だけで利用
- 記事本文はテンプレート埋め込み
- 出力サイトは静的 HTML

## ディレクトリ構成

```text
config/
  app.yaml
  prompt_prediction.txt
src/
  run_pre.py
  run_post.py
  run_pre_collect.py
  run_post_collect.py
  collect.py
  predict.py
  simulate.py
  evaluation.py
  render.py
  publish.py
  response_importer.py
  watcher.py
  llm_client.py
  utils.py
data/
  races/
inbox/
  prediction/
outbox/
  chat_input/
    prediction/
templates/
  race.html.j2
  index.html.j2
public/
  races/
requirements.txt
README.md
```

## セットアップ

1. Python 3.11 以上を用意します。
2. 依存関係を入れます。

```bash
pip install -r requirements.txt
```

3. 既定の `config/app.yaml` は `llm_provider: manual` なので、まずは API キーなしで manual モード運用できます。

4. 将来 OpenAI を使う場合は `config/app.yaml` の `llm_provider` を `openai` に変えたうえで、環境変数を設定します。

```bash
set OPENAI_API_KEY=your_api_key
```

PowerShell の場合は次です。

```powershell
$env:OPENAI_API_KEY="your_api_key"
```

5. 必要なら `config/app.yaml` を調整します。

主な設定値:

- `target_races`: 収集対象の開催場名
- `odds_reference_minutes_before_start`: 通常運用における推奨取得目標分数
- `simulation.budget`: 両方式共通の 1 レース予算上限
- `simulation.stake_unit`: 両方式共通の購入金額単位
- `simulation.value.ev_threshold`: 期待値重視方式の最低 EV（既定値 1.0）
- `simulation.value.kelly_fraction`: 期待値重視方式の fractional Kelly 係数
- `simulation.dutching.*`: ダッチング方式の最大頭数、最低カバー確率、最低グループ期待値、的中時利益条件
- `publish_mode`: `github_pages` を想定
- `llm_provider`: 既定は `manual`、実接続時は `openai`
- `llm_model`: 使用モデル名
- `data_dir`: レース JSON 保存先
- `public_dir`: 公開物の出力先

## 実行

レース前ジョブ:

```bash
python src/run_pre.py --date 2026-04-14
```

レース後ジョブ:

```bash
python src/run_post.py --date 2026-04-14
```

`run_pre.py` / `run_post.py` では、`--date` を省略すると当日の日付を使います。

## 生成物

- レース JSON: `data/races/YYYY-MM-DD/track_11r.json`
- レースページ: `public/races/YYYY-MM-DD/track_11r.html`
- 一覧ページ: `public/index.html`

各レース JSON のトップレベルは固定です。

```json
{
  "meta": {},
  "race": {},
  "horses": [],
  "prediction": null,
  "simulation": {
    "value": {
      "pre": null,
      "post": null
    },
    "dutching": {
      "pre": null,
      "post": null
    }
  },
  "result": null,
  "evaluation": null
}
```

`schema_version` は `4` です。`race` には取得時点の `weather` と正規化した `class_grade` を保存します。各馬の `past_runs` は対象レース自身を除外した直近5走で、走破タイム、ペース、馬体重、当時の人気・オッズなどの詳細を含みます。

馬成績のAJAXレスポンスに含まれる全JRA履歴はJSONへ保存せず、競馬場・surface・距離±200m・馬場・天候・クラス・騎手別の `career_summaries` に集計します。季節・枠番・馬番別集計や全履歴配列は生成しません。

## ジョブの流れ

`run_pre.py`

1. `collect.py` で当日の 11R 情報を取得
2. `predict.py` で各馬の 1 着確率を生成
3. `simulate.py` で `simulation.value.pre` と `simulation.dutching.pre` を生成
4. `render.py` で静的 HTML を生成
5. `publish.py` で `public/` を更新

`run_post.py`

1. `collect.py` で結果と払戻を取得
2. `simulate.py` で両方式の `post` を確定
3. `evaluation.py` で予測評価指標を生成
4. `render.py` で同じページを更新
5. `publish.py` で `public/` を更新

## 購入シミュレーション

正式な購入シミュレーションは次の2方式です。両方式のレース前想定を `simulation.*.pre`、結果確定後の収支を `simulation.*.post` に保存します。レース結果を取得しても `pre` は変更しません。

- `value`: 予測勝率と単勝オッズから EV と fractional Kelly を計算します。理論購入額が予算を超える場合だけ比例縮小し、余った予算の強制配分は行いません。
- `dutching`: 予測勝率上位を1頭から設定上限まで評価し、逆オッズ配分を購入単位へ丸めます。カバー確率、グループ期待値、的中時最低利益を満たす候補からグループ期待値が最大の頭数を採用します。

レースページのカスタムシミュレーターでは、両方式の条件をブラウザ内で変更できます。ダッチングは自動選択に加え、確認用の固定頭数も選べます。入力値と計算結果はrace JSON、正式な収支、localStorage、Cookieへ保存されません。HTMLへ埋め込む計算データは馬番、予測勝率、単勝オッズ、購入単位だけです。

## 予測評価

結果取得後、各race JSONの `evaluation` に次を保存します。

- 勝ち馬の予測確率と予測順位。順位は勝率降順、同率は馬番昇順です。
- `log_loss`: `-log(max(勝ち馬確率, 1e-12))`
- `brier_score`: 全出走馬の二乗誤差の平均
- `top1_hit` / `top3_hit` / `top5_hit`
- 単勝オッズの逆数を全馬で正規化した市場ベースライン。差分はモデル指標から市場指標を引きます。
- `simulation.value.post` と `simulation.dutching.post` の収支要約

有効な単勝オッズが全馬分そろわない場合、`market_baseline.available` は `false` です。発走後に記録されたオッズを使用した比較には `odds_recorded_after_start: true` と注記を保存します。購入なしの評価用ROIは `null` です。

状態はレース前入力生成後が `pre_status: awaiting_prediction`、予想公開後が `pre_status: published` です。`post_status` は結果待ちの `awaiting_result` から、結果・両post・evaluation・HTML公開完了後に `published` となります。

## Manual モード

manual モードでは LLM API は呼ばず、チャットへ貼る入力 JSON を `outbox/` に出し、返却 JSON を `inbox/` へ置いて downstream を進めます。

レース前:

```bash
python src/run_pre_collect.py
```

引数なしでは探索期間内で最も近い重賞開催日を選び、その日の重賞11Rをすべて収集します。期間内に重賞11Rがない場合だけ、最も近い開催日のうち発走が最も遅い11Rを1件選びます。各レースについて `odds_reference_minutes_before_start` に基づく推奨取得目標時刻を表示し、手動実行時は目標時刻より前でも警告を表示して収集と chat input 出力を続行します。

過去レース検証・再収集では日付を明示します。取得できるのはnetkeibaが返す単一スナップショットであり、発走後の時刻でもフロー検証に使用しますが、厳密なT-60履歴オッズではありません。

```bash
python src/run_pre_collect.py --date 2026-04-12
python src/watcher.py
```

1. `run_pre_collect.py` が `data/races/...json` を更新します。
2. prediction 用の chat input JSON を `outbox/chat_input/prediction/` に出力します。
3. 外部チャットから返ってきた prediction JSON を `inbox/prediction/` に置きます。
4. `watcher.py` が `prediction` を反映し、両方式の `pre -> render -> publish` を実行します。

レース後:

```bash
python src/run_post_collect.py --date 2026-04-12
```

1. `run_post_collect.py` が `result` を反映し、既存の決定的な計算で両方式の `post` を確定します。
2. `evaluation` を決定的に生成します。
3. 結果HTMLを生成し、`public/` を更新します。レース後のLLM処理や追加の `watcher.py` 実行は不要です。

inbox へ置く response JSON の想定:

prediction:

```json
{
  "meta": {
    "race_id": "202606030611"
  },
  "prediction": {
    "horses": [
      {
        "horse_number": 1,
        "win_probability": 0.12,
        "reason": "短い理由"
      }
    ],
    "optional_summary": "短い総括"
  }
}
```

## 補足

- `collect.py` は `netkeiba` の HTML 構造に依存します。取得に失敗したレースはスキップし、ログへ出します。
- `predict.py` の LLM 応答が不正 JSON の場合は再試行します。
- `prediction` がない場合は両方式の `pre` を作りません。
- `result` がない場合は両方式の `post` を作りません。
- `prediction`、`result`、両方式の `post` がそろわない場合は `evaluation` を作りません。
- `render.py` はいったんステージング領域へ出力し、`publish.py` が成功したときだけ `public/` を差し替えます。

## テスト

固定データだけを使用し、netkeibaやLLM APIへ接続しません。

```bash
python -m unittest discover -s tests -v
```

## GitHub Pages

この実装では `public/` を静的サイト出力先にしています。GitHub Actions の `Deploy Pages` workflow が `public/` を Pages artifact としてアップロードし、GitHub Pages へ配布します。Actions 側ではビルド処理を行いません。
