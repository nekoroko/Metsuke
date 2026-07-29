# app.py — AI Agent Studio
import streamlit as st
import json
from db import (
    init_db,
    add_tool, get_tool, get_all_tools, get_verified_tools, update_tool, delete_tool,
    add_agent_task, get_agent_task, get_all_agent_tasks, update_agent_task, delete_agent_task,
    add_schedule, get_schedules_by_target, delete_schedule, toggle_schedule,
    get_executions, get_execution,
    get_all_settings, set_settings,
)
from executor import (
    run_tool, run_agent, run_preview, parse_history_for_display, WORKSPACE,
)
from ai_creator import generate_tool, fix_code
from scheduler import start as start_scheduler, add_job, remove_job, run_agent_now
from config import get_current_provider_info
from sandbox import image_status as sandbox_image_status, ensure_sandbox_image
from tool_runtime import (
    read_raw, write_raw, format_for_prompt, check_host_packages, install_to_host,
)
import time

# --- 初期化 ---
init_db()
if "scheduler_started" not in st.session_state:
    start_scheduler()
    st.session_state.scheduler_started = True

st.set_page_config(page_title="AI Agent Studio", page_icon="⚡", layout="wide")
st.title("⚡ AI Agent Studio")
st.caption("ツール作成（AI） → 確認・保存 → スケジュール実行 ／ エージェントに自律的に任せる")

_provider_info = get_current_provider_info()
if _provider_info["provider"] == "local":
    _provider_label = "🖥️ ローカルLLM"
elif _provider_info.get("provider_kind") == "anthropic":
    _provider_label = "☁️ API (Anthropic ネイティブ)"
else:
    _provider_label = "☁️ API (OpenAI互換)"
st.caption(f"{_provider_label} — {_provider_info['model']} @ {_provider_info['base_url']}")

tab_create, tab_library, tab_agent, tab_schedule, tab_history, tab_settings = st.tabs([
    "🤖 ツール作成", "📦 ライブラリ", "🧠 エージェント", "🕐 スケジュール", "📋 実行履歴", "⚙️ 設定"
])


def preview_options(key_prefix: str):
    """
    プレビュー実行時のサンドボックス設定UI。
    ボタンのコールバック内で描画するとリラン時に消えるため、
    プレビューボタンより前に描画して値だけを渡す。
    """
    col1, col2 = st.columns(2)
    network = col1.checkbox(
        "🌐 ネットワークを許可", key=f"pvnet_{key_prefix}",
        help="Tavily等のAPIアクセスやスクレイピングを行うツールで必要です。"
             "オフの場合は --network none で通信を遮断します。",
    )
    writable = col2.checkbox(
        "✏️ 書き込みを許可", key=f"pvwr_{key_prefix}",
        help=f"作業ディレクトリ（{WORKSPACE}）へのファイル書き込みを許可します。"
             "オフの場合は読み取り専用でマウントします。",
    )
    return network, writable


def preview_and_fix(code: str, key_prefix: str, original_prompt: str = "",
                    network: bool = False, writable: bool = False):
    """プレビュー実行 → エラー表示 → AI修正の共通フロー"""
    spinner_msg = "Podmanで実行中..."
    if sandbox_image_status()["state"] in ("missing", "stale"):
        spinner_msg = (
            "サンドボックスイメージをビルド中...（初回、および "
            "requirements-tools.txt 変更後は数分かかります）"
        )
    with st.spinner(spinner_msg):
        result = run_preview(code, network=network, writable_workspace=writable)

    if result["status"] == "done":
        st.success("✅ 実行成功")
        if result["stdout"]:
            st.code(result["stdout"])
        else:
            st.caption("（出力なし）")
        return code

    st.error("❌ 実行失敗")
    if result["stderr"]:
        st.markdown("**エラー内容:**")
        st.code(result["stderr"])
    if result["stdout"]:
        st.markdown("**標準出力:**")
        st.code(result["stdout"])

    if st.button("🔧 AIに修正させる", key=f"fix_{key_prefix}"):
        with st.status("AIがコードを修正中...", expanded=True) as status:
            fixed_code = None
            for fix_step in fix_code(code, result["stdout"], result["stderr"], original_prompt):
                attempt = fix_step["attempt"]
                st.write(f"**修正試行 {attempt}**")

                if fix_step.get("fix_summary"):
                    st.info(f"修正内容: {fix_step['fix_summary']}")

                if fix_step.get("fixed_code"):
                    fixed_code = fix_step["fixed_code"]
                    status.update(label="修正完了", state="complete")

                    st.markdown("**修正後のコードを自動プレビュー中...**")
                    retry_result = run_preview(
                        fixed_code, network=network, writable_workspace=writable
                    )

                    if retry_result["status"] == "done":
                        st.success("✅ 修正後の実行に成功しました")
                        if retry_result["stdout"]:
                            st.code(retry_result["stdout"])
                    else:
                        st.warning("⚠️ 修正後もエラー")
                        if retry_result["stderr"]:
                            st.code(retry_result["stderr"])
                else:
                    with st.expander("生データ", expanded=False):
                        st.text(fix_step["raw"][:500])

            if fixed_code:
                return fixed_code

    return code


# ========== タブ1: ツール作成 ==========
with tab_create:
    st.subheader("AIにツールを作ってもらう / エージェントタスクを登録する")

    mode = st.radio(
        "実行モードを選択",
        ["tool", "agent"],
        format_func=lambda x: (
            "🔧 ツールモード（固定処理、LLM不要で繰り返し実行）" if x == "tool"
            else "🧠 エージェントモード（動的判断、毎回LLMが自律実行）"
        ),
        horizontal=True,
    )

    if mode == "tool":
        st.caption("ファイル操作、データ集計、検索・スクレイピングなど、毎回同じ処理を行うタスク向け")
    else:
        st.caption("最新情報の調査、要約、判断が必要なタスクなど、状況に応じて処理を変える必要があるタスク向け")

    st.divider()

    if mode == "tool":
        # --- ツールモード ---
        prompt = st.text_area(
            "どんなツールが欲しいか説明してください",
            placeholder="例: /tmp/agent_workspace 内のCSVを全て読み込み、各ファイルの行数とカラム名を一覧表示する",
            height=100,
            key="tool_prompt",
        )

        if st.button("生成", type="primary", disabled=not prompt, key="gen_tool"):
            with st.status("AIがコードを生成中...", expanded=True) as status:
                generated = None
                not_suitable = None
                for step in generate_tool(prompt):
                    st.write(f"**Turn {step['turn']}**")

                    if step.get("not_suitable"):
                        not_suitable = step
                        status.update(label="ツールでは対応不可", state="error")
                        break

                    if step.get("tool_name"):
                        st.write(f"ツール名: {step['tool_name']}")
                    if step.get("description"):
                        st.write(f"説明: {step['description']}")
                    if step.get("code"):
                        generated = step
                        generated["original_prompt"] = prompt

                        # レビュー結果を表示
                        if step.get("reviews"):
                            reviews = step["reviews"]
                            verdict = reviews["verdict"]
                            if verdict == "OK":
                                st.success("🛡️ レビュー: 問題なし")
                            else:
                                st.warning("⚠️ レビューで指摘事項あり")
                            if reviews["issues"]:
                                for issue in reviews["issues"]:
                                    st.markdown(f"- {issue}")

                        status.update(label="コード生成完了", state="complete")
                    else:
                        with st.expander("生データ", expanded=False):
                            st.text(step["raw"][:500])

                if generated and generated.get("code"):
                    st.session_state["generated"] = generated
                    if "not_suitable" in st.session_state:
                        del st.session_state["not_suitable"]

                if not_suitable:
                    st.session_state["not_suitable"] = not_suitable
                    if "generated" in st.session_state:
                        del st.session_state["generated"]

        # --- NOT_SUITABLE警告 → エージェント登録誘導 ---
        if "not_suitable" in st.session_state:
            ns = st.session_state["not_suitable"]
            st.divider()
            st.warning(f"⚠️ このタスクはツールモードでは対応できません")
            st.markdown(f"**理由:** {ns.get('not_suitable', '')}")

            if ns.get("suggested_prompt"):
                st.markdown("**エージェントタスクとして登録できます:**")
                suggested_name = st.text_input("タスク名", value="", placeholder="例: キオクシア最新情報調査", key="ns_name")
                suggested_desc = st.text_input("説明", value="", placeholder="このタスクの目的", key="ns_desc")
                suggested_prompt = st.text_area(
                    "タスクプロンプト（編集可能）",
                    value=ns["suggested_prompt"],
                    height=100,
                    key="ns_prompt",
                )

                col1, col2 = st.columns(2)
                with col1:
                    if st.button("🧠 エージェントタスクとして登録", disabled=not suggested_name):
                        tid = add_agent_task(suggested_name, suggested_desc, suggested_prompt)
                        st.success(f"登録完了: {tid}（エージェントタブで確認）")
                        del st.session_state["not_suitable"]
                        st.rerun()
                with col2:
                    if st.button("キャンセル"):
                        del st.session_state["not_suitable"]
                        st.rerun()

        # --- 生成結果の表示・編集・保存 ---
        if "generated" in st.session_state:
            gen = st.session_state["generated"]
            st.divider()
            st.subheader("生成されたコード")

            tool_name = st.text_input("ツール名", value=gen.get("tool_name", ""))
            tool_desc = st.text_input("説明", value=gen.get("description", ""))
            tool_code = st.text_area("コード（編集可能）", value=gen["code"], height=400)

            pv_network, pv_writable = preview_options("create")

            col1, col2, col3 = st.columns(3)
            with col1:
                if st.button("🧪 プレビュー実行"):
                    fixed = preview_and_fix(
                        tool_code, "create",
                        original_prompt=gen.get("original_prompt", ""),
                        network=pv_network, writable=pv_writable,
                    )
                    if fixed != tool_code:
                        st.session_state["generated"]["code"] = fixed
                        st.rerun()
            with col2:
                if st.button("💾 下書き保存"):
                    tid = add_tool(tool_name, tool_desc, tool_code, status="draft")
                    st.success(f"下書き保存: {tid}")
                    del st.session_state["generated"]
                    st.rerun()
            with col3:
                if st.button("✅ 検証済みとして保存"):
                    tid = add_tool(tool_name, tool_desc, tool_code, status="verified")
                    st.success(f"検証済み保存: {tid}")
                    del st.session_state["generated"]
                    st.rerun()

    else:
        # --- エージェントモード ---
        st.caption("エージェントタスクを直接登録します。")
        at_name = st.text_input("タスク名", placeholder="例: キオクシア最新情報調査")
        at_desc = st.text_input("説明", placeholder="例: 最新ニュースと業績を調べて要約する")
        at_prompt = st.text_area(
            "タスクプロンプト",
            placeholder="例: キオクシアの最新の業績、ニュース、株価動向をweb_searchで調べて要約してください。",
            height=120,
        )

        # 使用するツール（保存済みType1ツール）のマルチセレクト
        verified_tools_for_agent = get_verified_tools()
        selected_tool_ids = []
        if verified_tools_for_agent:
            tool_options = {f"{t['name']} ({t['id']})": t["id"] for t in verified_tools_for_agent}
            selected_labels = st.multiselect(
                "このタスクで使用可能なツール（任意）",
                options=list(tool_options.keys()),
                help="選択したツールがエージェントから run_saved_tool で呼び出し可能になります",
            )
            selected_tool_ids = [tool_options[label] for label in selected_labels]
        else:
            st.caption("保存済みの検証済みツールはまだありません。")

        if st.button("🧠 エージェントタスクを登録", type="primary",
                     disabled=not (at_name and at_prompt)):
            tid = add_agent_task(at_name, at_desc, at_prompt,
                                allowed_tool_ids=selected_tool_ids if selected_tool_ids else None)
            st.success(f"登録完了: {tid}（エージェントタブで実行できます）")


# ========== タブ2: ライブラリ ==========
with tab_library:
    st.subheader("保存済みツール（Type 1: 固定コード実行）")

    tools = get_all_tools()
    if not tools:
        st.caption("まだツールがありません。")
    else:
        for t in tools:
            icon = "✅" if t["status"] == "verified" else "📝"
            with st.expander(f"{icon} {t['name']} — {t['description'] or ''}", expanded=False):
                st.caption(f"ID: {t['id']} | {t['status']} | {t['updated_at'][:16]}")

                edited_code = st.text_area("コード", value=t["code"], height=300, key=f"tc_{t['id']}")

                pv_network, pv_writable = preview_options(f"lib_{t['id']}")

                col1, col2, col3, col4 = st.columns(4)
                run_clicked = col1.button("▶ 実行", key=f"tr_{t['id']}")
                preview_clicked = col2.button("🧪 プレビュー", key=f"tp_{t['id']}")
                new_s = "verified" if t["status"] == "draft" else "draft"
                lbl = "✅ 検証済みに" if new_s == "verified" else "📝 下書きに"
                status_clicked = col3.button(lbl, key=f"ts_{t['id']}")
                delete_clicked = col4.button("🗑️", key=f"td_{t['id']}")

                # 実行結果はフル幅で表示
                if run_clicked:
                    if t["status"] != "verified":
                        st.warning("検証済みのみ実行可能")
                    else:
                        with st.spinner("実行中..."):
                            r = run_tool(t["id"], t["name"], edited_code)
                        st.divider()
                        st.markdown("### 実行結果")
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


# ========== タブ3: エージェントタスク ==========
with tab_agent:
    st.subheader("エージェントタスク（Type 2: LLMが自律実行）")

    tasks = get_all_agent_tasks()
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


# ========== タブ4: スケジュール ==========
with tab_schedule:
    st.subheader("スケジュール設定")

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


# ========== タブ5: 実行履歴 ==========
with tab_history:
    st.subheader("実行履歴")

    if st.button("更新"):
        st.rerun()

    execs = get_executions(limit=30)
    if not execs:
        st.caption("まだ実行履歴がありません。")
    else:
        for e in execs:
            s_icon = {"done": "✅", "error": "❌", "running": "⏳"}.get(e["status"], "❓")
            t_icon = "🔧" if e["exec_type"] == "tool" else "🧠"
            t_label = {"manual": "手動", "schedule": "定期"}.get(e["trigger"], e["trigger"])

            with st.expander(
                f"{s_icon} {t_icon} {e.get('target_name', '?')} [{t_label}] — {e['started_at'][:16]}",
                expanded=False,
            ):
                st.caption(f"ID: {e['id']} | Type: {e['exec_type']} | {t_label}")
                if e.get("finished_at"):
                    st.caption(f"完了: {e['finished_at'][:19]}")
                if e.get("stdout"):
                    st.markdown("**出力:**")
                    st.markdown(e["stdout"])
                if e.get("stderr"):
                    st.markdown("**エラー:**")
                    st.code(e["stderr"])
                if e.get("history"):
                    try:
                        history = json.loads(e["history"])
                        with st.expander("ReActループ履歴", expanded=False):
                            for entry in history:
                                if entry["role"] == "assistant":
                                    st.markdown(f"🤖 {entry['content']}")
                                elif entry["role"] == "result":
                                    st.code(entry["content"])
                    except (json.JSONDecodeError, TypeError):
                        pass


# ========== タブ6: 設定 ==========
with tab_settings:
    st.subheader("LLMプロバイダ設定")
    st.caption("ローカルLLM（LM Studio）とAPI（OpenAI互換エンドポイント）を切り替えられます。"
               "APIモードはAWS Bedrock等のOpenAI互換ゲートウェイやOpenAI本家を想定しています。")

    current_settings = get_all_settings()

    provider = st.radio(
        "使用するLLM",
        ["local", "api"],
        index=0 if current_settings.get("llm_provider", "local") == "local" else 1,
        format_func=lambda x: "🖥️ ローカルLLM（LM Studio）" if x == "local" else "☁️ API（OpenAI互換エンドポイント）",
        horizontal=True,
    )

    st.divider()

    if provider == "local":
        st.markdown("### ローカルLLM設定")
        local_base_url = st.text_input(
            "Base URL",
            value=current_settings.get("local_base_url", "http://10.0.2.2:1234/v1"),
            help="VM上からホストのLM Studioに接続する場合は http://10.0.2.2:1234/v1",
        )
        local_model = st.text_input(
            "モデル名",
            value=current_settings.get("local_model", "gemma-4-12b-qat"),
            help="LM Studioで読み込んでいるモデルの識別子",
        )
        local_api_key = st.text_input(
            "API Key（通常は変更不要）",
            value=current_settings.get("local_api_key", "lm-studio"),
        )

        st.markdown("**最大出力トークン数**")
        st.caption(
            "サーバー側のContext Length設定に依存します。多くのローカルサーバーは"
            "この値をAPIから自動取得できませんが、対応しているサーバー（実装による）"
            "であれば取得できる場合もあるため、まず接続テストを試すことをお勧めします。"
            "取得できなければ、サーバー側の設定画面（例: LM Studioの「Load」タブの"
            "Context Length）を確認して手入力してください。"
        )

        col1, col2 = st.columns([1, 2])
        with col1:
            if st.button("🔌 接続テスト（自動取得を試す）", key="local_probe"):
                with st.spinner("問い合わせ中..."):
                    from config import fetch_openai_compatible_max_tokens
                    result = fetch_openai_compatible_max_tokens(
                        local_base_url.strip(), local_model.strip(), local_api_key.strip()
                    )
                if result:
                    st.session_state["local_probed_max_tokens"] = result
                    st.success(f"取得成功: {result} トークン")
                else:
                    st.warning("自動取得できませんでした。下の欄に手入力してください。")

        probed = st.session_state.get("local_probed_max_tokens")
        default_val = probed or int(current_settings.get("local_max_output_tokens", "3000") or 3000)
        local_max_tokens = st.number_input(
            "最大出力トークン数（手入力可）",
            min_value=1,
            value=default_val,
            step=100,
            key="local_max_tokens_input",
        )

        if local_max_tokens < 2500:
            st.warning(
                "⚠️ 2500未満だと、Thinking機能を持つモデル（Gemma 4等）が"
                "内部思考だけでこの上限を使い切り、実際の回答が空になる"
                "不具合が起きやすくなります（実際に観測済み）。"
                "3000〜4000程度を推奨します。ただし、この値はお使いのサーバーの"
                "Context Length（LM Studio側で別途設定）を超えては意味がないので、"
                "そちらも合わせて確認してください。"
            )

        if st.button("💾 ローカルLLM設定を保存", type="primary"):
            set_settings({
                "llm_provider": "local",
                "local_base_url": local_base_url.strip(),
                "local_model": local_model.strip(),
                "local_api_key": local_api_key.strip(),
                "local_max_output_tokens": str(int(local_max_tokens)),
            })
            st.success("保存しました。次回のLLM呼び出しから反映されます。")
            st.rerun()

    else:
        st.markdown("### API設定")

        provider_kind = st.radio(
            "プロバイダ種別",
            ["openai_compatible", "anthropic"],
            index=0 if current_settings.get("api_provider_kind", "openai_compatible") == "openai_compatible" else 1,
            format_func=lambda x: (
                "OpenAI互換エンドポイント（OpenAI本家 / Bedrock Gateway / Gemini / LiteLLM等）"
                if x == "openai_compatible" else
                "Anthropic（Claude、ネイティブ接続）"
            ),
            help="AnthropicはOpenAI互換レイヤーが公式に本番非推奨とされているため、"
                 "ネイティブライブラリ（langchain-anthropic）で接続します。",
        )

        if provider_kind == "openai_compatible":
            st.caption(
                "- AWS Bedrock: Bedrock Access Gateway等でOpenAI互換エンドポイントを立てて指定\n"
                "- OpenAI本家: https://api.openai.com/v1\n"
                "- LiteLLM Proxy等の中継サーバーも利用可能"
            )
            api_base_url = st.text_input(
                "Base URL",
                value=current_settings.get("api_base_url", ""),
                placeholder="例: https://api.openai.com/v1",
            )
            api_model = st.text_input(
                "モデル名",
                value=current_settings.get("api_model", ""),
                placeholder="例: gpt-4o-mini",
            )
            api_key = st.text_input(
                "API Key",
                value=current_settings.get("api_key", ""),
                type="password",
            )

            st.markdown("**最大出力トークン数**")
            st.caption(
                "標準のOpenAI API仕様には含まれない情報のため、多くのプロバイダで"
                "自動取得は失敗します。ダメ元で接続テストを試し、失敗したら"
                "プロバイダのドキュメントを確認して手入力してください。"
            )

            col1, col2 = st.columns([1, 2])
            with col1:
                if st.button("🔌 接続テスト（自動取得を試す）", key="openai_compat_probe",
                             disabled=not (api_base_url and api_model)):
                    with st.spinner("問い合わせ中..."):
                        from config import fetch_openai_compatible_max_tokens
                        result = fetch_openai_compatible_max_tokens(
                            api_base_url.strip(), api_model.strip(), api_key.strip()
                        )
                    if result:
                        st.session_state["openai_compat_probed_max_tokens"] = result
                        st.success(f"取得成功: {result} トークン")
                    else:
                        st.warning("自動取得できませんでした。下の欄に手入力してください。")

            probed = st.session_state.get("openai_compat_probed_max_tokens")
            default_val = probed or int(current_settings.get("api_max_output_tokens", "4000") or 4000)
            api_openai_max_tokens = st.number_input(
                "最大出力トークン数（手入力可）",
                min_value=1,
                value=default_val,
                step=100,
                key="openai_compat_max_tokens_input",
            )

            if st.button("💾 API設定を保存", type="primary",
                         disabled=not (api_base_url and api_model)):
                set_settings({
                    "llm_provider": "api",
                    "api_provider_kind": "openai_compatible",
                    "api_base_url": api_base_url.strip(),
                    "api_model": api_model.strip(),
                    "api_key": api_key.strip(),
                    "api_max_output_tokens": str(int(api_openai_max_tokens)),
                })
                st.success("保存しました。次回のLLM呼び出しから反映されます。")
                st.rerun()

            if not (api_base_url and api_model):
                st.info("Base URLとモデル名が未入力の間は、実行時に自動的にローカルLLMへフォールバックします。")

        else:
            st.caption(
                "langchain-anthropic 経由でClaude APIにネイティブ接続します。"
                "Extended Thinking等の機能をフルに使えます。"
            )
            api_model = st.text_input(
                "モデル名",
                value=current_settings.get("api_model", ""),
                placeholder="例: claude-sonnet-5",
            )
            api_key = st.text_input(
                "Anthropic API Key",
                value=current_settings.get("api_key", ""),
                type="password",
            )

            st.caption(
                "※ 未インストールの場合、初回実行時に "
                "`pip install langchain-anthropic` が必要というエラーが出ます。"
            )

            st.markdown("**最大出力トークン数**")
            st.caption(
                "Anthropicは公式Models APIでモデルの実際の上限を取得できます。"
                "接続テストを推奨しますが、失敗した場合や別のプロキシ経由の場合は"
                "手入力してください。"
            )

            col1, col2 = st.columns([1, 2])
            with col1:
                if st.button("🔌 接続テスト（自動取得を試す）", key="anthropic_probe",
                             disabled=not (api_model and api_key)):
                    with st.spinner("Anthropic Models APIに問い合わせ中..."):
                        from config import fetch_anthropic_max_tokens
                        result = fetch_anthropic_max_tokens(api_key.strip(), api_model.strip())
                    if result:
                        st.session_state["anthropic_probed_max_tokens"] = result
                        st.success(f"取得成功: {result} トークン")
                    else:
                        st.warning("自動取得できませんでした。下の欄に手入力してください。")

            probed = st.session_state.get("anthropic_probed_max_tokens")
            default_val = probed or int(current_settings.get("api_max_output_tokens", "4000") or 4000)
            api_anthropic_max_tokens = st.number_input(
                "最大出力トークン数（手入力可）",
                min_value=1,
                value=default_val,
                step=100,
                key="anthropic_max_tokens_input",
            )

            if st.button("💾 API設定を保存", type="primary",
                         disabled=not (api_model and api_key)):
                set_settings({
                    "llm_provider": "api",
                    "api_provider_kind": "anthropic",
                    "api_model": api_model.strip(),
                    "api_key": api_key.strip(),
                    "api_max_output_tokens": str(int(api_anthropic_max_tokens)),
                })
                st.success("保存しました。次回のLLM呼び出しから反映されます。")
                st.rerun()

            if not (api_model and api_key):
                st.info("モデル名とAPI Keyが未入力の間は、実行時に自動的にローカルLLMへフォールバックします。")

    st.divider()
    st.subheader("Thinking / Reasoning 設定")
    st.caption(
        "一部のモデル（Gemma 4等）は応答前に内部で長い思考（Thinking）を行う機能を持ちます。"
        "無効化するとステップの消費が減り高速になりますが、モデルによっては無効化が"
        "効かないことがあります。その場合でも自動的に予算調整して落ちないようにしています。"
    )

    disable_thinking_ui = st.toggle(
        "Thinkingを無効化する（推奨・高速）",
        value=current_settings.get("disable_thinking", "true") == "true",
        help="OFFにすると、モデルが対応していれば内部思考を行った上で回答します。"
             "処理は遅くなりますが、複雑な判断の精度が上がる場合があります。",
    )

    if st.button("💾 Thinking設定を保存"):
        set_settings({"disable_thinking": "true" if disable_thinking_ui else "false"})
        st.success("保存しました。次回のLLM呼び出しから反映されます。")
        st.rerun()

    st.divider()
    st.subheader("接続テスト")
    if st.button("🔌 現在の設定でテスト実行"):
        with st.spinner("接続確認中..."):
            try:
                from config import get_llm as _get_llm
                _llm = _get_llm(temperature=0.0)
                _resp = _llm.invoke([{"role": "user", "content": "テスト。'OK'とだけ返してください。"}])
                st.success(f"接続成功: {_resp.content[:100]}")
            except Exception as e:
                st.error(f"接続失敗: {e}")

    st.divider()
    st.subheader("検索プロバイダ設定")
    st.caption(
        "エージェントの web_search が使用する検索エンジンを切り替えられます。"
        "デフォルトはAPIキー不要のDuckDuckGoです。精度を上げたい場合は"
        "各サービスのAPIキーを設定してください（キー未設定時は自動的にDuckDuckGoにフォールバックします）。"
    )

    search_provider = st.radio(
        "使用する検索プロバイダ",
        ["duckduckgo", "tavily", "google", "brave"],
        index=["duckduckgo", "tavily", "google", "brave"].index(
            current_settings.get("search_provider", "duckduckgo")
        ),
        format_func=lambda x: {
            "duckduckgo": "🦆 DuckDuckGo（APIキー不要・デフォルト）",
            "tavily": "🔍 Tavily（AIエージェント向け・無料枠1000件/月）",
            "google": "🔎 Google Custom Search（無料枠100件/日）",
            "brave": "🦁 Brave Search（無料枠2000件/月）",
        }[x],
    )

    search_settings_to_save = {"search_provider": search_provider}

    if search_provider == "tavily":
        st.markdown("[Tavily APIキーの取得はこちら](https://tavily.com/)")
        tavily_key = st.text_input(
            "Tavily API Key", value=current_settings.get("tavily_api_key", ""),
            type="password",
        )
        search_settings_to_save["tavily_api_key"] = tavily_key.strip()

    elif search_provider == "google":
        st.markdown(
            "[Google Custom Search 設定はこちら](https://developers.google.com/custom-search/v1/overview)"
        )
        google_key = st.text_input(
            "Google API Key", value=current_settings.get("google_api_key", ""),
            type="password",
        )
        google_cse = st.text_input(
            "Search Engine ID (CSE ID)", value=current_settings.get("google_cse_id", ""),
        )
        search_settings_to_save["google_api_key"] = google_key.strip()
        search_settings_to_save["google_cse_id"] = google_cse.strip()

    elif search_provider == "brave":
        st.markdown("[Brave Search APIキーの取得はこちら](https://brave.com/search/api/)")
        brave_key = st.text_input(
            "Brave API Key", value=current_settings.get("brave_api_key", ""),
            type="password",
        )
        search_settings_to_save["brave_api_key"] = brave_key.strip()

    else:
        st.caption("DuckDuckGoはAPIキー不要ですぐに使えます。")

    if st.button("💾 検索プロバイダ設定を保存", type="primary"):
        set_settings(search_settings_to_save)
        st.success("保存しました。次回のweb_search呼び出しから反映されます。")
        st.rerun()

    st.divider()
    st.caption(
        "※ agent-project（ReActエージェントのコア）もこのDBの設定を参照するため、"
        "ここでの変更はツール実行・エージェント実行・レビュアーすべてに反映されます。"
    )

    # ========== ツール実行時ライブラリ ==========
    st.divider()
    st.subheader("ツール実行時ライブラリ")
    st.caption(
        "AI生成ツール（Type1）が使ってよい外部ライブラリの一覧です。"
        "AIが自分でライブラリを追加することはできないため、追加・削除はここで行います。"
    )

    _img = sandbox_image_status()
    _badge = {
        "ready": ("✅ サンドボックスイメージは最新です", st.success),
        "stale": ("⚠️ ライブラリ定義が変更されています。次回のプレビュー実行時に自動で再ビルドされます", st.warning),
        "missing": ("ℹ️ サンドボックスイメージは未ビルドです。初回のプレビュー実行時に自動でビルドされます", st.info),
        "user_managed": (
            f"🔧 AGENT_SANDBOX_IMAGE で指定されたイメージ（{_img['image']}）を使用中です。"
            "自動ビルドの対象外のため、イメージの管理は利用者側で行ってください",
            st.info,
        ),
    }[_img["state"]]
    _badge[1](_badge[0])

    st.markdown("**定義ファイル（`requirements-tools.txt`）**")
    st.caption(
        "1行に1パッケージ。行末の「# ...」はAIへの説明文として使われます。"
        "バージョン指定（例: `pandas>=2.0`）も書けます。"
    )
    _libs_text = st.text_area(
        "requirements-tools.txt",
        value=read_raw(),
        height=260,
        label_visibility="collapsed",
        key="libs_raw",
    )

    lib_col1, lib_col2 = st.columns(2)
    with lib_col1:
        if st.button("💾 保存", type="primary", key="libs_save"):
            write_raw(_libs_text)
            st.success(
                "保存しました。AIへの許可リストは即座に反映されます。"
                "サンドボックスイメージは次回のプレビュー実行時に自動で再ビルドされます。"
            )
            st.rerun()
    with lib_col2:
        if st.button("🔄 イメージを今すぐ再ビルド", key="libs_rebuild"):
            with st.spinner("イメージをビルド中...（数分かかります）"):
                _r = ensure_sandbox_image(force=True)
            if _r["ok"]:
                st.success("ビルドが完了しました。")
                st.rerun()
            else:
                st.error("ビルドに失敗しました。")
                st.code(_r["message"])

    st.markdown("**AIに提示される許可リスト（このファイルから自動生成）**")
    st.code(format_for_prompt(), language="text")

    st.markdown("**ホスト側（検証済みツールの実行環境）の状態**")
    st.caption(
        "検証済みツールはサンドボックスではなくホスト上で直接実行されるため、"
        "こちらにもインストールが必要です。"
    )
    _host = check_host_packages()
    _missing = [p["name"] for p in _host if not p["installed"]]

    st.table([
        {
            "パッケージ": p["name"],
            "状態": "✅ インストール済み" if p["installed"] else "❌ 未インストール",
            "バージョン": p["version"] or "-",
        }
        for p in _host
    ])

    if _missing:
        st.warning(
            f"未インストール: {', '.join(_missing)} — "
            "このままだと、これらを使う検証済みツールはホスト実行時に "
            "ModuleNotFoundError になります。"
        )
        if st.button("📥 ホストにインストール", key="libs_host_install"):
            with st.spinner("pip install を実行中..."):
                _r = install_to_host()
            if _r["ok"]:
                st.success(
                    "インストールが完了しました。"
                    "ツール実行は毎回新しいプロセスで動くため、再起動は不要です。"
                )
                st.rerun()
            else:
                st.error("インストールに失敗しました。")
                st.code(_r["message"])
    else:
        st.success("必要なライブラリはすべてインストール済みです。")