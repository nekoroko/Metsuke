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

## テスト

`tests/test_research_graph.py` — LLMもネットワークも使わない。
`_ask` / `web_search` / `fetch_url` を差し替えて、各ノードの入出力、
失敗時のフォールバック、遷移先を確認する。ルーティングは
`NODES` / `ROUTES` の定義から「遷移先が未登録のノードを指していないか」を検査する。
