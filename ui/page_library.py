# ui/page_library.py — ライブラリ
#
# app.py から分割した画面。st.Page には render を callable として渡す。

import streamlit as st

from db import get_all_tools, update_tool, delete_tool
from executor import run_tool
from ui.common import badge, preview_options, preview_and_fix, verified_profile_caption


def render():
    st.subheader("ライブラリ")
    st.caption("保存済みツール（Type1）の一覧・編集・実行")
    st.divider()

    tools = get_all_tools()

    # 件数が増えると一覧を目で追うのが厳しくなるため、絞り込みを用意する
    if tools:
        f1, f2 = st.columns([3, 2])
        query = f1.text_input(
            "検索", placeholder="ツール名・説明・コードで絞り込み",
            label_visibility="collapsed", key="lib_q",
        ).strip().lower()
        status_filter = f2.radio(
            "状態", ["すべて", "検証済み", "下書き"],
            horizontal=True, label_visibility="collapsed", key="lib_status",
        )
        if query:
            tools = [
                t for t in tools
                if query in (t["name"] or "").lower()
                or query in (t["description"] or "").lower()
                or query in (t["code"] or "").lower()
            ]
        if status_filter != "すべて":
            want = "verified" if status_filter == "検証済み" else "draft"
            tools = [t for t in tools if t["status"] == want]
        st.caption(f"{len(tools)}件を表示")
    if not tools:
        st.caption("まだツールがありません。")
    else:
        for t in tools:
            # ツールごとにカード（bordered container）で表示する。
            # 以前は st.expander だったが、一覧時に名前と状態が見えるよう
            # 常時表示のカードにし、コード本文だけを折りたたむ形にした。
            with st.container(border=True):
                head_l, head_r = st.columns([5, 2])
                with head_l:
                    st.markdown(
                        f'<p class="as-card-title">{t["name"]} {badge(t["status"])}</p>'
                        f'<p class="as-card-desc">{t["description"] or "（説明なし）"}</p>',
                        unsafe_allow_html=True,
                    )
                with head_r:
                    st.markdown(
                        f'<p class="as-meta" style="text-align:right">'
                        f'ID {t["id"]}<br>更新 {t["updated_at"][:16]}</p>',
                        unsafe_allow_html=True,
                    )

                with st.expander("📄 コードを表示・編集", expanded=False):
                    edited_code = st.text_area(
                        "コード", value=t["code"], height=300,
                        key=f"tc_{t['id']}", label_visibility="collapsed",
                    )
                    st.divider()
                    pv_network, pv_writable = preview_options(f"lib_{t['id']}")
                    verified_profile_caption()

                col1, col2, col3, col4 = st.columns([1, 1, 1.2, 0.6])
                run_clicked = col1.button(
                    "▶ 実行", key=f"tr_{t['id']}", type="primary",
                    disabled=t["status"] != "verified",
                    use_container_width=True,
                    help=None if t["status"] == "verified" else "検証済みのツールのみ実行できます",
                )
                preview_clicked = col2.button(
                    "🧪 プレビュー", key=f"tp_{t['id']}", use_container_width=True)
                new_s = "verified" if t["status"] == "draft" else "draft"
                lbl = "✅ 検証済みに" if new_s == "verified" else "📝 下書きに"
                status_clicked = col3.button(lbl, key=f"ts_{t['id']}", use_container_width=True)
                delete_clicked = col4.button(
                    "🗑️", key=f"td_{t['id']}", use_container_width=True, help="削除")

                # 実行結果はフル幅で表示
                if run_clicked:
                    with st.spinner("サンドボックスで実行中..."):
                        r = run_tool(t["id"], t["name"], edited_code)
                    st.divider()
                    st.markdown("**実行結果**")
                    if r["status"] == "done":
                        st.success("完了")
                        if r["stdout"]:
                            st.code(r["stdout"])
                        else:
                            st.caption("（出力なし）")
                    else:
                        st.error("エラー")
                        if r["stderr"]:
                            st.markdown("**エラー出力:**")
                            st.code(r["stderr"])
                        if r["stdout"]:
                            st.markdown("**標準出力:**")
                            st.code(r["stdout"])

                if preview_clicked:
                    st.divider()
                    st.markdown("### プレビュー結果")
                    fixed = preview_and_fix(
                        edited_code, f"lib_{t['id']}",
                        network=pv_network, writable=pv_writable,
                    )
                    if fixed != edited_code:
                        update_tool(t["id"], code=fixed)
                        st.rerun()

                if status_clicked:
                    update_tool(t["id"], code=edited_code, status=new_s)
                    st.rerun()
                if delete_clicked:
                    delete_tool(t["id"])
                    st.rerun()

                if edited_code != t["code"]:
                    if st.button("💾 保存", key=f"tsv_{t['id']}"):
                        update_tool(t["id"], code=edited_code)
                        st.rerun()
