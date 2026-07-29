# state.py — エージェントの状態定義
from typing import TypedDict, Literal

class AgentState(TypedDict):
    task: str
    history: list[dict]       # {"role": "assistant"|"result"|"critic", "content": "..."}
    generated_code: str       # 直近の生成コード
    status: Literal["running", "done", "error", "needs_revision"]
    step_count: int           # 現在のステップ数
    max_steps: int            # 最大ステップ数
    critique_count: int       # 批判ループの回数
    max_critiques: int        # 批判ループの最大回数
    reasoning_detected: bool  # Thinking抑制が効いていないことを検知したか（耐性ロジック用）
    last_action_type: str     # 直前react_stepでの行動種別（"tool"/"code"/"done"/"unknown"/""）
    last_tool_name: str       # 直前に呼んだツール名（action_type=="tool"の場合のみ）
    tool_verify_count: int    # verify_toolノードを通った回数（step_countとは別管理）
    max_tool_verifies: int    # verify_toolノードの最大実行回数
    findings: list[dict]      # 検索結果から機械抽出した数値・日付。履歴トリミングの対象外
    sources: list[dict]       # 出典の取得結果（digestノード用。現時点では未使用）
    verification_notes: list[str]  # 差し戻せなかった検証結果。最終回答に注記として出す
    trace: list[dict]         # ノードの実行履歴（どこから来て何をして次はどこか）
    correction_count: int     # correctノードを通った回数
    max_corrections: int      # 訂正の差し戻し上限
    # --- ここから下はリサーチ用ステートマシン（graph_research.py）でのみ使う ---
    plan_items: list[dict]    # 調査項目 {"id","question","query","status","hits"}
    research_round: int       # 検索ラウンド数
    max_rounds: int           # 検索ラウンドの上限
    queries_done: list[str]   # 実行済みクエリ（同じクエリの空回りを防ぐ）
    compose_count: int        # レポートを書いた回数（差し戻しを含む）
    max_composes: int         # レポート執筆の上限


def make_initial_state(task: str, max_steps: int = 10, max_critiques: int = 2,
                       max_tool_verifies: int = 6, max_corrections: int = 2,
                       max_rounds: int = 3, max_composes: int = 3) -> AgentState:
    """
    エージェントの初期状態を生成する。

    AgentStateにキーを追加した際、初期化箇所（run.py / executor.pyの3箇所）の
    どれかが取り残されると、そのキーを直接添字アクセスするノードに到達した
    時点でKeyErrorになる。実際、run.py が critique_count 等を持たないまま
    残っており、DONE後に critic_step の state["critique_count"] で落ちていた。

    初期状態の生成をここへ集約し、追加漏れが構造的に起きないようにする。
    """
    return {
        "task": task,
        "history": [],
        "generated_code": "",
        "status": "running",
        "step_count": 0,
        "max_steps": max_steps,
        "critique_count": 0,
        "max_critiques": max_critiques,
        "reasoning_detected": False,
        "last_action_type": "",
        "last_tool_name": "",
        "tool_verify_count": 0,
        "max_tool_verifies": max_tool_verifies,
        "findings": [],
        "sources": [],
        "verification_notes": [],
        "trace": [],
        "correction_count": 0,
        "max_corrections": max_corrections,
        "plan_items": [],
        "research_round": 0,
        "max_rounds": max_rounds,
        "queries_done": [],
        "compose_count": 0,
        "max_composes": max_composes,
    }
