# reviewers.py — 専門家レビュアープール
# 各レビュアーは {"verdict": "OK"|"NEEDS_REVISION", "issues": [...], "instruction": "..."} を返す
import re
from config import get_llm, extract_text_content, invoke_with_retry
from datetime import datetime
from numeric import (
    extract_numbers, find_conversion_pairs, conversion_plausible, matches_any,
    format_findings, forecast_marked_as_actual, untagged_ratio,
    dropped_supported_numbers, sign_conflicts, FORECAST_WORDS,
    label_value_mismatches, candidates_for, tag_conflicts,
    appears_verbatim, labeled_values, primary_label, TRUNCATION_MARK,
    candidate_suggestion, ohlc_values, matches_bare, source_texts, mask_urls,
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
    "9. **予想と実績の取り違え（最重要）**: 「コンセンサス」「見通し」「見込み」と\n"
    "   書かれた出典の数値が、最終回答で実績値のように扱われていないか確認してください。\n"
    "   各数値に [実績] / [予想] の区別が付いているかも見てください。付いていなければ\n"
    "   NEEDS_REVISION としてください。\n"
    "10. **市場・通貨の混同**: 同じ企業が複数市場に上場している場合\n"
    "   （例: 韓国取引所のウォン建てと米国ADRのドル建て）、異なる市場・通貨の数値が\n"
    "   断りなく同じ項目に並べられていないか確認してください。出典の記載が、その数値を\n"
    "   実際に取得したページと食い違っていないかも確認してください。\n"
    "11. **株価の時点**: 株価・騰落率に「いつ時点か」「どの市場か」が併記されているか\n"
    "   確認してください。「ある時点において」のような曖昧な記述は不十分です。\n"
    "12. **根拠のない記述（最重要）**: 最終回答の各段落について、その内容が\n"
    "   実行履歴の検索結果に由来するか確認してください。履歴に無い一般論・推測・\n"
    "   業界知識で埋められた段落があれば、**加筆ではなく削除**を指示してください。\n"
    "\n"
    "## 指示を書くときの制約（必ず守ること）\n"
    "- 指示は「実行履歴のどの情報を追加・訂正・削除すべきか」に限ること。\n"
    "- **結論の方向（好調/不調、上昇/下落、懸念/反転）を指示してはいけない。**\n"
    "  レビュアーが結論を先に決めると、エージェントはその結論に合わせて\n"
    "  根拠のない記述を作り出す。実測で、「懸念→反転のストーリーラインを\n"
    "  作成してください」と指示した結果、反転を裏付ける情報が履歴に一切無いまま\n"
    "  「市場は再評価が進んでいる段階」と作文させてしまった事故が起きている。\n"
    "- 「ストーリー」「対比構造」「説得力」「網羅性」を理由に加筆を求めないこと。\n"
    "  これは報告書であって物語ではない。空白のまま残す方が、埋めるより良い。\n"
    "- 実行履歴に無い情報の追加を求めないこと。履歴に無いなら、\n"
    "  「確認できなかった」と書かせるのが正しい対応である。\n"
    "- 章立てが揃っていないこと自体は問題ではない。情報が無い章は削らせること。\n"
    "- 「欠落している」と指摘するときは、その情報が実行履歴や数値一覧の\n"
    "  どこにあるかを示すこと（例:「Step 4 の検索結果にある」）。\n"
    "  在り処を示せば、エージェントは再検索せず読み直しで直せる。\n"
    "- 誤りの削除を求めるときは、正しい値への置き換えまで指示すること。\n"
    "  「この記述は不正確です。削除するか、次の正しい値に置き換えてください: ◯◯」\n"
    "  という形にすること。削除だけの指示は禁止。置き換える値が履歴に無い\n"
    "  場合は「置き換えられる値は履歴にありません」と明記すること。\n"
    "- **文章の構成・トーン・見出しの型・章の並びに口を出さないこと。**\n"
    "  レビュアーの仕事は事実と出典の整合性の検証だけである。\n"
    "- 「懸念点と好材料を対比させて」のような書き方の指示は、\n"
    "  **その両方が実際に検索結果に存在する場合に限る**。\n"
    "  片方しか無いなら、無い側を書かせないこと。\n"
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

    ここは置換候補の母集団でもある。内部メッセージを混ぜると、correct が
    却下したばかりの値を「候補」として提案し返すことになる。
    """
    # URLは潰す。潰さないと %XX が「XX%」として候補に並び、照合も素通りする
    texts = [mask_urls(t) for t in source_texts(history)]
    numbers = []
    for t in texts:
        numbers.extend(extract_numbers(t))
    for s in (sources or []):
        for f in s.get("facts", []):
            numbers.extend(extract_numbers(f.get("raw", "")))
    return numbers


def _forecast_in_context(history: list, findings: list) -> bool:
    """取得済みの情報に、予想値であることを示す語が含まれているか。

    差し戻し文にも「予想」の語は普通に出るので、出典だけを見る。
    """
    texts = source_texts(history)
    texts += [f.get("context", "") for f in (findings or [])]
    return any(w in t for t in texts for w in FORECAST_WORDS)


def numeric_checker(output: str, history: list = None, sources: list = None,
                    findings: list = None, previous_output: str = None) -> dict:
    """
    回答中の数値を、実行履歴と出典から機械的に突き合わせる。LLMは使わない。

    チェックA: 換算併記の整合
        「9兆ウォン（約9兆円）」のように、ウォンと円がほぼ同値になっている等、
        桁として成立しない換算を検出する。出典を見ずに回答単体で判定できる。

    チェックB: 未照合の数値
        回答中の単位付き数値が、検索結果にも出典にも存在しない場合に指摘する。
        株価の時系列表は単位も区切りも無く TOKEN_RE では拾えないため、
        専用経路（ohlc_values）で読んだ値とも突き合わせる。実測で、原文に
        ある終値・高値・安値を「出典に無い」と却下した事故が起きている。

    チェックC: 予想を実績として書いていないか
        [実績] と書かれた数値の出典側の文脈に「予想」「コンセンサス」等が
        あれば指摘する。実測で、発表前のコンセンサスを実績として書いた事故がある。

    チェックD: 種別タグの欠落
        単位付き数値に [実績] / [予想] が1つも付いていない場合に指摘する。
        数値ごとに指摘すると量が増えるため、全体で1件にまとめる。

    チェックF: 符号の食い違い
        「+5.2%（-1,200円）」のように、変動率と変動額の符号が逆の記述を検出する。

    チェックE: 情報量の後退
        前回の回答にあり台帳にも裏付けがあった数値が、今回消えていれば指摘する。
        訂正ループは「消す」方向にしか働かないため、実績値ごと落ちることがある。

    LLMに数値照合をさせない理由は docs/accuracy-improvements.md §0.1 を参照。
    実測で、書く側と検証する側の双方が「ありそうな値」へ無意識に正規化していた。
    """
    issues = []

    # 回答本文のURLも潰す。§7 で出典明記を求めているので、回答に
    # 「出典: https://…」が入るのは通常のケース。潰さないと %XX が
    # 「XX%」として拾われ、1本のURLで7件の偽の指摘が出る。
    # 差し戻しの枠には余白が無いので、偽の指摘が本物の枠を消費してしまう。
    #
    # 判定はこのマスク済みテキストに対して行い、指摘文に載せる位置や
    # 本文の書き換え（mechanical_fixes）は元のテキストのまま扱う。
    # 空白で潰しているので start / end は元テキストと一致する。
    ans = mask_urls(output or "")

    for pair in find_conversion_pairs(ans):
        if not conversion_plausible(pair):
            issues.append(
                f"換算が矛盾しています: 「{pair['raw']}」。"
                f"{pair['src_unit']}と{pair['dst_unit']}の比率が桁として成立していません"
                f"（比率 {pair['ratio']:.3g}）。"
                f"検索結果の値をそのまま書き写せているか確認してください。"
            )

    # 連結せずテキストの並びとして持つ。連結すると、別々のページの末尾と
    # 先頭がたまたま隣り合って「ラベルが近い」と誤判定する。
    #
    # 履歴からは出典由来のものだけを採る。correct / critic の差し戻し文には
    # 却下した数値がそのまま引用されているので、混ぜるとその値が
    # 「出典にある」ことになり、次のラウンドで素通りする。
    # URLは潰しておく。潰さないと、回答の「88%」が出典URLの
    # パーセントエンコーディング（%E5 等）と一致して「出典にある」ことになる
    src_texts = [mask_urls(t) for t in (
        source_texts(history)
        + [f.get("context", "") for f in (findings or [])]
        + [s.get("excerpt", "") for s in (sources or [])]
    )]
    # 元ページが取得上限で切れていたか。抜粋に印は残らないので、
    # 構造化フィールドを正とし、印は後方互換のために併せて見る
    truncated = (any(s.get("source_truncated") for s in (sources or []))
                 or any(TRUNCATION_MARK in t for t in src_texts))

    candidates = _history_numbers(history, sources)
    # 時系列表から読み取れた価格。単位が無いので「存在するか」の判定にだけ使う
    bare = ohlc_values(src_texts)
    answer_labels = {lv["raw"]: primary_label(lv) for lv in labeled_values(ans)}
    for n in extract_numbers(ans):
        # 抽出は完璧ではない。原文にその表記がそのまま出ているなら、
        # 「出典に無い」と断じない（実測で、原文にある売上高が却下された）
        if (not matches_any(n, candidates)
                and not appears_verbatim(n["raw"], src_texts)
                and not matches_bare(n, bare)):
            label = answer_labels.get(n["raw"], "")
            kind, hints = candidate_suggestion(n, findings or [], label=label)
            if kind == "labeled":
                suggestion = (f"【置換候補】{'、'.join(hints)}"
                              f"（出典で「{label}」に付いている値）に置き換えてください")
            elif kind == "unlabeled":
                suggestion = (f"【候補（参考）】{'、'.join(hints)}"
                              "。単位と桁が近いだけで、同じ項目の値とは限りません")
            else:
                suggestion = (
                    "【置換候補なし】"
                    + (f"出典に「{label}」の値は見当たりません。" if label else "")
                    + "削除するか、出典を取り直してください"
                )
            trunc_note = (
                "（出典の本文が途中で切れています。切れた先に書かれている可能性があります）"
                if truncated else ""
            )
            issues.append(
                f"「{n['raw']}」は検索結果・出典のどこにも見当たりません。"
                f"{suggestion}{trunc_note}（該当箇所: …{n['context']}…）。"
            )

    for m in label_value_mismatches(ans, findings or [], src_texts):
        if m["reason"] == "value":
            issues.append(
                f"「{m['label']}」の値が出典と違います。回答は「{m['raw']}」ですが、"
                f"出典で「{m['label']}」に対応するのは {'、'.join(m['expected'])} です。"
                f"出典の値に置き換えてください。"
            )
        else:
            issues.append(
                f"「{m['raw']}」を「{m['label']}」として書いていますが、出典では"
                f"{'、'.join(m['expected'])} に付いている値です"
                f"（出典: …{m['context'][:50]}…）。"
                f"ラベルの対応を出典どおりに直すか、この記述を削除してください。"
            )

    for t in tag_conflicts(ans, findings or []):
        issues.append(
            f"「{t['raw']}」の種別タグが出典と違います。回答は {t['written']} ですが、"
            f"出典の文脈は {t['expected']} です（出典: …{t['context'][:50]}…）。"
        )

    for m in forecast_marked_as_actual(ans, findings or []):
        issues.append(
            f"「{m['raw']}」に [実績] と付いていますが、出典の文脈は予想です"
            f"（出典: …{m['context'][:60]}…）。[予想] に直すか、"
            f"実績値を検索して置き換えてください。"
        )

    untagged, total = untagged_ratio(ans)
    if total and untagged == total and _forecast_in_context(history, findings):
        # タグの欠落を常に差し戻すと、予想と実績の取り違えが起こりえない
        # タスクでも書式だけのために1ラウンド消える。取得済みの情報に
        # 「予想」「コンセンサス」等が混ざっているときに限って指摘する。
        issues.append(
            f"数値{total}件のいずれにも [実績] / [予想] が付いていません。"
            f"取得済みの情報には予想値が含まれています。"
            f"判断できないものは [種別不明] と書き、省略しないでください。"
        )

    for c in sign_conflicts(ans):
        issues.append(
            f"符号が食い違っています: 変動率「{c['rate']}」と変動額「{c['amount']}」"
            f"（該当箇所: …{c['context']}…）。"
            f"どちらかが誤りです。出典で裏を取るか、"
            f"「符号不整合のため要確認」と明記してください。"
        )

    for d in dropped_supported_numbers(mask_urls(previous_output or ""), ans,
                                       findings or []):
        issues.append(
            f"前回の回答にあった「{d['raw']}」が消えています。"
            f"この数値は取得済みの情報に裏付けがあります。"
            f"誤りだったのでなければ、書き直しの際に戻してください。"
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
#
# 以前はここに numeric_checker が入っていた。だが correct_step が
# critic_step の直前で同じ関数を同じ入力に対して既に呼んでおり、
# 実測で2回・入力完全一致だった。無駄な再実行にとどまらず、
# 訂正予算（max_corrections）を使い切ったあとに critic 枠で同じ機械指摘が
# もう一度差し戻しを起こす（＝上限の迂回）、同じ指摘が最終回答の注記に
# 二重に出る、という実害が出ていた。
#
# 数値照合は correct_step の一箇所で行い、結果を state["numeric_result"]
# に載せて critic へ渡す。critic は再実行しない。
#
# リストと _with_always_on の仕組み自体は残す。「LLMの選定に委ねず必ず
# 走らせたいレビュアー」という枠は今後も要りうるし、run_reviewers 側の
# 「常時レビュアーの失敗は黙って通さない」分岐もこの枠に紐づいている。
ALWAYS_ON_REVIEWERS: list[str] = []


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
                    findings=kwargs.get("findings", []),
                    previous_output=kwargs.get("previous_output", ""),
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