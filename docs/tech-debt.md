# 技術的負債の一覧

精度そのものではなく、**構造・安全性・冗長さ**の問題を置く場所。
精度改善の経緯は `accuracy-improvements.md` にある。

各項目は「何が問題か → 具体的な箇所 → 直すとどうなるか → 今やらない理由」の順で書く。
着手するときに現物を読み直さなくて済む粒度を目安にする。

---

## S. セキュリティ

### S1. API に認証が無く、全インターフェースで待ち受けている

`api/__main__.py` が `host="0.0.0.0"` で uvicorn を起動する。認証も無い。
同一LAN上の別端末から `PUT /api/settings` を叩けば、LLMプロバイダのAPIキーを
書き換えられる。`GET` はマスクされるので読み出しはできないが、書き換えは通る。

- **直し方（小）**: 既定を `127.0.0.1` にし、外に出したい人だけ環境変数で
  `HOST=0.0.0.0` を指定する形にする
- **直し方（大）**: トークン認証を足す。ローカル前提のアプリなので過剰かもしれない
- **今やらない理由**: 判断待ち。2回報告して返答をもらえていない

### S2. APIキーがDBに平文で入る

`agent_studio.db` の `settings` テーブルと `llm_profiles` テーブルに平文で入る。

現状の緩和策は `docs/web-ui.md`「APIキーの置き方」にまとめてある
（0600 / リポジトリ外へ移動 / サンドボックスへのマウント禁止 / 却下の可視化）。
同一ユーザーで動くプロセスからは読める、という限界は残ったまま。

- **直し方**: OSのキーチェーン（`keyring`）へ寄せる。ただし headless / WSL では
  バックエンドが無いことがあり、フォールバックの設計が要る
- **今やらない理由**: 次回スコープ

---

## R. 冗長・重複

### R1. `run_agent` / `run_agent_background` / `run_agent_streaming` の3重実装

`executor.py`。同じ「エージェントを1回動かす」処理が3つある。
`run_agent` は実質どこからも呼ばれていない疑いがある（要確認）。

- **直し方**: ストリーミング版を土台にし、非ストリーミングはそれを
  消費するだけの薄い関数にする
- **注意**: 3つで初期状態の作り方が微妙に違う可能性がある。
  `make_initial_state` へ集約した経緯（`state.py` のコメント）と同じ事故が
  ここにも残っていないか、先に差分を取ること

### R2. 表示用パーサの重複（フェーズ4-1で対応予定）

`executor.parse_history_for_display` と `executor.run_agent_streaming` に
THOUGHT/ACTION の正規表現が二重にある（`executor.py:297,303,308` と `:348,355,361`）。
`graph.parse_action` にも近い実装がある。

DONE本文の抽出は `parsing.py` へ集約済み（フェーズ1-2）。残りはここ。
なお `executor.py:308,361` の `DONE:\s*(.+)` は行頭限定になっておらず、
`parsing.parse_done` とは別の結果を返す。表示専用なので最終回答には影響しないが、
**画面に出る本文と実際の回答が食い違う**可能性はある。

### R3. `_normalize_query` / `is_duplicate_query` の重複（フェーズ4-2で対応予定）

`graph.py` と `graph_research.py` に同一内容で存在する。

### R4. プロンプト定数が Python に埋まっている

`graph_research.py` の `_SEARCH_*` / `_WRITE_*` など。
外部ファイル化すると差分が読みやすくなる一方、プロンプトとコードの
対応が追いにくくなる面もある。**やるかどうかから判断が要る。**

---

## D. 構造・依存

### D1. `db.py` の末尾で `init_db()` を実行している（import 副作用）

`import db` しただけでファイルが作られ、マイグレーションが走る。
テストや `--help` を出すだけの経路でも走ってしまう。

- **直し方**: 明示呼び出しに変える。`api/main.py` の startup と
  `app_streamlit.py` は既に呼んでいるので、そこだけ残す
- **注意**: `tools.py` など「DBがある前提」で書かれた経路が
  暗黙にこの副作用へ依存していないか確認が要る

### D2. DBアクセス経路が分散している

`tools.py`（`:315-342`）と `executor._build_tool_context` がそれぞれ
直接DBを触る。`settings_store.py` を作って読み取りは集約したが、
ツール実行まわりは残っている。

### D3. `ai_creator.AGENT_PROJECT_PATH` 等、旧構成の残骸

2リポジトリ構成の名残。現在の `paths.py` 中心の構成と二重になっている。

### D4. スケジューラが FastAPI の startup に同居（フェーズ7-2で対応予定）

`BackgroundScheduler` が API プロセス内で動く。API を落とすと定期実行も止まる。
`start()` が内部で `load_schedules()` を呼んだ直後に `main.py` が二重に呼んでいる
箇所もある。

---

## P. 性能（フェーズ2〜5・7で対応予定）

記録のみ。担当フェーズが決まっているものはそちらで消化する。

| 項目 | 箇所 | フェーズ |
|---|---|---|
| SSE の二重パース | `api/events.py` | 2-1 |
| `get_executions` の `SELECT *` / `started_at` 索引 | `db.py` | 2-2 |
| イメージハッシュ・`get_llm` のキャッシュ | `sandbox.py` / `config.py` | 3-1 |
| `verify_tool` / Dispatcher の条件付き化、レビュアー並列化 | `graph.py` / `reviewers.py` | 5-1 |
| 進捗保存が毎ノード全体再シリアライズ（O(n²)） | `db.update_execution_progress` | 7-1 |
| SQLite が WAL でない | `db.py` | 7-2 |

---

## U. UI

### U1. Streamlit 版と React 版の二重メンテ（フェーズ6で判断）

`ui/` と `app_streamlit.py` が残っている。廃止の前に
「React 側でどの機能が未移行か」の棚卸しが要る。
