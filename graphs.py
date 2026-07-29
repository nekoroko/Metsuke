# graphs.py — 実行グラフの選択
#
# ReAct（graph.py）とリサーチ用ステートマシン（graph_research.py）を
# 切り替える窓口。executor はここだけを見る。
#
# import は関数の中で行う。両方を先に読み込むと、片方しか使わない実行でも
# LLMクライアントの初期化コストを払うため。

from settings_store import read_settings

REACT = "react"
RESEARCH = "research"

GRAPH_LABELS = {
    REACT: "ReActループ（汎用）",
    RESEARCH: "リサーチ（ステートマシン）",
}

GRAPH_DESCRIPTIONS = {
    REACT: "毎ステップLLMが次の行動を決める。コード生成・ファイル操作・"
           "保存済みツールの実行など、手順が読めない作業向け。",
    RESEARCH: "検索→本文取得→充足判定→執筆を固定の工程で回す。"
              "調査・レポート作成向け。書式崩れによる空回りが起きない。",
}

DEFAULT_KIND = REACT
SETTING_KEY = "default_graph_kind"

# ステートマシンは1ラウンドで search / digest と2ノード進むため、
# ReActと同じ step 予算（10）では correct / critic が「差し戻す予算がない」
# と判断してレビューを飛ばしてしまう。工程数に合わせて広げる。
KIND_BUDGETS = {
    REACT: {},
    RESEARCH: {"max_steps": 18, "max_rounds": 3, "max_composes": 3},
}


def normalize_kind(kind: str) -> str:
    return kind if kind in GRAPH_LABELS else DEFAULT_KIND


def default_kind() -> str:
    """設定画面で選んだ既定のグラフ。"""
    return normalize_kind((read_settings().get(SETTING_KEY) or "").strip())


def resolve_kind(task_id: str = None, override: str = None) -> str:
    """
    使用するグラフを決める。優先順位は override > タスク個別 > 設定の既定。

    タスク個別の指定が空文字・NULL・"default" のときは既定に従う。
    """
    if override:
        return normalize_kind(override)
    if task_id:
        try:
            from db import get_agent_task
            task = get_agent_task(task_id)
        except Exception:
            task = None
        if task:
            per_task = (task.get("graph_kind") or "").strip()
            if per_task and per_task != "default":
                return normalize_kind(per_task)
    return default_kind()


def get_app(kind: str):
    """指定のグラフのコンパイル済みアプリを返す。"""
    if normalize_kind(kind) == RESEARCH:
        from graph_research import app as research_app
        return research_app
    from graph import app as react_app
    return react_app


def make_state(kind: str, task_prompt: str):
    """グラフに合わせた予算で初期状態を作る。"""
    from state import make_initial_state
    return make_initial_state(task_prompt, **KIND_BUDGETS.get(normalize_kind(kind), {}))
