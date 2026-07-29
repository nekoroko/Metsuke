# ui/page_settings.py — 設定
#
# app.py から分割した画面。st.Page には render を callable として渡す。

import os

import streamlit as st

from db import get_all_settings, set_settings
from executor import WORKSPACE
from sandbox import image_status as sandbox_image_status, ensure_sandbox_image, parse_mounts
from tool_runtime import read_raw, write_raw, format_for_prompt


def render():
    st.subheader("設定")
    st.caption("LLM接続・検索プロバイダ・サンドボックスの設定")

    # 1画面に縦積みすると400行を超えて目的の項目まで到達しづらいため、
    # 設定の対象ごとにタブで分ける。
    tab_llm, tab_search, tab_sandbox = st.tabs(
        ["🧠 LLM", "🔍 検索", "📦 サンドボックス"]
    )

    with tab_llm:
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
