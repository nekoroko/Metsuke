# ui/page_agent.py — エージェント
#
# app.py から分割した画面。st.Page には render を callable として渡す。

import json

import streamlit as st

import graphs
import llm_profiles
from db import (
    get_all_agent_tasks, update_agent_task, delete_agent_task,
    get_verified_tools, get_execution,
)
from executor import parse_history_for_display
from trace_view import parse_trace, skipped_count, trace_rows
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

                # 実行グラフ（未指定なら設定画面の既定に従う）
                kind_options = ["default", graphs.REACT, graphs.RESEARCH]
                current_kind = (t.get("graph_kind") or "default").strip() or "default"
                if current_kind not in kind_options:
                    current_kind = "default"
                edited_kind = st.selectbox(
                    "実行グラフ",
                    options=kind_options,
                    index=kind_options.index(current_kind),
                    format_func=lambda k: (
                        f"設定の既定に従う（{graphs.GRAPH_LABELS[graphs.default_kind()]}）"
                        if k == "default" else graphs.GRAPH_LABELS[k]
                    ),
                    key=f"agk_{t['id']}",
                )
                if edited_kind != "default":
                    st.caption(graphs.GRAPH_DESCRIPTIONS[edited_kind])

                # 使用するモデル（未指定なら設定画面の既定に従う）。
                # グラフと同じ3層（既定 / タスク個別 / いま画面で選んだもの）
                profiles = llm_profiles.profiles()
                by_id = {p["id"]: p for p in profiles}
                default_profile = by_id.get(llm_profiles.default_profile_id())
                profile_options = [llm_profiles.DEFAULT] + [p["id"] for p in profiles]
                current_profile = (t.get("llm_profile_id") or llm_profiles.DEFAULT).strip()
                if current_profile not in profile_options:
                    current_profile = llm_profiles.DEFAULT
                edited_profile = st.selectbox(
                    "使用するモデル",
                    options=profile_options,
                    index=profile_options.index(current_profile),
                    format_func=lambda pid: (
                        "設定の既定に従う"
                        + (f"（{llm_profiles.profile_label(default_profile)}）"
                           if default_profile else "")
                        if pid == llm_profiles.DEFAULT
                        else llm_profiles.profile_label(by_id[pid])
                    ),
                    key=f"alp_{t['id']}",
                    help="保存すればこのタスクの既定になります。保存せずに実行すると、"
                         "今回だけこのモデルで走ります。",
                )
                if edited_profile != llm_profiles.DEFAULT:
                    st.caption(llm_profiles.profile_summary(by_id[edited_profile]))

                col1, col2, col3 = st.columns(3)
                run_clicked = col1.button("▶ 実行（バックグラウンド）", key=f"ar_{t['id']}")
                save_clicked = False
                prompt_changed = edited_prompt != t["task_prompt"]
                tools_changed = set(edited_tool_ids) != set(current_tool_ids)
                kind_changed = edited_kind != current_kind
                profile_changed = edited_profile != current_profile
                if prompt_changed or tools_changed or kind_changed or profile_changed:
                    save_clicked = col2.button("💾 保存", key=f"as_{t['id']}")
                delete_clicked = col3.button("🗑️", key=f"ad_{t['id']}")

                # 実行ボタン押下時：ジョブを投入してexec_idをセッションに保存
                # 未保存でも、いま画面で選んでいるグラフで走らせる
                if run_clicked:
                    exec_id = run_agent_now(
                        t["id"], t["name"], edited_prompt,
                        graph_kind=None if edited_kind == "default" else edited_kind,
                        llm_profile_id=(None if edited_profile == llm_profiles.DEFAULT
                                        else edited_profile),
                    )
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
                        run_trace = parse_trace(exec_data.get("trace"))
                        run_name = graphs.run_label(exec_data.get("graph_kind"), run_trace)
                        from ui.page_history import parse_llm_info
                        run_model = llm_profiles.snapshot_label(
                            parse_llm_info(exec_data.get("llm_info")))
                        st.caption(
                            f"exec_id: {running_exec_id}"
                            + (f" | グラフ: {run_name}" if run_name else "")
                            + (f" | モデル: {run_model}" if run_model else "")
                            + f" | 開始: {exec_data['started_at'][:19]}"
                        )
                        if exec_data.get("finished_at"):
                            st.caption(f"完了: {exec_data['finished_at'][:19]}")

                        # 履歴を表示
                        if exec_data.get("history"):
                            try:
                                history = json.loads(exec_data["history"])
                                steps = parse_history_for_display(history)
                                st.markdown(f"#### {graphs.log_title(exec_data.get('graph_kind'), run_trace)}")
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
                                        if info.get("text"):
                                            st.write(f"**Step {step_num}** 📝 {info['text']}")
                                    elif role == "result":
                                        with st.expander(f"Step {step_num} 結果", expanded=False):
                                            st.code(info.get("result", ""))
                            except (json.JSONDecodeError, TypeError):
                                pass

                        # ノード遷移（実行中も進捗と一緒に書き込まれる）
                        trace = run_trace
                        if trace:
                            skipped = skipped_count(trace)
                            label = f"🔀 ノード遷移（{len(trace)}件"
                            label += f" / スキップ {skipped}件）" if skipped else "）"
                            with st.expander(label, expanded=False):
                                st.dataframe(
                                    trace_rows(trace),
                                    use_container_width=True,
                                    hide_index=True,
                                )

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
                        graph_kind=None if edited_kind == "default" else edited_kind,
                        llm_profile_id=(None if edited_profile == llm_profiles.DEFAULT
                                        else edited_profile),
                    )
                    st.rerun()
                if delete_clicked:
                    delete_agent_task(t["id"])
                    st.rerun()
