# tools.py — エージェントが使うツール群
import os
import json
import sqlite3
import subprocess
import tempfile
import urllib.request
import urllib.parse
from paths import DB_PATH as AGENT_STUDIO_DB  # db.py（UI側）と同一のDBを指す

WORKSPACE = "/tmp/agent_workspace"


def _read_settings() -> dict:
    """agent-studioのDBから設定を読み込む。失敗時は空dict。"""
    try:
        if not os.path.exists(AGENT_STUDIO_DB):
            return {}
        conn = sqlite3.connect(AGENT_STUDIO_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        conn.close()
        return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {}


def read_file(path: str) -> str:
    """ファイルを読む"""
    try:
        if not os.path.isabs(path):
            path = os.path.join(WORKSPACE, path)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"エラー: {e}"


def write_file(arg: str) -> str:
    """ファイルに書き込む。argは 'path|content' 形式"""
    try:
        if "|" not in arg:
            return "エラー: write_fileの引数は 'path|content' 形式です"
        path, content = arg.split("|", 1)
        path = path.strip()
        if not os.path.isabs(path):
            path = os.path.join(WORKSPACE, path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"書き込み成功: {path} ({len(content)}文字)"
    except Exception as e:
        return f"エラー: {e}"


def list_directory(path: str = "") -> str:
    """ディレクトリの一覧を返す"""
    try:
        if not path:
            path = WORKSPACE
        if not os.path.isabs(path):
            path = os.path.join(WORKSPACE, path)
        if not os.path.exists(path):
            return f"パスが存在しません: {path}"
        entries = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isdir(full):
                entries.append(f"[DIR] {name}")
            else:
                size = os.path.getsize(full)
                entries.append(f"[FILE] {name} ({size} bytes)")
        return "\n".join(entries) if entries else "(空のディレクトリ)"
    except Exception as e:
        return f"エラー: {e}"


def run_shell(command: str) -> str:
    """シェルコマンドを実行する（VM上で直接実行）"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=WORKSPACE,
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        return output[:2000] if output else "(出力なし)"
    except subprocess.TimeoutExpired:
        return "エラー: タイムアウト（30秒）"
    except Exception as e:
        return f"エラー: {e}"


def fetch_url(url: str) -> str:
    """URLを取得する"""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        return content[:3000]
    except Exception as e:
        return f"エラー: {e}"


# ===== Web検索: プロバイダごとの実装 =====

def _search_duckduckgo(query: str) -> str:
    """DuckDuckGoで検索（APIキー不要、デフォルト）"""
    try:
        from ddgs import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=3):
                snippet = r.get('body', '')[:200]
                results.append(
                    f"タイトル: {r.get('title', '')}\n"
                    f"URL: {r.get('href', '')}\n"
                    f"概要: {snippet}"
                )
        if results:
            return "\n---\n".join(results)
        return "検索結果が見つかりませんでした。"
    except Exception as e:
        return f"検索エラー: {e}"


def _search_tavily(query: str, api_key: str) -> str:
    """Tavily APIで検索（AIエージェント向け、無料枠あり）"""
    try:
        body = json.dumps({
            "api_key": api_key,
            "query": query,
            "max_results": 3,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.tavily.com/search",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = []
        for r in data.get("results", [])[:3]:
            snippet = (r.get("content", "") or "")[:250]
            results.append(
                f"タイトル: {r.get('title', '')}\n"
                f"URL: {r.get('url', '')}\n"
                f"概要: {snippet}"
            )
        if results:
            return "\n---\n".join(results)
        return "検索結果が見つかりませんでした。"
    except Exception as e:
        return f"検索エラー（Tavily）: {e}"


def _search_google(query: str, api_key: str, cse_id: str) -> str:
    """Google Custom Search JSON APIで検索"""
    try:
        params = urllib.parse.urlencode({
            "key": api_key,
            "cx": cse_id,
            "q": query,
            "num": 3,
        })
        url = f"https://www.googleapis.com/customsearch/v1?{params}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = []
        for r in data.get("items", [])[:3]:
            snippet = (r.get("snippet", "") or "")[:250]
            results.append(
                f"タイトル: {r.get('title', '')}\n"
                f"URL: {r.get('link', '')}\n"
                f"概要: {snippet}"
            )
        if results:
            return "\n---\n".join(results)
        return "検索結果が見つかりませんでした。"
    except Exception as e:
        return f"検索エラー（Google）: {e}"


def _search_brave(query: str, api_key: str) -> str:
    """Brave Search APIで検索"""
    try:
        params = urllib.parse.urlencode({"q": query, "count": 3})
        url = f"https://api.search.brave.com/res/v1/web/search?{params}"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = []
        for r in data.get("web", {}).get("results", [])[:3]:
            snippet = (r.get("description", "") or "")[:250]
            results.append(
                f"タイトル: {r.get('title', '')}\n"
                f"URL: {r.get('url', '')}\n"
                f"概要: {snippet}"
            )
        if results:
            return "\n---\n".join(results)
        return "検索結果が見つかりませんでした。"
    except Exception as e:
        return f"検索エラー（Brave）: {e}"


def web_search(query: str) -> str:
    """
    Web検索を実行する。
    使用するプロバイダはagent-studioの「⚙️ 設定」タブで切り替え可能。
    デフォルトはAPIキー不要のDuckDuckGo。
    """
    settings = _read_settings()
    provider = settings.get("search_provider", "duckduckgo")

    if provider == "tavily":
        api_key = settings.get("tavily_api_key", "").strip()
        if api_key:
            return _search_tavily(query, api_key)
        # キー未設定ならDuckDuckGoにフォールバック

    elif provider == "google":
        api_key = settings.get("google_api_key", "").strip()
        cse_id = settings.get("google_cse_id", "").strip()
        if api_key and cse_id:
            return _search_google(query, api_key, cse_id)

    elif provider == "brave":
        api_key = settings.get("brave_api_key", "").strip()
        if api_key:
            return _search_brave(query, api_key)

    return _search_duckduckgo(query)


def suggest_keywords(query: str) -> str:
    """指定キーワードに対するサジェスト（Google Suggest API）を取得する"""
    try:
        url = f"https://www.google.com/complete/search?client=firefox&hl=ja&q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list):
            suggestions = data[1][:10]
            if suggestions:
                return f"「{query}」に関連してよく検索されるキーワード:\n" + "\n".join(
                    f"- {s}" for s in suggestions
                )
        return f"「{query}」のサジェストが取得できませんでした。"
    except Exception as e:
        return f"サジェスト取得エラー: {e}"


def list_saved_tools(arg: str = "") -> str:
    """agent-studioに保存されている検証済みツール（Type1）の一覧を取得する"""
    try:
        if not os.path.exists(AGENT_STUDIO_DB):
            return "保存済みツールDBが見つかりません。agent-studioが初期化されていない可能性があります。"
        conn = sqlite3.connect(AGENT_STUDIO_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, name, description FROM tools WHERE status = 'verified' ORDER BY name"
        ).fetchall()
        conn.close()

        if not rows:
            return "検証済みのツールはありません。"

        lines = ["利用可能な検証済みツール:"]
        for r in rows:
            desc = r["description"] or "（説明なし）"
            lines.append(f"- ID: {r['id']} / 名前: {r['name']} / 説明: {desc}")
        return "\n".join(lines)
    except Exception as e:
        return f"エラー: {e}"


def run_saved_tool(tool_id: str) -> str:
    """agent-studioに保存されている検証済みツール（Type1）を実行して結果を返す"""
    try:
        if not os.path.exists(AGENT_STUDIO_DB):
            return "エラー: agent-studioのDBが見つかりません"

        conn = sqlite3.connect(AGENT_STUDIO_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, name, code, status FROM tools WHERE id = ?", (tool_id.strip(),)
        ).fetchone()
        conn.close()

        if not row:
            return f"エラー: ツール ID '{tool_id}' が見つかりません。list_saved_toolsで一覧を確認してください。"
        if row["status"] != "verified":
            return f"エラー: ツール '{row['name']}' (ID: {tool_id}) は未検証です。検証済みのツールのみ実行できます。"

        os.makedirs(WORKSPACE, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.py', delete=False,
            encoding='utf-8', dir=WORKSPACE
        ) as f:
            f.write(row["code"])
            path = f.name

        try:
            result = subprocess.run(
                ["python3", path],
                capture_output=True, text=True,
                timeout=120, cwd=WORKSPACE,
            )
            output_parts = [f"[ツール '{row['name']}' 実行結果]"]
            if result.returncode == 0:
                output_parts.append("ステータス: 成功")
                if result.stdout:
                    output_parts.append(f"出力:\n{result.stdout}")
                else:
                    output_parts.append("（出力なし）")
            else:
                output_parts.append(f"ステータス: エラー (returncode={result.returncode})")
                if result.stderr:
                    output_parts.append(f"stderr:\n{result.stderr}")
                if result.stdout:
                    output_parts.append(f"stdout:\n{result.stdout}")
            return "\n".join(output_parts)
        finally:
            os.unlink(path)

    except subprocess.TimeoutExpired:
        return f"エラー: ツール実行がタイムアウトしました（120秒）"
    except Exception as e:
        return f"エラー: {e}"


# ツールレジストリ
TOOL_REGISTRY = {
    # verifiable: True のツールだけが、graph.py の verify_tool ノードで
    # 「結果がクエリの意図に答えているか」をLLMに判定させる対象になる。
    # run_shell/fetch_url/run_saved_tool等は「関連性」という概念自体が
    # 存在しない（成功/失敗はあっても的外れという状態がない）ため対象外。
    "read_file": {"fn": read_file, "permission": "read", "verifiable": False},
    "write_file": {"fn": write_file, "permission": "write", "verifiable": False},
    "list_directory": {"fn": list_directory, "permission": "read", "verifiable": False},
    "run_shell": {"fn": run_shell, "permission": "execute", "verifiable": False},
    "fetch_url": {"fn": fetch_url, "permission": "read", "verifiable": False},
    "web_search": {"fn": web_search, "permission": "read", "verifiable": True},
    "suggest_keywords": {"fn": suggest_keywords, "permission": "read", "verifiable": True},
    "list_saved_tools": {"fn": list_saved_tools, "permission": "read", "verifiable": False},
    "run_saved_tool": {"fn": run_saved_tool, "permission": "execute", "verifiable": False},
}


def is_tool_verifiable(name: str) -> bool:
    """指定したツールが verify_tool ノードでの品質チェック対象かどうか"""
    tool = TOOL_REGISTRY.get(name)
    return bool(tool and tool.get("verifiable", False))


def get_tool_fn(name: str):
    """ツール名から関数を取得する"""
    tool = TOOL_REGISTRY.get(name)
    return tool["fn"] if tool else None


def get_tool_names() -> list[str]:
    """登録されているツール名の一覧"""
    return list(TOOL_REGISTRY.keys())


def build_workspace_context() -> str:
    """作業ディレクトリの状態を文字列で返す"""
    if not os.path.exists(WORKSPACE):
        return "作業ディレクトリが存在しません。"
    try:
        entries = []
        for name in sorted(os.listdir(WORKSPACE)):
            full = os.path.join(WORKSPACE, name)
            if os.path.isdir(full):
                entries.append(f"[DIR] {name}/")
            else:
                size = os.path.getsize(full)
                entries.append(f"[FILE] {name} ({size} bytes)")
        if entries:
            return f"作業ディレクトリ: {WORKSPACE}\n" + "\n".join(entries)
        return f"作業ディレクトリ {WORKSPACE} は空です。"
    except Exception as e:
        return f"作業ディレクトリの取得に失敗: {e}"