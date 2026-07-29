# ui/page_history.py — 実行履歴
#
# app.py から分割した画面。st.Page には render を callable として渡す。

import json

import streamlit as st

from db import get_executions
from ui.common import badge


def render():
    st.subheader("実行履歴")
    st.caption("手動実行・定期実行の結果を確認する")
    st.divider()

    execs = get_executions(limit=100)

    f1, f2, f3 = st.columns([2, 2, 1])
    status_filter = f1.radio(
        "状態", ["すべて", "完了", "エラー", "実行中"],
        horizontal=True, label_visibility="collapsed", key="hist_status",
    )
    type_filter = f2.radio(
        "種別", ["すべて", "ツール", "エージェント"],
        horizontal=True, label_visibility="collapsed", key="hist_type",
    )
    if f3.button("🔄 更新", use_container_width=True):
        st.rerun()

    if status_filter != "すべて":
        want = {"完了": "done", "エラー": "error", "実行中": "running"}[status_filter]
        execs = [e for e in execs if e["status"] == want]
    if type_filter != "すべて":
        want = "tool" if type_filter == "ツール" else "agent"
        execs = [e for e in execs if e["exec_type"] == want]
    execs = execs[:30]
    if not execs:
        st.caption("まだ実行履歴がありません。")
    else:
        for e in execs:
            t_icon = "🔧" if e["exec_type"] == "tool" else "🧠"
            t_label = {"manual": "手動", "schedule": "定期"}.get(e["trigger"], e["trigger"])

            # 実行結果はカードで表示し、出力・エラー・ReAct履歴を
            # それぞれ折りたたみに分ける。以前は全体が1つのexpanderに
            # 入っていたため、一覧の時点では状態や所要時間が読めなかった。
            with st.container(border=True):
                head_l, head_r = st.columns([5, 2])
                with head_l:
                    st.markdown(
                        f'<p class="as-card-title">{t_icon} {e.get("target_name", "?")} '
                        f'{badge(e["status"])} {badge("muted", t_label)}</p>'
                        f'<p class="as-meta">開始 {e["started_at"][:19]}'
                        + (f' / 完了 {e["finished_at"][:19]}' if e.get("finished_at") else "")
                        + "</p>",
                        unsafe_allow_html=True,
                    )
                with head_r:
                    st.markdown(
                        f'<p class="as-meta" style="text-align:right">'
                        f'ID {e["id"]}<br>{e["exec_type"]}</p>',
                        unsafe_allow_html=True,
                    )

                if e.get("stdout"):
                    with st.expander("📤 出力", expanded=False):
                        st.markdown(e["stdout"])
                if e.get("stderr"):
                    with st.expander("⚠️ エラー出力", expanded=False):
                        st.code(e["stderr"])
                if e.get("history"):
                    try:
                        history = json.loads(e["history"])
                    except (json.JSONDecodeError, TypeError):
                        history = None
                    if history:
                        with st.expander(f"🔁 ReActループ履歴（{len(history)}件）", expanded=False):
                            for entry in history:
                                if entry["role"] == "assistant":
                                    st.markdown(f"🤖 {entry['content']}")
                                elif entry["role"] == "result":
                                    st.code(entry["content"])
                if not any(e.get(k) for k in ("stdout", "stderr", "history")):
                    st.caption("（記録された出力はありません）")
