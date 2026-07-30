# app.py — AI Agent Studio のエントリポイント
#
# このファイルが持つのは初期化とナビゲーション定義だけで、
# 各画面の実装は ui/page_*.py にある。
#
# 画面構成はアプリの機能領域をそのまま反映している。
#
#   ツール管理   … 作成 / ライブラリ / エージェント
#   スケジュール … スケジュール
#   実行管理     … 実行履歴
#   （設定は3領域のいずれにも属さない横断機能なので独立）
#
# サイドバーの並びは階層ではなく利用順序（作る → 動かす時期を決める →
# 結果を見る）を表す。
#
# st.tabs ではなく st.navigation を使う理由:
# st.tabs は選択されていないタブの中身も毎回実行するため、設定画面の
# サンドボックス状態取得（podman image inspect のプロセス起動）が
# 画面上のあらゆる操作のたびに走っていた。ページ方式では選択中の画面しか
# 実行されないので、この種の無駄が構造的に無くなる。

import streamlit as st

from db import init_db
from scheduler import start as start_scheduler
from config import get_current_provider_info
from ui.common import inject_css
from ui import (
    page_create, page_library, page_agent,
    page_schedule, page_history, page_settings,
)

# --- 初期化 ---
init_db()
if "scheduler_started" not in st.session_state:
    start_scheduler()
    st.session_state.scheduler_started = True

st.set_page_config(page_title="AI Agent Studio", page_icon="⚡", layout="wide")

# エントリスクリプトは毎回のリランで先頭から実行されるため、
# ここで注入したCSSは全画面に効く
inject_css()


def _sidebar_header():
    """サイドバー上部のアプリ名と接続中プロバイダ"""
    info = get_current_provider_info()
    if info["provider"] == "local":
        label = "🖥️ ローカルLLM"
    elif info.get("provider_kind") == "anthropic":
        label = "☁️ API (Anthropic ネイティブ)"
    else:
        label = "☁️ API (OpenAI互換)"

    with st.sidebar:
        st.markdown('<p class="as-brand">⚡ AI Agent Studio</p>', unsafe_allow_html=True)
        st.markdown(
            f'<p class="as-meta">{label}<br>'
            f'<span title="{info["base_url"]}">{info["model"] or "(モデル未設定)"}</span></p>',
            unsafe_allow_html=True,
        )
        st.divider()


_sidebar_header()

# アイコンは絵文字ではなくMaterialアイコンを使う。
# 絵文字はOS・ブラウザで字形が変わり、並べたときの統一感が出ないため。
nav = st.navigation({
    "ツール管理": [
        st.Page(page_create.render, title="作成",
                icon=":material/add_circle:", url_path="create", default=True),
        st.Page(page_library.render, title="ライブラリ",
                icon=":material/inventory_2:", url_path="library"),
        st.Page(page_agent.render, title="エージェント",
                icon=":material/smart_toy:", url_path="agent"),
    ],
    "スケジュール": [
        st.Page(page_schedule.render, title="スケジュール",
                icon=":material/schedule:", url_path="schedule"),
    ],
    "実行管理": [
        st.Page(page_history.render, title="実行履歴",
                icon=":material/history:", url_path="history"),
    ],
    "": [
        st.Page(page_settings.render, title="設定",
                icon=":material/settings:", url_path="settings"),
    ],
})

nav.run()
