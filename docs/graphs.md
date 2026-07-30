# 実行グラフの切り替え

エージェント（Type2）の動かし方を2種類から選べる。切り替えは
設定画面（⚙️ 設定 → 🔀 グラフ）とエージェントタスクごとの指定で行う。

## 2つのグラフ

### ReActループ — `graph.py`

```
react → verify_tool ─┐
  ↑                  │
  └──────────────────┘
react → correct → critic → END
```

毎ステップ、LLMが `THOUGHT / ACTION / DONE` を書き、次の行動を自分で決める。
ツール実行・コード生成・ファイル操作など、手順が事前に読めない作業に向く。

### リサーチ（ステートマシン） — `graph_research.py`

```
plan → search → digest → gap ─┬→ search（不足あり／ラウンド上限まで）
                              └→ compose → correct → critic → END
```

| ノード | LLM | 役割 |
|---|---|---|
| `plan` | 1回 | タスクを調査項目（最大5件）と初期クエリに分解する |
| `search` | なし | 未充足の項目について `web_search` を実行（コード側が呼ぶ） |
| `digest` | なし | 検索で得たURLを `fetch_url` し、関係する段落だけ抜き出す |
| `gap` | ラウンドごと1回 | 各項目が今ある情報で答えられるか判定し、不足なら次のクエリを決める |
| `compose` | 1回 | 台帳と抜粋だけを渡してレポートを書かせる |
| `correct` / `critic` | — | ReAct版と同じものを流用（数値の機械照合・レビュアー） |

ReActで起きていた次の空回りが、構造的に起きなくなる。

- `ACTION:` の書式崩れでステップを1回まるごと捨てる
- 同じ内容のクエリを言い換えて繰り返す
- DONEを書く局面でも、行動指示のルールがプロンプトの枠を占める

代わりに、ツール実行やコード生成はしない。調査・レポート作成の専用線と考える。

#### digest がLLM要約ではなく機械抽出な理由

`fetch_url` は最大8000文字返す。これをそのままLLMへ渡すと、8192コンテキストの
ローカルモデルでは回答を書く枠が消える。ページごとに要約させると1ページ1回の
LLM呼び出しが増える。そこで `relevant_excerpt()` が段落単位でスコアを付け
（調査項目の語を含む／数字を含む／単位付きの数値を含む）、上位だけを
700文字まで残す。抽出した数値は `findings`（台帳）にも積むので、
履歴のトリミングで消えない。

#### 失敗しても止めない

`plan` が書式を守らなかった場合はタスクそのものを1項目として進み、
`gap` の判定が解釈できない・LLMが落ちた場合は「充足」とみなして `compose` へ進む。
いずれも trace に `skipped` と理由が残る。調査系のタスクで、
途中の判定が1回崩れただけで全損するのは割に合わない。

## 切り替えの優先順位

```
実行時の引数（run_agent_now の graph_kind）
  > エージェントタスクの graph_kind 列
    > 設定画面の既定（settings.default_graph_kind）
      > react
```

解決は `graphs.resolve_kind()` に集約している。executor は
`graphs.get_app(kind)` と `graphs.make_state(kind, prompt)` しか呼ばない。

### ステップ予算がグラフごとに違う理由

`graphs.KIND_BUDGETS` で、ステートマシンだけ `max_steps` を18にしている。
1ラウンドで `search` と `digest` の2ノードを進むため、ReActと同じ10のままだと
`correct` / `critic` が「差し戻す予算がない」と判断してレビューを飛ばしてしまう。

`RECURSION_LIMIT`（60）も同じ理由で引き上げてある。LangGraphの既定は25で、
ラウンドを回し切る前に打ち切られる。

## 状態とUI

状態は `AgentState` を共有している（`plan_items` / `research_round` /
`queries_done` / `compose_count` などを追加）。`history` `trace` `status` の
契約が同じなので、実行履歴画面・ノード遷移の表示・エクスポート・
最終結果の抽出（`DONE:` を目印にする）はどちらのグラフでも同じものが動く。

`compose` が回答を `DONE: ` で始めるのは、この互換性のため。

## 実行履歴での呼び名

`executions.graph_kind` に、その実行で使ったグラフを記録する（解決した直後の
`executor._prepare_agent_run` が書く。実行を始める側は、タスクの指定と設定の
既定で何になるかを知らないため）。

見出しは `graphs.log_title()` が決める。

| 状況 | 表示 |
|---|---|
| `graph_kind` が保存されている | `🔁 リサーチ工程のログ` / `🔁 ReActループのログ` |
| 列が空でも、ノード遷移から判別できる | 同上（`plan` / `digest` / `gap` / `compose` があればリサーチ） |
| どちらも判別できない | `🔁 実行ログ`（グラフ名は名乗らない） |

3つ目を「ReActループ」と書かないのは、ReAct固定だった頃の履歴と、
trace を持たない古い履歴を区別できないため。判別できないものに名前を
付けると、そこだけ嘘になる。カードのバッジも同じ規則で、判別できない実行には出さない。

## テスト

`tests/test_research_graph.py` — LLMもネットワークも使わない。
`_ask` / `web_search` / `fetch_url` を差し替えて、各ノードの入出力、
失敗時のフォールバック、遷移先を確認する。ルーティングは
`NODES` / `ROUTES` の定義から「遷移先が未登録のノードを指していないか」を検査する。

---

# LLM接続プロファイル（複数保存 + 実行時選択）

## なぜ

接続設定は settings の `local_*` / `api_*` に1組だけ持っていた。モデルを変えて
比べるには上書きするしかなく、前の設定が失われる。Gemma 4 と Qwen 3.5 で
同じタスクを流して比べる、といった使い方ができなかった。

## 構造

役割は `graphs.py` と対になっている。グラフの選び方と同じ形にしてあるので、
片方を読めばもう片方も読める。

| | グラフ | モデル |
|---|---|---|
| 定義・解決 | `graphs.py` | `llm_profiles.py` |
| 保存先 | `settings.default_graph_kind` | `llm_profiles` テーブル + `settings.default_llm_profile_id` |
| タスク個別 | `agent_tasks.graph_kind` | `agent_tasks.llm_profile_id` |
| 実行時の上書き | `run_agent_now(graph_kind=...)` | `run_agent_now(llm_profile_id=...)` |
| 解決 | `graphs.resolve_kind` | `llm_profiles.resolve_profile` |
| 履歴への記録 | `executions.graph_kind` | `executions.llm_info`（JSON） |

優先順位はどちらも **実行時の指定 > タスク個別 > 設定画面の既定**。

## get_llm() への渡し方

`get_llm()` はグラフの各ノードから直接呼ばれる（十数箇所）。引数で引き回すと
全ノードのシグネチャが変わるので、実行の外側で「今どのプロファイルか」を立てて
`get_llm()` 側が見る形にした。

```python
with config.use_profile(profile):
    for step in app.stream(state):
        ...
```

- **thread-local** にしている。スケジューラのジョブと Streamlit の画面は別スレッドで
  同時に走るため、モジュール変数だと片方の実行がもう片方のモデルを差し替える。
- **回すところを1箇所に寄せた**（`executor._agent_steps`）。3つの実行経路それぞれで
  `with` を書くと、どれか1つで書き漏らしたときに黙って既定のモデルで走る。
  `TestExecutorWiring` が「`.stream(` は1箇所だけ」を構造として固定している。

## 設定はプロファイル側へ一本化した

初回起動時に、それまでの `local_*` / `api_*` から1件を自動で作る（`db._migrate_llm_profile`）。
移行元のキーは消さずに残すが、編集経路は無くなる。

`config._effective_settings()` は、実行中のプロファイルが無い場合も**既定の
プロファイル**を重ねる。こうしないと「設定画面で編集した値と、ツール生成AIが
使う値が食い違う」状態になる。プロファイルが1件も無いとき（DBが読めない等）だけ
従来のキーが効く。

## APIキーは履歴に出さない

`executions.llm_info` に入れるのは `llm_profiles.SNAPSHOT_FIELDS` だけで、
**`api_key` を含めない。** 履歴の「まとめてコピー」はそのまま外へ貼られる
前提のテキストなので、鍵が1度混ざると貼った先すべてから消す必要が出る。
`TestApiKeyNeverLeaves` が、写し・DB・コピー用テキストの3段で固定している。

IDではなく値を写して持つのは、後でプロファイルを編集・削除したときに
「何で実行したか」が分からなくなるため。

## 削除の扱い

最後の1件は削除できない（0件になると実行時に選ぶものが無くなる）。
既定に選ばれていたものを削除したら、残っているものへ付け替える。
タスク側が消えたIDを指していても、`resolve_profile_id` が既定へ落とすので
実行は止まらない。

## verify_run.py

`--list-profiles` で一覧、`--profile <名前かID>` で選んで実行できる。
モデルを変えた実測の比較に使う。
