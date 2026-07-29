# trace_view.py — ノード遷移トレースの整形
#
# graph.py の _with_trace が積んだレコードを、画面表示用の行と
# コピー用のテキストに変換する。streamlit に依存させないので、
# UI からもエクスポートからもテストからも同じ関数を使える。

import json

# ノード名は内部の識別子そのままだと何をする場所か分かりにくいので、
# 画面ではアイコンと役割を添える。
NODE_LABELS = {
    "react": "🤖 react（思考・行動）",
    "verify_tool": "🔍 verify_tool（ツール結果の検算）",
    "correct": "🛠 correct（数値の自動検証）",
    "critic": "🧐 critic（レビュー）",
}

_END = "(終了)"


def node_label(node: str) -> str:
    return NODE_LABELS.get(node, node or "?")


def parse_trace(raw) -> list[dict]:
    """
    DBのtrace列（JSON文字列）またはstateのlistを、行のリストに正規化する。

    履歴を持たない過去の実行ではNULLなので、その場合は空リストを返す。
    """
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict)]


def counters_text(entry: dict) -> str:
    """そのノードを抜けた時点のカウンタを1行にまとめる。"""
    return (
        f"step={entry.get('step_count', 0)}/"
        f"verify={entry.get('tool_verify_count', 0)}/"
        f"correct={entry.get('correction_count', 0)}/"
        f"critique={entry.get('critique_count', 0)}"
    )


def trace_rows(trace: list[dict]) -> list[dict]:
    """st.dataframe に渡すための行を作る。"""
    rows = []
    for e in trace:
        rows.append({
            "#": e.get("seq", 0),
            "遷移元": e.get("from", ""),
            "ノード": node_label(e.get("node", "")),
            "実行": "スキップ" if e.get("skipped") else "実行",
            "内容": e.get("summary", ""),
            "補足": e.get("note", ""),
            "次": e.get("next") or _END,
            "status": e.get("status", ""),
            "カウンタ": counters_text(e),
        })
    return rows


def format_trace_lines(trace: list[dict]) -> list[str]:
    """コピー・ダウンロード用のプレーンテキスト行を作る。"""
    lines = []
    for e in trace:
        mark = "スキップ" if e.get("skipped") else "実行"
        lines.append(
            f"#{e.get('seq', 0)} {e.get('from', '')} → {e.get('node', '')} "
            f"[{mark}] : {e.get('summary', '')}"
        )
        if e.get("note"):
            lines.append(f"    理由: {e['note']}")
        lines.append(
            f"    次: {e.get('next') or _END} / status={e.get('status', '')} / "
            f"{counters_text(e)}"
        )
    return lines


def skipped_count(trace: list[dict]) -> int:
    return sum(1 for e in trace if e.get("skipped"))
