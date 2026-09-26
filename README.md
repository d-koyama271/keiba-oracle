# keiba-oracle

中央競馬の平地重賞を対象にレース情報を収集し、各馬の1着確率をAIで予想するファイルベースのシステムです。購入シミュレーションと予測評価は通常コードで計算し、静的HTMLをGitHub Pagesへ公開します。通常運用はschedulerから行います。

## 設計方針

- Python・DBなし・1レース1JSON。predictionとsimulationを分離します。
- LLMはpredictionのみ担当し、記事・説明はテンプレートで生成します。
- EV、Kelly、Dutching、馬連確率、払戻、evaluationは決定論的に計算し、乱数・Monte Carloを使いません。
- `general`（総合AI予想）と`statistical`（統計重視予想）は独立し、片方だけでも保存・公開・評価できます。
- prediction inputはAI実行前に確定snapshotとして保存し、resumeでは同じ入力を再利用します。
- 自動運用では発走後に新しいpredictionを生成しません。保存済みpreを結果取得後に変更しません。
- renderで業務計算を再実装せず、保存済みsimulationを優先します。
- phase完了判定の主な根拠はrace JSONの成果物です。途中・失敗stateが残っていれば、一部成果物が存在しても復旧処理を続けます。
- 過去JSONの一括migrationやblanket backfillは行いません。

## ディレクトリ構成

標準設定での配置です。データ・公開先は`config/app.yaml`の`data_dir`・`public_dir`に従います。

```text
config/
  app.yaml
  prompt_prediction.txt
  prompt_prediction_statistical.txt
src/
  scheduler.py / automation_state.py
  run_pre.py / run_post.py
  run_pre_collect.py / run_post_collect.py
  collect.py / predict.py / llm_client.py
  simulate.py / quinella.py
  evaluation.py / evaluation_summary.py / backtest.py
  render.py / publish.py / deploy.py / utils.py
  response_importer.py / watcher.py
data/
  races/YYYY-MM-DD/<stem>.json
  prediction_inputs/YYYY-MM-DD/<stem>.json
  prediction_inputs/YYYY-MM-DD/<stem>.statistical.json
  automation/YYYY-MM-DD/<stem>.json
  automation/discovery_cache.json
  automation/scheduler.lock
  evaluation_summary.json
  _site_stage/
  deploy/github_pages/         # 自動deploy専用clone
  deploy/github_pages_state.json
templates/
  base.html.j2                 # ページ共通のHTML・基本CSS
  index.html.j2 / race.html.j2 # 一覧・レースページ
  quinella.html.j2             # 馬連固有の表示
  components/
    ui.html.j2                # バッジ・tooltip・結果サマリー
    tabs.html.j2              # AI方式・券種のタブ
    tables.html.j2            # 表のスクロール枠
    simulation.html.j2        # 共通の購入シミュレーション表示
  scripts/                    # inlineで展開するレースページのJS
public/
  index.html
  races/YYYY-MM-DD/<stem>.html
  races/YYYY-MM-DD/<stem>_result.html
inbox/prediction/              # 旧手動応答の取込専用
  processed/YYYY-MM-DD/
logs/
tests/
.github/workflows/pages.yml
requirements.txt
```

`<stem>`は`nakayama_11r`などの競馬場・レース番号です。日付別ディレクトリと組み合わせて識別します。runtimeデータ、評価集計、stage、専用clone、`public/`はmainのGit管理対象外です。

## セットアップ

Python 3.11以上とGitを用意し、リポジトリルートで仮想環境を作成します。

```bash
python -m venv .venv
```

以降の`python`は仮想環境のPythonを使用してください。Windowsでは`.venv\Scripts\python.exe`、POSIXでは`.venv/bin/python`です。

```bash
python -m pip install -r requirements.txt
codex login status
```

通常の予想runtimeはCodex CLIです。実行ユーザーで`codex`をPATHから起動でき、ログイン済みであることを確認します。GitHub Pagesへ自動公開するユーザーには、Gitのcommit用ユーザー設定と対象remoteへのpush権限も必要です。

設定の正本は`config/app.yaml`です。モデル名や購入条件の具体値はこのファイルを参照してください。

| 設定 | 用途 |
| --- | --- |
| `target_races` | 対象競馬場 |
| `automation.*` | phase実行時刻・retry間隔・上限 |
| `odds_reference_minutes_before_start` | 手動収集時の推奨取得目標。schedulerの実行時刻とは別設定 |
| `llm_provider` / `llm_model` / `llm_reasoning_effort` | 予想runtime・モデル・reasoning effort |
| `simulation.budget` / `stake_unit` | 各シミュレーションの予算・購入単位 |
| `simulation.value` / `simulation.dutching` | 単勝の購入条件 |
| `simulation.quinella` | 馬連の確率近似・購入条件 |
| `publish_mode` / `deployment.github_pages` | deploy backendとremote名・公開branch |
| `data_dir` / `public_dir` | データ・公開物の保存先 |

Codex CLIにはモデル・reasoning effortを明示して渡し、ユーザー設定に依存させません。一時ディレクトリのread-only sandbox・ephemeral・構造化出力を使用し、確定入力以外のファイルやWebを参照しないようプロンプトで指示します。1回のprediction実行につきCLI実行は1回で、内部retryはありません。`llm_provider: openai`のAPI経路もあり、その場合は`OPENAI_API_KEY`が必要です。

## 通常の自動運用

```bash
python src/scheduler.py             # 探索・予定・判定の確認のみ
python src/scheduler.py --execute   # 対象phaseを実行し、deployまで行う
```

Task Scheduler / cron等で`--execute`を10分ごとに起動する運用です。scheduler自身は常駐せず、1回の起動で各対象phaseを最大1回実行します。Windowsでは仮想環境のPythonとスクリプトの絶対パスを指定し、作業ディレクトリをリポジトリルートにします。`pythonw.exe`にも対応し、runtimeのCodex・Git子プロセスはコンソールを表示しません。

### 対象と予定時刻

JSTの今日・明日について、実際の開催データから`target_races`内の平地GI/GII/GIIIを探索します。曜日や11Rに限定せず、障害重賞は対象外です。重賞がない日は空で正常終了し、自動運用では11Rへのfallbackを行いません。

| phase | 実行予定 |
| --- | --- |
| statistical | レース前日の`automation.statistical_time` |
| general | 発走の`automation.general_minutes_before_start`分前 |
| result | 発走の`automation.result_minutes_after_start`分後 |

予定はraceの日付・発走時刻から共通関数で計算します。indexの自動更新説明と次回更新予定も同じ設定・計算を使います。実処理は予定到達後のscheduler起動時に行われ、結果未公開やretryによって遅れる場合があります。

### 探索キャッシュ・開催中止

通常探索はキャッシュを再利用し、毎tick・毎時には再取得しません。日付構成・対象競馬場の変更、キャッシュ欠損・破損・対象日不足時に探索し、同日でも`statistical_time`到達後に未探索なら1回再探索します。正常な既存キャッシュがある場合、探索失敗後はそれを維持し、再探索まで1時間空けます。phase・retry判定は毎tick継続します。確認表示だけの起動は探索通信を行いますが、キャッシュ・state・公開物は更新しません。

開催中止確認は、当日の未result・未cancelledの対象レースがある場合だけ、netkeiba公式お知らせを最大1時間に1回取得します。通信失敗時も同じ間隔を空けます。告知の日付・競馬場・レース範囲が一致した場合だけ中止とし、取得失敗や一覧からの消失を中止とは扱いません。

中止後はpreと通常result取得を停止し、評価集計から除外します。予想と中止記録を残して予想ページを「開催中止」とし、結果ページは生成しません。中止表示の公開失敗もresult phaseで復旧します。

告知に代替日が明記されている場合は`replacement_date`を保存します。通常探索の更新時に、その日付で同じrace_idを確認した場合だけ新日程のJSONを作成します。元日付のJSON・予想・中止記録は維持し、古い確定inputは流用しません。代替確認のために過去の告知記事を再取得しません。

### retry・途中終了・lock

処理単位は日付 × race_id × phaseです。race別automation stateには`in_progress`・`retry_wait`・`blocked`を保存します。開始時はattemptsを増やさず、失敗記録で加算します。retry間隔と上限はpre用・result用のautomation設定に従い、上限到達後はblockedとして自動再実行を止めます。不正なstateを黙って初期化しません。

完了はrace JSONのpredictionやresultを根拠に判定します。ただし途中・失敗stateがあればそれを優先し、simulation・evaluation・render・publishを含むフローの正常終了と成果物確認後だけ解除します。result完了には存在するpredictionのevaluation、利用可能なpreに対応するpost、readyな馬連の精算完了が必要です。simulationがないこと自体は未完了理由にしません。

前日以前の`retry_wait`・`in_progress`もローカルJSONから復旧対象へ追加します。blockedを復活させたり、過去レースを外部探索したりはしません。発走後のpre復旧は、有効な確定inputと現在のAI実行設定に対応する保存済みpredictionがある場合だけ許可し、新規predictionは生成しません。

`--execute`全体をOS管理の非ブロッキングファイルlockで保護します。競合時は正常skipし、異常終了時はOSが解放するためlockファイルの手動削除は不要です。deployも同じlock内で実行します。

## 手動実行・resume

```bash
python src/run_pre.py --date YYYY-MM-DD --phase statistical
python src/run_pre.py --date YYYY-MM-DD --phase general
python src/run_pre.py --date YYYY-MM-DD --phase all
python src/run_post.py --date YYYY-MM-DD
```

| pre phase | 処理 |
| --- | --- |
| statistical | 収集 → statistical input保存・予想 → render・publish。general input確定とsimulationは行わない |
| general | 再収集 → general input確定・予想 → 保存済みgeneral/statisticalのpre simulation → render・publish |
| all（省略時） | 1回の収集 → 両方式の入力保存・予想 → pre simulation → render・publish |

allでは片方の方式だけ成功しても、その予想のsimulation・公開を継続します。両方失敗したレースは成功対象から除外し、成功レースも中止レースもなければエラー終了します。general実行はstatisticalを新規生成しません。

日付省略のpreは次の開催期間を探索し、重賞がない場合だけ各場11Rへfallbackする手動選択です。日付指定時も対象場の重賞がなければ11Rへfallbackします。postの日付省略は当日です。自動運用の探索範囲とは異なります。

`--race-id <race_id>`でpre・post・resumeの対象を1レースに限定できます。postは利用可能なpreを精算し、predictionを評価して集計・render・publishします。statistical predictionとresultだけでも評価・結果ページを生成できます。手動pre/postはローカルpublishまでで、remote deployは行いません。

### 確定入力からの再開

```bash
python src/run_pre.py --date YYYY-MM-DD --phase general --race-id <race_id> --resume
python src/run_pre.py --date YYYY-MM-DD --phase statistical --race-id <race_id> --resume
```

resumeは日付と単独phaseが必須です。再収集せず、generalは`prediction_inputs/YYYY-MM-DD/<stem>.json`、statisticalは同じ場所の`<stem>.statistical.json`を使用します。入力のrace_id・日付・競馬場・レース番号・方式を検証し、欠損・不正入力を現在のrace JSONから作り直しません。保存後にrace/horsesが更新されても、確定snapshotを入力の正本として扱います。有効な既存predictionは再利用して後続処理を続けます。

通常のgeneral実行は再収集して入力を確定し直すため、失敗時に元のsnapshotで再開したい場合は`--resume`を使用してください。statisticalは既存の確定inputがあれば検証して再利用します。

手動入口にはschedulerの発走後ガードが一律には適用されません。statisticalは明示inputによる手動復旧を許可しますが、resultが存在すれば新規生成を拒否します。generalの低レベル入口には同じ時刻ガードがありません。自動運用の制約を迂回して発走後の新規予想を通常成績へ追加する用途には使いません。`predicted_at`は実際の生成時刻です。

## Race JSON・prediction

トップレベル構造は固定、`meta.schema_version`は10です。

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

`prediction[]`の各要素には`id`、AI情報、独立した`general`／`statistical`を保存します。AI情報は親に一度だけ保持します。`provider`・`family`・`model`・`runtime_provider`・`reasoning_effort`がすべて一致するentryを再利用し、設定が変われば別IDを作成します。`p1`等はレース内の参照IDで、AI名・モデル名をJSONキーやIDには使用しません。

現在のCodex運用では`provider`はOpenAI、`family`はGPTです。`model`・`runtime_provider`・`reasoning_effort`はそれぞれ設定の`llm_model`・`llm_provider`・`llm_reasoning_effort`に対応します。

```text
prediction[]: id / AI情報 / general / statistical
simulation[]: prediction_id → general|statistical → win|quinella → value|dutching → pre|post
evaluation[]: prediction_id → general|statistical
```

予想には各馬の確率・理由、総括、生成日時、使用プロンプト・入力のハッシュを保存します。generalは収集した市場情報を含み、statisticalは今回・過去走のオッズ、人気、取得元、市場由来情報を除外します。prediction・simulation・result・evaluationや馬連snapshotは予想入力へ含めません。

収集では単勝オッズをnetkeibaから取得し、完全な検証に失敗した場合だけJRA公式へfallbackします。取得元を混在させず、採用元と取得時刻を記録します。馬の対象レース自身を除く直近5走と、取得した履歴に基づく条件別`career_summaries`を保存します。

結果取得は全出走馬・勝ち馬の払戻等を検証します。確定単勝オッズは`result.final_win_odds`に保存し、postフローでは予想時点の単勝オッズを置き換えません。retryで欠けた確定オッズ・天候・馬場や確定済み馬連払戻は既存値を維持し、有効な新値があれば優先します。

schema v9以前は読み込み時に旧本体をgeneral、統計重視variantをstatisticalへ正規化し、prediction・simulation・evaluationのIDを対応付けます。読み込みだけでは元ファイルを変更せず、通常処理で保存する場合にv10形式になります。既存の監査情報・計算値は維持し、一括移行はしません。

## 購入シミュレーション

general/statistical × 単勝/馬連 × value/dutchingはそれぞれ独立した仮想シミュレーションです。各々に設定の予算上限を適用し、全方式へ予算を分割したり、一つの実運用収支に合算したりしません。

- **value（期待値重視）**：確率×オッズでEVを計算し、fractional Kellyで購入額を決定します。予算超過時だけ比例縮小し、購入単位へ切り捨てます。余った予算は強制配分しません。
- **dutching（分配）**：確率上位1件から設定上限まで評価します。各対象に1単位を配り、想定払戻が最小の対象へ残りを順次配分します。カバー確率・グループEV・最低利益率を満たす候補を、グループEV、カバー確率、少ない選択数の順で選びます。最低利益率の分母は実際の合計購入額です。

単勝・馬連はDutchingの配分・候補評価・選択処理を共有し、丸め・EPSILON・閾値判定も揃えています。利益率0では、他の条件を満たせば損益分岐も適格です。

馬連は全出走馬の1着確率からDiscounted Harvilleで組確率を算出します。

```text
P(i,j) = p_i * p_j^lambda / sum(k != i, p_k^lambda)
       + p_j * p_i^lambda / sum(k != j, p_k^lambda)
```

全ペアの確率合計・発走前の完全な馬連オッズsnapshotを検証し、候補だけへの再正規化は行いません。`probabilities`・`harville_lambda`・`odds_snapshot`を同じ予想方式のvalue/dutchingで共有します。馬連データが利用不可でも単勝処理は継続します。

保存済みの有効なpreは再実行で上書きせず、結果取得後も維持します。新規馬連preは発走前・result未取得時だけ生成します。postは保存済み購入対象を公式払戻で精算し、単勝・馬連とも出走取消・競走除外による返還を扱います。馬連は同着にも対応します。馬連払戻未確定は`awaiting_payouts`、確定後は`settled`とし、未確定を外れ扱いにしません。返還だけでは的中に数えません。

単勝valueのpreにはEV、Kelly、理論購入額、最低予算等の`details`も保存します。renderは保存値を使い、詳細のない旧JSONだけ共通の計算関数へfallbackします。設定変更後の再renderで新しい購入計算を保存結果へ混ぜません。

カスタム購入シミュレーションは保存済み確率・オッズ・設定を基にブラウザ内で計算します。馬連確率をブラウザで再推定せず、結果をrace JSONや正式収支へ保存しません。PythonとJavaScriptの計算一致をテストします。

## 評価・backtest

`evaluation`は保存済みpredictionとresultを対応付け、勝ち馬の予測確率・順位、Top1/3/5、Log Loss、Brier Score、市場ベースライン比較を方式別に計算します。simulationの有無に依存せず評価し、開催中止は対象外です。

`evaluation_summary.json`は評価済みrace JSONと保存済みpostから再生成する派生データです。方式別の成績・条件別集計・確率校正・収支を保持し、同一race・prediction_idの両方式が揃う場合だけ対比較します。発走後オッズは正式な市場比較から除外します。馬連収支は確定postだけを集計し、返還を的中扱いにしません。

```bash
python src/evaluation_summary.py
python src/backtest.py
```

backtestは保存済みprediction・単勝オッズ・resultを使い、現在の設定で単勝value/dutchingを再計算して方式別の収支を表示します。馬連backtestではなく、race JSONや保存済みsimulationも変更しません。通常の評価集計は再計算値ではなく保存済みpostを使用します。

## render・publish・deploy

```text
scheduler → run_pre / run_post → render → data/_site_stage
                                      → publish → public
                                                → deploy → deploy-pages → GitHub Pages
```

`render_site()`は保存済みデータからstageへHTMLを生成し、`publish_site()`がローカルpublicを差し替えます。途中終了でpublicがなくbackupだけ残った場合は復旧します。publish自体はホスティング先を知りません。

テンプレートでは同じ意味・構造のUIをJinja macroへ集約し、券種ごとに同じ表示ロジックを複製しません。表のスクロール枠など共通部分のみをまとめ、券種固有のデータや列は各テンプレートで扱います。文脈や構造が異なる表示は無理に万能component化せず、各ページに残します。

indexには公開済み予想と中止レースを掲載し、前日予想公開・直前予想公開・結果公開・開催中止を表示します。未結果の公開済み予想には次回更新予定を併記します。結果ページは正常な結果・評価がある場合に生成します。

全体の再render・publish・deployを手動で行う場合は、リポジトリルートから次を実行します。業務データの再計算は行いません。手動の関数呼出しにはscheduler lockが自動適用されないため、定期実行と重ならないようにします。

```bash
python -c "import sys; sys.path.insert(0, 'src'); from utils import load_config; from render import render_site; from publish import publish_site; from deploy import deploy_site; c=load_config(); render_site(c, 'manual-render'); publish_site(c); deploy_site(c)"
```

`python src/render.py`は全raceを再renderします。`python src/render.py --date YYYY-MM-DD`は指定日だけを再renderし、他の日の公開済みrace HTMLを維持します。indexはどちらも公開済みrace全体から作成します。

mainはソース管理、deploy-pagesは公開物の管理に使います。`deploy_site()`は現在`github_pages`のみ対応し、`deployment.github_pages.remote`からURLを取得して専用cloneを使用します。公開branchは初回のみ手動作成が必要です。

publicのハッシュが成功済み公開と一致すれば、GitHubへ通信せず終了します。変更時・push失敗後は専用cloneをremote基準へ同期し、publicを削除分も含め完全同期して、差分がある場合だけpublicをcommit/pushします。開発用working treeをcommit・reset・stashしません。

schedulerはphase実行0件でもdeployを試みます。deploy失敗はraceのattemptsと分離し、次回起動で再試行します。GitHub Actionsはdeploy-pagesの`public/**`更新を受けてPagesへ配布し、Actions上で予想やサイト生成は行いません。

## 旧manual flow

`response_importer.py`・`watcher.py`・`inbox/prediction/`は、手動作成した予想応答の後方互換用です。通常の自動予想では使用しません。`run_pre_collect.py`は現在も通常preの収集・input生成に使用します。

手動応答には`meta.race_id`（またはトップレベルの`race_id`）と、全出走馬分の`prediction.horses`、必要な総括を含めます。importerは応答を検証してgeneralへ保存し、既存generalがあれば上書きせず拒否します。

```bash
python src/response_importer.py --kind prediction --file inbox/prediction/response.json  # 単独取込
# または、watcherで取込から公開まで行う
python src/watcher.py --once
```

importer単独は取込のみです。watcherは未取込の応答を取り込み、pre simulation・render・publish後、`inbox/prediction/processed/YYYY-MM-DD/`へ移動します。常駐監視には`--interval`を使えます。remote deployは行いません。

## テスト

```bash
python -m unittest discover -s tests -v
```

固定fixtureとモックでparser・prediction・保存互換・simulation・評価・scheduler復旧・公開処理を検証します。netkeibaや実際のLLMには接続しません。Git deployは一時ローカルrepo、Python/JavaScript parityはNode.jsを使用します。Node.jsがない場合、そのテストはskipされます。

renderテストはUI文言そのものより、状態判定、DOM構造、表示有無、保存データから表示値への伝播、リンク先、data属性、計算結果を優先します。UIラベルや説明文を固定文字列としてテスト側に複製せず、`ui_labels.py` の第二の文言辞書を作りません。文言変更でテストが失敗した場合は期待文字列を置換する前に、その文言自体を保証する必要があるか見直します。文言が明示的な仕様である場合を除き、意味的なclass・data属性やfixtureの入力値で確認します。日時・金額・確率などの表示値は必要に応じて検証し、色・余白・font-sizeなどの装飾値は明示的な仕様がない限り固定しません。その他のテストではidentity、境界条件、snapshot再利用、成果物、状態遷移を優先します。
