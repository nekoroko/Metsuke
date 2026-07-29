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