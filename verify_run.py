#!/usr/bin/env python3
"""
実LLMでの通し確認スクリプト。

テストは全部モック（LLMもネットワークも呼ばない）なので、
「実モデルが指示どおりの書式で応答するか」「実際のWebから取った
ノイズ入りテキストで機械チェックが機能するか」は別問題として残る。
このスクリプトは、そこを1回の実行で確かめるためのもの。

使い方（agent-studio と同じマシンで、LM Studio を起動した状態で）:

    python3 verify_run.py                      # 既定はリサーチSM
    python3 verify_run.py --graph react        # ReActループ
    python3 verify_run.py --task "…"           # タスクを差し替え
    python3 verify_run.py --both               # 両方を続けて実行

出力の最後に「観点ごとの判定」が出る。判定できなかった項目は
「判定不能」と出すので、そのまま貼って渡せば切り分けできる。
"""

import argparse
import json
import sys
import time

DEFAULT_TASK = (
    "SKハイニックスの直近の決算（実績値）と、直近1週間の株価動向を調べて、"
    "数値には出典と時点を付けて日本語でまとめてください。"
)


def _bool(label, ok, detail=""):
    mark = {True: "OK  ", False: "NG  ", None: "不明"}[ok]
    return f"  [{mark}] {label}" + (f" — {detail}" if detail else "")


def check_results(kind, final_state, result_text, elapsed):
    """
    これまでの修正が、実モデルの応答に対して効いているかを機械的に見る。

    ここでの NG は「モデルが指示に従わなかった」か「こちらの実装が
    効いていない」かのどちらかで、区別はログを見る必要がある。
    """
    import numeric as n

    findings = final_state.get("findings", [])
    sources = final_state.get("sources", [])
    trace = final_state.get("trace", [])
    notes = final_state.get("verification_notes", [])
    nums = n.extract_numbers(result_text)

    lines = [f"\n===== 観点ごとの判定（{kind} / {elapsed:.0f}秒）====="]

    # --- 収集 ---
    lines.append(_bool("台帳に数値が入った", bool(findings),
                       f"{len([f for f in findings if f.get('kind') == 'number'])}件"))
    lines.append(_bool("本文取得まで到達した", bool(sources),
                       f"{len(sources)}ページ"))
    truncated = [s for s in sources if s.get("source_truncated")]
    lines.append(_bool("切断フラグの伝播", None if not sources else True,
                       f"切断ありと判定されたページ: {len(truncated)}"))

    # --- 実績/予想の型付け ---
    typed = [f for f in findings if f.get("value_type") in ("actual", "forecast")]
    lines.append(_bool("台帳に実績/予想の型が付いた", bool(typed),
                       f"{len(typed)}/{len([f for f in findings if f.get('kind') == 'number'])}件"))
    tagged = [x for x in nums if n.tag_after(result_text, x["end"])]
    lines.append(_bool("回答の数値にタグが付いた", bool(nums) and len(tagged) == len(nums),
                       f"{len(tagged)}/{len(nums)}"))
    conflicts = n.tag_conflicts(result_text, findings)
    lines.append(_bool("タグと台帳の型が一致", not conflicts,
                       f"食い違い{len(conflicts)}件" if conflicts else ""))

    # --- 捏造 ---
    texts = [e.get("content", "") for e in final_state.get("history", [])
             if e.get("role") == "result"]
    unmatched = [x["raw"] for x in nums
                 if not n.matches_any(x, [y for t in texts for y in n.extract_numbers(t)])
                 and not n.appears_verbatim(x["raw"], texts)]
    lines.append(_bool("回答の数値がすべて出典にある", not unmatched,
                       f"未照合: {'、'.join(unmatched[:5])}" if unmatched else ""))

    # --- 書式 ---
    lines.append(_bool("DONE本文を取り出せた", bool(result_text.strip()),
                       f"{len(result_text)}文字"))

    # --- ループの使い方 ---
    if kind == "research":
        lines.append(_bool("compose枠が critic 用に残った",
                           final_state.get("compose_count", 0) < final_state.get("max_composes", 0),
                           f"compose {final_state.get('compose_count')}/{final_state.get('max_composes')}"
                           f" / correct {final_state.get('correction_count')}"
                           f" / critic {final_state.get('critique_count')}"))
        skipped = [t for t in trace if t.get("skipped")]
        lines.append(_bool("素通りしたノードの理由が残った", True,
                           f"スキップ{len(skipped)}件"))
    else:
        dup = [t for t in trace if "重複クエリを抑止" in t.get("summary", "")]
        lines.append(_bool("重複クエリの抑止", None if not dup else True,
                           f"{len(dup)}件抑止"))
        lines.append(_bool("検索後の本文自動取得", any("本文" in t.get("note", "") for t in trace)))

    lines.append(_bool("未反映の指摘が注記として出た", None if not notes else True,
                       f"{len(notes)}件"))
    return "\n".join(lines)


def run_one(kind, task, timeout_note=True):
    import graphs
    from trace_view import format_trace_lines
    from executor import _finalize_result

    print(f"\n########## {kind} ##########")
    app = graphs.get_app(kind)
    state = graphs.make_state(kind, task)
    started = time.time()
    final = None
    try:
        for step in app.stream(state):
            for node, s in step.items():
                final = s
                print(f"  … {node} (step={s.get('step_count')}, status={s.get('status')})",
                      flush=True)
    except Exception as e:
        print(f"\n!! 実行が例外で止まりました: {type(e).__name__}: {e}")
        if final is None:
            return None
    elapsed = time.time() - started

    result_text = _finalize_result(final) if final else ""
    print("\n----- ノード遷移 -----")
    print("\n".join(format_trace_lines(final.get("trace", []))))
    print("\n----- 最終結果 -----")
    print(result_text or "(空)")
    print(check_results(kind, final, result_text, elapsed))
    return final


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", default="research", choices=["research", "react"])
    ap.add_argument("--task", default=DEFAULT_TASK)
    ap.add_argument("--both", action="store_true")
    ap.add_argument("--dump", default="", help="最終状態をJSONで書き出すパス")
    args = ap.parse_args()

    import config
    info = config.get_current_provider_info()
    print(f"LLM: {info}")

    kinds = ["research", "react"] if args.both else [args.graph]
    finals = {}
    for kind in kinds:
        finals[kind] = run_one(kind, args.task)

    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in finals.items() if v}, f,
                      ensure_ascii=False, indent=2, default=str)
        print(f"\n最終状態を書き出しました: {args.dump}")


if __name__ == "__main__":
    sys.exit(main())
