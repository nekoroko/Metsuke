# tool_runtime.py — ツール実行時ランタイム（AI生成ツールが使えるライブラリ）の定義
#
# requirements-tools.txt を唯一の定義元として読み取り、
# 以下の用途に必要な形へ変換する。
#
#   - ai_creator.py … AIに提示する許可ライブラリ一覧の文言
#   - sandbox.py    … イメージに焼き込む内容ハッシュ（再ビルド要否の判定用）
#
# これにより、ライブラリの追加・削除は requirements-tools.txt の1箇所で完結する。

import hashlib
import os
import re
import subprocess
import sys

from paths import BASE_DIR

REQUIREMENTS_PATH = os.path.join(BASE_DIR, "requirements-tools.txt")

INSTALL_TIMEOUT = 900


def read_entries() -> list[dict]:
    """
    requirements-tools.txt を解析する。

    戻り値の各要素:
      {"spec": "pandas>=2.0", "name": "pandas", "comment": "データ処理・集計"}

    spec    … pip に渡す指定そのもの（バージョン指定を含む）
    name    … パッケージ名のみ
    comment … 行末のインラインコメント。AIへの説明文として使う
    """
    try:
        with open(REQUIREMENTS_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []

    entries = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        spec, _, comment = line.partition("#")
        spec = spec.strip()
        if not spec:
            continue
        # "pandas>=2.0" や "requests[socks]" から名前部分だけを取り出す
        name = re.split(r"[<>=!~\[;]", spec, maxsplit=1)[0].strip()
        entries.append({"spec": spec, "name": name, "comment": comment.strip()})
    return entries


def format_for_prompt() -> str:
    """AIに提示する許可ライブラリ一覧（箇条書き）を生成する"""
    entries = read_entries()
    if not entries:
        return "- 標準ライブラリのみ使用可能"

    lines = ["- 標準ライブラリ全般"]
    for e in entries:
        lines.append(f"- {e['name']}（{e['comment']}）" if e["comment"] else f"- {e['name']}")
    lines.append("- 上記以外のライブラリは使用不可")
    return "\n".join(lines)


def format_inline() -> str:
    """許可ライブラリ一覧を1行で表現する（短いプロンプト用）"""
    names = [e["name"] for e in read_entries()]
    if not names:
        return "標準ライブラリのみ"
    return "標準ライブラリ全般、" + "、".join(names)


def requirements_hash() -> str:
    """
    ライブラリ定義の内容ハッシュ。

    サンドボックスイメージのラベルに焼き込み、定義が変わったことを検知して
    自動再ビルドするために使う（「ライブラリを足したのに再ビルドを忘れて
    古いイメージで動く」を防ぐ）。

    順序の入れ替えや説明文の変更だけで無駄な再ビルドが走らないよう、
    パッケージ指定をソートしたものからハッシュを取る。
    """
    specs = sorted(e["spec"] for e in read_entries())
    return hashlib.sha256("\n".join(specs).encode("utf-8")).hexdigest()[:16]


# ===== UIからの編集用 =====

def read_raw() -> str:
    """requirements-tools.txt の中身をそのまま返す"""
    try:
        with open(REQUIREMENTS_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def write_raw(text: str) -> None:
    """
    requirements-tools.txt を書き換える。

    設定をDBに持たせず、あくまでファイルを唯一の定義元とするのが要点。
    Containerfile（COPY + pip install -r）と requirements.txt（-r）が
    実ファイルを読むため、DBに持たせると定義元が二重化してしまう。
    UIはこのファイルのエディタとして振る舞う。
    """
    if not text.endswith("\n"):
        text += "\n"
    with open(REQUIREMENTS_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def check_host_packages() -> list[dict]:
    """
    ホスト側（このアプリを動かしているvenv）に各パッケージが
    インストール済みかどうかを調べる。

    import名ではなく配布名で判定する（beautifulsoup4 は bs4 としてimportするため、
    import名で見ると誤判定する）。importlib.metadata は配布名で引けるので、
    この差を意識せずに済む。
    """
    from importlib import metadata

    results = []
    for e in read_entries():
        try:
            version = metadata.version(e["name"])
            installed = True
        except Exception:
            version = ""
            installed = False
        results.append({"name": e["name"], "installed": installed, "version": version})
    return results


def install_to_host() -> dict:
    """
    requirements-tools.txt をホストのvenvにインストールする。

    検証済みツール（Type1）は executor.run_tool が subprocess で
    毎回新しい python3 を起動して実行するため、ここでインストールすれば
    Streamlitを再起動しなくても次回のツール実行から反映される。

    戻り値: {"ok": bool, "message": str}
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS_PATH],
            capture_output=True, text=True, timeout=INSTALL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"インストールがタイムアウトしました（{INSTALL_TIMEOUT}秒）"}
    except Exception as e:
        return {"ok": False, "message": f"インストールを実行できませんでした: {e}"}

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "")[-2000:]
        return {"ok": False, "message": f"pip install に失敗しました。\n\n{detail}"}

    return {"ok": True, "message": (result.stdout or "")[-2000:]}
