# db.py — AI Agent Studio データベース（Type 1 + Type 2対応）
import sqlite3
import uuid
import json
from datetime import datetime
from paths import DB_PATH  # 実行CWDに依存しない絶対パス（paths.py参照）

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
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
            created_at  TEXT,
            updated_at  TEXT
        )
    """)

    # 既存テーブルにカラムがなければ追加（マイグレーション）
    cursor = conn.execute("PRAGMA table_info(agent_tasks)")
    cols = [row[1] for row in cursor.fetchall()]
    if "allowed_tool_ids" not in cols:
        conn.execute("ALTER TABLE agent_tasks ADD COLUMN allowed_tool_ids TEXT")

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
            started_at  TEXT,
            finished_at TEXT
        )
    """)

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
    }
    for k, v in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
        )

    conn.commit()
    conn.close()

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

def add_agent_task(name, description, task_prompt, allowed_tool_ids=None):
    task_id = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    conn = get_connection()
    conn.execute(
        "INSERT INTO agent_tasks (id, name, description, task_prompt, allowed_tool_ids, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, name, description, task_prompt,
         json.dumps(allowed_tool_ids) if allowed_tool_ids else None,
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
    for key in ("name", "description", "task_prompt"):
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

def update_execution_progress(exec_id: str, history: list):
    """実行中の履歴を逐次更新する（バックグラウンド実行の進捗用）"""
    conn = get_connection()
    conn.execute(
        "UPDATE executions SET history = ? WHERE id = ?",
        (json.dumps(history, ensure_ascii=False) if history else None, exec_id)
    )
    conn.commit()
    conn.close()

def get_execution(exec_id: str) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM executions WHERE id = ?", (exec_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def finish_execution(exec_id, status, stdout="", stderr="", history=None):
    conn = get_connection()
    conn.execute(
        "UPDATE executions SET status=?, stdout=?, stderr=?, history=?, finished_at=? WHERE id=?",
        (status, stdout, stderr, json.dumps(history, ensure_ascii=False) if history else None,
         datetime.now().isoformat(), exec_id)
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