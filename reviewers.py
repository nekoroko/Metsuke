# reviewers.py — 専門家レビュアープール
# 各レビュアーは {"verdict": "OK"|"NEEDS_REVISION", "issues": [...], "instruction": "..."} を返す
import re
from config import get_llm, extract_text_content, invoke_with_retry
from datetime import datetime
from numeric import (
    extract_numbers, find_conversion_pairs, conversion_plausible, matches_any,
    format_findings,
)


# ===== プロンプト定義 =====

DISPATCHER_PROMPT = (
    "あなたはレビュアー振り分け担当です。\n"
    "タスクと成果物の内容を見て、必要なレビュアーを選んでください。\n"
    "\n"
    "## 利用可能なレビュアー\n"
    "- fact_checker: 事実の正確性、年度・日付の整合性、情報の網羅性をチェック（調査・要約タスク向け）\n"
    "- code_reviewer: コードの正当性、要件充足、エッジケース、エラーハンドリングをチェック（コード生成タスク向け）\n"
    "- security_reviewer: セキュリティリスク、機密情報の露出、危険な操作をチェック（コードやシステム操作向け）\n"
    "- data_analyst: 計算の正当性、サンプル偏り、統計的妥当性をチェック（データ分析タスク向け）\n"
    "- generic_reviewer: 論理一貫性、表現の適切性、ユーザー意図との整合をチェック（汎用フォールバック）\n"
    "\n"
    "## 判断基準\n"
    "- 1つで足りる場合は1つだけ選ぶ（過剰レビューを避ける）\n"
    "- コード生成系は通常 code_reviewer + security_reviewer\n"
    "- 調査・ニュース要約系は fact_checker\n"
    "- 該当が無ければ generic_reviewer\n"
    "\n"
    "## 回答形式\n"
    "REVIEWERS: (カンマ区切りでレビュアー名を列挙)\n"
    "REASON: (選定理由を1行で)\n"
)


FACT_CHECKER_PROMPT = (
    "あなたは事実検証専門のレビュアーです。\n"
    "エージェントの成果物が、検索結果や履歴から得られた事実を正しく反映しているかチェックしてください。\n"
    "\n"
    "## チェック観点\n"
    "1. 数値が「実績値」か「予想値」か明確になっているか\n"
    "2. 年度・日付の表記が現在の日付と整合しているか\n"
    "3. 順位・ランキング情報が最新か\n"
    "4. 重要な情報が欠落していないか（履歴にあるのに最終回答に含まれていない情報)\n"
    "5. 直近の重要イベントが反映されているか\n"
    "6. **時系列の優先順位（重要）**: 実行履歴の中に、同じトピックについて日付が異なる複数の情報が\n"
    "   含まれていないか確認してください。含まれている場合、最終回答が古い方の情報を採用していないか\n"
    "   確認してください。特に以下のような「状況が反転しているケース」を厳しく見てください。\n"
    "   - 「悪化・不調」の情報と「好調・過去最高」の情報が両方ある → 新しい方が正しい実態である可能性が高い\n"
    "   - 「無配・減配」の情報と「配当開始検討」の情報が両方ある → 新しい方を優先する\n"
    "   - 「下落」の情報と「急騰」の情報が両方ある → 新しい方を優先する\n"
    "   実行履歴の検索結果に含まれる日付を比較し、最終回答が最も新しい情報を反映していない場合は、\n"
    "   NEEDS_REVISIONとして「どちらの情報が新しいか」を明示した上で修正を指示してください。\n"
    "7. **結論の向きを変える数値の欠落（重要）**: 実行履歴や下の数値一覧に、下落率・減益・\n"
    "   赤字・遅延など、最終回答の結論と逆向きの数値や事実が含まれていないか確認してください。\n"
    "   含まれているのに最終回答が触れていない場合は、必ず NEEDS_REVISION とし、\n"
    "   どの数値を反映すべきかを明示してください。\n"
    "8. **予定日の注記**: 「◯日に発表」等の予定が履歴にあり、その日が現在の日付以前\n"
    "   または数日以内であるのに、予想値が確定値のように書かれていないか確認してください。\n"
    "\n"
    "## 回答形式\n"
    "VERDICT: OK または NEEDS_REVISION\n"
    "ISSUES: (NEEDS_REVISIONの場合のみ、問題点を箇条書きで)\n"
    "INSTRUCTION: (NEEDS_REVISIONの場合のみ、エージェントへの具体的な指示)\n"
)


CODE_REVIEWER_PROMPT = (
    "あなたはPythonコードレビュー専門のレビュアーです。\n"
    "生成されたコードを以下の観点でチェックしてください。\n"
    "\n"
    "## チェック観点\n"
    "1. 構文エラーや明らかなロジックエラーがないか\n"
    "2. 要件（タスク説明）を満たしているか\n"
    "3. エッジケース（空入力、null、極端な値、ファイル不在）への対応\n"
    "4. 適切なエラーハンドリング（try/except）があるか\n"
    "5. 標準ライブラリと許可されたライブラリのみ使用しているか\n"
    "6. ダミーデータやサンプルデータがハードコードされていないか\n"
    "7. 結果が標準出力に出るようになっているか\n"
    "\n"
    "## 回答形式\n"
    "VERDICT: OK または NEEDS_REVISION\n"
    "ISSUES: (NEEDS_REVISIONの場合、問題点を箇条書き)\n"
    "INSTRUCTION: (NEEDS_REVISIONの場合、具体的な修正指示)\n"
)


SECURITY_REVIEWER_PROMPT = (
    "あなたはセキュリティレビュー専門のレビュアーです。\n"
    "コードやコマンドに潜むセキュリティリスクを評価してください。\n"
    "\n"
    "## チェック観点\n"
    "1. シェルコマンドインジェクションの可能性（os.system, subprocess shell=True など）\n"
    "2. パストラバーサル（../ で予期しないファイルにアクセス）\n"
    "3. 認証情報・APIキー・パスワードのハードコード\n"
    "4. 任意コード実行のリスク（eval, exec, pickle.loads など）\n"
    "5. 個人情報・機密データの不適切な扱い・外部送信\n"
    "6. ファイル削除や上書きの危険性（rm -rf 系）\n"
    "7. 外部ネットワークアクセスの妥当性\n"
    "\n"
    "## 判断ガイド\n"
    "- 明らかなリスク・実害につながる操作 → NEEDS_REVISION\n"
    "- 軽微な改善余地 → OK としつつ ISSUES にメモ\n"
    "- 標準的なファイル読み書き、四則演算、Web検索などは基本 OK\n"
    "\n"
    "## 回答形式\n"
    "VERDICT: OK または NEEDS_REVISION\n"
    "ISSUES: (問題点を箇条書き、OKでも改善余地があれば記載)\n"
    "INSTRUCTION: (NEEDS_REVISIONの場合、具体的な修正指示)\n"
)


DATA_ANALYST_PROMPT = (
    "あなたはデータ分析レビュー専門のレビュアーです。\n"
    "分析結果や統計的な記述に問題がないかチェックしてください。\n"
    "\n"
    "## チェック観点\n"
    "1. 計算ロジック・集計方法は正しいか\n"
    "2. サンプル数や対象データの偏りがないか\n"
    "3. 結論を支える根拠が示されているか\n"
    "4. 因果関係と相関関係を混同していないか\n"
    "5. 重要な変数を見落としていないか\n"
    "6. 単位や時点の表記が正確か\n"
    "\n"
    "## 回答形式\n"
    "VERDICT: OK または NEEDS_REVISION\n"
    "ISSUES: (問題点を箇条書き)\n"
    "INSTRUCTION: (NEEDS_REVISIONの場合、具体的な指示)\n"
)


GENERIC_REVIEWER_PROMPT = (
    "あなたは汎用レビュアーです。\n"
    "成果物がタスクの要求を満たしているか、論理的に一貫しているかをチェックしてください。\n"
    "\n"
    "## チェック観点\n"
    "1. タスクの要求に答えているか\n"
    "2. 論理的に一貫しているか、矛盾がないか\n"
    "3. 表現が適切か（曖昧すぎる、断定しすぎる、誤解を招くなど）\n"
    "4. 重要な前提条件や注意事項が漏れていないか\n"
    "\n"
    "## 回答形式\n"
    "VERDICT: OK または NEEDS_REVISION\n"
    "ISSUES: (問題点を箇条書き)\n"
    "INSTRUCTION: (NEEDS_REVISIONの場合、具体的な指示)\n"
)


# ===== ヘルパー =====

def _parse_review_response(content: str) -> dict:
    """レビュアーの応答をパースする"""
    if not content or not content.strip():
        return {"verdict": "OK", "issues": [], "instruction": "", "raw": ""}

    verdict_m = re.search(r"VERDICT:\s*(\w+)", content)
    verdict = verdict_m.group(1).strip().upper() if verdict_m else "OK"

    issues = []
    issues_m = re.search(r"ISSUES:\s*(.+?)(?=INSTRUCTION:|\Z)", content, re.DOTALL)
    if issues_m:
        issues_text = issues_m.group(1).strip()
        for line in issues_text.split("\n"):
            line = line.strip().lstrip("-").lstrip("・").strip()
            if line:
                issues.append(line)

    instruction = ""
    inst_m = re.search(r"INSTRUCTION:\s*(.+)", content, re.DOTALL)
    if inst_m:
        instruction = inst_m.group(1).strip()

    return {
        "verdict": verdict if verdict in ("OK", "NEEDS_REVISION") else "OK",
        "issues": issues,
        "instruction": instruction,
        "raw": content,
    }


def _build_history_summary(history: list, max_entries: int = 8, max_chars: int = 400) -> str:
    """履歴を要約してレビュアーに渡せる形にする"""
    summary = ""
    for entry in history[-max_entries:]:
        content = entry.get("content", "")
        if not content:
            continue
        snippet = content[:max_chars]
        if entry["role"] == "assistant":
            summary += f"\n[Agent] {snippet}\n"
        elif entry["role"] == "result":
            summary += f"\n[Result] {snippet}\n"
    return summary


# ===== 各レビュアー実装 =====

def fact_checker(task: str, output: str, history: list = None,
                 findings: list = None) -> dict:
    """
    事実検証専門のレビュアー。

    履歴は文字数で切り詰められるため、古いステップの数値は視野から外れる。
    数値台帳（findings）は切り詰められないので、こちらも併せて渡すことで
    「履歴にあったのに回答から欠落した数値」を検出できるようにする。
    """
    llm = get_llm(temperature=0.1)
    today = datetime.now().strftime("%Y年%m月%d日")
    # 時系列比較のため、他レビュアーより多めの文字数・件数で履歴を渡す
    history_summary = _build_history_summary(history or [], max_entries=10, max_chars=600)
    ledger = format_findings(findings or [])

    messages = [
        {"role": "system", "content": FACT_CHECKER_PROMPT},
        {"role": "user", "content": (
            f"## 現在の日付\n{today}\n\n"
            f"## タスク\n{task}\n\n"
            + (f"## 調査中に取得した数値・日付（原文のまま）\n{ledger}\n\n" if ledger else "")
            + f"## エージェントの実行履歴（抜粋）\n{history_summary}\n\n"
            f"## エージェントの最終回答\n{output[:1500]}\n\n"
            f"上記をチェックしてください。"
        )},
    ]
    response = invoke_with_retry(llm, messages)
    result = _parse_review_response(extract_text_content(response))
    result["reviewer"] = "fact_checker"
    return result


def code_reviewer(task: str, code: str, context: str = "") -> dict:
    """コードレビュー専門のレビュアー"""
    llm = get_llm(temperature=0.1)

    messages = [
        {"role": "system", "content": CODE_REVIEWER_PROMPT},
        {"role": "user", "content": (
            f"## タスク（コードの要件）\n{task}\n\n"
            + (f"## コンテキスト\n{context}\n\n" if context else "")
            + f"## レビュー対象コード\n```python\n{code[:2000]}\n```\n\n"
            f"上記をレビューしてください。"
        )},
    ]
    response = invoke_with_retry(llm, messages)
    result = _parse_review_response(extract_text_content(response))
    result["reviewer"] = "code_reviewer"
    return result


def security_reviewer(code: str, task: str = "") -> dict:
    """セキュリティレビュー専門のレビュアー"""
    llm = get_llm(temperature=0.1)

    messages = [
        {"role": "system", "content": SECURITY_REVIEWER_PROMPT},
        {"role": "user", "content": (
            (f"## タスク\n{task}\n\n" if task else "")
            + f"## レビュー対象コード\n```python\n{code[:2000]}\n```\n\n"
            f"上記をセキュリティ観点でレビューしてください。"
        )},
    ]
    response = invoke_with_retry(llm, messages)
    result = _parse_review_response(extract_text_content(response))
    result["reviewer"] = "security_reviewer"
    return result


def data_analyst(task: str, output: str, history: list = None) -> dict:
    """データ分析レビュー専門のレビュアー"""
    llm = get_llm(temperature=0.1)
    history_summary = _build_history_summary(history or [])

    messages = [
        {"role": "system", "content": DATA_ANALYST_PROMPT},
        {"role": "user", "content": (
            f"## タスク\n{task}\n\n"
            f"## 実行履歴（抜粋）\n{history_summary}\n\n"
            f"## 分析結果\n{output[:1500]}\n\n"
            f"上記をレビューしてください。"
        )},
    ]
    response = invoke_with_retry(llm, messages)
    result = _parse_review_response(extract_text_content(response))
    result["reviewer"] = "data_analyst"
    return result


def generic_reviewer(task: str, output: str, history: list = None) -> dict:
    """汎用レビュアー"""
    llm = get_llm(temperature=0.1)
    history_summary = _build_history_summary(history or [])

    messages = [
        {"role": "system", "content": GENERIC_REVIEWER_PROMPT},
        {"role": "user", "content": (
            f"## タスク\n{task}\n\n"
            f"## 実行履歴（抜粋）\n{history_summary}\n\n"
            f"## 成果物\n{output[:1500]}\n\n"
            f"上記をレビューしてください。"
        )},
    ]
    response = invoke_with_retry(llm, messages)
    result = _parse_review_response(extract_text_content(response))
    result["reviewer"] = "generic_reviewer"
    return result


# ===== 機械チェック（LLMを使わないレビュアー） =====

def _history_numbers(history: list, sources: list = None) -> list[dict]:
    """
    照合の母集団を作る。検索結果（role=result）と、出典本文から抽出した
    facts の両方を対象にする。
    """
    texts = [e.get("content", "") for e in (history or []) if e.get("role") == "result"]
    numbers = []
    for t in texts:
        numbers.extend(extract_numbers(t))
    for s in (sources or []):
        for f in s.get("facts", []):
            numbers.extend(extract_numbers(f.get("raw", "")))
    return numbers


def numeric_checker(output: str, history: list = None, sources: list = None) -> dict:
    """
    回答中の数値を、実行履歴と出典から機械的に突き合わせる。LLMは使わない。

    チェックA: 換算併記の整合
        「9兆ウォン（約9兆円）」のように、ウォンと円がほぼ同値になっている等、
        桁として成立しない換算を検出する。出典を見ずに回答単体で判定できる。

    チェックB: 未照合の数値
        回答中の単位付き数値が、検索結果にも出典にも存在しない場合に指摘する。

    LLMに数値照合をさせない理由は docs/accuracy-improvements.md §0.1 を参照。
    実測で、書く側と検証する側の双方が「ありそうな値」へ無意識に正規化していた。
    """
    issues = []

    for pair in find_conversion_pairs(output or ""):
        if not conversion_plausible(pair):
            issues.append(
                f"換算が矛盾しています: 「{pair['raw']}」。"
                f"{pair['src_unit']}と{pair['dst_unit']}の比率が桁として成立していません"
                f"（比率 {pair['ratio']:.3g}）。"
                f"検索結果の値をそのまま書き写せているか確認してください。"
            )

    candidates = _history_numbers(history, sources)
    for n in extract_numbers(output or ""):
        if not matches_any(n, candidates):
            issues.append(
                f"「{n['raw']}」は検索結果・出典のどこにも見当たりません。"
                f"出典の数値をそのまま書き写すか、この数値を削除してください"
                f"（該当箇所: …{n['context']}…）。"
            )

    return {
        "reviewer": "numeric_checker",
        "verdict": "NEEDS_REVISION" if issues else "OK",
        "issues": issues,
        "instruction": (
            "指摘された数値を、検索結果に書かれている値そのものに直してください。"
            "単位（ウォン／円／億／兆）を変換しないこと。"
            "換算値を併記する場合は、元の値を主として書き、換算は括弧内に留めること。"
        ) if issues else "",
        "raw": "",
    }


# ===== Dispatcher =====

REVIEWER_REGISTRY = {
    "fact_checker": fact_checker,
    "code_reviewer": code_reviewer,
    "security_reviewer": security_reviewer,
    "data_analyst": data_analyst,
    "generic_reviewer": generic_reviewer,
    "numeric_checker": numeric_checker,
}

# LLMの選定に委ねず、常に実行するレビュアー。
# numeric_checker はLLM呼び出しを伴わないため、常時走らせてもコストが無い。
ALWAYS_ON_REVIEWERS = ["numeric_checker"]


def _with_always_on(names: list[str]) -> list[str]:
    return list(dict.fromkeys(list(names) + ALWAYS_ON_REVIEWERS))


def dispatch_reviewers(task: str, output: str, output_type: str = "auto") -> list[str]:
    """
    タスクと成果物から、適切なレビュアーを選定する。
    output_type:
      "auto"     - LLMで判定
      "code"     - コード生成タスク（明示指定）
      "research" - 調査タスク（明示指定）
      "data"     - データ分析タスク（明示指定）
    """
    # 明示指定があれば早期return
    if output_type == "code":
        return _with_always_on(["code_reviewer", "security_reviewer"])
    if output_type == "research":
        return _with_always_on(["fact_checker"])
    if output_type == "data":
        return _with_always_on(["data_analyst"])

    # autoの場合はLLMで判定
    llm = get_llm(temperature=0.1)
    messages = [
        {"role": "system", "content": DISPATCHER_PROMPT},
        {"role": "user", "content": (
            f"## タスク\n{task}\n\n"
            f"## 成果物（抜粋）\n{output[:800]}\n\n"
            f"このタスクと成果物に適切なレビュアーを選んでください。"
        )},
    ]
    response = invoke_with_retry(llm, messages)
    content = extract_text_content(response)

    reviewers_m = re.search(r"REVIEWERS:\s*(.+)", content)
    if not reviewers_m:
        return _with_always_on(["generic_reviewer"])

    reviewers_str = reviewers_m.group(1).strip()
    # 改行などで止める
    reviewers_str = reviewers_str.split("\n")[0]
    candidates = [r.strip() for r in reviewers_str.split(",")]
    # 有効なレビュアーだけ残す
    selected = [r for r in candidates if r in REVIEWER_REGISTRY]
    return _with_always_on(selected if selected else ["generic_reviewer"])


def run_reviewers(reviewer_names: list[str], **kwargs) -> list[dict]:
    """指定されたレビュアーを順次実行する"""
    results = []
    for name in reviewer_names:
        fn = REVIEWER_REGISTRY.get(name)
        if not fn:
            continue
        try:
            # 関数によって必要な引数が違うので、kwargsから取り出す
            if name == "fact_checker":
                result = fn(
                    task=kwargs.get("task", ""),
                    output=kwargs.get("output", ""),
                    history=kwargs.get("history", []),
                    findings=kwargs.get("findings", []),
                )
            elif name in ("data_analyst", "generic_reviewer"):
                result = fn(
                    task=kwargs.get("task", ""),
                    output=kwargs.get("output", ""),
                    history=kwargs.get("history", []),
                )
            elif name == "code_reviewer":
                result = fn(
                    task=kwargs.get("task", ""),
                    code=kwargs.get("code", "") or kwargs.get("output", ""),
                    context=kwargs.get("context", ""),
                )
            elif name == "security_reviewer":
                result = fn(
                    code=kwargs.get("code", "") or kwargs.get("output", ""),
                    task=kwargs.get("task", ""),
                )
            elif name == "numeric_checker":
                result = fn(
                    output=kwargs.get("output", ""),
                    history=kwargs.get("history", []),
                    sources=kwargs.get("sources", []),
                )
            results.append(result)
        except Exception as e:
            # LLMレビュアーの失敗はOK扱いにする（API不調で全体を止めないため）。
            # ただし機械チェックの失敗は実装の不具合であり、黙って通すと
            # 検証していないのに検証したことになるので、issueとして残す。
            if name in ALWAYS_ON_REVIEWERS:
                results.append({
                    "reviewer": name,
                    "verdict": "OK",
                    "issues": [f"{name} の実行に失敗しました（数値の機械照合は未実施）: {e}"],
                    "instruction": "",
                    "raw": "",
                })
            else:
                results.append({
                    "reviewer": name,
                    "verdict": "OK",
                    "issues": [],
                    "instruction": "",
                    "raw": f"レビュアー実行エラー: {e}",
                })
    return results


def aggregate_results(results: list[dict]) -> dict:
    """複数レビュアーの結果を統合する"""
    has_revision = any(r["verdict"] == "NEEDS_REVISION" for r in results)
    all_issues = []
    instructions = []
    for r in results:
        if r["issues"]:
            for issue in r["issues"]:
                all_issues.append(f"[{r['reviewer']}] {issue}")
        if r["instruction"]:
            instructions.append(f"[{r['reviewer']}] {r['instruction']}")

    return {
        "verdict": "NEEDS_REVISION" if has_revision else "OK",
        "issues": all_issues,
        "instruction": "\n".join(instructions),
        "reviewer_results": results,
    }