# ui/page_create.py — ツール作成
#
# app.py から分割した画面。st.Page には render を callable として渡す。

import streamlit as st

import graphs
from db import add_tool, add_agent_task, get_verified_tools
from ai_creator import generate_tool
from ui.common import preview_options, preview_and_fix


def render():
    st.subheader("ツール作成")
    st.caption("AIにツールを作らせる／エージェントタスクを登録する画面")
    st.divider()

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

        # 実行グラフ。調査系はステートマシンの方が空回りしにくい
        at_kind_options = ["default", graphs.REACT, graphs.RESEARCH]
        at_kind = st.selectbox(
            "実行グラフ",
            options=at_kind_options,
            format_func=lambda k: (
                f"設定の既定に従う（{graphs.GRAPH_LABELS[graphs.default_kind()]}）"
                if k == "default" else graphs.GRAPH_LABELS[k]
            ),
            help="タスクごとにグラフを固定できます。未指定なら設定画面の既定に従います。",
        )
        if at_kind != "default":
            st.caption(graphs.GRAPH_DESCRIPTIONS[at_kind])

        if st.button("🧠 エージェントタスクを登録", type="primary",
                     disabled=not (at_name and at_prompt)):
            tid = add_agent_task(at_name, at_desc, at_prompt,
                                allowed_tool_ids=selected_tool_ids if selected_tool_ids else None,
                                graph_kind=None if at_kind == "default" else at_kind)
            st.success(f"登録完了: {tid}（エージェントタブで実行できます）")
