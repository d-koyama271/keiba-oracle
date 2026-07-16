# keiba-oracle

中央競馬の各開催場 11R を対象に、`netkeiba` から必要情報を取得し、LLM で各馬の 1 着確率を予想し、購入シミュレーションを行い、静的 HTML を生成する最小構成のファイルベース実装です。

実装方針は次の通りです。

- 1 レース 1 JSON
- `prediction` と `simulation` を分離
- LLM は `predict.py` と `feedback.py` だけで利用
- 記事本文はテンプレート埋め込み
- 出力サイトは静的 HTML

## ディレクトリ構成

```text
config/
  app.yaml
  prompt_prediction.txt
  prompt_feedback.txt
src/
  run_pre.py
  run_post.py
  run_pre_collect.py
  run_post_collect.py
  collect.py
  predict.py
  simulate.py
  feedback.py
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
  feedback/
outbox/
  chat_input/
    prediction/
    feedback/
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
- `odds_reference_minutes_before_start`: 引数なしのレース前収集で使う取得目標分数
- `race_budget`: 1 レースの投資上限
- `ev_threshold`: 購入候補の EV 閾値
- `kelly_fraction`: fractional Kelly の倍率
- `stake_unit`: 購入金額の単位
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
    "pre": null,
    "post": null
  },
  "result": null,
  "feedback": null
}
```

## ジョブの流れ

`run_pre.py`

1. `collect.py` で当日の 11R 情報を取得
2. `predict.py` で各馬の 1 着確率を生成
3. `simulate.py` で `simulation.pre` を生成
4. `render.py` で静的 HTML を生成
5. `publish.py` で `public/` を更新

`run_post.py`

1. `collect.py` で結果と払戻を取得
2. `simulate.py` で `simulation.post` を確定
3. `feedback.py` で補正要約を生成
4. `render.py` で同じページを更新
5. `publish.py` で `public/` を更新

## 購入シミュレーション

`simulation.pre` / `simulation.post` は、`config/app.yaml` の共通設定を使う正式な基準シミュレーションです。候補ごとの購入額は `race_budget * fractional Kelly` を基準とし、合計が予算を超える場合だけ比例縮小したうえで `stake_unit` 単位に切り捨てます。余った予算を使い切るための追加配分は行いません。

レースページの「購入シミュレーション」では、閲覧者が予算・最低EV・Kelly係数を変えてブラウザ内で再計算できます。初期値はそのレースの `simulation.pre` を使用します。入力値と計算結果はrace JSON、`simulation.post`、feedback、localStorage、Cookieへ保存されず、正式な基準結果にも影響しません。

## Manual モード

manual モードでは LLM API は呼ばず、チャットへ貼る入力 JSON を `outbox/` に出し、返却 JSON を `inbox/` へ置いて downstream を進めます。

レース前:

```bash
python src/run_pre_collect.py
```

引数なしでは次回対象レースを選び、発走時刻から `odds_reference_minutes_before_start` を引いた取得目標時刻を判定します。目標時刻より前なら収集や chat input 出力を行わず、再実行時刻を表示して終了します。

過去レース検証・再収集では日付を明示します。取得できるのはnetkeibaが返す単一スナップショットであり、発走後の時刻でもフロー検証に使用しますが、厳密なT-60履歴オッズではありません。

```bash
python src/run_pre_collect.py --date 2026-04-12
python src/watcher.py
```

1. `run_pre_collect.py` が `data/races/...json` を更新します。
2. prediction 用の chat input JSON を `outbox/chat_input/prediction/` に出力します。
3. 外部チャットから返ってきた prediction JSON を `inbox/prediction/` に置きます。
4. `watcher.py` が `prediction` を反映し、`simulation.pre -> render -> publish` を実行します。

レース後:

```bash
python src/run_post_collect.py --date 2026-04-12
python src/watcher.py
```

1. `run_post_collect.py` が `result` を反映し、既存の決定的な計算で `simulation.post` を確定します。
2. `simulation.post` を含む feedback 用 chat input JSON を `outbox/chat_input/feedback/` に出力します。
3. 外部チャットから返ってきた feedback JSON を `inbox/feedback/` に置きます。
4. `watcher.py` が `feedback` を反映し、同じ計算で `simulation.post` を再確認して `render -> publish` を実行します。

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

feedback:

```json
{
  "meta": {
    "race_id": "202606030611"
  },
  "feedback": {
    "probability_error_summary": "短い要約",
    "ranking_error_summary": "短い要約",
    "profit_summary": "短い要約",
    "calibration_notes": "短い要約",
    "next_prediction_adjustment_summary": "短い要約"
  }
}
```

## 補足

- `collect.py` は `netkeiba` の HTML 構造に依存します。取得に失敗したレースはスキップし、ログへ出します。
- `predict.py` の LLM 応答が不正 JSON の場合は再試行します。
- `prediction` がない場合は `simulation.pre` を作りません。
- `result` がない場合は `simulation.post` を作りません。
- `render.py` はいったんステージング領域へ出力し、`publish.py` が成功したときだけ `public/` を差し替えます。

## GitHub Pages

この実装では `public/` を静的サイト出力先にしています。GitHub Actions の `Deploy Pages` workflow が `public/` を Pages artifact としてアップロードし、GitHub Pages へ配布します。Actions 側ではビルド処理を行いません。
