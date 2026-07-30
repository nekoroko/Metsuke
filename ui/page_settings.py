# ui/page_settings.py — 設定
#
# app.py から分割した画面。st.Page には render を callable として渡す。

import os

import streamlit as st

import graphs
import llm_profiles
from db import (
    get_all_settings, set_settings,
    get_llm_profiles, get_llm_profile, add_llm_profile, update_llm_profile,
    delete_llm_profile,
)
from executor import WORKSPACE
from sandbox import image_status as sandbox_image_status, ensure_sandbox_image, parse_mounts
from tool_runtime import read_raw, write_raw, format_for_prompt


def render():
    st.subheader("設定")
    st.caption("LLM接続・検索プロバイダ・サンドボックスの設定")

    # 1画面に縦積みすると400行を超えて目的の項目まで到達しづらいため、
    # 設定の対象ごとにタブで分ける。
    tab_llm, tab_graph, tab_search, tab_sandbox = st.tabs(
        ["🧠 LLM", "🔀 グラフ", "🔍 検索", "📦 サンドボックス"]
    )

    with tab_llm:
        st.subheader("LLM接続プロファイル")
        st.caption(
            "接続先を複数保存して、実行時に選べます。"
            "モデルを変えて結果を比べたいときに、設定を上書きせずに済みます。"
            "既定のプロファイルは、タスク側で指定が無いときに使われます。"
        )

        profiles = get_llm_profiles()
        current_settings = get_all_settings()

        if not profiles:
            st.warning(
                "プロファイルが1件もありません。アプリの初回起動時に、"
                "それまでのLLM設定から1件が自動で作られます。"
                "下の「新規追加」で作成してください。"
            )

        # --- 一覧と既定の選択 ---
        if profiles:
            default_id = llm_profiles.default_profile_id()
            ids = [p["id"] for p in profiles]
            by_id = {p["id"]: p for p in profiles}
            chosen_default = st.radio(
                "既定のプロファイル",
                ids,
                index=ids.index(default_id) if default_id in ids else 0,
                format_func=lambda pid: llm_profiles.profile_label(by_id[pid]),
            )
            st.caption(llm_profiles.profile_summary(by_id[chosen_default]))
            if chosen_default != default_id:
                if st.button("💾 既定を変更", type="primary"):
                    set_settings({llm_profiles.SETTING_KEY: chosen_default})
                    st.success("既定を変更しました。次の実行から反映されます。")
                    st.rerun()

            with st.container(border=True):
                for p in profiles:
                    mark = " ⭐️ 既定" if p["id"] == default_id else ""
                    st.markdown(
                        f"- **{llm_profiles.profile_label(p)}**{mark}  \n"
                        f"  {llm_profiles.profile_summary(p)}"
                        f" / 最大出力 {p.get('max_output_tokens') or '未設定'}"
                        f" / Thinking "
                        f"{'無効' if (p.get('disable_thinking') or 'true') == 'true' else '有効'}"
                    )

        st.divider()

        # --- 編集・追加 ---
        NEW = "__new__"
        edit_options = [p["id"] for p in profiles] + [NEW]
        edit_target = st.selectbox(
            "編集するプロファイル",
            edit_options,
            format_func=lambda pid: (
                "➕ 新規追加" if pid == NEW
                else llm_profiles.profile_label(
                    next(p for p in profiles if p["id"] == pid))
            ),
            key="llm_edit_target",
        )
        editing = None if edit_target == NEW else get_llm_profile(edit_target)
        cur = editing or {}
        # 新規のときだけ、既定値をローカルLLMの一般的な設定で埋める
        form_key = edit_target

        name = st.text_input(
            "プロファイル名",
            value=cur.get("name", ""),
            placeholder="例: Gemma 4 12B（ローカル）",
            key=f"lp_name_{form_key}",
        )

        provider = st.radio(
            "接続先",
            [llm_profiles.PROVIDER_LOCAL, llm_profiles.PROVIDER_API],
            index=0 if (cur.get("provider") or "local") == "local" else 1,
            format_func=lambda x: (
                "🖥️ ローカルLLM（LM Studio等）" if x == llm_profiles.PROVIDER_LOCAL
                else "☁️ API"
            ),
            horizontal=True,
            key=f"lp_provider_{form_key}",
        )

        provider_kind = llm_profiles.KIND_OPENAI
        if provider == llm_profiles.PROVIDER_API:
            provider_kind = st.radio(
                "プロバイダ種別",
                [llm_profiles.KIND_OPENAI, llm_profiles.KIND_ANTHROPIC],
                index=0 if (cur.get("provider_kind") or llm_profiles.KIND_OPENAI)
                == llm_profiles.KIND_OPENAI else 1,
                format_func=lambda x: (
                    "OpenAI互換エンドポイント（OpenAI本家 / Bedrock Gateway / Gemini / LiteLLM等）"
                    if x == llm_profiles.KIND_OPENAI else
                    "Anthropic（Claude、ネイティブ接続）"
                ),
                help="AnthropicはOpenAI互換レイヤーが公式に本番非推奨とされているため、"
                     "ネイティブライブラリ（langchain-anthropic）で接続します。",
                key=f"lp_kind_{form_key}",
            )

        needs_base_url = not (provider == llm_profiles.PROVIDER_API
                              and provider_kind == llm_profiles.KIND_ANTHROPIC)
        base_url = cur.get("base_url", "")
        if needs_base_url:
            placeholder = ("例: http://10.0.2.2:1234/v1"
                           if provider == llm_profiles.PROVIDER_LOCAL
                           else "例: https://api.openai.com/v1")
            base_url = st.text_input(
                "Base URL",
                value=base_url or ("http://10.0.2.2:1234/v1"
                                   if provider == llm_profiles.PROVIDER_LOCAL and not editing
                                   else ""),
                placeholder=placeholder,
                help=("VM上からホストのLM Studioに接続する場合は http://10.0.2.2:1234/v1"
                      if provider == llm_profiles.PROVIDER_LOCAL else
                      "- AWS Bedrock: Bedrock Access Gateway等のOpenAI互換エンドポイント\n"
                      "- OpenAI本家: https://api.openai.com/v1\n"
                      "- LiteLLM Proxy等の中継サーバーも利用可能"),
                key=f"lp_base_{form_key}",
            )
        else:
            st.caption("Anthropicネイティブ接続では Base URL は不要です。")

        model = st.text_input(
            "モデル名",
            value=cur.get("model", ""),
            placeholder=("例: gemma-4-12b-qat" if provider == llm_profiles.PROVIDER_LOCAL
                         else "例: gpt-4o-mini / claude-sonnet-5"),
            key=f"lp_model_{form_key}",
        )

        api_key = st.text_input(
            "API Key" + ("（通常は変更不要）" if provider == llm_profiles.PROVIDER_LOCAL else ""),
            value=cur.get("api_key", "") or (
                "lm-studio" if provider == llm_profiles.PROVIDER_LOCAL and not editing else ""),
            type="default" if provider == llm_profiles.PROVIDER_LOCAL else "password",
            key=f"lp_key_{form_key}",
        )

        st.markdown("**最大出力トークン数**")
        st.caption(
            "実際の上限は環境によって異なります（ローカルはサーバーのContext Length設定次第、"
            "APIはモデル・プロバイダ次第）。まず接続テストで自動取得を試し、"
            "取得できなければ手入力してください。"
        )

        probe_key = f"lp_probed_{form_key}"
        can_probe = bool(model) and (bool(base_url) or not needs_base_url)
        if st.button("🔌 接続テスト（最大出力トークンの自動取得を試す）",
                     key=f"lp_probe_{form_key}", disabled=not can_probe):
            with st.spinner("問い合わせ中..."):
                if provider == llm_profiles.PROVIDER_API and \
                        provider_kind == llm_profiles.KIND_ANTHROPIC:
                    from config import fetch_anthropic_max_tokens
                    result = fetch_anthropic_max_tokens(api_key.strip(), model.strip())
                else:
                    from config import fetch_openai_compatible_max_tokens
                    result = fetch_openai_compatible_max_tokens(
                        base_url.strip(), model.strip(), api_key.strip())
            if result:
                st.session_state[probe_key] = result
                st.success(f"取得成功: {result} トークン")
            else:
                st.warning("自動取得できませんでした。下の欄に手入力してください。")

        probed = st.session_state.get(probe_key)
        fallback_tokens = 3000 if provider == llm_profiles.PROVIDER_LOCAL else 4000
        try:
            saved_tokens = int(cur.get("max_output_tokens") or fallback_tokens)
        except (TypeError, ValueError):
            saved_tokens = fallback_tokens
        max_tokens = st.number_input(
            "最大出力トークン数（手入力可）",
            min_value=1,
            value=probed or saved_tokens,
            step=100,
            key=f"lp_tokens_{form_key}",
        )

        if max_tokens < 2500:
            st.warning(
                "⚠️ 2500未満だと、Thinking機能を持つモデル（Gemma 4等）が"
                "内部思考だけでこの上限を使い切り、実際の回答が空になる"
                "不具合が起きやすくなります（実際に観測済み）。"
                "3000〜4000程度を推奨します。ただしサーバー側のContext Lengthを"
                "超えては意味がないので、そちらも合わせて確認してください。"
            )

        disable_thinking_ui = st.toggle(
            "Thinkingを無効化する（推奨・高速）",
            value=(cur.get("disable_thinking") or "true") == "true",
            help="OFFにすると、モデルが対応していれば内部思考を行った上で回答します。"
                 "処理は遅くなりますが、複雑な判断の精度が上がる場合があります。"
                 "この設定はプロファイルごとに持ちます。",
            key=f"lp_think_{form_key}",
        )

        can_save = bool(name.strip()) and bool(model.strip()) and \
            (bool(base_url.strip()) or not needs_base_url)
        if not can_save:
            st.info("プロファイル名・モデル名（API以外はBase URLも）が必要です。")

        fields = dict(
            name=name.strip(),
            provider=provider,
            provider_kind=provider_kind,
            base_url=base_url.strip() if needs_base_url else "",
            model=model.strip(),
            api_key=api_key.strip(),
            max_output_tokens=str(int(max_tokens)),
            disable_thinking="true" if disable_thinking_ui else "false",
        )

        btn_save, btn_del = st.columns([1, 1])
        if editing:
            if btn_save.button("💾 このプロファイルを保存", type="primary",
                               disabled=not can_save, key=f"lp_save_{form_key}"):
                update_llm_profile(edit_target, **fields)
                st.success("保存しました。次のLLM呼び出しから反映されます。")
                st.rerun()
            # 0件になると実行時に選ぶものが無くなるため、最後の1件は消させない
            if btn_del.button("🗑️ 削除", key=f"lp_del_{form_key}",
                              disabled=len(profiles) <= 1):
                if delete_llm_profile(edit_target):
                    st.success("削除しました。")
                    st.rerun()
                else:
                    st.error("最後の1件は削除できません。")
            if len(profiles) <= 1:
                st.caption("プロファイルが1件のときは削除できません（実行時に選ぶものが無くなるため）。")
        else:
            if btn_save.button("➕ 追加", type="primary", disabled=not can_save,
                               key=f"lp_add_{form_key}"):
                new_id = add_llm_profile(**fields)
                if not profiles:
                    set_settings({llm_profiles.SETTING_KEY: new_id})
                st.success("追加しました。")
                st.rerun()

        st.divider()
        st.subheader("接続テスト")
        st.caption("既定のプロファイルで実際に1回呼び出します。")
        if st.button("🔌 既定のプロファイルでテスト実行"):
            with st.spinner("接続確認中..."):
                try:
                    from config import get_llm as _get_llm, get_current_provider_info
                    info = get_current_provider_info()
                    _llm = _get_llm(temperature=0.0)
                    _resp = _llm.invoke([{"role": "user", "content": "テスト。'OK'とだけ返してください。"}])
                    st.success(
                        f"接続成功（{info.get('profile_name')} / {info.get('model')}）: "
                        f"{_resp.content[:100]}"
                    )
                except Exception as e:
                    st.error(f"接続失敗: {e}")

    with tab_graph:
        st.subheader("実行グラフ")
        st.caption(
            "エージェントの動かし方を切り替えます。タスクの内容によって"
            "向き不向きがあるため、タスクごとの上書き指定もできます"
            "（🧠 エージェント の各タスク内）。"
        )

        graph_settings = get_all_settings()
        kinds = [graphs.REACT, graphs.RESEARCH]
        current_kind = graphs.normalize_kind(
            graph_settings.get(graphs.SETTING_KEY, "") or ""
        )
        chosen_kind = st.radio(
            "既定のグラフ",
            kinds,
            index=kinds.index(current_kind),
            format_func=lambda k: graphs.GRAPH_LABELS[k],
        )
        st.caption(graphs.GRAPH_DESCRIPTIONS[chosen_kind])

        with st.container(border=True):
            st.markdown(
                "**ReActループ**\n\n"
                "`react → verify_tool / correct → critic`\n\n"
                "毎ステップLLMが次の行動（ツール実行・コード生成・完了）を決める。\n\n"
                "**リサーチ（ステートマシン）**\n\n"
                "`plan → search → digest → gap →（不足なら search へ戻る）→ "
                "compose → correct → critic`\n\n"
                "工程が固定で、検索と本文取得はコード側が実行する。"
                "LLMに任せるのは調査計画・充足判定・執筆の3つだけ。"
                "書式崩れでステップを空費することがなく、執筆時にプロンプトの枠を"
                "行動ルールに取られない。ツール実行やコード生成は行わない。"
            )

        if st.button("💾 グラフ設定を保存"):
            set_settings({graphs.SETTING_KEY: chosen_kind})
            st.success("保存しました。次の実行から反映されます。")
            st.rerun()

    with tab_search:
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

    with tab_sandbox:
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

        st.caption(
            "ツールコードはすべてサンドボックス内で実行されるため、"
            "ここで定義したライブラリをホスト側にインストールする必要はありません。"
        )

        # ========== サンドボックスの追加マウント ==========
        st.divider()
        st.subheader("サンドボックスのファイルアクセス")
        st.caption(
            "ツール・エージェントのコード実行はすべてコンテナ内で行われます。"
            f"既定で見えるのは作業ディレクトリ（{WORKSPACE}）だけなので、"
            "ホスト上の他のファイル（ログ、CSV等）を扱う場合はここでマウントを指定します。"
        )

        _mounts_text = st.text_area(
            "追加マウント",
            value=current_settings.get("sandbox_extra_mounts", ""),
            height=120,
            placeholder="/var/log:/var/log:ro\n/home/user/data:/data:ro",
            help="1行1マウント。「ホストのパス:コンテナ内のパス:ro または rw」形式。"
                 "コンテナ内パスとモードは省略可（省略時は同じパス・読み取り専用）。",
            key="sandbox_mounts",
        )

        _parsed_mounts = parse_mounts(_mounts_text)
        if _parsed_mounts:
            st.table([
                {"ホスト": h, "コンテナ内": c, "モード": "読み取り専用" if m == "ro" else "読み書き可"}
                for h, c, m in _parsed_mounts
            ])
            _nonexistent = [h for h, _, _ in _parsed_mounts if not os.path.exists(h)]
            if _nonexistent:
                st.warning(f"ホスト上に存在しないパス: {', '.join(_nonexistent)}")
            if any(m == "rw" for _, _, m in _parsed_mounts):
                st.warning(
                    "書き込み可（rw）でマウントされたパスは、ツールのバグや"
                    "AIが生成したコードによって変更・削除される可能性があります。"
                    "必要な範囲に限定してください。"
                )
        elif _mounts_text.strip():
            st.info("有効なマウント指定として認識された行がありません。")

        if st.button("💾 マウント設定を保存", type="primary", key="mounts_save"):
            set_settings({"sandbox_extra_mounts": _mounts_text})
            st.success("保存しました。次回の実行から反映されます。")
            st.rerun()
