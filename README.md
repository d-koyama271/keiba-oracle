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
  quinella.py
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
  prediction_inputs/YYYY-MM-DD/
  evaluation_summary.json
inbox/
  prediction/
outbox/
  chat_input/
    prediction/
templates/
  race.html.j2
  index.html.j2
  quinella.html.j2
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
- `simulation.value.kelly_fraction`: 期待値重視方式の fractional Kelly 係数（既定値 0.75）
- `simulation.dutching.*`: 単勝分配方式（内部キー `dutching`）の最大頭数、最低カバー確率、最低グループ期待値、最低利益率（既定値20%、合計購入額基準）
- `simulation.quinella.*`: 馬連専用の確率近似・購入条件（下記参照）。単勝設定とは独立し、旧設定にこの項目がなくても単勝は動作します。
- `publish_mode`: `github_pages` を想定
- `llm_provider`: 通常運用では `codex`
- `llm_model`: Codex で使用するモデル名
- `llm_reasoning_effort`: Codex CLI の `model_reasoning_effort` に渡す設定値
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

自動運用の処理途中・失敗・再試行状態は `src/automation_state.py` で扱います。保存先は `data_dir/automation/YYYY-MM-DD/<race JSONと同じstem>.json` です。schedulerはphase開始前に `in_progress` を保存し、フロー正常終了と成果物の確認後だけ解除します。開始記録ではattemptsを増やさず、失敗記録ごとに加算します。`retry_wait` は `next_retry_at` 必須、`blocked`／`in_progress` はnullです。不正なstateはエラーとし、clearは指定phaseだけを削除します。`data/automation/` はGit管理対象外です。

各レース JSON のトップレベルは固定です。

```json
{
  "meta": {},
  "race": {},
  "horses": [],
  "prediction": [],
  "simulation": [],
  "result": null,
  "evaluation": []
}
```

`meta.schema_version` は `10` です。`prediction[]` の各要素はレース内参照ID（現在は `p1`）とAI情報、独立した `general`（総合AI予想）／`statistical`（統計重視予想）を持ちます。片方のみの生成・表示・シミュレーション・評価も可能で、生成順には依存しません。AI情報は親に一度だけ保存し、`provider` は `OpenAI`、`family` は `GPT`、`model`／`runtime_provider`／`reasoning_effort` はそれぞれ設定の `llm_model`／`llm_provider`／`llm_reasoning_effort` に対応します。AI名やモデル名をJSONキーや参照IDには使用しません。

`simulation[]` と `evaluation[]` は `prediction_id` で予想へ対応付けます。simulationは `general`／`statistical` → `win`／`quinella` → `value`／`dutching` → `pre`／`post` の階層、evaluationは `general`／`statistical` 配下に従来の評価を保持します。

予想IDはAI実行設定のスナップショットを識別します。`provider`／`family`／`model`／`runtime_provider`／`reasoning_effort` がすべて一致するentryを再利用し、対象方式が未保存ならそこへ追加します。一致するentryがない場合のみ新しいIDを作成します。予想日時やプロンプト・入力のハッシュはIDの同一性判定に使用しません。

schema v9以前は読み込み時に、本体を `general`、`variants` 内の統計重視予想を `statistical` へ正規化します。予想・simulation・evaluationを同じIDへ対応付け、保存済み確率・オッズ・計算結果・評価値は維持します。旧データに記録されていないAI情報は現在設定で補完しません。読み込みだけでは元ファイルを変更せず、通常処理で保存する場合にv10形式になります。一括移行・バックフィルは行いません。

結果取得時に全出走馬の確定単勝オッズが揃った場合のみ、予想時点の `horses[].win_odds` を変更せず `result.final_win_odds` へ保存します。馬連の追加キーがない旧JSONも読み込み可能です。

`race` には取得時点の `weather` と正規化した `class_grade` を保存します。各馬の `past_runs` は対象レース自身を除外した直近5走で、走破タイム、ペース、馬体重、当時の人気・オッズなどの詳細を含みます。

単勝オッズはnetkeibaを優先し、全出走馬分を検証できない場合だけJRA公式へ切り替えます。同一レース内で取得元は混在させず、`race.odds_source` と採用元の `race.odds_source_url` を保存します。`race.odds_captured_at` は全馬の単勝オッズと人気の検証に成功した時刻で、両取得元とも失敗した場合は3項目とも `null` です。

馬成績のAJAXレスポンスに含まれる全JRA履歴はJSONへ保存せず、競馬場・surface・距離±200m・馬場・天候・クラス・騎手別の `career_summaries` に集計します。季節・枠番・馬番別集計や全履歴配列は生成しません。

## ジョブの流れ

`python src/scheduler.py` はJSTの今日・明日の開催データから、`target_races` 内の重賞と各フェーズの予定日時・現在の実行可否を表示します。曜日判定や11Rへのfallbackは行いません。予定は `automation.statistical_time`（前日の時刻）、`automation.general_minutes_before_start`（発走何分前）、`automation.result_minutes_after_start`（発走何分後）から毎回計算します。既存の `odds_reference_minutes_before_start` とは別設定です。この段階では予定を保存せず、予想・結果取得・retry・公開も実行しません。

通常探索したレースは3つの `PhaseTask`（日付・race_id・phase）へ展開し、過去retryは保存stateに残る対象phaseだけを追加します。`decide_phases()` は各taskのrace JSON、automation state、保存済みinputを参照し、`state`（scheduled／running／completed／retry_wait／blocked）、`scheduled_at`、`mode`（normal／resume）、`runnable`、実行制約の `reason` を返します。runningは保存上の `in_progress` に対応し、modeとは独立しています。完了済み・blocked・予定時刻前・retry待機中は対象外です。発走後のpre再開には有効な確定inputと現在の実行設定に対応する既存predictionが必要です。判定処理はrace JSONやautomation stateへ書き込みません。中止公開もresult taskとして同じ実行・成果物確認・state更新の経路を通ります。

`python src/scheduler.py --execute` を指定すると、判定に従ってレース単位で既存pre／postフローを実行し、ローカルの `public/` まで更新します。resultは少なくとも一方のpredictionがある場合だけ実行します。実行後のrace JSONで成果物を確認し、成功したphaseの失敗stateをclearします。失敗時は `automation.retry_interval_minutes`／`max_attempts`、resultでは `result_retry_interval_minutes`／`result_max_attempts` に従ってretry待機またはblockedを記録します。これらは正の整数です。発走時刻以降にinputがないpre phaseは実行せず即blockedとし、既にblockedのphaseは再記録しません。各phaseは1回の起動で最大1回実行し、自動待機ループは行いません。`--execute` なしでは表示のみで、stateや `public/` を変更しません。

`--execute` は `data_dir/automation/scheduler.lock` のOS管理の非ブロッキングlockで重賞検知から実処理全体を保護します。競合時は失敗stateを更新せず正常skipします。異常終了時もOSがlockを解放するため、残ったlockファイルの削除は不要です。確認表示のみの場合はlockを取得しません。

前日以前でも `retry_wait`／`in_progress` が残るレースはローカルrace JSONから候補へ追加し、その未完了phaseだけを再開します。blockedやstate解除済みのレースは追加せず、過去の探索・中止記事取得は行いません。発走後のpre再開には保存済み確定inputと現在の実行設定に対応する既存predictionが必要で、新たなpredictionは生成しません。

`--execute` の重賞探索結果は `data_dir/automation/discovery_cache.json` に保存します。キャッシュ未作成・破損・対象日不足・日付構成変更時に探索し、同じ日でも当日の `statistical_time` 以降にまだ探索していなければ1回再探索します。それ以外はキャッシュを再利用します。正常な既存キャッシュがある場合、探索失敗後の再試行は1時間空けます。phase判定・retry判定・deployは起動ごとに継続します。確認表示のみの場合は従来どおり探索し、キャッシュを書き込みません。

開催中止確認は当日の対象レースのうち未result・未cancelledだけを対象に最大1時間に1回行い、通信失敗時も同じ間隔を空けます。過去の中止記録は探索更新時だけ代替開催確認のために走査し、中止確定済み記事は再取得しません。

`run_pre.py`

`run_pre.py`、`run_post.py`、`run_post_collect.py` は `--race-id <race_id>` で対象を1レースに限定できます。preの各phaseとresumeでも指定でき、対象外のレースのinput・予想・結果・公開済みHTMLは更新しません。トップページと結果集計は対象レースの更新を反映します。省略時は従来の日付単位処理です。resume／postで対象日のrace JSONが見つからない場合はエラーになります。

失敗後の再開は `python src/run_pre.py --date YYYY-MM-DD --phase general --resume` または `--phase statistical --resume` を使用します。再収集せず、`data_dir/prediction_inputs/YYYY-MM-DD/<stem>.json`（general）／`<stem>.statistical.json`（statistical）を読み込みます。statistical inputもCodex実行前に保存します。race_id・日付・競馬場・レース番号・方式を照合し、不正なinputは再生成・上書きしません。後日の再収集でrace／horsesが変わっていても、予想には確定済みsnapshotを使用します。statisticalの市場情報除外と結果取得後の生成禁止は維持します。対象日の該当inputがない場合は失敗し、旧outboxや現在のrace JSONから補完しません。`--resume` には日付と単独フェーズの指定が必要です。通常のgeneral実行は従来どおり再収集・input確定を行います。

`--phase statistical` は収集後に統計重視予想のみ生成・公開し、総合用inputの確定とsimulationは行いません。`--phase general` は再収集時点の総合用inputを確定して総合予想のみ生成し、保存済みの両予想方式でpre simulation・公開を行います。`--phase all`（省略時）は以下の一括処理です。各フェーズで `--date YYYY-MM-DD` を指定できます。

1. `collect.py` で対象レース情報を取得
2. 予想開始時点の `meta` / `race` / `horses` を確定し、総合AI予想入力と、市場情報を除いた統計重視予想入力を独立して作成
3. `predict.py` から Codex を実行し、総合AI予想と統計重視予想の各馬の 1 着確率・理由・総括を検証して保存
4. 両AI予想を個別に入力として、`simulate.py` で単勝・馬連の期待値重視方式と分配方式のpreを生成（馬連は発走前の完全なデータがある場合のみ）
5. `render.py` で予想ページと index を生成
6. `publish.py` で `public/` を更新

各レースで片方の予想方式だけ成功した場合も、成功した方式のシミュレーション・表示・公開を継続します。両方式とも失敗したレースは処理対象から除外し、全レースが失敗した場合は公開せずエラーで終了します。

Codex は一時作業ディレクトリ内の読み取り専用・構造化出力モードで実行され、プロンプトに埋め込んだ確定済み予想入力 JSON だけを予想材料にします。Web、リポジトリ内ファイル、公開済み HTML、結果、過去の別予想、評価データは参照させません。

正常に保存した新規予想の `general`／`statistical` には `horses`、`optional_summary`、`predicted_at` に加え、実際に使用したプロンプトと安定化した予想入力 JSON の `prompt_sha256`、`prediction_input_sha256` を記録します。過去予想へは補完しません。通常フローの再実行では、現在のAI実行設定に一致するentryの有効な予想を再利用します。設定変更時も以前のentryは上書きしません。

統計重視予想は `config/prompt_prediction_statistical.txt` を使用します。今回・過去走のオッズ、人気、オッズ取得元・時刻・URL、市場由来の順位・確率を再帰的に除外し、レース条件、過去成績、走破タイム、着差、通過順、上がり、馬体重、`career_summaries` などの客観データだけを渡します。`prediction`、`simulation`、`result`、`evaluation` は入力に含めません。通常は発走後の生成を禁止しますが、inputを明示指定した復旧では、race JSONとの整合性検証後に生成可能です。結果取得済みの場合は引き続き拒否し、`predicted_at` は実際の生成時刻を記録します。

`run_post.py`

1. `collect.py` で結果と払戻を取得
2. `simulate.py` で保存済みの各AI予想・両購入方式の `post` を確定
3. `evaluation.py` で総合AI予想と統計重視予想へ同じ予測評価指標を生成
4. `evaluation_summary.py` で総合AI予想の集計、方式別集計、同一レース比較を更新
5. `render.py` で予想ページを維持し、結果ページと index を生成・更新
6. `publish.py` で `public/` を更新

## 購入シミュレーション

単勝の購入シミュレーションは次の2方式です。レース前想定と収支は `simulation[].general.win`／`simulation[].statistical.win` の各購入方式の `pre/post` に保存します。レース結果を取得しても各 `pre` は変更しません。

- `value`: 予測勝率と単勝オッズから EV と fractional Kelly を計算します。理論購入額が予算を超える場合だけ比例縮小し、余った予算の強制配分は行いません。
- `dutching`（画面表示: 単勝分配方式）: 予測勝率上位を1頭から設定上限まで評価し、逆オッズ配分を購入単位へ丸めます。カバー確率、グループ期待値、的中時最低利益を満たす候補からグループ期待値が最大の頭数を採用します。

予想ページではAI予想と正式シミュレーションを総合AI予想／統計重視予想のタブで切り替えます。購入シミュレーション・カスタム・購入結果には、その下に単勝／馬連の券種タブがあります。初期表示は総合AI予想（統計重視のみの場合は統計重視予想）・単勝です。AI予想表は券種にかかわらず各馬の1着確率を表示します。カスタムでは選択中のAI・券種の保存済み確率、オッズ、設定を使い、自動選択に加え確認用の固定頭数・固定組数を指定できます。入力値と計算結果はrace JSON、正式な収支、localStorage、Cookieへ保存されません。馬連の確率はPythonで算出した保存値を埋め込み、ブラウザで再推定しません。

### 馬連

総合AI／統計重視 × 単勝／馬連 × 分配／期待値の8通りは、各々が共通予算上限3,000円・購入単位100円の独立した仮想シミュレーションです。8通りへ予算を分割したり、1つの実運用収支へ合算したりしません。

`config/app.yaml` の `simulation.quinella` の条件は次の通りです。最適化済みの設定ではありません。

| 項目 | 値 |
| --- | ---: |
| `harville_lambda` | 0.81 |
| `value.ev_threshold` | 1.10 |
| `value.kelly_fraction` | 0.80 |
| `dutching.max_selection_count` | 10 |
| `dutching.min_coverage_probability` | 0.40 |
| `dutching.min_group_expected_value` | 0.75 |
| `dutching.min_profit_rate` | 0.20 |

既存netkeiba APIの `type=all` レスポンスから単勝 `odds["1"]` と馬連 `odds["4"]` を同時に取得します。組番は辞書キーではなく `row[3]`、オッズは `row[0]` を読みます。昇順整数ペアの完全な集合、重複、欠落、有限・有効な数値を検証し、`race.quinella_odds` に `pairs`、`fetched_at`、`source`、`source_url`、`official_datetime`、`api_status`、`api_reason`、`update_count`、`available`、`reason` を保存します。不完全なスナップショットを部分利用したり、取消馬を推定したりしません。APIの発走前状態と更新・取得時刻も確認し、結果時点のオッズはpreに使いません。馬連取得失敗時も単勝の検証とJRAフォールバックは継続します。馬連情報は両AIの予想入力から除外します。

全出走馬の1着確率から、同着なしの近似であるDiscounted Harvilleを使用します。

```text
P(i,j) = p_i * p_j^lambda / sum(k != i, p_k^lambda)
       + p_j * p_i^lambda / sum(k != j, p_k^lambda)
```

全ペア合計を許容誤差1e-6以内で検証し、単勝オッズによる対象除外や候補内の再正規化は行いません。馬連valueは既存のEV・Kelly計算と比例縮小・購入単位切り捨てを再利用します。馬連dutchingは確率上位1～設定上限組数を評価し、各組へ1単位を配分後、想定払戻が最小の組へ順に残りを配ります。確率同率や払戻同額は馬番ペアの数値昇順です。条件適合候補をグループEV最大、カバー確率最大、組数最小の順で選びます。最低利益率の基準は実際の合計購入額です。

保存先は `simulation[].general.quinella` と `simulation[].statistical.quinella` です。`probabilities`、`harville_lambda`、`odds_snapshot` を方式間で共有し、その配下に `value.pre/post` と `dutching.pre/post` を持ちます。preには予算・購入単位・設定・ペアごとの購入値・候補評価を保持します。`status: ready` の中でpreの `purchased` / `no_purchase` を区別し、計算不可は `status: unavailable` と理由を保存してpreを `null` にします。結果待ちは `post_status: awaiting_result`、払戻未確定は `awaiting_payouts`、確定後は `settled` です。保存済みpreは単勝・馬連とも再実行で上書きしません。新規馬連preは発走前・結果未取得時に限定し、過去へのバックフィルは行いません。

結果ページの馬連払戻DOMを組番と金額の対応を維持して読み、`result.payouts.quinella` に `horse_numbers` と `payout_per_100` を保存します。同着時は全払戻組を照合し、`result.quinella_settlement` に完全性・確定した取消／除外の馬番を保持します。中止・失格は返還しません。postは実払戻一覧だけで的中を判定し、当初購入額 `total_stake`、返還 `total_refund`、的中払戻＋返還 `total_return`、損益 `profit` を保存します。`roi` は損益／当初購入額（購入0円なら0）です。払戻や返還情報が不完全ならpostは `null` のままとし、外れとして確定せず、単勝結果の処理を継続します。保存済みの正常な馬連結果・postを取得失敗で消しません。全面中止など払戻一覧を確認できないケースも未確定として残します。

馬連の集計は保存済みの確定postだけから `evaluation_summary.simulation.quinella` と `methods.*.simulation.quinella` に生成します。対象・購入・的中レース数、購入額、返還額、回収額、損益、回収率を方式別に保持し、返還を的中へ数えません。正常な購入なしは対象数に含め、計算不可・未確定は除外します。`overall_roi` は回収／当初購入額（購入0円ならnull）で、既存の定義を維持します。indexでは各AIの単勝と馬連の方式別収支を分けて表示します。1着予想のevaluation指標は変更しません。

## 予測評価

結果取得後、各race JSONの `evaluation[].general`／`evaluation[].statistical` に次を保存します。

- 勝ち馬の予測確率と予測順位。順位は勝率降順、同率は馬番昇順です。
- `log_loss`: `-log(max(勝ち馬確率, 1e-12))`
- `brier_score`: 全出走馬の二乗誤差の平均
- `top1_hit` / `top3_hit` / `top5_hit`
- 単勝オッズの逆数を全馬で正規化した市場ベースライン。差分はモデル指標から市場指標を引きます。
- 総合AI予想の `win.value.post` と `win.dutching.post` の収支要約

総合AI予想と統計重視予想に同じ勝ち馬確率・順位、Top1/3/5、Log Loss、Brier Score、市場ベースライン比較を独立して計算し、対応する `prediction_id` の `general`／`statistical` へ保存します。統計重視予想の評価にはシミュレーション収支を混在させません。

有効な単勝オッズが全馬分そろわない場合、`market_baseline.available` は `false` です。発走後に記録されたオッズを使用した比較には `odds_recorded_after_start: true` と注記を保存します。購入なしの評価用ROIは `null` です。

`data/evaluation_summary.json` は正常な `evaluation` があるrace JSONだけから再生成する派生データです。既存のトップレベル集計と `simulation` は総合AI予想の意味を維持します。`methods.general` と `methods.statistical` に方式別のTop1・Top3・Top5成績、Log Loss・Brier Score、確率校正、条件別精度を保存し、各方式の `simulation` は保存済みpostだけを集計します。`paired_comparison` は両方式の評価がそろう同一レース・同一 `prediction_id` だけを母数とし、Log Loss・Brier Score差は `statistical - general`（負なら統計重視予想が優位）です。race JSONへは書き戻さず、発走後オッズのレースは正式な市場比較から除外します。任意に再集計する場合は次を実行します。

```bash
python src/evaluation_summary.py
```

トップページの「総合AI予想の予測成績」と「統計重視予想の予測成績」はこの集計ファイルを読み込みます。統計重視予想の累計収支は、保存済みのsimulation postだけを集計します。ファイルがない場合は未算出として `-` を表示し、予想入力にはこの集計を含めません。

result完了には、result・存在する全predictionのevaluation・利用可能なpre simulationに対応するpostが必要です。simulation自体がない場合はevaluationを確認します。`status: ready` の馬連simulationは全ての `quinella.post_status` が `settled` になるまで未完了です。

post処理では利用可能なpre simulationを精算し、simulationの有無とは独立して保存済みpredictionを評価・公開します。statistical predictionとresultだけでも評価・結果ページを生成します。

stateがある場合は成果物より `in_progress`／`retry_wait`／`blocked` を優先し、後続のsimulation・evaluation・render・publishを含むフローが正常終了した後だけstateを解除します。stateのない既存データは成果物で完了判定します。中止保存前にもresultの `in_progress` を記録し、中断・公開失敗後は中止記事を再取得せず公開を再試行します。

## Codex 予想フロー

通常のレース前運用は `run_pre.py` だけで完了します。確定した予想入力 JSON は監査・復旧用に `data_dir/prediction_inputs/YYYY-MM-DD/` へ保存します。`outbox/chat_input/prediction/` は自動運用では使用しません。

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
- 1回のprediction実行でCodex CLIは1回だけ実行します。CLI失敗・不正JSONはphase失敗とし、自動運用の再試行はschedulerが管理します。
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

レース一覧と予想ページには、統計重視のみの「前日予想公開」、総合予想ありの「直前予想公開」、結果・評価がある「結果公開」、優先表示の「開催中止」を使用します。予想ページは予想の公開段階を表示し、結果ページは常に「結果公開」です。

schedulerの `--execute` は今日の未完了・未中止レースがある場合だけ、通常探索と独立してnetkeiba公式お知らせ（`https://info.netkeiba.com/`）を最大1時間に1回確認します。確認開始前に同じ探索キャッシュへ `cancellation_checked_at` を保存するため、通信失敗時も10分ごとには再取得しません。明日のレース、result保存済み、中止確定済みは確認対象外です。過去の中止記事や中止確定の根拠URLを再取得せず、代替開催は保存済み `replacement_date` と通常探索の同一race_id・日付を照合します。中止判定には公式告知と対象日付・競馬場・レース範囲の一致を必要とし、取得失敗や一覧からの消失では中止にしません。

公式記事に代替日が明記されている場合は `replacement_date` を記録し、その日付の探索で同じrace_idを確認できたら、新しい日付のrace JSONを `rescheduled_from` 付きで作成します。元日のJSON・予想は中止記録として残し、新日程は既存フローで処理します。元日付の確定inputを新日程のresumeに流用しません。日付やレース範囲を確定できない告知は推測で適用しません。過去JSONの一括移行は不要です。

ソースコードは `main`、公開用 `public/` は `deploy-pages` で管理します。mainでは `public/` をGit管理せず、ローカルの生成物として保持します。公開先ブランチは初回のみ手動作成が必要です。GitHub Actionsのpushトリガーは `deploy-pages` の `public/**` を対象とします。

`publish_site()` はホスティング先に依存せず、stageをローカル `public/` へ反映します。`deploy_site()` は `publish_mode` に応じて公開し、現在は `github_pages` のみ対応します。schedulerの `--execute` はphase処理後、処理件数が0件でも同じlock内でdeployを試みます。deploy失敗はraceの失敗回数に加算せず、次回起動で再試行します。

GitHub Pagesへのdeployは `deployment.github_pages.remote`（実行元repoのremote名）と `branch` を使います。成功済み公開内容のハッシュをローカルに記録し、`public/` に変更も再送待ちもなければネットワークアクセスせず終了します。変更時・失敗後は `data_dir/deploy/github_pages/` の専用cloneをremoteへ同期し、完成済み `public/` を完全コピーして、差分がある場合だけ `public/` をcommit／pushします。開発用working treeはcommit／resetしません。実行環境にはGitのcommit用ユーザー設定とremoteへのpush権限が必要です。

この実装では `public/` を静的サイト出力先にしています。GitHub Actions の `Deploy Pages` workflow が `public/` を Pages artifact としてアップロードし、GitHub Pages へ配布します。Actions 側ではビルド処理を行いません。
