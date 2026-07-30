# ui/page_schedule.py — スケジュール
#
# app.py から分割した画面。st.Page には render を callable として渡す。

import streamlit as st

from db import (
    get_all_tools, get_all_agent_tasks, get_verified_tools,
    add_schedule, get_schedules_by_target, delete_schedule, toggle_schedule,
)
from scheduler import add_job, remove_job


def render():
    st.subheader("スケジュール")
    st.caption("ツール／エージェントの定期実行を登録する")
    st.divider()

    exec_type = st.radio(
        "実行タイプ",
        ["tool", "agent"],
        format_func=lambda x: "🔧 ツール（Type 1）" if x == "tool" else "🧠 エージェント（Type 2）",
        horizontal=True,
    )

    targets = []
    if exec_type == "tool":
        targets = get_verified_tools()
        if not targets:
            st.caption("検証済みツールがありません。")
    else:
        targets = get_all_agent_tasks()
        if not targets:
            st.caption("エージェントタスクがありません。")

    if targets:
        selected = st.selectbox(
            "対象を選択", targets,
            format_func=lambda t: f"{t['name']} — {t.get('description') or t.get('task_prompt', '')[:40]}",
        )

        col1, col2 = st.columns([3, 1])
        with col1:
            cron_input = st.text_input("cron式", placeholder="0 9 * * *")
        with col2:
            st.caption("分 時 日 月 曜日")
            st.caption("`0 9 * * *` 毎日9時")

        if st.button("スケジュール登録", disabled=not cron_input):
            sched_id = add_schedule(exec_type, selected["id"], cron_input)
            success = add_job(exec_type, selected["id"], cron_input, sched_id)
            if success:
                st.success(f"登録完了: {selected['name']} ({cron_input})")
            else:
                st.error("cron式が不正です")

    st.divider()
    st.subheader("登録済みスケジュール")

    all_items = get_all_tools() + get_all_agent_tasks()
    has_any = False
    for item in all_items:
        scheds = get_schedules_by_target(item["id"])
        for s in scheds:
            has_any = True
            icon = "🟢" if s["enabled"] else "⏸️"
            t_icon = "🔧" if s["exec_type"] == "tool" else "🧠"
            col1, col2, col3 = st.columns([4, 1, 1])
            with col1:
                st.markdown(f"{icon} {t_icon} **{item['name']}** — `{s['cron_expr']}`")
            with col2:
                lbl = "停止" if s["enabled"] else "再開"
                if st.button(lbl, key=f"stg_{s['id']}"):
                    toggle_schedule(s["id"], not s["enabled"])
                    if s["enabled"]:
                        remove_job(s["id"])
                    else:
                        add_job(s["exec_type"], s["target_id"], s["cron_expr"], s["id"])
                    st.rerun()
            with col3:
                if st.button("削除", key=f"sdl_{s['id']}"):
                    remove_job(s["id"])
                    delete_schedule(s["id"])
                    st.rerun()

    if not has_any:
        st.caption("スケジュールはありません。")
