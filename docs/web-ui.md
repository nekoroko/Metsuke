# React Web UI — 実装メモ

デザイン指示書（`design_handoff_react_webui/README.md`）に沿って、Streamlit UI を
React SPA ＋ FastAPI に置き換えたときの判断を残す。指示書そのものは配布物なので、
ここには「なぜそう作ったか」だけを書く。

## 構成

```
api/     FastAPI。既存の db / executor / scheduler / graphs / llm_profiles を薄く包む
web/     Vite + React 18 + TypeScript。styles.css（Modernist）をそのまま使う
app_streamlit.py   旧UI。同じDBを見るので、移行中は両方立ち上げて比べられる
```

配信は1プロセス。`vite build` の成果物を FastAPI の StaticFiles が配る
（`python -m api` → `http://localhost:8000`）。開発時だけ Vite dev server を
別に立て、`/api` を 8000 へプロキシする。

## ロジックをフロントに持たせない

`THOUGHT:` / `ACTION:` / `DONE:` のパースは `executor.parse_history_for_display`
のまま。API が `steps` として整形済みの配列を返し、フロントは表示するだけにした。

同じ正規表現をフロントにも書くと、**片方だけ直す事故**が起きる。実行の見え方が
Streamlit 版とズレると、移行中にどちらが正しいのか判断できなくなる。
`graph_label` / `model_label` も同じ理由でサーバ側で作っている。

## APIキーを外へ出さない

- `GET /api/llm-profiles` は `api_key` を返さない。`has_api_key`（真偽値）だけ。
- `GET /api/settings` は `*_api_key` を `********` に置き換える。
- `PATCH /api/llm-profiles/{id}` に**空文字の api_key が来たら無視する**。
  画面は鍵の実値を持っていないので、入力欄が空のまま保存されると鍵が消える。
- `PUT /api/settings` に伏せ字がそのまま返ってきた場合も無視する。

一度でも返すと devtools・プロキシログ・画面キャプチャに残り、あとから消せない。
`tests/test_api.py::TestLlmProfilesApi` が、作成・一覧・更新の3経路で固定している。

## 実行の追従は SSE、ポーリングはしない

エージェント実行は APScheduler のジョブとして走り、進捗を
`update_execution_progress` でDBへ書く。API のハンドラはその外側にいるので
generator を直接受け取れない。`api/events.py` がDBを1秒ごとに見て、
history / trace の**件数差分**だけを送る。

件数で持つのは、同じイベントを二度送らないため。再接続時はサーバが履歴を頭から
流し直すので、**ページを離れて戻っても復元できる**（クライアント側に復元処理は無い）。

ツール生成（`ai_creator`）は同一プロセスの generator なので、そちらは
ワーカースレッド経由で yield をそのまま流す（LLM呼び出しでイベントループを
止めないため）。

## 自動スクロールは末尾にいるときだけ

タイムラインは、ユーザーが上を読んでいる間は動かさない。`scrollIntoView` は使わず、
スクロール位置が末尾から40px以内のときだけ `scrollTop` を更新する。
新しいトークンが来るたびに視点が飛ぶと、流れているログは読めない。

## デザインシステムの守り方

`web/src/styles/modernist.css` がトークンの正。画面固有CSS（`app.css`）でも
hex・フォント名・角丸を直に書かない。指示書 §8 の確定値（サイドバー212px、
右パネル392px、区切り2px/行1px 等）は `app.css` に集約してある。

- 角丸は全て 0（`--radius-*: 0px`）
- `:focus-visible` は 2px のアクセント。ブラウザ既定の青は出さない
- アニメーションは 120–160ms の opacity/transform のみ

## 停止（cancel）の限界

`POST /api/executions/{id}/cancel` は、APScheduler のジョブがまだ始まっていなければ
取り消す。走り出したグラフには割り込めないので、状態を error にして画面と履歴の上では
終わったことにする。ワーカーは現在のステップを終えてから `finish_execution` を
呼ぶが、そのときには既に終了済みなので実害はない。

**「実行中のグラフを本当に止める」ことは今の設計ではできない。** やるなら
`AgentState` に停止フラグを持たせ、各ノードの入口で見る必要がある（未着手）。

## デザインシステムのクラス名を借りるときの注意

サイドバーに `.nav` / `.nav-item` を使ったところ、**選択中の項目が消えた**。

modernist.css の `.nav` は「ヘッダーバー」用の別コンポーネントで、

```css
.nav a:hover, .nav a[aria-current='page'] { color: var(--color-accent); }
```

を持っている。`NavLink` は選択中のリンクに `aria-current="page"` を付けるので、
`.nav a[aria-current='page']`（詳細度 0,2,1）が `.nav-item.active`（0,2,0）に勝ち、
**赤背景の上に赤文字**になっていた。

詳細度で殴り返すのではなく、`.sidenav-*` に名前を分けた。`.nav` は
デザインシステム側では別のものを指しているので、そもそも借りるのが誤りだった。

**用途が違うなら名前も分けること。** クラス名を再利用すると、
今は見えていない指定まで一緒に付いてくる。

## ヘルプは画面の中に置く

`web/src/help/topics.ts` に Markdown で持ち、`/help/:topic` で出す。
README に飛ばさないのは、「Podman 未構築」と出ている人が知りたいのは
**その場での直し方**であって、リポジトリの構成説明ではないため。

サイドバーの状態タグ（検索プロバイダ / Podman）は、それぞれの
ヘルプへのリンクにしてある。表示から1クリックで手順に着く。

ヘルプの冒頭には `GET /api/meta` の現在値を出す（Podman の state、既定モデル、
検索プロバイダ）。手順だけ読んでも「自分がどれに当てはまるか」が分からない。

## この環境で確認したこと / していないこと

確認済み:

- `npm run build` が通り、`python -m api` で SPA とAPIが同一オリジンで配れること
- SPA のディープリンク（`/runs/xxx` を直接開く）が index.html を返すこと
- タスク作成 → 実行投入 → `graph_kind` と `llm_info` の記録 → 実行詳細の取得
- SSE が `text/event-stream` で `status` イベントを即時に返すこと
- `tests/test_api.py`（32件）

未確認:

- **ブラウザでの実際の表示**。ヘッドレス環境なので、レイアウト崩れや
  キーボード操作（⌘K / ⌘E / ⌘M）の挙動は目視できていない。
- LLM を実際に呼ぶ経路（ローカルLLMがこの環境から見えない）。
  タイムラインにステップが流れる様子は、実機で確認が要る。
