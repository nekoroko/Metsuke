# ui/common.py — 画面をまたいで使うUI部品
#
# CSS注入、状態バッジ、プレビュー実行まわりをここに集約する。
# 各 ui/page_*.py はこのモジュールから必要なものを import する。

import streamlit as st

from executor import run_preview, WORKSPACE
from ai_creator import fix_code
from sandbox import image_status as sandbox_image_status, PROFILE_VERIFIED_TOOL


def inject_css():
    """
    見た目の調整用CSS。

    色は .streamlit/config.toml のテーマ変数に委ね、ここでは余白・角丸・
    タイポグラフィといった形状だけを扱う。Streamlitの内部クラス名は
    バージョンで変わるため、比較的安定している data-testid と
    自前のクラス（as-*）に絞って指定している。
    """
    st.markdown(
        """
        <style>
        /* 全体の余白を詰めて情報密度を上げる */
        .block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1400px; }

        /* タブ: 下線タイプにして押しやすくする */
        [data-testid="stTabs"] [data-baseweb="tab-list"] { gap: 0.25rem; }
        [data-testid="stTabs"] [data-baseweb="tab"] {
            padding: 0.6rem 1rem; border-radius: 8px 8px 0 0; font-weight: 500;
        }

        /* ボタン: 角丸と余白を揃える */
        .stButton > button {
            border-radius: 8px; padding: 0.4rem 0.9rem; font-weight: 500;
            transition: transform .04s ease-in-out;
        }
        .stButton > button:active { transform: translateY(1px); }

        /* 入力系の角丸を統一 */
        .stTextInput input, .stTextArea textarea, .stNumberInput input,
        .stSelectbox div[data-baseweb="select"] > div { border-radius: 8px; }

        /* コードブロック: 等幅・折り返し・スクロール量を見やすく */
        .stCode, pre { border-radius: 8px !important; }
        .stCode code { font-size: 0.82rem; line-height: 1.55; }

        /* カード（st.container(border=True)）の見た目を整える */
        [data-testid="stVerticalBlockBorderWrapper"] {
            border-radius: 12px;
        }

        /* 自前のバッジ */
        .as-badge {
            display: inline-block; padding: 0.12rem 0.55rem; border-radius: 999px;
            font-size: 0.72rem; font-weight: 600; letter-spacing: .02em;
            vertical-align: middle; white-space: nowrap;
        }
        .as-ok      { background: #e7f6ec; color: #17693a; }
        .as-draft   { background: #fdf3e2; color: #8a5a00; }
        .as-err     { background: #fdecec; color: #a01b1b; }
        .as-run     { background: #e8effd; color: #1b4ba0; }
        .as-muted   { background: #eef0f4; color: #4b5563; }

        /* サイドバー */
        .as-brand { font-size: 1.05rem; font-weight: 700; margin: .2rem 0 .1rem 0; }
        [data-testid="stSidebarNav"] { padding-top: .2rem; }

        /* カード見出し */
        .as-card-title { font-size: 1.02rem; font-weight: 650; margin: 0 0 .15rem 0; }
        .as-card-desc  { font-size: .86rem; color: #5b6472; margin: 0; }
        .as-meta       { font-size: .76rem; color: #79818f; }
        </style>
        """,
        unsafe_allow_html=True,
    )


BADGE_CLASS = {
    "verified": ("as-ok", "検証済み"),
    "draft": ("as-draft", "下書き"),
    "done": ("as-ok", "完了"),
    "error": ("as-err", "エラー"),
    "running": ("as-run", "実行中"),
}


def badge(kind: str, label: str = None) -> str:
    """状態バッジのHTMLを返す（st.markdown(unsafe_allow_html=True) 用）"""
    cls, default_label = BADGE_CLASS.get(kind, ("as-muted", kind))
    return f'<span class="as-badge {cls}">{label or default_label}</span>'


def preview_options(key_prefix: str):
    """
    プレビュー実行時のサンドボックス設定UI。

    ここで指定した内容が効くのは「プレビュー」だけで、「実行」（検証済み
    ツールの本番実行）は PROFILE_VERIFIED_TOOL の固定値で動く。
    以前は実行ボタンのすぐ上にチェックボックスだけを並べていたため、
    実行にも効くように見えてしまっていた。適用範囲を明記する。

    ボタンのコールバック内で描画するとリラン時に消えるため、
    プレビューボタンより前に描画して値だけを渡す。
    """
    st.caption("🧪 **プレビュー実行時の設定**（「実行」には影響しません）")
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


def verified_profile_caption():
    """検証済みツールの本番実行がどの権限で動くかを明示する"""
    p = PROFILE_VERIFIED_TOOL
    st.caption(
        "▶ **実行**（検証済みツールの本番実行）は固定設定です: "
        f"通信{'あり' if p['network'] else 'なし'} / "
        f"作業ディレクトリ{'書き込み可' if p['writable_workspace'] else '読み取り専用'} / "
        f"タイムアウト{p['timeout']}秒"
    )


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
