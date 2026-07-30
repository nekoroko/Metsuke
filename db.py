# db.py — Metsuke のデータベース（Type 1 + Type 2対応）
# ファイル名 agent_studio.db は旧名のまま。既存環境の移行を避けるため。
import sqlite3
import uuid
import json
from datetime import datetime
from paths import DB_PATH, ensure_db_location, secure_db_file  # paths.py参照

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    # 置き場所の用意と旧位置からの引っ越し。接続より前に済ませる
    ensure_db_location()
    conn = get_connection()

    # ツールライブラリ（Type 1用：保存済みコード）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tools (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT,
            code        TEXT NOT NULL,
            status      TEXT DEFAULT 'draft',
            created_at  TEXT,
            updated_at  TEXT
        )
    """)

    # タスクテンプレート（Type 2用：エージェントに渡すタスク文）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT,
            task_prompt TEXT NOT NULL,
            allowed_tool_ids TEXT,
            graph_kind  TEXT,
            created_at  TEXT,
            updated_at  TEXT
        )
    """)

    # 既存テーブルにカラムがなければ追加（マイグレーション）
    cursor = conn.execute("PRAGMA table_info(agent_tasks)")
    cols = [row[1] for row in cursor.fetchall()]
    if "allowed_tool_ids" not in cols:
        conn.execute("ALTER TABLE agent_tasks ADD COLUMN allowed_tool_ids TEXT")
    # graph_kind: 空/NULLなら設定画面の既定に従う（graphs.resolve_kind）
    if "graph_kind" not in cols:
        conn.execute("ALTER TABLE agent_tasks ADD COLUMN graph_kind TEXT")
    # どのLLMプロファイルで実行するか（空なら設定画面の既定に従う）
    if "llm_profile_id" not in cols:
        conn.execute("ALTER TABLE agent_tasks ADD COLUMN llm_profile_id TEXT")

    # スケジュール（Type 1/Type 2両方）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id          TEXT PRIMARY KEY,
            exec_type   TEXT NOT NULL DEFAULT 'tool',
            target_id   TEXT NOT NULL,
            cron_expr   TEXT NOT NULL,
            enabled     INTEGER DEFAULT 1,
            created_at  TEXT
        )
    """)

    # 実行履歴（統合）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS executions (
            id          TEXT PRIMARY KEY,
            exec_type   TEXT NOT NULL,
            target_id   TEXT NOT NULL,
            target_name TEXT,
            schedule_id TEXT,
            trigger     TEXT DEFAULT 'manual',
            status      TEXT DEFAULT 'running',
            stdout      TEXT,
            stderr      TEXT,
            history     TEXT,
            trace       TEXT,
            graph_kind  TEXT,
            started_at  TEXT,
            finished_at TEXT
        )
    """)

    # 既存DBに列が無ければ追加する（agent_tasks と同じマイグレーション方式）
    cursor = conn.execute("PRAGMA table_info(executions)")
    exec_cols = [row[1] for row in cursor.fetchall()]
    if "trace" not in exec_cols:
        conn.execute("ALTER TABLE executions ADD COLUMN trace TEXT")
    # どのグラフで実行したか。履歴画面の見出しに使う
    if "graph_kind" not in exec_cols:
        conn.execute("ALTER TABLE executions ADD COLUMN graph_kind TEXT")
    # どのモデルで実行したか（JSON）。プロファイルIDだけでは、後から
    # 編集・削除されたときに何で実行したか分からなくなるため値を写して持つ。
    # api_key は入れない（llm_profiles.SNAPSHOT_FIELDS を参照）
    if "llm_info" not in exec_cols:
        conn.execute("ALTER TABLE executions ADD COLUMN llm_info TEXT")

    # AI生成セッション
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_sessions (
            id          TEXT PRIMARY KEY,
            prompt      TEXT NOT NULL,
            history     TEXT,
            result_code TEXT,
            status      TEXT DEFAULT 'running',
            created_at  TEXT
        )
    """)

    # LLM接続設定（複数保存して実行時に選ぶ）
    #
    # 以前は settings の local_* / api_* に1組だけ持っていた。モデルを
    # 変えて比べるには上書きするしかなく、前の設定が失われていた。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_profiles (
            id                TEXT PRIMARY KEY,
            name              TEXT NOT NULL,
            provider          TEXT,
            provider_kind     TEXT,
            base_url          TEXT,
            model             TEXT,
            api_key           TEXT,
            max_output_tokens TEXT,
            disable_thinking  TEXT,
            created_at        TEXT
        )
    """)

    # 設定（LLMプロバイダ切り替え等のキーバリューストア）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # デフォルト設定を初期投入（既に存在すれば無視）
    defaults = {
        "llm_provider": "local",  # "local" or "api"
        "local_base_url": "http://10.0.2.2:1234/v1",
        "local_model": "gemma-4-12b-qat",
        "local_api_key": "lm-studio",
        "api_base_url": "",
        "api_model": "",
        "api_key": "",
        # 検索プロバイダ設定（デフォルトはAPIキー不要のDuckDuckGo）
        "search_provider": "duckduckgo",  # "duckduckgo" | "tavily" | "google" | "brave"
        "tavily_api_key": "",
        "google_api_key": "",
        "google_cse_id": "",
        "brave_api_key": "",
        # Thinking/Reasoning制御（デフォルトは無効化=高速・省トークン）
        "disable_thinking": "true",
        # APIプロバイダの種別。"openai_compatible" または "anthropic"
        # Anthropicはネイティブのlangchain_anthropicを使う（OpenAI互換は非推奨のため）
        "api_provider_kind": "openai_compatible",
        # 最大出力トークン数。「⚙️ 設定」タブの接続テストで自動取得を試み、
        # 失敗した場合はユーザーが手入力する。プロバイダ環境ごとに実際の上限が
        # 異なる（ローカルはサーバー設定次第、APIはモデル次第）ため、
        # コード側でハードコードした決め打ち値に頼らない設計にしている。
        # 値が空の場合のみ、コード内の保守的な既定値にフォールバックする。
        #
        # デフォルトを1500→3000に引き上げた経緯: Thinking機能を持つ
        # モデル（Gemma 4等）で、内部思考だけで1500トークンを使い切り
        # 実際の回答が空になる不具合が実際に観測されたため。
        # ただし固定の「最低8192」等は強制しない
        # （Context Length自体が4096程度の環境も多く、環境ごとに
        #  実際に使える上限は異なるため。UIでは警告のみ表示する）。
        "local_max_output_tokens": "3000",
        "api_max_output_tokens": "4000",
        # サンドボックスへの追加マウント（1行1マウント、host:container:ro|rw）。
        # 全実行がコンテナ内で行われるため、作業ディレクトリ以外のホストファイルに
        # 触るツールはここでマウントを明示する必要がある。
        "sandbox_extra_mounts": "",
        # 既定のLLMプロファイルID。初回は下の移行処理が埋める。
        "default_llm_profile_id": "",
    }
    for k, v in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
        )

    conn.commit()
    _migrate_llm_profile(conn)
    conn.commit()
    conn.close()
    # sqlite が作ったファイルは既定 0644。APIキーが入るので 0600 に落とす
    secure_db_file()


def _migrate_llm_profile(conn):
    """
    従来の単一LLM設定を、プロファイル1件として移行する。

    設定が2箇所（settings の local_*/api_* と llm_profiles）にあると、
    どちらが効いているのか分からない状態が残る。プロファイル側へ一本化し、
    以後の編集はプロファイル画面だけにする。従来のキーは読まれなくなるが、
    移行元として消さずに残す（取り違えたときに戻せるように）。

    プロファイルが1件でもあれば何もしない（2回目以降の起動）。
    """
    row = conn.execute("SELECT COUNT(*) FROM llm_profiles").fetchone()
    if row and row[0]:
        return

    cur = conn.execute("SELECT key, value FROM settings")
    st = {k: v for k, v in cur.fetchall()}
    provider = (st.get("llm_provider") or "local").strip() or "local"
    kind = (st.get("api_provider_kind") or "openai_compatible").strip()
    if provider == "api":
        base_url = st.get("api_base_url") or ""
        model = st.get("api_model") or ""
        api_key = st.get("api_key") or ""
        tokens = st.get("api_max_output_tokens") or ""
    else:
        base_url = st.get("local_base_url") or ""
        model = st.get("local_model") or ""
        api_key = st.get("local_api_key") or ""
        tokens = st.get("local_max_output_tokens") or ""

    # 移行元が空でも1件は作る。プロファイルが0件だと実行時に選ぶものが無い
    name = (model or "既定").strip() or "既定"
    pid = str(uuid.uuid4())[:8]
    conn.execute(
        "INSERT INTO llm_profiles (id, name, provider, provider_kind, base_url, "
        "model, api_key, max_output_tokens, disable_thinking, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pid, name, provider, kind, base_url, model, api_key, tokens,
         st.get("disable_thinking") or "true", datetime.now().isoformat()),
    )
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("default_llm_profile_id", pid),
    )


# --- LLMプロファイル操作 ---

def get_llm_profiles() -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM llm_profiles ORDER BY created_at, id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_llm_profile(profile_id: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM llm_profiles WHERE id = ?", (profile_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_llm_profile(name, provider="local", provider_kind="openai_compatible",
                    base_url="", model="", api_key="", max_output_tokens="",
                    disable_thinking="true") -> str:
    profile_id = str(uuid.uuid4())[:8]
    conn = get_connection()
    conn.execute(
        "INSERT INTO llm_profiles (id, name, provider, provider_kind, base_url, "
        "model, api_key, max_output_tokens, disable_thinking, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (profile_id, name, provider, provider_kind, base_url, model, api_key,
         str(max_output_tokens), disable_thinking, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return profile_id


def update_llm_profile(profile_id: str, **kwargs):
    allowed = {"name", "provider", "provider_kind", "base_url", "model",
               "api_key", "max_output_tokens", "disable_thinking"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn = get_connection()
    conn.execute(f"UPDATE llm_profiles SET {sets} WHERE id = ?",
                 (*[str(v) for v in fields.values()], profile_id))
    conn.commit()
    conn.close()


def delete_llm_profile(profile_id: str) -> bool:
    """
    プロファイルを削除する。最後の1件は消さない。

    0件になると実行時に選ぶものが無くなり、LLM呼び出しが即座に落ちる。
    参照しているタスク側の指定は残るが、resolve_profile_id が
    「存在しないIDなら既定に落とす」ので実行は止まらない。
    """
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM llm_profiles").fetchone()[0]
    if total <= 1:
        conn.close()
        return False
    conn.execute("DELETE FROM llm_profiles WHERE id = ?", (profile_id,))
    # 既定に選ばれていたなら、残っているものへ付け替える
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'default_llm_profile_id'").fetchone()
    if row and row[0] == profile_id:
        nxt = conn.execute(
            "SELECT id FROM llm_profiles ORDER BY created_at, id LIMIT 1").fetchone()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("default_llm_profile_id", nxt[0] if nxt else ""),
        )
    conn.commit()
    conn.close()
    return True


# --- 設定操作 ---

def get_setting(key: str, default: str = "") -> str:
    conn = get_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    if row is None:
        return default
    return row["value"] if row["value"] is not None else default

def get_all_settings() -> dict:
    conn = get_connection()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}

def set_setting(key: str, value: str):
    conn = get_connection()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value)
    )
    conn.commit()
    conn.close()

def set_settings(settings: dict):
    """複数の設定を一括更新する"""
    conn = get_connection()
    for k, v in settings.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (k, v)
        )
    conn.commit()
    conn.close()

# --- ツール操作 ---

def add_tool(name, description, code, status="draft"):
    tool_id = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    conn = get_connection()
    conn.execute(
        "INSERT INTO tools (id, name, description, code, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tool_id, name, description, code, status, now, now)
    )
    conn.commit()
    conn.close()
    return tool_id

def get_tool(tool_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM tools WHERE id = ?", (tool_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_tools():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM tools ORDER BY updated_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_verified_tools():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM tools WHERE status = 'verified' ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_tool(tool_id, **kwargs):
    conn = get_connection()
    sets = ["updated_at = ?"]
    vals = [datetime.now().isoformat()]
    for key in ("name", "description", "code", "status"):
        if key in kwargs:
            sets.append(f"{key} = ?")
            vals.append(kwargs[key])
    vals.append(tool_id)
    conn.execute(f"UPDATE tools SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()

def delete_tool(tool_id):
    conn = get_connection()
    conn.execute("DELETE FROM schedules WHERE target_id = ? AND exec_type = 'tool'", (tool_id,))
    conn.execute("DELETE FROM tools WHERE id = ?", (tool_id,))
    conn.commit()
    conn.close()

# --- エージェントタスク操作 ---

def add_agent_task(name, description, task_prompt, allowed_tool_ids=None, graph_kind=None,
                   llm_profile_id=None):
    task_id = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    conn = get_connection()
    conn.execute(
        "INSERT INTO agent_tasks (id, name, description, task_prompt, allowed_tool_ids, "
        "graph_kind, llm_profile_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, name, description, task_prompt,
         json.dumps(allowed_tool_ids) if allowed_tool_ids else None,
         graph_kind or None,
         llm_profile_id or None,
         now, now)
    )
    conn.commit()
    conn.close()
    return task_id

def get_agent_task(task_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_agent_tasks():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM agent_tasks ORDER BY updated_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_agent_task(task_id, **kwargs):
    conn = get_connection()
    sets = ["updated_at = ?"]
    vals = [datetime.now().isoformat()]
    for key in ("name", "description", "task_prompt", "graph_kind", "llm_profile_id"):
        if key in kwargs:
            sets.append(f"{key} = ?")
            vals.append(kwargs[key])
    if "allowed_tool_ids" in kwargs:
        sets.append("allowed_tool_ids = ?")
        ids = kwargs["allowed_tool_ids"]
        vals.append(json.dumps(ids) if ids else None)
    vals.append(task_id)
    conn.execute(f"UPDATE agent_tasks SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()

def delete_agent_task(task_id):
    conn = get_connection()
    conn.execute("DELETE FROM schedules WHERE target_id = ? AND exec_type = 'agent'", (task_id,))
    conn.execute("DELETE FROM agent_tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

# --- スケジュール操作 ---

def add_schedule(exec_type, target_id, cron_expr):
    sched_id = str(uuid.uuid4())[:8]
    conn = get_connection()
    conn.execute(
        "INSERT INTO schedules (id, exec_type, target_id, cron_expr, enabled, created_at) VALUES (?, ?, ?, ?, 1, ?)",
        (sched_id, exec_type, target_id, cron_expr, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    return sched_id

def get_all_schedules():
    """有効・無効を問わず全件。API の一覧はトグルも見せるため両方要る。"""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM schedules ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_all_enabled_schedules():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM schedules WHERE enabled = 1").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_schedules_by_target(target_id):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM schedules WHERE target_id = ?", (target_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def toggle_schedule(sched_id, enabled):
    conn = get_connection()
    conn.execute("UPDATE schedules SET enabled = ? WHERE id = ?", (1 if enabled else 0, sched_id))
    conn.commit()
    conn.close()

def delete_schedule(sched_id):
    conn = get_connection()
    conn.execute("DELETE FROM schedules WHERE id = ?", (sched_id,))
    conn.commit()
    conn.close()

# --- 実行履歴操作 ---

def add_execution(exec_type, target_id, target_name, trigger="manual", schedule_id=None):
    exec_id = str(uuid.uuid4())[:8]
    conn = get_connection()
    conn.execute(
        "INSERT INTO executions (id, exec_type, target_id, target_name, schedule_id, trigger, status, started_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
        (exec_id, exec_type, target_id, target_name, schedule_id, trigger, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    return exec_id

def set_execution_llm_info(exec_id: str, info: dict):
    """
    その実行で使ったモデルを記録する（履歴の「まとめてコピー」に出す）。

    info には api_key を入れない。履歴はそのまま外へ貼られる前提のテキストに
    なるため、鍵が1度混ざると貼った先すべてから消す必要が出る。
    写しの作成は llm_profiles.snapshot が担う。
    """
    conn = get_connection()
    conn.execute("UPDATE executions SET llm_info = ? WHERE id = ?",
                 (json.dumps(info, ensure_ascii=False) if info else None, exec_id))
    conn.commit()
    conn.close()


def set_execution_graph_kind(exec_id: str, graph_kind: str):
    """
    その実行で使ったグラフを記録する。

    実行を開始する側（scheduler / UI）はどのグラフになるか知らない
    ことがある（タスクの指定と設定の既定で決まるため）。解決した直後の
    executor から呼ぶ。
    """
    conn = get_connection()
    conn.execute("UPDATE executions SET graph_kind = ? WHERE id = ?",
                 (graph_kind or None, exec_id))
    conn.commit()
    conn.close()

def update_execution_progress(exec_id: str, history: list, trace: list = None):
    """実行中の履歴とノード遷移を逐次更新する（バックグラウンド実行の進捗用）"""
    conn = get_connection()
    conn.execute(
        "UPDATE executions SET history = ?, trace = ? WHERE id = ?",
        (json.dumps(history, ensure_ascii=False) if history else None,
         json.dumps(trace, ensure_ascii=False) if trace else None,
         exec_id)
    )
    conn.commit()
    conn.close()

def get_execution(exec_id: str) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM executions WHERE id = ?", (exec_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def finish_execution(exec_id, status, stdout="", stderr="", history=None, trace=None):
    """
    実行を終了状態にする。

    history / trace は「渡されたときだけ」上書きする。エラー終了の呼び出しは
    これらを渡さないため、無条件にNULLを書くと update_execution_progress が
    実行中に記録した履歴とノード遷移が消えてしまう。落ちた原因を追うのに
    一番必要な情報なので、消さずに残す。
    """
    sets = ["status=?", "stdout=?", "stderr=?"]
    vals = [status, stdout, stderr]
    if history is not None:
        sets.append("history=?")
        vals.append(json.dumps(history, ensure_ascii=False))
    if trace is not None:
        sets.append("trace=?")
        vals.append(json.dumps(trace, ensure_ascii=False))
    sets.append("finished_at=?")
    vals.append(datetime.now().isoformat())

    conn = get_connection()
    conn.execute(
        f"UPDATE executions SET {', '.join(sets)} WHERE id=?",
        (*vals, exec_id)
    )
    conn.commit()
    conn.close()

def get_executions(limit=30):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM executions ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# --- AIセッション ---

def add_ai_session(prompt):
    session_id = str(uuid.uuid4())[:8]
    conn = get_connection()
    conn.execute(
        "INSERT INTO ai_sessions (id, prompt, status, created_at) VALUES (?, ?, 'running', ?)",
        (session_id, prompt, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    return session_id

def finish_ai_session(session_id, status, history=None, result_code=None):
    conn = get_connection()
    conn.execute(
        "UPDATE ai_sessions SET status=?, history=?, result_code=? WHERE id=?",
        (status, json.dumps(history, ensure_ascii=False) if history else None, result_code, session_id)
    )
    conn.commit()
    conn.close()

init_db()