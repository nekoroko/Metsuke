# ai_creator.py — AIアシストによるツール生成 + コードレビュー + セキュリティレビュー
import re
import sys
import os
from config import get_llm, extract_text_content, invoke_with_retry
from db import add_ai_session, finish_ai_session
from tool_runtime import format_for_prompt, format_inline

# agent-projectからreviewersをimportするためのパス追加
AGENT_PROJECT_PATH = os.path.expanduser("~/agent-project")
if AGENT_PROJECT_PATH not in sys.path:
    sys.path.insert(0, AGENT_PROJECT_PATH)


CREATOR_PROMPT_TEMPLATE = (
    "あなたはPythonツール作成アシスタントです。\n"
    "ユーザーの要望に基づいて、繰り返し実行可能な固定処理のPythonスクリプトを生成してください。\n\n"
    "## 利用可能なライブラリ\n"
    "{libraries}\n\n"
    "## 重要な制約\n"
    "- **ダミーデータやサンプルデータをコードにハードコードしてはいけません**。\n"
    "  - 例：ニュース記事の内容、検索結果、価格、人物情報などをコード内に書き込むこと\n"
    "  - これらは捏造データなので、絶対にやってはいけません\n"
    "- 動的情報（最新ニュース、リアルタイムデータ、調査結果の要約・分析判断など）が必要なタスクは、\n"
    "  このツールでは対応できません。その場合は以下のように回答してください：\n\n"
    "  NOT_SUITABLE: (なぜツールでは対応できないかの説明)\n"
    "  SUGGESTION: エージェントモードで「(具体的なタスクプロンプト)」と登録することを推奨します\n\n"
    "- 結果はprint()で標準出力に出すこと\n"
    "- エラーハンドリングを含めること\n"
    "- 作業ディレクトリは /tmp/agent_workspace を前提とすること\n\n"
    "## 実行環境（重要）\n"
    "生成したコードはコンテナ内で実行されます。ホストOSの情報は見えません。\n"
    "- アクセスできるファイルは /tmp/agent_workspace 配下と、"
    "利用者が明示的にマウントを設定したパスのみです\n"
    "- ディスク使用量・メモリ使用量・CPU数・プロセス一覧などを取得しても、"
    "それはコンテナ自身の値であり、ホストの実態とは異なります。"
    "**ホストのシステム情報を取得するツールは作れません。**"
    "そうした要望には NOT_SUITABLE で答えてください\n"
    "- 利用できるメモリは512MB程度です。巨大なデータを一度に読み込まないでください\n\n"
    "## 対応可能なタスクの例\n"
    "- ファイル操作（CSV読み込み・集計、ログ分析、ファイル一覧）\n"
    "- 計算処理（数値集計、統計、変換）\n"
    "- 検索・スクレイピング（ddgs/requestsで実データを取得する処理）\n\n"
    "## 対応できないタスクの例\n"
    "- 「最新ニュースを要約して」（要約の判断はLLMが必要）\n"
    "- 「異常があれば報告」（異常判断はLLMが必要）\n"
    "- 「重要なトピックを深掘り」（重要度判断はLLMが必要）\n"
    "- 「ホストのディスク/メモリ使用量を監視」（コンテナ内からは取得できない）\n\n"
    "## 回答形式\n"
    "対応可能な場合:\n"
    "TOOL_NAME: (ツール名、短く)\n"
    "DESCRIPTION: (このツールが何をするか、1行)\n"
    "```python\n"
    "(完全なPythonスクリプト)\n"
    "```\n\n"
    "対応できない場合:\n"
    "NOT_SUITABLE: (理由)\n"
    "SUGGESTION: エージェントモードで「(タスクプロンプト)」と登録することを推奨します\n"
)

FIX_PROMPT_TEMPLATE = (
    "あなたはPythonコードのデバッグアシスタントです。\n"
    "以下のコードを実行したところエラーが発生しました。エラーを修正した完全なコードを返してください。\n\n"
    "## 利用可能なライブラリ\n"
    "- {libraries}\n\n"
    "## 制約\n"
    "- ダミーデータをハードコードしてはいけません\n"
    "- 修正後のコード全体を返すこと（差分ではなく完全なスクリプト）\n"
    "- 結果はprint()で標準出力に出すこと\n"
    "- 作業ディレクトリは /tmp/agent_workspace を前提とすること\n\n"
    "## 回答形式\n"
    "FIX_SUMMARY: (何を修正したか、1行)\n"
    "```python\n"
    "(修正済みの完全なPythonスクリプト)\n"
    "```\n"
)


def _creator_prompt() -> str:
    """
    ツール生成プロンプトを組み立てる。

    利用可能ライブラリの一覧は requirements-tools.txt から生成する。
    以前はここに散文でハードコードされており、実際にインストールされる
    ライブラリとAIへの提示内容がズレる余地があった。
    毎回生成するのは、UIやエディタでの定義変更を再起動なしに反映するため。
    """
    return CREATOR_PROMPT_TEMPLATE.format(libraries=format_for_prompt())


def _fix_prompt() -> str:
    """コード修正プロンプトを組み立てる（利用可能ライブラリは同上）"""
    return FIX_PROMPT_TEMPLATE.format(libraries=format_inline())


def generate_tool(prompt: str):
    """
    ユーザーの要望からツールコードを生成する。
    生成後、code_reviewer + security_reviewer で自動レビューする。
    """
    session_id = add_ai_session(prompt)
    llm = get_llm(temperature=0.1)

    messages = [
        {"role": "system", "content": _creator_prompt()},
        {"role": "user", "content": prompt},
    ]

    history = []
    max_turns = 3

    for turn in range(max_turns):
        response = invoke_with_retry(llm, messages)
        content = extract_text_content(response)
        history.append({"role": "assistant", "content": content})

        code_match = re.search(r"```python\s*\n(.+?)```", content, re.DOTALL)
        tool_name_match = re.search(r"TOOL_NAME:\s*(.+)", content)
        desc_match = re.search(r"DESCRIPTION:\s*(.+)", content)
        not_suitable_match = re.search(r"NOT_SUITABLE:\s*(.+)", content)
        suggestion_match = re.search(r"SUGGESTION:.*「(.+?)」", content, re.DOTALL)

        result = {
            "turn": turn + 1,
            "raw": content,
            "code": code_match.group(1).strip() if code_match else None,
            "tool_name": tool_name_match.group(1).strip() if tool_name_match else None,
            "description": desc_match.group(1).strip() if desc_match else None,
            "not_suitable": not_suitable_match.group(1).strip() if not_suitable_match else None,
            "suggested_prompt": suggestion_match.group(1).strip() if suggestion_match else None,
            "reviews": None,
        }

        # コードが生成されていればレビューを実施
        if code_match:
            try:
                from reviewers import run_reviewers, aggregate_results
                review_results = run_reviewers(
                    ["code_reviewer", "security_reviewer"],
                    task=prompt,
                    output=code_match.group(1).strip(),
                    code=code_match.group(1).strip(),
                )
                aggregated = aggregate_results(review_results)
                result["reviews"] = {
                    "verdict": aggregated["verdict"],
                    "issues": aggregated["issues"],
                    "instruction": aggregated["instruction"],
                    "details": review_results,
                }
            except Exception as e:
                result["reviews"] = {
                    "verdict": "OK",
                    "issues": [],
                    "instruction": "",
                    "details": [],
                    "error": str(e),
                }

        yield result

        # ツール対応不可と判定された場合
        if not_suitable_match:
            finish_ai_session(session_id, "not_suitable", history=history)
            return

        if code_match:
            finish_ai_session(
                session_id, "done",
                history=history,
                result_code=code_match.group(1).strip(),
            )
            return

        feedback = "回答形式が正しくありません。TOOL_NAME/DESCRIPTION/コード、または NOT_SUITABLE/SUGGESTION の形式で回答してください。"
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": feedback})
        history.append({"role": "user", "content": feedback})

    finish_ai_session(session_id, "error", history=history)


def fix_code(original_code: str, error_stdout: str, error_stderr: str,
             original_prompt: str = "", max_attempts: int = 3):
    """エラーが出たコードをAIに修正させる"""
    llm = get_llm(temperature=0.1)

    current_code = original_code
    current_stdout = error_stdout
    current_stderr = error_stderr

    for attempt in range(max_attempts):
        error_info = ""
        if current_stderr:
            error_info += f"標準エラー出力:\n{current_stderr}\n"
        if current_stdout:
            error_info += f"標準出力:\n{current_stdout}\n"

        user_message = (
            f"## 元の要望\n{original_prompt}\n\n" if original_prompt else ""
        ) + (
            f"## エラーが発生したコード\n```python\n{current_code}\n```\n\n"
            f"## エラー内容\n{error_info}\n"
            f"修正してください。"
        )

        messages = [
            {"role": "system", "content": _fix_prompt()},
            {"role": "user", "content": user_message},
        ]

        response = invoke_with_retry(llm, messages)
        content = extract_text_content(response)

        code_match = re.search(r"```python\s*\n(.+?)```", content, re.DOTALL)
        fix_match = re.search(r"FIX_SUMMARY:\s*(.+)", content)

        result = {
            "attempt": attempt + 1,
            "raw": content,
            "fixed_code": code_match.group(1).strip() if code_match else None,
            "fix_summary": fix_match.group(1).strip() if fix_match else None,
        }

        yield result

        if code_match:
            return

        current_code = original_code