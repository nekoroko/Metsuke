# executor.py — ツール実行（Type 1）+ エージェント実行（Type 2）
import os
from db import add_execution, finish_execution, update_execution_progress
from sandbox import (
    execute_in_sandbox, build_sandbox_env, PROFILE_VERIFIED_TOOL,
)
from state import make_initial_state

WORKSPACE = "/tmp/agent_workspace"


def run_tool(tool_id: str, tool_name: str, code: str,
             trigger: str = "manual", schedule_id: str = None) -> dict:
    """
    Type 1: 検証済みツールを実行する（LLM不要）。

    以前はホスト上で直接実行していたが、プレビューと本番の実行環境を
    一致させるため、サンドボックス経由に統一した。人間のレビューを
    通過している前提で、通信とファイル出力は許可する（PROFILE_VERIFIED_TOOL）。
    """
    exec_id = add_execution("tool", tool_id, tool_name, trigger, schedule_id)

    result = execute_in_sandbox(
        code,
        env=build_sandbox_env(),
        **PROFILE_VERIFIED_TOOL,
    )
    status = "done" if result["success"] else "error"
    finish_execution(exec_id, status, stdout=result["stdout"], stderr=result["stderr"])
    return {"exec_id": exec_id, "status": status,
            "stdout": result["stdout"], "stderr": result["stderr"]}


def run_agent(task_id: str, task_name: str, task_prompt: str,
              trigger: str = "manual", schedule_id: str = None) -> dict:
    """
    Type 2: ReActエージェントにタスクを投げる（LLM必要）
    agent-projectのgraph.pyを呼び出す。
    """
    os.makedirs(WORKSPACE, exist_ok=True)
    exec_id = add_execution("agent", task_id, task_name, trigger, schedule_id)

    try:
        # agent-projectをPythonパスに追加
        from graph import app as react_app

        initial_state = make_initial_state(task_prompt)

        final_state = None
        for step in react_app.stream(initial_state):
            for node_name, state in step.items():
                final_state = state

        if final_state is None:
            finish_execution(exec_id, "error", stderr="実行結果が空です")
            return {"exec_id": exec_id, "status": "error", "stdout": "", "stderr": "実行結果が空です"}

        # 最終結果を抽出
        result_text = ""
        if final_state["status"] == "done":
            for entry in reversed(final_state["history"]):
                if entry["role"] == "assistant" and "DONE:" in entry["content"]:
                    result_text = entry["content"].split("DONE:")[1].strip()
                    break
        elif final_state["status"] == "error":
            result_text = f"ステップ上限（{final_state['max_steps']}）に到達"

        finish_execution(
            exec_id, final_state["status"],
            stdout=result_text,
            history=final_state["history"],
        )
        return {"exec_id": exec_id, "status": final_state["status"],
                "stdout": result_text, "stderr": ""}

    except Exception as e:
        error_msg = f"エージェント実行エラー: {e}"
        finish_execution(exec_id, "error", stderr=error_msg)
        return {"exec_id": exec_id, "status": "error", "stdout": "", "stderr": error_msg}


def _build_tool_context(task_id: str) -> str:
    """
    エージェントタスクに設定された allowed_tool_ids から、
    利用可能なツール情報の文字列を構築する。
    """
    import json
    from db import get_agent_task, get_connection

    task = get_agent_task(task_id)
    if not task or not task.get("allowed_tool_ids"):
        return ""

    try:
        tool_ids = json.loads(task["allowed_tool_ids"])
    except (json.JSONDecodeError, TypeError):
        return ""

    if not tool_ids:
        return ""

    conn = get_connection()
    placeholders = ",".join(["?"] * len(tool_ids))
    rows = conn.execute(
        f"SELECT id, name, description FROM tools WHERE id IN ({placeholders}) AND status = 'verified'",
        tool_ids
    ).fetchall()
    conn.close()

    if not rows:
        return ""

    lines = ["", "## このタスクで使用可能な保存済みツール（Type1）"]
    for r in rows:
        desc = r["description"] or "（説明なし）"
        lines.append(f"- ID: {r['id']} / 名前: {r['name']} / 説明: {desc}")
    lines.append("")
    lines.append("上記ツールは run_saved_tool(tool_id) で呼び出して結果を取得できます。")
    lines.append("前処理が必要な場合はこれらのツールを活用してください。")
    return "\n".join(lines)


def run_agent_background(exec_id: str, task_prompt: str, task_id: str = None):
    """
    Type 2のバックグラウンド実行版。
    APSchedulerから呼ばれ、各ステップごとにDBの進捗を更新する。
    task_idが指定されていれば、許可ツール情報をプロンプトに注入する。
    """
    try:
        from graph import app as react_app

        # 許可ツール情報をプロンプトに注入
        if task_id:
            tool_context = _build_tool_context(task_id)
            if tool_context:
                task_prompt = task_prompt + "\n" + tool_context

        initial_state = make_initial_state(task_prompt)

        final_state = None
        for step in react_app.stream(initial_state):
            for node_name, state in step.items():
                final_state = state
                # 各ステップごとにDBに進捗を保存
                update_execution_progress(exec_id, state["history"])

        if final_state is None:
            finish_execution(exec_id, "error", stderr="実行結果が空です")
            return

        # 最終結果を抽出
        result_text = ""
        if final_state["status"] == "done":
            for entry in reversed(final_state["history"]):
                if entry["role"] == "assistant" and "DONE:" in entry["content"]:
                    result_text = entry["content"].split("DONE:")[1].strip()
                    break
        elif final_state["status"] == "error":
            result_text = f"ステップ上限（{final_state['max_steps']}）に到達"

        finish_execution(
            exec_id, final_state["status"],
            stdout=result_text,
            history=final_state["history"],
        )

    except Exception as e:
        finish_execution(exec_id, "error", stderr=str(e))


def parse_history_for_display(history: list) -> list:
    """
    DBに保存された履歴を、UI表示用にステップ情報のリストへ変換する。
    全量を返す（UI側で必要に応じてexpander等で表示制御する）。
    """
    import re as _re
    steps = []
    step_num = 0
    for entry in history:
        role = entry["role"]
        content = entry["content"]
        if role == "assistant":
            step_num += 1
            info = {"step": step_num, "role": "assistant"}
            thought_m = _re.search(
                r"THOUGHT:\s*(.+?)(?=\n(?:ACTION:|DONE:)|\Z)",
                content, _re.DOTALL
            )
            if thought_m:
                info["thought"] = thought_m.group(1).strip()
            action_m = _re.search(
                r"ACTION:\s*(.+?)(?=\n(?:THOUGHT:|DONE:)|\Z)",
                content, _re.DOTALL
            )
            if action_m:
                info["action"] = action_m.group(1).strip()
            done_m = _re.search(r"DONE:\s*(.+)", content, _re.DOTALL)
            if done_m:
                info["done"] = done_m.group(1).strip()
            steps.append(info)
        elif role == "result":
            steps.append({"step": step_num, "role": "result", "result": content})
    return steps


def run_agent_streaming(task_id: str, task_name: str, task_prompt: str):
    """
    Type 2のストリーミング版。UIでリアルタイム表示に使う。
    """
    os.makedirs(WORKSPACE, exist_ok=True)
    exec_id = add_execution("agent", task_id, task_name, trigger="manual")

    try:
        from graph import app as react_app

        initial_state = make_initial_state(task_prompt)

        import re as _re

        final_state = None
        for step in react_app.stream(initial_state):
            for node_name, state in step.items():
                final_state = state
                step_info = {"step": state["step_count"], "status": state["status"]}

                if state["history"]:
                    latest = state["history"][-1]
                    step_info["role"] = latest["role"]
                    if latest["role"] == "assistant":
                        content = latest["content"]
                        # THOUGHT: (次のキーワードまで or 末尾)
                        thought_m = _re.search(
                            r"THOUGHT:\s*(.+?)(?=\n(?:ACTION:|DONE:)|\Z)",
                            content, _re.DOTALL
                        )
                        if thought_m:
                            step_info["thought"] = thought_m.group(1).strip()
                        # ACTION: (次のキーワードまで or 末尾)
                        action_m = _re.search(
                            r"ACTION:\s*(.+?)(?=\n(?:THOUGHT:|DONE:)|\Z)",
                            content, _re.DOTALL
                        )
                        if action_m:
                            step_info["action"] = action_m.group(1).strip()
                        # DONE: (末尾まで全部)
                        done_m = _re.search(r"DONE:\s*(.+)", content, _re.DOTALL)
                        if done_m:
                            step_info["done"] = done_m.group(1).strip()
                    elif latest["role"] == "result":
                        step_info["result"] = latest["content"][:500]

                yield step_info

        # 最終的にfinal_stateからDONEを抽出してyieldする
        if final_state and final_state["status"] == "done":
            for entry in reversed(final_state["history"]):
                if entry["role"] == "assistant" and "DONE:" in entry["content"]:
                    done_text = entry["content"].split("DONE:")[1].strip()
                    yield {
                        "step": final_state["step_count"],
                        "status": "done",
                        "role": "final",
                        "done": done_text,
                    }
                    break

        if final_state:
            result_text = ""
            if final_state["status"] == "done":
                for entry in reversed(final_state["history"]):
                    if entry["role"] == "assistant" and "DONE:" in entry["content"]:
                        result_text = entry["content"].split("DONE:")[1].strip()
                        break

            finish_execution(
                exec_id, final_state["status"],
                stdout=result_text,
                history=final_state["history"],
            )

    except Exception as e:
        finish_execution(exec_id, "error", stderr=str(e))
        yield {"step": -1, "status": "error", "result": str(e)}


def run_preview(code: str, network: bool = False,
                writable_workspace: bool = False) -> dict:
    """
    プレビュー実行（Podmanサンドボックス内）。

    起動オプションの構築は sandbox.execute_in_sandbox に一本化している
    （以前はここに同等のpodmanコマンドが重複定義されており、
      片方だけ設定が古くなる状態だった）。

    network=True のときだけ、検索APIキーを環境変数として渡す。
    通信できない状態でキーを渡しても意味がないため。
    """
    result = execute_in_sandbox(
        code,
        timeout=60,
        network=network,
        writable_workspace=writable_workspace,
        env=build_sandbox_env() if network else None,
    )
    return {
        "status": "done" if result["success"] else "error",
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }
