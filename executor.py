# executor.py — ツール実行（Type 1）+ エージェント実行（Type 2）
import os
from datetime import datetime

from db import (
    add_execution, finish_execution, update_execution_progress,
    set_execution_graph_kind, set_execution_llm_info,
)
from sandbox import (
    execute_in_sandbox, build_sandbox_env, PROFILE_VERIFIED_TOOL,
)
from state import make_initial_state  # noqa: F401  （外部から参照されている）
import config
import graphs
import llm_profiles
import metrics

WORKSPACE = "/tmp/agent_workspace"


def _prepare_agent_run(exec_id: str, task_id: str, task_prompt: str,
                       graph_kind: str = None, llm_profile_id: str = None):
    """
    使用するグラフとモデルを決めて、そのグラフ用の初期状態を作る。

    切り替えの優先順位は、グラフもモデルも 引数 > タスク個別設定 > 設定画面の既定。
    グラフごとにステップ予算が違う（ステートマシンは1ラウンドで
    複数ノード進むため）ので、初期状態の生成もここへ寄せている。

    決まった種別とモデルはここでDBへ記録する。実行を開始する側は、どちらに
    なるかを知らないまま exec_id を作っている。記録を呼び出し側に任せると
    3つの実行経路のどれかで書き漏らす。
    """
    kind = graphs.resolve_kind(task_id, graph_kind)
    profile = llm_profiles.resolve_profile(task_id, llm_profile_id)
    try:
        set_execution_graph_kind(exec_id, kind)
        set_execution_llm_info(exec_id, llm_profiles.snapshot(profile))
    except Exception:
        pass          # 記録は表示用。失敗しても実行は続ける
    return (kind, graphs.get_app(kind), graphs.make_state(kind, task_prompt),
            profile)


def _agent_steps(agent_app, initial_state, profile):
    """
    プロファイルを効かせた状態でグラフを回し、(ノード名, 状態) を順に返す。

    get_llm() はグラフの各ノードから直接呼ばれるので、実行の外側で
    「今どのモデルか」を立てておく必要がある。3つの実行経路それぞれで
    with を書くと、どれか1つで書き漏らしたときに黙って既定のモデルで
    走ってしまう。回すところを1箇所に寄せる。

    計測（metrics）も同じ理由でここに置く。3経路それぞれで囲うと、
    どれか1つで書き漏らして「その経路だけトークンが0」になる。
    """
    with config.use_profile(profile), metrics.collect_run():
        for step in agent_app.stream(initial_state):
            for node_name, state in step.items():
                yield node_name, state


def run_totals(final_state: dict = None) -> dict:
    """
    実行全体の合計。trace から足す。

    集計器（collect_run のスコープ）は _agent_steps を抜けた時点で
    畳まれているので、保存時には trace が唯一の情報源になる。
    trace は毎ノード追記されるので、実行中の途中経過にも同じ関数が使える。
    """
    trace = (final_state or {}).get("trace") or []
    keys = ("elapsed_ms", "llm_ms", "llm_calls", "llm_attempts",
            "input_tokens", "output_tokens", "reasoning_tokens", "missing_usage")
    out = {k: 0 for k in keys}
    out["nodes"] = len(trace)
    for entry in trace:
        for k in keys:
            value = entry.get(k)
            if isinstance(value, int):
                out[k] += value
    out["tokens_reported"] = out["llm_calls"] > 0 and out["missing_usage"] == 0
    return out


def _extract_done_text(history: list) -> str:
    """
    履歴から最終回答を取り出す。実体は parsing.extract_done。

    以前はここに graph._parse_done と同じ規則を**コピー**していた。
    片方だけ直す事故を避けるため parsing.py に一本化した。
    """
    from parsing import extract_done

    return extract_done(history)


def _append_verification_notes(text: str, notes: list) -> str:
    """
    機械照合で引っかかったが、ステップ予算が尽きて差し戻せなかった項目を
    最終回答の末尾に出す。

    黙って通すと、確認できていない数値が「検証済みのレポート」として
    読まれてしまう。直せないなら、せめて直せなかったことを見せる。
    """
    if not notes:
        return text
    body = "\n".join(f"- {n}" for n in notes)
    return (
        f"{text}\n\n---\n\n"
        "**⚠️ 自動検証で確認できなかった点**\n\n"
        f"{body}\n\n"
        "_これらはステップ上限に達したため訂正できませんでした。"
        "該当箇所は出典を確認してから利用してください。_"
    )


def _finalize_result(final_state: dict) -> str:
    """最終回答を組み立てる（本文 → 検証メモ → 時点表示）"""
    text = _extract_done_text(final_state.get("history", []))
    if not text:
        return ""
    text = _append_verification_notes(text, final_state.get("verification_notes", []))
    return _stamp_result(text)


def _stamp_result(text: str) -> str:
    """
    レポートの先頭に基準時刻を付ける。

    調査結果は時間が経てば陳腐化する（株価・決算・ニュース等）。
    読み手が「いつ時点の話か」を判断できるよう、機械的に付与する。
    エージェント自身に書かせると付け忘れるため、コード側で行う。
    """
    if not text or not text.strip():
        return text
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"_本レポートは {stamp} 時点で取得した情報に基づきます。_\n\n{text}"


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
              trigger: str = "manual", schedule_id: str = None,
              graph_kind: str = None, llm_profile_id: str = None) -> dict:
    """
    Type 2: エージェントにタスクを投げる（LLM必要）
    ReAct（graph.py）かリサーチ用ステートマシン（graph_research.py）を
    設定に従って選ぶ。
    """
    os.makedirs(WORKSPACE, exist_ok=True)
    exec_id = add_execution("agent", task_id, task_name, trigger, schedule_id)

    try:
        _kind, agent_app, initial_state, profile = _prepare_agent_run(
            exec_id, task_id, task_prompt, graph_kind, llm_profile_id)

        final_state = None
        for _node_name, state in _agent_steps(agent_app, initial_state, profile):
            final_state = state

        if final_state is None:
            finish_execution(exec_id, "error", stderr="実行結果が空です")
            return {"exec_id": exec_id, "status": "error", "stdout": "", "stderr": "実行結果が空です"}

        # 最終結果を抽出
        result_text = ""
        if final_state["status"] == "done":
            result_text = _finalize_result(final_state)
        elif final_state["status"] == "error":
            result_text = f"ステップ上限（{final_state['max_steps']}）に到達"

        finish_execution(
            exec_id, final_state["status"],
            stdout=result_text,
            history=final_state["history"],
            trace=final_state.get("trace"),
            metrics=run_totals(final_state),
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


def run_agent_background(exec_id: str, task_prompt: str, task_id: str = None,
                         graph_kind: str = None, llm_profile_id: str = None):
    """
    Type 2のバックグラウンド実行版。
    APSchedulerから呼ばれ、各ステップごとにDBの進捗を更新する。
    task_idが指定されていれば、許可ツール情報をプロンプトに注入する。
    """
    try:
        # 許可ツール情報をプロンプトに注入
        if task_id:
            tool_context = _build_tool_context(task_id)
            if tool_context:
                task_prompt = task_prompt + "\n" + tool_context

        _kind, agent_app, initial_state, profile = _prepare_agent_run(
            exec_id, task_id, task_prompt, graph_kind, llm_profile_id)

        final_state = None
        for _node_name, state in _agent_steps(agent_app, initial_state, profile):
            final_state = state
            # 各ステップごとにDBに進捗を保存
            update_execution_progress(exec_id, state["history"], state.get("trace"),
                                      run_totals(state))

        if final_state is None:
            finish_execution(exec_id, "error", stderr="実行結果が空です")
            return

        # 最終結果を抽出
        result_text = ""
        if final_state["status"] == "done":
            result_text = _finalize_result(final_state)
        elif final_state["status"] == "error":
            result_text = f"ステップ上限（{final_state['max_steps']}）に到達"

        finish_execution(
            exec_id, final_state["status"],
            stdout=result_text,
            history=final_state["history"],
            trace=final_state.get("trace"),
            metrics=run_totals(final_state),
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
            if not any(k in info for k in ("thought", "action", "done")):
                # ステートマシンの plan ノードなど、THOUGHT/ACTION を
                # 書かない発話もある。何も表示されないと空行になるため、
                # 本文をそのまま出す
                info["text"] = content.strip()
            steps.append(info)
        elif role == "result":
            steps.append({"step": step_num, "role": "result", "result": content})
    return steps


def run_agent_streaming(task_id: str, task_name: str, task_prompt: str,
                        graph_kind: str = None, llm_profile_id: str = None):
    """
    Type 2のストリーミング版。UIでリアルタイム表示に使う。
    """
    os.makedirs(WORKSPACE, exist_ok=True)
    exec_id = add_execution("agent", task_id, task_name, trigger="manual")

    try:
        _kind, agent_app, initial_state, profile = _prepare_agent_run(
            exec_id, task_id, task_prompt, graph_kind, llm_profile_id)

        import re as _re

        final_state = None
        for _node_name, state in _agent_steps(agent_app, initial_state, profile):
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
            done_text = _finalize_result(final_state)
            if done_text:
                yield {
                    "step": final_state["step_count"],
                    "status": "done",
                    "role": "final",
                    "done": done_text,
                }

        if final_state:
            result_text = ""
            if final_state["status"] == "done":
                result_text = _finalize_result(final_state)

            finish_execution(
                exec_id, final_state["status"],
                stdout=result_text,
                history=final_state["history"],
                trace=final_state.get("trace"),
                metrics=run_totals(final_state),
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
