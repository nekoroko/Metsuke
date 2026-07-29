# graph.py — ReActループ + 専門家プール（マルチレビュアー）+ ステップ予算管理
import re
from langgraph.graph import StateGraph, END
from config import get_llm, extract_reasoning_tokens, invoke_with_continuation, extract_text_content
from state import AgentState
from tools import get_tool_fn, get_tool_names, build_workspace_context, is_tool_verifiable
from sandbox import execute_in_sandbox, PROFILE_AGENT_CODE
from reviewers import (
    dispatch_reviewers, run_reviewers, aggregate_results, numeric_checker,
)
from numeric import (
    collect_from_text, merge_findings, format_findings, pending_event_warnings,
    claims_absence, unused_numbers,
)
from datetime import datetime


# --- システムプロンプトの構成要素 ---
#
# 実測（gemma-4-e4b / コンテキスト8192）で、プロンプトが7160トークンまで膨らみ、
# 出力に残った枠1032のうち1029がreasoningに消費されて本文が空になった。
# ルールを足すほど回答を書く枠が失われるため、常時必要なものだけを常設し、
# 残りは局面（探索中／執筆中）で出し分ける。

# 常に入れる
_ALWAYS_0 = '## 回答形式\n毎回、以下のいずれかの形式で回答してください。\n\nツールを使う場合:\nTHOUGHT: (何をしようとしているか。検索結果の数値は出典・単位を含めて正確に書き写すこと)\nACTION: tool_name(引数)\n\nPythonコードを生成・実行する場合:\nTHOUGHT: (何をしようとしているか)\nACTION: generate_code\n```python\n(コード)\n```\n\nタスクが完了した場合:\nTHOUGHT: (結果の要約)\nDONE: (最終結果)\n'
_ALWAYS_1 = '## 制約\n- 標準ライブラリのみ使用すること。pandas, numpy等は使用不可\n- サンプルデータやダミーデータを自分で作らないこと\n- 作業環境に実在するファイルを使うこと\n- 結果はprint()で出力すること\n'
_ALWAYS_2 = '## 数値の書き写し（最優先）\n- **検索結果の数値は、単位を変換せずそのまま書き写すこと。**\n  「83兆ウォン（約9兆円）」と書かれていたら「83兆ウォン（約9兆円）」と書く。\n  ウォンの位置に円の値を入れてはいけない。\n- 換算値は括弧内の補助にとどめ、主となる値は必ず出典の単位のまま書くこと。\n- 「A単位（約B別単位）」の形で書いたら、AとBの桁が妥当か一度確認すること。\n  ウォンと円は10倍程度の差があり、同じ値になることはない。\n- 検索結果に「◯日に発表」等の予定があり、その日が今日以前または数日以内の\n  場合、予想値を書くときは「◯日発表予定のため未確定」と必ず注記すること。\n'

# 探索中だけ入れる（DONEを書く局面では不要）
_SEARCH_0 = '## 検索クエリの組み立て方（重要）\nweb_search や suggest_keywords は、単語を詰め込みすぎると精度が落ちます。\n検索エンジンは短いクエリの方が的確な結果を返します。\n- suggest_keywords: 主題語1つ、または主題語+ジャンル語程度（2語以内）に絞ること\n- web_search: 2〜3語程度に絞ること。1回のクエリで全てを調べようとせず、\n  知りたい観点ごとにクエリを分けて複数回検索すること\n- 検索結果が的外れだった場合は、単語数を減らして再検索すること\n'
_SEARCH_1 = '## 調査タスクの推奨フロー\n1. 最初に suggest_keywords でメインキーワード（1〜2語）のサジェストを取得\n2. サジェストから複数の観点を選び、それぞれ2〜3語のクエリで web_search を実行\n3. 必要に応じて fetch_url で記事本文を確認\n4. 集めた情報を統合してDONEで回答\n\n公式サイトのプレスリリースだけに頼らず、サジェストから世間の関心事を捉えること。\n'
_SEARCH_2 = '## 保存済みツール（Type1）の活用\nタスクに「ツールID xxxを実行」「保存済みの○○ツールで前処理」といった指示がある場合、\nまたは定型的な前処理が必要な場合は、run_saved_tool を活用してください。\n利用可能なツールが分からない時は list_saved_tools で一覧を取得できます。\n'
_SEARCH_3 = '## 出典の選び方\n- 決算・業績・財務の数値は、**企業の公式発表（IR）や主要な経済メディア**を\n  優先すること。株価情報サイトや集計サイトのスニペットだけを根拠にしないこと。\n- 集計サイト（株価ポータル、まとめサイト等）の数値しか得られていない場合は、\n  「一次情報未確認」と明記すること。\n- アナリスト評価・センチメントスコア・掲示板の強気弱気比率などを引用する場合は、\n  **「個人投資家向けサイトの集計値（参考値）」であることを必ず併記**すること。\n  これらは公式の格付けや専門機関の評価とは別物である。\n'
_SEARCH_4 = '## 決算・業績を調べるときの手順\n「決算」「業績」を扱うタスクでは、予想と実績の両方を探すこと。\n「（対象名） 決算」だけで終わらせず、**「（対象名） 決算 実績」または\n「（対象名） 決算 発表」でも最低1回は検索する**こと。\n「決算」だけで検索すると発表前のプレビュー記事（予想）ばかりが集まり、\n予想値を実績値と取り違える原因になる。\n'
_SEARCH_5 = '## 検索結果を評価する際の注意\n直前の検索結果を受け取ったら、次のクエリを考える前に、\nまず「タスクに必要な情報のうち、何がまだ埋まっていないか」を\nTHOUGHT内で明示的に言語化すること。\nその上で、その不足点をピンポイントで埋めるクエリを組み立てること。\n「なんとなく違う言い回しで検索し直す」ことは避けること。\n検索結果の要約（スニペット）だけで数値が読み取れない場合は、\nfetch_urlで該当ページの本文を取得すること。\n'

# 執筆中だけ入れる（検索している間は不要）
_WRITE_0 = '## 数値には出所と種別を必ず付ける\n- すべての数値に **[実績]** か **[予想]** のどちらかを付けること。\n  どちらか判断できない場合は **[種別不明]** と書くこと。省略しないこと。\n  例: 売上高 84.1兆ウォン [予想]（証券14社コンセンサス、2026年7月29日発表予定）\n- 「コンセンサス」「見通し」「見込み」「予想」と書かれた数値は必ず [予想] である。\n  これを実績のように書くことは重大な誤りである。\n- **株価・騰落率には必ず「いつ時点か」と「どの市場か」を併記すること。**\n  例: -8.81%（NASDAQ上場ADR SKHY、2026年7月28日終値）\n  時点が分からない株価データは、「時点不明」と明記するか、採用しないこと。\n- **同じ企業が複数の市場に上場している場合、市場と通貨を分けて書くこと。**\n  例: 韓国取引所 000660.KS（ウォン建て）と NASDAQ ADR SKHY（ドル建て）は別物である。\n  異なる市場・通貨の数値を、断りなく同じ項目に並べてはいけない。\n- 出典を書くときは、その数値を実際に取得したページを書くこと。\n  上の一覧には数値ごとの取得元が併記されているので、それと食い違わせないこと。\n'
_WRITE_1 = '## 情報が足りないとき\n「直近1週間」「最近」等の条件を満たす情報が集まらなかった場合、\n「見つかりませんでした」と書く前に、**期間を明示したクエリで最低1回は再検索**する\nこと（例:「（対象名） 株価 今週」「（対象名） 7月28日」）。\n再検索しても不足する場合に限り、何がどこまで確認できたのかを具体的に書くこと。\n「情報は限定的でした」とだけ書いて終えないこと。\n'
_WRITE_2 = '## DONEを出す前の点検\n「これまでに取得した数値・日付」の一覧を上から1件ずつ確認し、\nタスクの問いに関係するものを回答に含めたか点検すること。\n特に、増減率・価格・日付など、結論の向きを左右する数値を\n取りこぼしていないか確認すること。\n「情報が見つからなかった」と書く前に、この一覧に該当する数値が\n無いか必ず確認すること。一覧にある数値を使わずに「不明」と書くのは誤りである。\n'
_WRITE_3 = '## 表記ルール\n- 「予想」「見通し」「ガイダンス」と「実績」を明確に区別すること\n- 数値には必ず時点（例：2026年5月15日発表）を付記すること\n- 順位・比較情報には観測日を付記すること（例：2026年6月12日時点で首位）\n- 検索結果から実際に得られた情報のみを使い、推測で補完しないこと\n- **複数の検索結果で同じトピックについて異なる情報が出てきた場合、日付を比較して\n  最も新しい情報を採用すること。** 状況が良化・悪化している対象は特に注意すること\n  （例：「業績悪化」の記事と「過去最高益」の記事が両方出てきたら、日付が新しい方を信じる）\n'
_WRITE_4 = '## 期間の要求を満たしているか確認する\nタスクが「直近◯◯以内」のように期間を指定している場合、\nDONEを出す前に、使おうとしている情報の日付が実際にその期間内に\n収まっているか必ず確認すること。今日の日付から逆算して確認すること。\n収まっていない情報しかない場合は、「◯◯時点の情報が最新（直近◯◯以内の\nデータは見つからず）」と明記した上で、より新しい情報を探すクエリで\n再検索すること。古い情報を、あたかも直近の情報であるかのように\n書かないこと。\n'
_WRITE_5 = '## 同じ内容を繰り返し生成しない\n1回の応答につき、THOUGHTとACTION（またはDONE）はそれぞれ1回だけ\n書くこと。一度ACTION: generate_codeやACTION: tool_name(...)を書いたら、\n同じ回答の中でもう一度別のACTIONを書き直したり、同じ内容を形を変えて\n繰り返したりしないこと。DONEを出す場合も同様で、直前に別のACTIONで\n書いた内容をDONEの本文にそのまま書き写す必要はない。すでに結論が\n出ているなら、それ以上検討し直さず、そのままDONEで終えること。\n'

SECTIONS_ALWAYS = [_ALWAYS_0, _ALWAYS_1, _ALWAYS_2]
SECTIONS_SEARCH = [_SEARCH_0, _SEARCH_1, _SEARCH_2, _SEARCH_3, _SEARCH_4, _SEARCH_5]
SECTIONS_WRITE = [_WRITE_0, _WRITE_1, _WRITE_2, _WRITE_3, _WRITE_4, _WRITE_5]
# 「不完全な日付を勝手に補完しない」は現在の月を埋め込むため、
# 定数ではなく build_system_prompt 内で生成する。


def _with_trace(prev_state: dict, out: dict, node: str, summary: str,
                next_node: str = "", note: str = "", skipped: bool = False) -> dict:
    """
    ノードの実行を1件記録して返す。

    履歴（history）はLLMへ渡す会話ログなので、そこにノード情報を混ぜると
    トリミングの枠を食ってしまう。実行経路の記録は別系統（trace）で持つ。

    カウンタは「そのノードを抜けた時点」の値を入れる。
    """
    prev = prev_state.get("trace") or []
    entry = {
        "seq": len(prev) + 1,
        "node": node,
        "from": prev[-1]["node"] if prev else "(開始)",
        "summary": summary,
        "next": next_node,
        "note": note,
        "skipped": skipped,
        "status": out.get("status", prev_state.get("status", "")),
        "step_count": out.get("step_count", prev_state.get("step_count", 0)),
        "critique_count": out.get("critique_count", prev_state.get("critique_count", 0)),
        "correction_count": out.get("correction_count", prev_state.get("correction_count", 0)),
        "tool_verify_count": out.get("tool_verify_count", prev_state.get("tool_verify_count", 0)),
    }
    return {**out, "trace": prev + [entry]}


def _findings_section(findings: list) -> str:
    """
    数値台帳をプロンプトへ埋め込む節を作る。

    履歴は MAX_HISTORY_ROUNDS で切り詰められるため、古いステップで得た数値は
    DONE を書く時点では見えなくなっている。実測では、検索結果にあった株価の
    下落率（-7.47%）が窓の外に落ち、最終回答から丸ごと欠落していた。
    台帳は数値と日付だけを保持しトリミングしないので、この欠落を防げる。
    """
    body = format_findings(findings or [])
    if not body:
        return ""

    section = (
        "## これまでに取得した数値・日付\n"
        "以下は、これまでの検索結果から機械的に抜き出したものです。原文のままなので、\n"
        "回答に書くときはこの表記を変えないでください（単位も変換しないこと）。\n"
        f"{body}\n\n"
    )

    # 予定日が到来しているイベントは、日付比較をコード側で行って明示する。
    # 「本日発表予定」に気づかず予想値を確定値のように書く事故を防ぐ。
    warnings = pending_event_warnings(findings or [])
    if warnings:
        section += (
            "## 注意: 予定日が到来しているイベントがあります\n"
            + "\n".join(f"- {w}" for w in warnings)
            + "\n"
            "これらは「予定」として書かれた情報ですが、予定日はすでに到来しています。\n"
            "DONEを出す前に「（対象名） 速報」「（対象名） 発表」等で**必ず再検索**し、\n"
            "実際に発表済みか、実績値が出ていないかを確認してください。\n"
            "確認できない場合は、予想値であることと「◯日発表予定（未確認）」である旨を\n"
            "両方明記してください。予想値を実績値のように書いてはいけません。\n\n"
        )
    return section


def build_system_prompt(step_count: int = 0, max_steps: int = 10,
                        reasoning_detected: bool = False,
                        findings: list = None) -> str:
    """
    システムプロンプトを構築する。

    ルールは足せば足すほど良くなるわけではない。実測（gemma-4-e4b /
    コンテキスト8192）で、プロンプトが7160トークンまで膨らんだ結果、
    出力に残った枠が1032しかなく、そのうち1029がreasoningに消費されて
    本文が空になり、「回答形式を認識できませんでした」で1ループ潰れた。
    ルールを増やすことと、回答を書く枠を残すことはトレードオフの関係にある。

    そのため、局面によって不要な節は落とす。
    - 探索中（まだ検索している）: 検索の組み立て方は要るが、
      最終回答の書式ルールはまだ要らない
    - 執筆中（そろそろDONE）    : その逆

    どちらの局面でも要るもの（回答形式・制約・数値の書き写し）は常設する。
    """
    tool_names = get_tool_names()
    workspace = build_workspace_context()
    today = datetime.now().strftime("%Y年%m月%d日")
    current_year = datetime.now().year
    current_month = datetime.now().month
    remaining = max_steps - step_count

    # Thinkingが検知されている場合、1ステップあたりのトークン消費が跳ね上がるため、
    # 収束指示のしきい値を早める（通常3→5、通常1→2）
    warn_threshold = 5 if reasoning_detected else 3
    critical_threshold = 2 if reasoning_detected else 1

    if remaining <= critical_threshold:
        budget_section = (
            "\n## ステップ予算: 残りわずか（最優先で守ること）\n"
            f"実行可能な残りステップ数は {remaining} です。今回の応答で必ず DONE を出してください。\n"
            "新しい検索・調査（ACTION）は行わないでください。これまでに得た情報だけを使い、\n"
            "確実に分かっていることだけで簡潔に回答をまとめてください。\n"
            "不確実な情報、未検証の推測、裏取りできていない数値は含めないか、\n"
            "「未確認」と明記してください。情報を新たに付け足すより、削って正確性を優先すること。\n"
        )
    elif remaining <= warn_threshold:
        budget_section = (
            "\n## ステップ予算: 残り少なめ\n"
            f"実行可能な残りステップ数は {remaining} です。新規の調査は本当に必要なものだけに絞り、\n"
            "既に集めた情報の整理・訂正・裏取りを優先してください。\n"
            "そろそろ DONE でまとめる準備をしてください。追加調査は1〜2回まで。\n"
        )
    else:
        budget_section = ""

    # 残りステップが少なくなったら「執筆中」とみなす。
    # critic や correct から差し戻された場合も step_count が進んでいるため、
    # 自然にこちら側へ寄る。
    writing_phase = remaining <= warn_threshold

    parts = [
        "あなたは自律型タスク実行エージェントです。\n"
        f"現在の日付は {today} です。\n"
        f"「最新」「直近」と言われた場合は {current_year}年 のデータを意味します。\n"
        f"古い年度（{current_year-2}年以前）の情報を「最新」として扱わないでください。\n\n"
        "与えられたタスクを、ツールの使用またはPythonコードの生成・実行によって完了させてください。\n\n"
        "## 利用可能なツール\n"
        f"{tool_names}\n"
        "- read_file(path): ファイルを読む\n"
        "- write_file(path|content): ファイルに書く\n"
        "- list_directory(path): ディレクトリ一覧\n"
        "- run_shell(command): シェルコマンド実行（サンドボックス内。通信は遮断されている）\n"
        "- fetch_url(url): URL取得\n"
        "- web_search(query): Web検索\n"
        "- suggest_keywords(query): キーワードのサジェスト取得\n"
        "- list_saved_tools(): 保存済みツール（Type1）の一覧\n"
        "- run_saved_tool(tool_id): 保存済みツールを実行\n\n"
    ]

    parts += SECTIONS_SEARCH if not writing_phase else SECTIONS_WRITE
    parts.append(budget_section + "\n")
    parts.append(_findings_section(findings))
    parts.append(f"## 作業環境\n{workspace}\n\n")
    parts += SECTIONS_ALWAYS

    if writing_phase:
        parts.append(
            "## 不完全な日付を勝手に補完しない\n"
            f"月や年の無い日付（例:「25日」）を、今日の月（現在は{current_month}月）だと\n"
            "決めつけないこと。手がかりが無ければ「月不明」と正直に書くこと。\n"
            "また、今日より未来の日付を「すでに起きたこと」として書かないこと。\n\n"
        )

    return "".join(parts)


MAX_HISTORY_ROUNDS = 3
MAX_ASSISTANT_LEN = 800
MAX_RESULT_LEN = 1000


def _parse_done(text: str) -> str:
    """
    DONE の本文を取り出す。

    以前は `DONE:` をテキスト中のどこからでも拾っていたため、
    「最終回答を『DONE: 』形式で再構成します」のように**書式そのものに
    言及した文**にヒットし、本文が「」形式で再構成します…」から始まる
    壊れた回答になっていた。しかも直前のフォーマット警告メッセージが
    「'DONE: ' と書いてください」と指示しているため、モデルにその文言を
    書かせて自分で踏む形になっていた。

    そのため、まず行頭の DONE: だけを見る。あわせて、実際に頻出する
    `ACTION: DONE` 形式もフォールバックとして受け付ける
    （これを弾くと1ループ丸ごと無駄になるうえ、再試行でも同じ形式が
     出てくることが実測で確認されている）。
    """
    m = re.search(r"^[ \t　]*DONE:[ \t　]*", text, re.MULTILINE)
    if m:
        content = text[m.end():].strip()
        if content:
            return content

    m = re.search(r"^[ \t　]*ACTION:[ \t　]*DONE[ \t　]*$", text, re.MULTILINE)
    if m:
        rest = text[m.end():].strip()
        rest = re.sub(r"^THOUGHT:[ \t　]*", "", rest)
        if rest:
            return rest

    return ""


def parse_action(text: str) -> dict:
    """LLMの出力（自由記述テキスト）からアクションを解析する。"""
    done_content = _parse_done(text)
    if done_content:
        return {"type": "done", "content": done_content}

    if "generate_code" in text.lower():
        code_match = re.search(r"```python\s*\n(.+?)```", text, re.DOTALL)
        if code_match:
            return {"type": "code", "content": code_match.group(1).strip()}
        return {"type": "code", "content": ""}

    action_match = re.search(r"ACTION:\s*(\w+)\((.+?)\)", text, re.DOTALL)
    if action_match:
        tool_name = action_match.group(1).strip()
        tool_arg = action_match.group(2).strip()
        kw_match = re.match(r'\w+\s*=\s*(.+)', tool_arg)
        if kw_match:
            tool_arg = kw_match.group(1).strip()
        tool_arg = tool_arg.strip("'\"")
        return {"type": "tool", "name": tool_name, "arg": tool_arg}

    code_match = re.search(r"```python\s*\n(.+?)```", text, re.DOTALL)
    if code_match:
        return {"type": "code", "content": code_match.group(1).strip()}

    return {"type": "unknown", "content": text}


def _force_finalize(state: AgentState):
    """
    ステップ上限に達しても DONE が出なかった場合の最終手段。
    これまでの履歴だけを使って、1回限りの強制収束呼び出しを行う。
    """
    llm = get_llm(temperature=0.1, boost_tokens=True)

    history_summary = ""
    for entry in state["history"][-6:]:
        content = entry.get("content", "")
        if not content:
            continue
        snippet = content[:600]
        if entry["role"] == "assistant":
            history_summary += f"\n[Agent] {snippet}\n"
        elif entry["role"] == "result":
            history_summary += f"\n[Result] {snippet}\n"

    prompt = (
        "ステップ数の上限に達しました。新しい調査はもうできません。\n"
        "これまでの調査結果だけを使って、タスクへの回答をまとめてください。\n"
        "確実に分かっていることだけで簡潔にまとめ、不確実な情報や未検証の推測は\n"
        "含めないか「未確認」と明記してください。情報を付け足すより、削って正確性を優先すること。\n"
        "箇条書き中心で、600文字程度を目安に簡潔にまとめてください。\n"
        "回答は必ず 'DONE: ' から始めてください。\n\n"
        f"タスク: {state['task']}\n\n"
        f"これまでの調査結果:\n{history_summary}\n"
    )

    messages = [
        {"role": "system", "content": "あなたは自律型タスク実行エージェントです。指示に厳密に従ってください。"},
        {"role": "user", "content": prompt},
    ]

    try:
        content, _resp = invoke_with_continuation(llm, messages, max_continuations=2)
        if not content or not content.strip():
            return None
        if "DONE:" not in content:
            content = f"DONE: {content.strip()}"
        return content
    except Exception:
        return None


def react_step(state: AgentState) -> AgentState:
    """
    ReActループの1ステップを実行する。

    自由記述（THOUGHT/ACTION/DONE）+ 正規表現パース方式。

    以前、構造化出力（with_structured_output）に切り替えたが、
    以下の理由で撤回した:
    - 'DONE:書き忘れ'バグはレアで、既存のリトライ＋フィードバック
      ループで自己修復できていた（改修コストに見合わなかった）
    - thoughtフィールドを短く強制した結果、検索結果の数値を丁寧に
      書き写して照合する「作業スペース」が失われ、単位混同のような
      新しい捏造バグ（例: 「36.9兆ウォン」と「3.9兆円」を混同して
      存在しない「3.7兆ウォン」を生成）を誘発した
    - JSON構造化出力は「途中で切れたら即座に全損」という、
      自由記述より脆い失敗モードを新たに抱え込んだ

    「レアなバグを、頻発する内容劣化と交換してしまった」という判断で
    自由記述方式に戻している。フォーマット逸脱への耐性は、
    具体的なフィードバックメッセージ（下記の空応答ガード・unknown処理）
    と invoke_with_continuation の軽量継続で担保する。
    """
    if state["step_count"] >= state["max_steps"]:
        forced = _force_finalize(state)
        if forced:
            new_history = state["history"] + [{"role": "assistant", "content": forced}]
            return _with_trace(state, {
                **state,
                "history": new_history,
                "status": "done",
                "step_count": state["step_count"] + 1,
                "last_action_type": "done",
                "last_tool_name": "",
            }, "react", "ステップ上限に到達したため強制的にまとめた", "END",
                note=f"step_count={state['step_count']} >= max_steps={state['max_steps']}")
        return _with_trace(state, {
            **state, "status": "error", "last_action_type": "", "last_tool_name": "",
        }, "react", "強制収束にも失敗", "END", note="回答を生成できなかった")

    reasoning_detected = state.get("reasoning_detected", False)
    llm = get_llm(temperature=0.1, boost_tokens=reasoning_detected)

    trimmed_history = state["history"][-(MAX_HISTORY_ROUNDS * 2):]

    messages = [
        {"role": "system", "content": build_system_prompt(
            state["step_count"], state["max_steps"], reasoning_detected,
            state.get("findings"),
        )},
        {"role": "user", "content": f"タスク: {state['task']}"},
    ]

    for entry in trimmed_history:
        if not entry.get("content"):
            continue
        if entry["role"] == "assistant":
            content = entry["content"]
            if len(content) > MAX_ASSISTANT_LEN:
                content = content[:MAX_ASSISTANT_LEN] + "\n...(以下省略)"
            messages.append({"role": "assistant", "content": content})
        elif entry["role"] == "result":
            content = entry["content"]
            if len(content) > MAX_RESULT_LEN:
                content = content[:MAX_RESULT_LEN] + "\n...(以下省略)"
            messages.append({"role": "user", "content": f"実行結果:\n{content}"})

    max_conts = 5 if reasoning_detected else 3
    llm_output, response = invoke_with_continuation(llm, messages, max_continuations=max_conts)

    # Thinking抑制が効いているか検知（設定に関わらず実際の応答を見る）
    if not reasoning_detected:
        try:
            r_tokens = extract_reasoning_tokens(response)
            if r_tokens > 0:
                reasoning_detected = True
        except Exception:
            pass

    # 空応答ガード
    if not llm_output or not llm_output.strip():
        new_history = state["history"] + [{
            "role": "result",
            "content": "LLMが空応答を返しました。THOUGHT/ACTION/DONE形式で再度回答してください。"
        }]
        return _with_trace(state, {
            **state,
            "history": new_history,
            "status": "running",
            "step_count": state["step_count"] + 1,
            "reasoning_detected": reasoning_detected,
            "last_action_type": "",
            "last_tool_name": "",
        }, "react", "LLMが空応答を返した", "react", note="再回答を促した")

    action = parse_action(llm_output)
    new_history = state["history"] + [{"role": "assistant", "content": llm_output}]

    if action["type"] == "done":
        return _with_trace(state, {
            **state,
            "history": new_history,
            "status": "needs_revision",
            "step_count": state["step_count"] + 1,
            "reasoning_detected": reasoning_detected,
            "last_action_type": "done",
            "last_tool_name": "",
        }, "react", f"DONEを出力（{len(action['content'])}文字）", "correct")

    elif action["type"] == "tool":
        tool_name = action.get("name", "")
        tool_fn = get_tool_fn(tool_name)
        if tool_fn:
            try:
                result = tool_fn(action["arg"])
            except Exception as e:
                result = f"エラー: {e}"
        else:
            result = f"ツール '{tool_name}' は存在しません。generate_codeでPythonコードを生成してください。"
        new_history.append({"role": "result", "content": result})
        verifiable = is_tool_verifiable(tool_name)

        # ツール結果から数値・日付を機械抽出して台帳へ積む。
        # 履歴と違ってトリミングされないので、後のステップでも参照できる。
        new_findings = merge_findings(
            state.get("findings", []),
            collect_from_text(
                result,
                source=f"{tool_name}({action['arg']})",
                step=state["step_count"] + 1,
            ),
        )
        added = len(new_findings) - len(state.get("findings", []))
        return _with_trace(state, {
            **state,
            "history": new_history,
            "findings": new_findings,
            "status": "running",
            "step_count": state["step_count"] + 1,
            "reasoning_detected": reasoning_detected,
            "last_action_type": "tool",
            "last_tool_name": tool_name,
        }, "react", f"ツール実行: {tool_name}({action['arg'][:40]})",
            "verify_tool" if verifiable else "react",
            note=(f"台帳に{added}件追加" if verifiable
                  else f"台帳に{added}件追加 / verify_tool はスキップ"
                       f"（{tool_name} は品質判定の対象外）"))

    elif action["type"] == "code":
        code = action["content"]
        if not code:
            new_history.append({"role": "result", "content": "コードが空です。Pythonコードブロックを含めてください。"})
            return _with_trace(state, {
                **state,
                "history": new_history,
                "status": "running",
                "step_count": state["step_count"] + 1,
                "reasoning_detected": reasoning_detected,
                "last_action_type": "code",
                "last_tool_name": "",
            }, "react", "generate_code だがコードが空", "react")
        result = execute_in_sandbox(code, **PROFILE_AGENT_CODE)
        if result["success"]:
            output = result["stdout"] if result["stdout"] else "(出力なし)"
        else:
            output = f"エラー:\n{result['stderr']}"
        new_history.append({"role": "result", "content": output})
        return _with_trace(state, {
            **state,
            "history": new_history,
            "generated_code": code,
            "status": "running",
            "step_count": state["step_count"] + 1,
            "reasoning_detected": reasoning_detected,
            "last_action_type": "code",
            "last_tool_name": "",
        }, "react", f"コード実行（{len(code)}文字）", "react",
            note="成功" if result["success"] else "エラー")

    else:
        new_history.append({"role": "result", "content": (
            "回答形式を認識できませんでした。\n"
            "最終回答を書く場合は、行の先頭を DONE から始め、コロンに続けて"
            "本文を書いてください。\n"
            "ツールを使う場合は ACTION の行にツール名と丸括弧の引数を書いてください。\n"
            "書式の説明を本文中で引用せず、実際にその書式で書いてください。"
        )})
        return _with_trace(state, {
            **state,
            "history": new_history,
            "status": "running",
            "step_count": state["step_count"] + 1,
            "reasoning_detected": reasoning_detected,
            "last_action_type": "unknown",
            "last_tool_name": "",
        }, "react", "回答形式を認識できなかった", "react",
            note="THOUGHT/ACTION/DONE のいずれも見つからない")


def verify_tool_step(state: AgentState) -> AgentState:
    """
    直前のツール実行結果が、投げたクエリの意図に答えているかをLLMに軽量判定させる。

    設計方針:
    - 判定だけを行い、実際の再検索は行わない。「この結果は不十分だった、
      次はこのクエリを試すと良い」というフィードバックを履歴に積んで
      react に戻すだけ。実際にそのクエリで動くかどうかは react 自身の
      判断に委ねる（判定と実行の役割を分離する）。
    - web_search/suggest_keywords など、「関連性」という概念が成立する
      ツールだけが対象（tools.py の TOOL_REGISTRY で verifiable=True の
      もの）。run_shellやfetch_url等、成功/失敗はあっても的外れという
      状態が存在しないツールはそもそもこのノードに来ない
      （route_after_reactでフィルタ済み）。
    - step_count（reactの行動選択回数）とは別に tool_verify_count で
      予算管理する。これは「エージェントが選んだ行動」ではなく
      「品質チェックの内部処理」であるため。
    """
    max_verifies = state.get("max_tool_verifies", 6)
    verify_count = state.get("tool_verify_count", 0)

    if verify_count >= max_verifies:
        # 予算切れ。判定をスキップしてそのままreactに戻す
        return _with_trace(state,
            {**state, "status": "running", "tool_verify_count": verify_count + 1},
            "verify_tool", "スキップ", "react",
            note=f"判定の予算切れ（{verify_count}/{max_verifies}）", skipped=True)

    history = state["history"]

    # 直前の assistant（ACTION行を含む）と、その直後の result を取り出す
    last_assistant_content = None
    last_result_content = None
    for entry in reversed(history):
        if entry["role"] == "result" and last_result_content is None:
            last_result_content = entry.get("content", "")
        elif entry["role"] == "assistant" and last_assistant_content is None:
            last_assistant_content = entry.get("content", "")
        if last_assistant_content is not None and last_result_content is not None:
            break

    if not last_assistant_content or not last_result_content:
        return _with_trace(state,
            {**state, "status": "running", "tool_verify_count": verify_count + 1},
            "verify_tool", "スキップ", "react",
            note="判定に必要な履歴が揃っていない", skipped=True)

    action_match = re.search(r"ACTION:\s*(\w+)\((.+?)\)", last_assistant_content, re.DOTALL)
    if not action_match:
        return _with_trace(state,
            {**state, "status": "running", "tool_verify_count": verify_count + 1},
            "verify_tool", "スキップ", "react",
            note="直前のACTION行を解析できない", skipped=True)
    tool_query = action_match.group(2).strip().strip("'\"")

    llm = get_llm(temperature=0.1)
    verify_messages = [
        {"role": "system", "content": (
            "あなたは検索品質を判定する専門のレビュアーです。"
            "クエリと検索結果を見て、その結果がクエリの意図に"
            "答えているかどうかだけを短く判定してください。"
        )},
        {"role": "user", "content": (
            f"クエリ: {tool_query}\n\n"
            f"検索結果:\n{last_result_content[:1500]}\n\n"
            "この結果はクエリの意図に答えていますか？\n"
            "以下の形式で回答してください（説明は不要、この2行だけ）:\n"
            "VERDICT: OK または NG\n"
            "SUGGESTED_QUERY: (NGの場合のみ、次に試すべき具体的なクエリを1つ)"
        )},
    ]

    try:
        verify_output, _resp = invoke_with_continuation(llm, verify_messages, max_continuations=1)
    except Exception as e:
        # 判定自体が失敗したら、安全側でそのままreactに戻す（結果は信じる）
        return _with_trace(state,
            {**state, "status": "running", "tool_verify_count": verify_count + 1},
            "verify_tool", "判定に失敗", "react", note=str(e)[:80], skipped=True)

    verdict_match = re.search(r"VERDICT:\s*(OK|NG)", verify_output or "", re.IGNORECASE)
    verdict = verdict_match.group(1).upper() if verdict_match else "OK"

    new_history = list(history)
    if verdict == "NG":
        suggested_match = re.search(r"SUGGESTED_QUERY:\s*(.+)", verify_output or "", re.DOTALL)
        suggested_query = suggested_match.group(1).strip() if suggested_match else ""
        feedback = (
            f"（自動品質チェック）直前の検索結果はクエリ「{tool_query}」に対して"
            "不十分と判定されました。"
        )
        if suggested_query:
            feedback += f" 次はこのようなクエリを試すことを検討してください: 「{suggested_query}」"
        new_history.append({"role": "result", "content": feedback})

    return _with_trace(state, {
        **state,
        "history": new_history,
        "status": "running",
        "tool_verify_count": verify_count + 1,
    }, "verify_tool", f"検索結果の品質判定: {verdict}", "react",
        note=("再検索を促した" if verdict == "NG" else "結果は妥当と判断"))


def _extract_done(history: list) -> str:
    for entry in reversed(history):
        if entry["role"] == "assistant" and "DONE:" in entry.get("content", ""):
            return entry["content"].split("DONE:")[1].strip()
    return ""


def correct_step(state: AgentState) -> AgentState:
    """
    DONEの内容を機械チェックにかけ、数値の誤りがあれば訂正を差し戻す。

    LLMは呼ばない。critic（LLMレビュー）より前に置いているのは、
    数値が誤ったまま内容レビューに入ると、critic が誤った前提の上に
    指摘を積み上げてしまうため。

    status の使い分けに注意する。既存の route_after_react は
    needs_revision を「critic へ」の意味で使っているので、ここでも
    それに合わせ、訂正が必要なときだけ running にして react へ戻す。
    """
    done_content = _extract_done(state["history"])
    if not done_content:
        return _with_trace(state, {**state, "status": "needs_revision"},
            "correct", "スキップ", "critic",
            note="履歴にDONE本文が見つからない", skipped=True)

    try:
        result = numeric_checker(
            output=done_content,
            history=state["history"],
            sources=state.get("sources", []),
        )
    except Exception as e:
        # 機械チェックの失敗でループを止めない。ただし黙って通さず履歴に残す
        out = {
            **state,
            "history": state["history"] + [{
                "role": "result",
                "content": f"（自動訂正チェック）実行に失敗したため数値の照合は未実施です: {e}",
            }],
            "status": "needs_revision",
            "verification_notes": state.get("verification_notes", [])
            + [f"数値の機械照合を実行できませんでした: {e}"],
            "correction_count": state.get("correction_count", 0) + 1,
        }
        return _with_trace(state, out, "correct", "照合の実行に失敗", "critic",
                           note=str(e)[:80])

    issues = list(result["issues"])
    instruction = result["instruction"]

    # 「情報が見つかりませんでした」と書きながら、取得済みの数値を
    # 使っていないケースを拾う。実測で2回続けて発生している
    # （株価の下落率が台帳にあるのに「値動きは確認できず」と書いていた）。
    if claims_absence(done_content):
        unused = unused_numbers(done_content, state.get("findings", []))
        if unused:
            listed = "、".join(f"{u['raw']}（{u.get('context', '')[:30]}）" for u in unused[:5])
            issues.append(
                "回答に「情報が得られなかった」旨の記述がありますが、"
                f"取得済みで未使用の数値があります: {listed}"
            )
            instruction = (
                (instruction + "\n") if instruction else ""
            ) + (
                "これらの数値がタスクの問いに答えるものかを確認し、"
                "該当するなら回答へ反映してください。無関係なら、"
                "何が不足しているのかをより具体的に書いてください。"
            )

    if not issues:
        return _with_trace(state, {
            **state,
            "status": "needs_revision",
            "correction_count": state.get("correction_count", 0) + 1,
        }, "correct", "数値の機械照合: 問題なし", "critic")

    # 差し戻せるかどうかは予算次第。差し戻せない場合でも検証は済んでいるので、
    # 結果を捨てずに verification_notes へ残し、最終回答に注記として出す。
    #
    # 以前は予算が尽きていると検証自体をスキップしていたが、それでは
    # 「最後に出力される回答こそ検証されない」という逆の構造になっていた。
    # 実測で、ステップ上限際で出力された回答に、履歴に存在しない数値
    # （売上構成比20%以上など）が含まれたまま素通りしている。
    can_retry = (
        state.get("correction_count", 0) < state.get("max_corrections", 2)
        and state["max_steps"] - state["step_count"] > 1
    )

    if not can_retry:
        return _with_trace(state, {
            **state,
            "status": "needs_revision",
            "verification_notes": state.get("verification_notes", []) + issues,
            "correction_count": state.get("correction_count", 0) + 1,
        }, "correct", f"数値の問題を{len(issues)}件検出（差し戻せず）", "critic",
            note="訂正の予算切れ。最終回答に注記として残す")

    feedback = "（自動訂正チェック）最終回答に問題があります。\n"
    feedback += "\n".join(f"- {i}" for i in issues)
    if instruction:
        feedback += f"\n\n{instruction}"
    feedback += "\n訂正した上で、再度DONEで最終回答を出してください。"

    return _with_trace(state, {
        **state,
        "history": state["history"] + [{"role": "result", "content": feedback}],
        "status": "running",
        "correction_count": state.get("correction_count", 0) + 1,
    }, "correct", f"数値の問題を{len(issues)}件検出", "react", note="訂正を差し戻した")


def critic_step(state: AgentState) -> AgentState:
    """専門家プールによるレビュー"""
    if state["critique_count"] >= state["max_critiques"]:
        return _with_trace(state, {**state, "status": "done"},
            "critic", "スキップ", "END",
            note=f"レビューの予算切れ（{state['critique_count']}/{state['max_critiques']}）",
            skipped=True)

    remaining_steps = state["max_steps"] - state["step_count"]
    if remaining_steps <= 1:
        # 差し戻す予算がないため、そのままdoneにする
        return _with_trace(state, {**state, "status": "done"},
            "critic", "スキップ", "END",
            note=f"差し戻すステップ予算がない（残り{remaining_steps}）", skipped=True)

    done_content = ""
    for entry in reversed(state["history"]):
        if entry["role"] == "assistant" and "DONE:" in entry["content"]:
            done_content = entry["content"].split("DONE:")[1].strip()
            break

    if not done_content:
        return _with_trace(state, {**state, "status": "done"},
            "critic", "スキップ", "END",
            note="履歴にDONE本文が見つからない", skipped=True)

    try:
        reviewer_names = dispatch_reviewers(state["task"], done_content, output_type="auto")
    except Exception:
        reviewer_names = ["generic_reviewer"]

    code_for_review = state.get("generated_code", "")
    review_results = run_reviewers(
        reviewer_names,
        task=state["task"],
        output=done_content,
        history=state["history"],
        code=code_for_review,
        findings=state.get("findings", []),
    )

    aggregated = aggregate_results(review_results)

    if aggregated["verdict"] == "OK":
        return _with_trace(state, {
            **state,
            "status": "done",
            "critique_count": state["critique_count"] + 1,
        }, "critic", f"レビュー: OK（{', '.join(reviewer_names)}）", "END")

    feedback_content = "複数のレビュアーから以下の指摘がありました。"
    feedback_content += "指摘を反映してから再度DONEで最終回答を出してください。\n\n"
    feedback_content += "## 問題点\n"
    for issue in aggregated["issues"]:
        feedback_content += f"- {issue}\n"
    if aggregated["instruction"]:
        feedback_content += f"\n## 指示\n{aggregated['instruction']}\n"

    new_history = state["history"] + [{
        "role": "result",
        "content": feedback_content,
    }]

    return _with_trace(state, {
        **state,
        "history": new_history,
        "status": "running",
        "critique_count": state["critique_count"] + 1,
    }, "critic", f"レビュー: 要修正（{', '.join(reviewer_names)}）", "react",
        note=f"指摘{len(aggregated['issues'])}件")


def route_after_react(state: AgentState) -> str:
    if state["status"] == "error":
        return END
    if state["status"] == "needs_revision":
        # まず機械チェック（correct）を通してから critic へ。
        # 数値が誤ったまま内容レビューに入ると、誤った前提の上に指摘が積まれる。
        return "correct"
    if state["status"] == "done":
        return END
    if (
        state.get("last_action_type") == "tool"
        and is_tool_verifiable(state.get("last_tool_name", ""))
    ):
        return "verify_tool"
    return "react"


def route_after_verify_tool(state: AgentState) -> str:
    if state["status"] == "error":
        return END
    if state["status"] == "done":
        return END
    return "react"


def route_after_correct(state: AgentState) -> str:
    if state["status"] == "error":
        return END
    if state["status"] == "running":
        return "react"          # 訂正を求めて差し戻す
    return "critic"


def route_after_critic(state: AgentState) -> str:
    if state["status"] == "done":
        return END
    return "react"


workflow = StateGraph(AgentState)
workflow.add_node("react", react_step)
workflow.add_node("critic", critic_step)
workflow.add_node("verify_tool", verify_tool_step)
workflow.add_node("correct", correct_step)
workflow.set_entry_point("react")
workflow.add_conditional_edges(
    "react", route_after_react,
    {"react": "react", "correct": "correct", "verify_tool": "verify_tool", END: END}
)
workflow.add_conditional_edges(
    "correct", route_after_correct,
    {"react": "react", "critic": "critic", END: END}
)
workflow.add_conditional_edges(
    "verify_tool", route_after_verify_tool,
    {"react": "react", END: END}
)
workflow.add_conditional_edges(
    "critic", route_after_critic,
    {"react": "react", END: END}
)

app = workflow.compile()