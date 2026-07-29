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
)
from datetime import datetime


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
    """システムプロンプトを構築する（残りステップ数に応じて収束指示を強める）"""
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

    return (
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
        "- suggest_keywords(query): キーワードに対するサジェスト取得。世間がそのキーワードでよく検索する関連語を取得できる\n"
        "- list_saved_tools(): agent-studioに保存されている検証済みツール（Type1）の一覧を取得\n"
        "- run_saved_tool(tool_id): 保存済みツールを実行して結果を取得\n\n"
        "## 検索クエリの組み立て方（重要）\n"
        "web_search や suggest_keywords は、単語を詰め込みすぎると精度が落ちます。\n"
        "検索エンジンは短いクエリの方が的確な結果を返します。\n"
        "- suggest_keywords: 主題語1つ、または主題語+ジャンル語程度（2語以内）に絞ること\n"
        "- web_search: 2〜3語程度に絞ること。1回のクエリで全てを調べようとせず、\n"
        "  知りたい観点ごとにクエリを分けて複数回検索すること\n"
        "- 検索結果が的外れだった場合は、単語数を減らして再検索すること\n\n"
        "## 調査タスクの推奨フロー\n"
        "1. 最初に suggest_keywords でメインキーワード（1〜2語）のサジェストを取得\n"
        "2. サジェストから複数の観点を選び、それぞれ2〜3語のクエリで web_search を実行\n"
        "3. 必要に応じて fetch_url で記事本文を確認\n"
        "4. 集めた情報を統合してDONEで回答\n\n"
        "公式サイトのプレスリリースだけに頼らず、サジェストから世間の関心事を捉えること。\n\n"
        "## 保存済みツール（Type1）の活用\n"
        "タスクに「ツールID xxxを実行」「保存済みの○○ツールで前処理」といった指示がある場合、\n"
        "または定型的な前処理が必要な場合は、run_saved_tool を活用してください。\n"
        "利用可能なツールが分からない時は list_saved_tools で一覧を取得できます。\n\n"
        f"{budget_section}\n"
        f"{_findings_section(findings)}"
        "## 作業環境\n"
        f"{workspace}\n\n"
        "## 回答形式\n"
        "毎回、以下のいずれかの形式で回答してください。\n\n"
        "ツールを使う場合:\n"
        "THOUGHT: (何をしようとしているか。検索結果の数値は出典・単位を含めて"
        "正確に書き写すこと)\n"
        "ACTION: tool_name(引数)\n\n"
        "Pythonコードを生成・実行する場合:\n"
        "THOUGHT: (何をしようとしているか)\n"
        "ACTION: generate_code\n"
        "```python\n"
        "(コード)\n"
        "```\n\n"
        "タスクが完了した場合:\n"
        "THOUGHT: (結果の要約)\n"
        "DONE: (最終結果)\n\n"
        "## 制約\n"
        "- 標準ライブラリのみ使用すること。pandas, numpy等は使用不可\n"
        "- サンプルデータやダミーデータを自分で作らないこと\n"
        "- 作業環境に実在するファイルを使うこと\n"
        "- 結果はprint()で出力すること\n\n"
        "## 数値の書き写し（最優先）\n"
        "- **検索結果の数値は、単位を変換せずそのまま書き写すこと。**\n"
        "  「83兆ウォン（約9兆円）」と書かれていたら「83兆ウォン（約9兆円）」と書く。\n"
        "  ウォンの位置に円の値を入れてはいけない。\n"
        "- 換算値は括弧内の補助にとどめ、主となる値は必ず出典の単位のまま書くこと。\n"
        "- 「A単位（約B別単位）」の形で書いたら、AとBの桁が妥当か一度確認すること。\n"
        "  ウォンと円は10倍程度の差があり、同じ値になることはない。\n"
        "- 検索結果に「◯日に発表」等の予定があり、その日が今日以前または数日以内の\n"
        "  場合、予想値を書くときは「◯日発表予定のため未確定」と必ず注記すること。\n\n"
        "## 数値には出所と種別を必ず付ける\n"
        "- すべての数値に **[実績]** か **[予想]** のどちらかを付けること。\n"
        "  どちらか判断できない場合は **[種別不明]** と書くこと。省略しないこと。\n"
        "  例: 売上高 84.1兆ウォン [予想]（証券14社コンセンサス、2026年7月29日発表予定）\n"
        "- 「コンセンサス」「見通し」「見込み」「予想」と書かれた数値は必ず [予想] である。\n"
        "  これを実績のように書くことは重大な誤りである。\n"
        "- **株価・騰落率には必ず「いつ時点か」と「どの市場か」を併記すること。**\n"
        "  例: -8.81%（NASDAQ上場ADR SKHY、2026年7月28日終値）\n"
        "  時点が分からない株価データは、「時点不明」と明記するか、採用しないこと。\n"
        "- **同じ企業が複数の市場に上場している場合、市場と通貨を分けて書くこと。**\n"
        "  例: 韓国取引所 000660.KS（ウォン建て）と NASDAQ ADR SKHY（ドル建て）は別物である。\n"
        "  異なる市場・通貨の数値を、断りなく同じ項目に並べてはいけない。\n"
        "- 出典を書くときは、その数値を実際に取得したページを書くこと。\n"
        "  上の一覧には数値ごとの取得元が併記されているので、それと食い違わせないこと。\n\n"
        "## 決算・業績を調べるときの手順\n"
        "「決算」「業績」を扱うタスクでは、予想と実績の両方を探すこと。\n"
        "「（対象名） 決算」だけで終わらせず、**「（対象名） 決算 実績」または\n"
        "「（対象名） 決算 発表」でも最低1回は検索する**こと。\n"
        "「決算」だけで検索すると発表前のプレビュー記事（予想）ばかりが集まり、\n"
        "予想値を実績値と取り違える原因になる。\n\n"
        "## DONEを出す前の点検\n"
        "「これまでに取得した数値・日付」の一覧を上から1件ずつ確認し、\n"
        "タスクの問いに関係するものを回答に含めたか点検すること。\n"
        "特に、増減率・価格・日付など、結論の向きを左右する数値を\n"
        "取りこぼしていないか確認すること。\n"
        "「情報が見つからなかった」と書く前に、この一覧に該当する数値が\n"
        "無いか必ず確認すること。一覧にある数値を使わずに「不明」と書くのは誤りである。\n\n"
        "## 表記ルール\n"
        "- 「予想」「見通し」「ガイダンス」と「実績」を明確に区別すること\n"
        "- 数値には必ず時点（例：2026年5月15日発表）を付記すること\n"
        "- 順位・比較情報には観測日を付記すること（例：2026年6月12日時点で首位）\n"
        "- 検索結果から実際に得られた情報のみを使い、推測で補完しないこと\n"
        "- **複数の検索結果で同じトピックについて異なる情報が出てきた場合、日付を比較して\n"
        "  最も新しい情報を採用すること。** 状況が良化・悪化している対象は特に注意すること\n"
        "  （例：「業績悪化」の記事と「過去最高益」の記事が両方出てきたら、日付が新しい方を信じる）\n\n"
        "## 検索結果を評価する際の注意\n"
        "直前の検索結果を受け取ったら、次のクエリを考える前に、\n"
        "まず「タスクに必要な情報のうち、何がまだ埋まっていないか」を\n"
        "THOUGHT内で明示的に言語化すること。\n"
        "その上で、その不足点をピンポイントで埋めるクエリを組み立てること。\n"
        "「なんとなく違う言い回しで検索し直す」ことは避けること。\n"
        "検索結果の要約（スニペット）だけで数値が読み取れない場合は、\n"
        "fetch_urlで該当ページの本文を取得すること。\n\n"
        "## 期間の要求を満たしているか確認する\n"
        "タスクが「直近◯◯以内」のように期間を指定している場合、\n"
        "DONEを出す前に、使おうとしている情報の日付が実際にその期間内に\n"
        "収まっているか必ず確認すること。今日の日付から逆算して確認すること。\n"
        "収まっていない情報しかない場合は、「◯◯時点の情報が最新（直近◯◯以内の\n"
        "データは見つからず）」と明記した上で、より新しい情報を探すクエリで\n"
        "再検索すること。古い情報を、あたかも直近の情報であるかのように\n"
        "書かないこと。\n\n"
        "## 同じ内容を繰り返し生成しない\n"
        "1回の応答につき、THOUGHTとACTION（またはDONE）はそれぞれ1回だけ\n"
        "書くこと。一度ACTION: generate_codeやACTION: tool_name(...)を書いたら、\n"
        "同じ回答の中でもう一度別のACTIONを書き直したり、同じ内容を形を変えて\n"
        "繰り返したりしないこと。DONEを出す場合も同様で、直前に別のACTIONで\n"
        "書いた内容をDONEの本文にそのまま書き写す必要はない。すでに結論が\n"
        "出ているなら、それ以上検討し直さず、そのままDONEで終えること。\n\n"
        "## 不完全な日付を勝手に補完しない\n"
        "検索結果の本文中に「25日」のように、月や年が書かれていない\n"
        "日付が出てきた場合、それを今日の月（現在は" + f"{current_month}" + "月）だと\n"
        "決めつけないこと。検索結果の他の部分（配信日時、URL、タイトル等）に\n"
        "月や年の手がかりがあれば、そこから補うこと。手がかりが見つからない\n"
        "場合は、「月不明・◯日という情報のみ確認」と正直に書き、日付を\n"
        "確定させないこと。存在しない具体的な日付を作り出すよりも、\n"
        "不明であることを明記する方が良いという原則を優先すること。\n"
        "また、DONEを出す前に、本文中に書こうとしている日付が今日\n"
        "（システムプロンプト冒頭に明記した日付）より未来になっていないか\n"
        "必ず確認すること。未来の日付は、まだ起きていない出来事のはずなので、\n"
        "「すでに起きたこと」として書くのは明らかな誤りである。\n"
    )


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
            return {
                **state,
                "history": new_history,
                "status": "done",
                "step_count": state["step_count"] + 1,
                "last_action_type": "done",
                "last_tool_name": "",
            }
        return {**state, "status": "error", "last_action_type": "", "last_tool_name": ""}

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
        return {
            **state,
            "history": new_history,
            "status": "running",
            "step_count": state["step_count"] + 1,
            "reasoning_detected": reasoning_detected,
            "last_action_type": "",
            "last_tool_name": "",
        }

    action = parse_action(llm_output)
    new_history = state["history"] + [{"role": "assistant", "content": llm_output}]

    if action["type"] == "done":
        return {
            **state,
            "history": new_history,
            "status": "needs_revision",
            "step_count": state["step_count"] + 1,
            "reasoning_detected": reasoning_detected,
            "last_action_type": "done",
            "last_tool_name": "",
        }

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
        return {
            **state,
            "history": new_history,
            "findings": new_findings,
            "status": "running",
            "step_count": state["step_count"] + 1,
            "reasoning_detected": reasoning_detected,
            "last_action_type": "tool",
            "last_tool_name": tool_name,
        }

    elif action["type"] == "code":
        code = action["content"]
        if not code:
            new_history.append({"role": "result", "content": "コードが空です。Pythonコードブロックを含めてください。"})
            return {
                **state,
                "history": new_history,
                "status": "running",
                "step_count": state["step_count"] + 1,
                "reasoning_detected": reasoning_detected,
                "last_action_type": "code",
                "last_tool_name": "",
            }
        result = execute_in_sandbox(code, **PROFILE_AGENT_CODE)
        if result["success"]:
            output = result["stdout"] if result["stdout"] else "(出力なし)"
        else:
            output = f"エラー:\n{result['stderr']}"
        new_history.append({"role": "result", "content": output})
        return {
            **state,
            "history": new_history,
            "generated_code": code,
            "status": "running",
            "step_count": state["step_count"] + 1,
            "reasoning_detected": reasoning_detected,
            "last_action_type": "code",
            "last_tool_name": "",
        }

    else:
        new_history.append({"role": "result", "content": (
            "回答形式を認識できませんでした。\n"
            "最終回答を書く場合は、行の先頭を DONE から始め、コロンに続けて"
            "本文を書いてください。\n"
            "ツールを使う場合は ACTION の行にツール名と丸括弧の引数を書いてください。\n"
            "書式の説明を本文中で引用せず、実際にその書式で書いてください。"
        )})
        return {
            **state,
            "history": new_history,
            "status": "running",
            "step_count": state["step_count"] + 1,
            "reasoning_detected": reasoning_detected,
            "last_action_type": "unknown",
            "last_tool_name": "",
        }


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
        return {**state, "status": "running", "tool_verify_count": verify_count + 1}

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
        return {**state, "status": "running", "tool_verify_count": verify_count + 1}

    action_match = re.search(r"ACTION:\s*(\w+)\((.+?)\)", last_assistant_content, re.DOTALL)
    if not action_match:
        return {**state, "status": "running", "tool_verify_count": verify_count + 1}
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
    except Exception:
        # 判定自体が失敗したら、安全側でそのままreactに戻す（結果は信じる）
        return {**state, "status": "running", "tool_verify_count": verify_count + 1}

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

    return {
        **state,
        "history": new_history,
        "status": "running",
        "tool_verify_count": verify_count + 1,
    }


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
    if state.get("correction_count", 0) >= state.get("max_corrections", 2):
        return {**state, "status": "needs_revision"}
    if state["max_steps"] - state["step_count"] <= 1:
        # 差し戻す予算がないので、そのまま critic へ送る
        return {**state, "status": "needs_revision"}

    done_content = _extract_done(state["history"])
    if not done_content:
        return {**state, "status": "needs_revision"}

    try:
        result = numeric_checker(
            output=done_content,
            history=state["history"],
            sources=state.get("sources", []),
        )
    except Exception as e:
        # 機械チェックの失敗でループを止めない。ただし黙って通さず履歴に残す
        return {
            **state,
            "history": state["history"] + [{
                "role": "result",
                "content": f"（自動訂正チェック）実行に失敗したため数値の照合は未実施です: {e}",
            }],
            "status": "needs_revision",
            "correction_count": state.get("correction_count", 0) + 1,
        }

    if result["verdict"] == "OK":
        return {
            **state,
            "status": "needs_revision",
            "correction_count": state.get("correction_count", 0) + 1,
        }

    feedback = "（自動訂正チェック）最終回答の数値に問題があります。\n"
    feedback += "\n".join(f"- {i}" for i in result["issues"])
    if result["instruction"]:
        feedback += f"\n\n{result['instruction']}"
    feedback += "\n訂正した上で、再度DONEで最終回答を出してください。"

    return {
        **state,
        "history": state["history"] + [{"role": "result", "content": feedback}],
        "status": "running",
        "correction_count": state.get("correction_count", 0) + 1,
    }


def critic_step(state: AgentState) -> AgentState:
    """専門家プールによるレビュー"""
    if state["critique_count"] >= state["max_critiques"]:
        return {**state, "status": "done"}

    remaining_steps = state["max_steps"] - state["step_count"]
    if remaining_steps <= 1:
        # 差し戻す予算がないため、そのままdoneにする
        return {**state, "status": "done"}

    done_content = ""
    for entry in reversed(state["history"]):
        if entry["role"] == "assistant" and "DONE:" in entry["content"]:
            done_content = entry["content"].split("DONE:")[1].strip()
            break

    if not done_content:
        return {**state, "status": "done"}

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
        return {
            **state,
            "status": "done",
            "critique_count": state["critique_count"] + 1,
        }

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

    return {
        **state,
        "history": new_history,
        "status": "running",
        "critique_count": state["critique_count"] + 1,
    }


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