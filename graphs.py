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

# 実行履歴の見出しなど、狭い場所で使う短い名前
GRAPH_SHORT_LABELS = {
    REACT: "ReActループ",
    RESEARCH: "リサーチ工程",
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
# compose 枠は、初稿1回 + correct の差し戻し + critic の差し戻し のすべてが消費する。
# 「critic <= compose - 1」だけでは足りず、実測（doc27）では correct が枠を
# 食い尽くして最後の critic 指摘が反映されないまま終わっていた。
# 不変条件: max_composes >= 1 + max_corrections + max_critiques
KIND_BUDGETS = {
    REACT: {},
    RESEARCH: {"max_steps": 18, "max_rounds": 3, "max_critiques": 2,
               "max_corrections": 2, "reserve_compose_for_critic": 1},
}


def required_composes(max_corrections: int = 2, max_critiques: int = 2) -> int:
    """
    compose 枠の下限。

    compose_count を増やすのは次の3つだけ。ここが増えたら式も直すこと。
      1. gap → compose（初稿）             : 常に1回
      2. correct → compose（数値の差し戻し）: 最大 max_corrections 回
      3. critic  → compose（レビュー差し戻し）: 最大 max_critiques 回

    空応答の再試行は compose_retry_count（別枠）で数える。同じ枠に入れると、
    空応答が差し戻しの枠を食って、この式が成り立たなくなる。

    correct / critic が「1回の差し戻しで複数の指摘を出す」ことは枠を増やさない
    （指摘の件数ではなく、差し戻しの回数が枠を消費する）。
    """
    return 1 + max(0, max_corrections) + max(0, max_critiques)


def _enforce_budget_invariant(budgets: dict) -> dict:
    """compose 枠を、初稿と2種類の差し戻しが全部収まる数に引き上げる。"""
    out = dict(budgets)
    if "max_critiques" not in out and "max_corrections" not in out:
        return out
    needed = required_composes(out.get("max_corrections", 2),
                               out.get("max_critiques", 2))
    out["max_composes"] = max(out.get("max_composes", 0), needed)
    return out


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


# ステートマシンにしか存在しないノード。graph_kind を保存する前に実行された
# 履歴でも、ノード遷移からどちらのグラフだったかを言い当てられる。
_RESEARCH_ONLY_NODES = {"plan", "digest", "gap", "compose"}


def kind_from_trace(trace) -> str:
    """ノード遷移の記録からグラフ種別を推定する。判別できなければ空文字。"""
    nodes = {e.get("node") for e in (trace or []) if isinstance(e, dict)}
    if nodes & _RESEARCH_ONLY_NODES:
        return RESEARCH
    if "react" in nodes:
        return REACT
    return ""


def run_label(graph_kind: str = None, trace=None) -> str:
    """
    その実行で使ったグラフの名前。判別できなければ空文字。

    保存された graph_kind を優先し、無ければノード遷移から推定する。
    ReAct固定だった頃の履歴に後から「ReActループ」と書くのは正しいが、
    判別できないものにまで書けば嘘になるので、そこは名乗らせない。
    """
    kind = (graph_kind or "").strip()
    if kind in GRAPH_SHORT_LABELS:
        return GRAPH_SHORT_LABELS[kind]
    inferred = kind_from_trace(trace)
    return GRAPH_SHORT_LABELS[inferred] if inferred else ""


def log_title(graph_kind: str = None, trace=None) -> str:
    """実行ログの見出し。グラフ名が分かればそれを冠する。"""
    label = run_label(graph_kind, trace)
    return f"{label}のログ" if label else "実行ログ"


def make_state(kind: str, task_prompt: str):
    """グラフに合わせた予算で初期状態を作る。"""
    from state import make_initial_state
    budgets = _enforce_budget_invariant(KIND_BUDGETS.get(normalize_kind(kind), {}))
    return make_initial_state(task_prompt, **budgets)
