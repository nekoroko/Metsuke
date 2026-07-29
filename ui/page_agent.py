# ui/page_agent.py — エージェント
#
# app.py から分割した画面。st.Page には render を callable として渡す。

import json

import streamlit as st

from db import (
    get_all_agent_tasks, update_agent_task, delete_agent_task,
    get_verified_tools, get_execution,
)
from executor import parse_history_for_display
from scheduler import run_agent_now


def render():
    st.subheader("エージェント")
    st.caption("エージェントタスク（Type2）の一覧・編集・実行")
    st.divider()

    tasks = get_all_agent_tasks()

    if tasks:
        query = st.text_input(
            "検索", placeholder="タスク名・説明・プロンプトで絞り込み",
            label_visibility="collapsed", key="agent_q",
        ).strip().lower()
        if query:
            tasks = [
                t for t in tasks
                if query in (t["name"] or "").lower()
                or query in (t["description"] or "").lower()
                or query in (t["task_prompt"] or "").lower()
            ]
        st.caption(f"{len(tasks)}件を表示")
    if not tasks:
        st.caption("まだエージェントタスクがありません。「ツール作成」タブからエージェントモードで登録してください。")
    else:
        for t in tasks:
            with st.expander(f"🧠 {t['name']} — {t['description'] or ''}", expanded=False):
                st.caption(f"ID: {t['id']} | {t['updated_at'][:16]}")

                edited_prompt = st.text_area(
                    "プロンプト", value=t["task_prompt"], height=100, key=f"ap_{t['id']}"
                )

                # 既存の許可ツールを取得
                current_tool_ids = []
                if t.get("allowed_tool_ids"):
                    try:
                        current_tool_ids = json.loads(t["allowed_tool_ids"])
                    except (json.JSONDecodeError, TypeError):
                        current_tool_ids = []

                # ツールマルチセレクト
                verified_tools_for_edit = get_verified_tools()
                edited_tool_ids = current_tool_ids
                if verified_tools_for_edit:
                    tool_options_edit = {f"{vt['name']} ({vt['id']})": vt["id"]
                                        for vt in verified_tools_for_edit}
                    # 既存選択をデフォルトに
                    default_labels = [
                        label for label, tid in tool_options_edit.items()
                        if tid in current_tool_ids
                    ]
                    selected_labels_edit = st.multiselect(
                        "使用可能なツール",
                        options=list(tool_options_edit.keys()),
                        default=default_labels,
                        key=f"at_tools_{t['id']}",
                    )
                    edited_tool_ids = [tool_options_edit[label] for label in selected_labels_edit]

                col1, col2, col3 = st.columns(3)
                run_clicked = col1.button("▶ 実行（バックグラウンド）", key=f"ar_{t['id']}")
                save_clicked = False
                prompt_changed = edited_prompt != t["task_prompt"]
                tools_changed = set(edited_tool_ids) != set(current_tool_ids)
                if prompt_changed or tools_changed:
                    save_clicked = col2.button("💾 保存", key=f"as_{t['id']}")
                delete_clicked = col3.button("🗑️", key=f"ad_{t['id']}")

                # 実行ボタン押下時：ジョブを投入してexec_idをセッションに保存
                if run_clicked:
                    exec_id = run_agent_now(t["id"], t["name"], edited_prompt)
                    st.session_state[f"running_exec_{t['id']}"] = exec_id
                    st.success(f"バックグラウンド実行を開始しました（exec_id: {exec_id}）")
                    st.caption("「進捗を更新」ボタンを押して状態を確認できます。ブラウザを閉じても処理は継続します。")

                # ポーリング表示
                running_exec_id = st.session_state.get(f"running_exec_{t['id']}")
                if running_exec_id:
                    st.divider()
                    col_refresh, col_clear = st.columns([1, 1])
                    if col_refresh.button("🔄 進捗を更新", key=f"ref_{t['id']}"):
                        st.rerun()
                    if col_clear.button("✕ 表示を閉じる", key=f"clr_{t['id']}"):
                        del st.session_state[f"running_exec_{t['id']}"]
                        st.rerun()

                    exec_data = get_execution(running_exec_id)
                    if exec_data:
                        status = exec_data["status"]
                        s_icon = {"done": "✅", "error": "❌", "running": "⏳"}.get(status, "❓")
                        st.markdown(f"### {s_icon} ステータス: {status}")
                        st.caption(f"exec_id: {running_exec_id} | 開始: {exec_data['started_at'][:19]}")
                        if exec_data.get("finished_at"):
                            st.caption(f"完了: {exec_data['finished_at'][:19]}")

                        # 履歴を表示
                        if exec_data.get("history"):
                            try:
                                history = json.loads(exec_data["history"])
                                steps = parse_history_for_display(history)
                                st.markdown("#### 実行ログ")
                                for info in steps:
                                    step_num = info["step"]
                                    role = info["role"]
                                    if role == "assistant":
                                        if info.get("thought"):
                                            st.write(f"**Step {step_num}** 🧠 {info['thought']}")
                                        if info.get("action"):
                                            st.write(f"　🔧 `{info['action']}`")
                                        if info.get("done"):
                                            st.write(f"　✅ DONE")
                                    elif role == "result":
                                        with st.expander(f"Step {step_num} 結果", expanded=False):
                                            st.code(info.get("result", ""))
                            except (json.JSONDecodeError, TypeError):
                                pass

                        # 最終結果
                        if status == "done":
                            st.divider()
                            st.markdown("### 最終結果")
                            if exec_data.get("stdout"):
                                st.markdown(exec_data["stdout"])
                            else:
                                st.warning("最終結果が空です")
                        elif status == "error":
                            st.divider()
                            st.markdown("### エラー")
                            if exec_data.get("stderr"):
                                st.code(exec_data["stderr"])

                if save_clicked:
                    update_agent_task(
                        t["id"],
                        task_prompt=edited_prompt,
                        allowed_tool_ids=edited_tool_ids if edited_tool_ids else None,
                    )
                    st.rerun()
                if delete_clicked:
                    delete_agent_task(t["id"])
                    st.rerun()
