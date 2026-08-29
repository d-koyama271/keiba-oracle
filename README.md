# keiba-oracle

中央競馬の重賞を対象に、`netkeiba` から必要情報を取得し、Codex で各馬の 1 着確率を予想し、購入シミュレーションを行い、静的 HTML を生成する最小構成のファイルベース実装です。次の開催期間に重賞がない場合だけ、各開催場の 11R を対象にします。

実装方針は次の通りです。

- 1 レース 1 JSON
- `prediction` と `simulation` を分離
- Codex は `predict.py` の予想だけで利用
- 記事本文はテンプレート埋め込み
- 出力サイトは静的 HTML

## ディレクトリ構成

```text
config/
  app.yaml
  prompt_prediction.txt
  prompt_prediction_statistical.txt
src/
  run_pre.py
  run_post.py
  run_pre_collect.py
  run_post_collect.py
  collect.py
  predict.py
  simulate.py
  evaluation.py
  evaluation_summary.py
  render.py
  publish.py
  response_importer.py
  watcher.py
  llm_client.py
  utils.py
data/
  races/
  evaluation_summary.json
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

3. Codex CLI を用意し、`codex` コマンドへログインします。

```bash
codex login status
```

4. 必要なら `config/app.yaml` を調整します。既定値は `llm_provider: codex` です。外部 AI API キーは使用しません。

主な設定値:

- `target_races`: 収集対象の開催場名
- `odds_reference_minutes_before_start`: 通常運用における推奨取得目標分数
- `simulation.budget`: 両方式共通の 1 レース予算上限
- `simulation.stake_unit`: 両方式共通の購入金額単位
- `simulation.value.ev_threshold`: 期待値重視方式の最低 EV（既定値 1.0）
- `simulation.value.kelly_fraction`: 期待値重視方式の fractional Kelly 係数
- `simulation.dutching.*`: ダッチング方式の最大頭数、最低カバー確率、最低グループ期待値、最低利益率（既定値20%、合計購入額基準）、的中時利益条件
- `publish_mode`: `github_pages` を想定
- `llm_provider`: 通常運用では `codex`
- `llm_model`: Codex で使用するモデル名
- `data_dir`: レース JSON 保存先
- `public_dir`: 公開物の出力先

## 実行

レース前ジョブ:

```bash
python src/run_pre.py
```

レース後ジョブ:

```bash
python src/run_post.py --date 2026-04-14
```

`run_pre.py` は日付を省略すると、次の連続する中央競馬開催期間を探索し、その期間の重賞をすべて対象にします。重賞が1件もない場合だけ、各開催場の11Rを対象にします。`run_post.py` は日付を省略すると当日を対象にします。過去レースを明示して検証する場合は、どちらも `--date YYYY-MM-DD` を使用できます。

## 生成物

- レース JSON: `data/races/YYYY-MM-DD/track_Nr.json`
- 予想ページ: `public/races/YYYY-MM-DD/track_Nr.html`
- 結果ページ: `public/races/YYYY-MM-DD/track_Nr_result.html`（結果公開後のみ）
- 一覧ページ: `public/index.html`
- 全体評価集計: `data/evaluation_summary.json`

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
    },
    "variants": []
  },
  "result": null,
  "evaluation": null
}
```

`schema_version` は `8` です。既存の `prediction` 本体は総合AI予想（内部識別子 `traditional`）のまま維持し、追加方式は `prediction.variants` に保存します。統計重視予想は `method: statistical` と `model_provider` / `model_name` で識別します。既存predictionに `method` がない場合は総合AI予想として扱い、過去JSONへのバックフィルは行いません。追加方式の購入シミュレーションは `simulation.variants`、結果評価は `evaluation.variants` へ同じ識別情報とともに保存します。結果取得時に全出走馬の確定単勝オッズが揃った場合のみ、予想時点の `horses[].win_odds` を変更せず `result.final_win_odds` へ保存します。

`race` には取得時点の `weather` と正規化した `class_grade` を保存します。各馬の `past_runs` は対象レース自身を除外した直近5走で、走破タイム、ペース、馬体重、当時の人気・オッズなどの詳細を含みます。

単勝オッズはnetkeibaを優先し、全出走馬分を検証できない場合だけJRA公式へ切り替えます。同一レース内で取得元は混在させず、`race.odds_source` と採用元の `race.odds_source_url` を保存します。`race.odds_captured_at` は全馬の単勝オッズと人気の検証に成功した時刻で、両取得元とも失敗した場合は3項目とも `null` です。

馬成績のAJAXレスポンスに含まれる全JRA履歴はJSONへ保存せず、競馬場・surface・距離±200m・馬場・天候・クラス・騎手別の `career_summaries` に集計します。季節・枠番・馬番別集計や全履歴配列は生成しません。

## ジョブの流れ

`run_pre.py`

1. `collect.py` で対象レース情報を取得
2. 予想開始時点の `meta` / `race` / `horses` を確定し、総合AI予想入力と、市場情報を除いた統計重視予想入力を独立して作成
3. `predict.py` から Codex を実行し、総合AI予想と統計重視予想の各馬の 1 着確率・理由・総括を検証して保存
4. 両AI予想を個別に入力として、`simulate.py` で期待値重視方式と上位予測ダッチング方式のpreを生成
5. `render.py` で予想ページと index を生成
6. `publish.py` で `public/` を更新

Codex は一時作業ディレクトリ内の読み取り専用・構造化出力モードで実行され、プロンプトに埋め込んだ確定済み予想入力 JSON だけを予想材料にします。Web、リポジトリ内ファイル、公開済み HTML、結果、過去の別予想、評価データは参照させません。

正常に保存した新規予想には `model_provider`、`model_name`、`predicted_at` に加え、実際に使用したプロンプトと安定化した予想入力 JSON の `prompt_sha256`、`prediction_input_sha256` を記録します。過去予想へは補完しません。通常フローを再実行しても、有効な既存予想は再生成・上書きせず、そのままシミュレーション以降へ渡します。

統計重視予想は `config/prompt_prediction_statistical.txt` を使用します。今回・過去走のオッズ、人気、オッズ取得元・時刻・URL、市場由来の順位・確率を再帰的に除外し、レース条件、過去成績、走破タイム、着差、通過順、上がり、馬体重、`career_summaries` などの客観データだけを渡します。`prediction`、`simulation`、`result`、`evaluation` は入力に含めません。結果取得済みまたは発走済みのレースへ統計予想を後付けしません。

`run_post.py`

1. `collect.py` で結果と払戻を取得
2. `simulate.py` で保存済みの各AI予想・両購入方式の `post` を確定
3. `evaluation.py` で総合AI予想と統計重視予想へ同じ予測評価指標を生成
4. `evaluation_summary.py` で総合AI予想の集計、方式別集計、同一レース比較を更新
5. `render.py` で予想ページを維持し、結果ページと index を生成・更新
6. `publish.py` で `public/` を更新

## 購入シミュレーション

正式な購入シミュレーションは次の2方式です。総合AI予想のレース前想定と収支は従来どおり `simulation.*.pre/post`、統計重視予想分は `simulation.variants` に保存します。レース結果を取得しても各 `pre` は変更しません。

- `value`: 予測勝率と単勝オッズから EV と fractional Kelly を計算します。理論購入額が予算を超える場合だけ比例縮小し、余った予算の強制配分は行いません。
- `dutching`: 予測勝率上位を1頭から設定上限まで評価し、逆オッズ配分を購入単位へ丸めます。カバー確率、グループ期待値、的中時最低利益を満たす候補からグループ期待値が最大の頭数を採用します。

予想ページではAI予想と正式シミュレーションを総合AI予想／統計重視予想のタブで切り替えます。カスタムシミュレーターも選択中タブの予測確率を使い、両購入方式の条件をブラウザ内で変更できます。ダッチングは自動選択に加え、確認用の固定頭数も選べます。入力値と計算結果はrace JSON、正式な収支、localStorage、Cookieへ保存されません。HTMLへ埋め込む計算データは馬番、予測勝率、単勝オッズ、購入単位だけです。

## 予測評価

結果取得後、各race JSONの `evaluation` に次を保存します。

- 勝ち馬の予測確率と予測順位。順位は勝率降順、同率は馬番昇順です。
- `log_loss`: `-log(max(勝ち馬確率, 1e-12))`
- `brier_score`: 全出走馬の二乗誤差の平均
- `top1_hit` / `top3_hit` / `top5_hit`
- 単勝オッズの逆数を全馬で正規化した市場ベースライン。差分はモデル指標から市場指標を引きます。
- `simulation.value.post` と `simulation.dutching.post` の収支要約

トップレベルの `evaluation` は総合AI予想の評価です。統計重視予想には同じ勝ち馬確率・順位、Top1/3/5、Log Loss、Brier Score、市場ベースライン比較を計算して `evaluation.variants` へ保存します。統計重視予想の評価にはシミュレーション収支を混在させません。

有効な単勝オッズが全馬分そろわない場合、`market_baseline.available` は `false` です。発走後に記録されたオッズを使用した比較には `odds_recorded_after_start: true` と注記を保存します。購入なしの評価用ROIは `null` です。

`data/evaluation_summary.json` は正常な `evaluation` があるrace JSONだけから再生成する派生データです。既存のトップレベル集計は総合AI予想の意味を維持します。`methods.traditional` と `methods.statistical` に方式別のTop1・Top3・Top5成績、Log Loss・Brier Score、確率校正、条件別精度を保存します。`paired_comparison` は両方式の評価がそろう同一レースだけを母数とし、Log Loss・Brier Score差は `statistical - traditional`（負なら統計重視予想が優位）です。市場比較と両購入シミュレーションの累積成績は総合AI予想のままです。race JSONへは書き戻さず、発走後オッズのレースは正式な市場比較から除外します。任意に再集計する場合は次を実行します。

```bash
python src/evaluation_summary.py
```

トップページの「総合AI予想の予測成績」はこの集計ファイルを読み込みます。ファイルがない場合は未算出として `-` を表示し、予想入力にはこの集計を含めません。

状態はレース前入力生成後が `pre_status: awaiting_prediction`、予想公開後が `pre_status: published` です。`post_status` は結果待ちの `awaiting_result` から、結果・保存済み全simulationのpost・evaluation・結果HTML公開完了後に `published` となります。

## Codex 予想フロー

通常のレース前運用は `run_pre.py` だけで完了します。収集時に確定した予想入力 JSON は監査用に `outbox/chat_input/prediction/` にも保存しますが、人が外部チャットへ貼り付けたり、応答を `inbox/` へ戻したりする必要はありません。

新規公開では総合AI予想と統計重視予想の両方が正常に保存されてからシミュレーションへ進みます。総合AI予想だけが既にある場合はそれを再利用し、欠けている統計重視予想だけを生成します。統計重視予想に失敗した場合は総合AI予想を残したまま停止し、不完全なページを公開しません。

引数なしでは、次の連続する中央競馬開催日を1開催期間として探索します。その期間の重賞（G1・G2・G3）をレース番号に関係なくすべて収集し、重賞が1件もない場合だけ各開催場の11Rをすべて収集します。各レースについて `odds_reference_minutes_before_start` に基づく推奨取得目標時刻を表示し、目標時刻より前でも警告だけを表示して処理を続行します。

過去レース検証・再収集では日付を明示します。取得できるのはnetkeibaが返す単一スナップショットであり、発走後の時刻でもフロー検証に使用しますが、厳密なT-60履歴オッズではありません。

```bash
python src/run_pre.py --date 2026-04-12
```

`run_pre_collect.py`、`response_importer.py`、`watcher.py`、`inbox/prediction/` は、過去の手動応答を扱う後方互換用として残しています。通常のCodex予想公開では使用しません。手動応答の取込時も、既に有効な予想があるレースは上書きしません。

レース後:

```bash
python src/run_post_collect.py --date 2026-04-12
```

1. `run_post_collect.py` が予想済みrace JSONの `meta.race_id` から結果だけを取得して `result` を反映し、既存の決定的な計算で両方式の `post` を確定します。
2. `evaluation` を決定的に生成します。
3. 既存予想ページを維持したまま結果HTML（`*_result.html`）を生成し、`public/` と index の結果リンクを更新します。レース後のAI予想処理や追加の `watcher.py` 実行は不要です。

後方互換用の inbox response JSON の想定:

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
- `predict.py` の Codex 応答が不正 JSON の場合は再試行します。
- `prediction` がない場合は両方式の `pre` を作りません。
- `result` がない場合は両方式の `post` を作りません。
- `prediction`、`result`、両方式の `post` がそろわない場合は `evaluation` を作りません。
- `render.py` はいったんステージング領域へ出力し、`publish.py` が成功したときだけ `public/` を差し替えます。

## テスト

固定データとモックしたCodex CLI応答だけを使用し、netkeibaやCodexサービスへ接続しません。

```bash
python -m unittest discover -s tests -v
```

## GitHub Pages

この実装では `public/` を静的サイト出力先にしています。GitHub Actions の `Deploy Pages` workflow が `public/` を Pages artifact としてアップロードし、GitHub Pages へ配布します。Actions 側ではビルド処理を行いません。
