# run.py — エージェント実行スクリプト（ReActループ版）
import os
from graph import app

os.makedirs("/tmp/agent_workspace", exist_ok=True)

initial_state = {
    "task": "1から100までの合計を計算して結果を表示してください。",
    "history": [],
    "generated_code": "",
    "status": "running",
    "step_count": 0,
    "max_steps": 10,
}

print("=== エージェント実行開始 ===\n")

for step in app.stream(initial_state):
    for node_name, state in step.items():
        step_num = state["step_count"]
        print(f"--- Step {step_num} [{state['status']}] ---")

        # 直近の履歴エントリを表示
        if state["history"]:
            latest = state["history"][-1]
            if latest["role"] == "assistant":
                lines = latest["content"].split("\n")
                for line in lines:
                    if line.startswith("THOUGHT:"):
                        print(f"  思考: {line[8:].strip()}")
                    elif line.startswith("ACTION:"):
                        print(f"  行動: {line[7:].strip()}")
                    elif line.startswith("DONE:"):
                        print(f"  完了: {line[5:].strip()}")
            elif latest["role"] == "result":
                preview = latest["content"][:200]
                print(f"  結果: {preview}")
                if len(latest["content"]) > 200:
                    print(f"  ... (残り{len(latest['content'])-200}文字)")

        print()

print("=== 完了 ===")
print(f"最終ステータス: {state['status']}")
if state["status"] == "done":
    for entry in reversed(state["history"]):
        if entry["role"] == "assistant" and "DONE:" in entry["content"]:
            done_line = entry["content"].split("DONE:")[1].strip()
            print(f"最終結果:\n{done_line}")
            break
elif state["status"] == "error":
    print(f"エラー: ステップ上限({state['max_steps']})に到達")