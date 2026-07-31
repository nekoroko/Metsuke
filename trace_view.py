# trace_view.py — ノード遷移トレースの整形
#
# graph.py の _with_trace が積んだレコードを、画面表示用の行と
# コピー用のテキストに変換する。streamlit に依存させないので、
# UI からもエクスポートからもテストからも同じ関数を使える。

import json

# ノード名は内部の識別子そのままだと何をする場所か分かりにくいので、
# 画面ではアイコンと役割を添える。
NODE_LABELS = {
    # ReActループ（graph.py）
    "react": "🤖 react（思考・行動）",
    "verify_tool": "🔍 verify_tool（ツール結果の検算）",
    # 両方で使う
    "correct": "🛠 correct（数値の自動検証）",
    "critic": "🧐 critic（レビュー）",
    # リサーチ用ステートマシン（graph_research.py）
    "plan": "🗂 plan（調査項目の分解）",
    "search": "🔎 search（検索）",
    "digest": "📄 digest（本文取得・抜粋）",
    "gap": "🧭 gap（充足判定）",
    "compose": "✍️ compose（レポート執筆）",
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


def cost_text(entry: dict) -> str:
    """
    そのノードの所要時間とトークンを1行にする。

    計測が無い（＝この機能より前に走った）実行では空文字を返す。
    0 を作らない。「計測していない」と「0だった」は違う。
    """
    from metrics import fmt_ms, fmt_tokens

    if entry.get("elapsed_ms") is None:
        return ""
    parts = [fmt_ms(entry.get("elapsed_ms"))]
    if entry.get("llm_calls"):
        parts.append(f"LLM {entry['llm_calls']}回 {fmt_ms(entry.get('llm_ms'))}")
        parts.append(fmt_tokens(entry))
    return " / ".join(parts)


def totals_text(trace: list[dict]) -> str:
    """実行全体の合計を1行にする。"""
    from metrics import fmt_ms, fmt_tokens

    measured = [e for e in trace if e.get("elapsed_ms") is not None]
    if not measured:
        return "（この実行では所要時間・トークンを計測していません）"
    total = {k: sum(e.get(k) or 0 for e in measured)
             for k in ("elapsed_ms", "llm_ms", "llm_calls",
                       "input_tokens", "output_tokens", "reasoning_tokens",
                       "missing_usage")}
    share = (f"{round(total['llm_ms'] / total['elapsed_ms'] * 100)}%"
             if total["elapsed_ms"] else "—")
    return (
        f"合計 {fmt_ms(total['elapsed_ms'])}"
        f"（うちLLM待ち {fmt_ms(total['llm_ms'])} = {share}）"
        f" / LLM {total['llm_calls']}回 / {fmt_tokens(total)}"
        + (f" / thinking {total['reasoning_tokens']:,}"
           if total["reasoning_tokens"] else "")
    )


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
            "所要・トークン": cost_text(e),
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
        cost = cost_text(e)
        if cost:
            lines.append(f"    所要: {cost}")
    return lines


def skipped_count(trace: list[dict]) -> int:
    return sum(1 for e in trace if e.get("skipped"))
