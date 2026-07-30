# graph_research.py — リサーチ用ステートマシン
#
# ReAct（graph.py）は「次に何をするか」を毎ステップLLMに決めさせる。
# 調査タスクではこれが不利に働いていた:
#   - ACTIONの書式崩れでステップを1回丸ごと捨てる
#   - 同じような検索を言い換えて繰り返す
#   - DONEを書く時点で、行動指示のルールがプロンプトの枠を食っている
#
# こちらは工程を固定し、ツール呼び出しはコード側が行う。
# LLMに任せるのは「何を調べるか（plan）」「まだ足りないか（gap）」
# 「どう書くか（compose）」の3つだけ。
#
#   plan → search → digest → gap ─┬→ search（不足あり／ラウンド上限まで）
#                                 └→ compose → correct → critic → END
#
# correct（数値の機械照合）と critic（レビュアー）は graph.py のものを
# そのまま使う。状態のキーも共有しているので、履歴画面・ノード遷移・
# executor の最終結果抽出は ReAct 版と同じものが動く。

import re

from langgraph.graph import StateGraph, END

from config import get_llm, invoke_with_continuation
from state import AgentState
from tools import web_search, fetch_url
from numeric import (
    collect_from_text, merge_findings, format_findings, pending_event_warnings,
    format_confirmed, TRUNCATION_MARK,
    ORIGIN_SOURCE, ORIGIN_INTERNAL, internal_texts,
)
from graph import (
    _with_trace, correct_step, critic_step, SECTIONS_ALWAYS, SECTIONS_WRITE,
)
from datetime import datetime


# 1ラウンドで投げる検索数と本文取得数。LM Studio上のローカルモデル
# （8192コンテキスト）を想定し、1回のプロンプトが膨らまない範囲に抑える。
MAX_ITEMS = 5
MAX_SEARCHES_PER_ROUND = 3
MAX_FETCH_PER_ROUND = 3
EXCERPT_CHARS = 700          # 1ページあたりの抜粋
SOURCES_CHAR_BUDGET = 1800   # composeへ渡す抜粋の合計
RECURSION_LIMIT = 60         # LangGraphの既定(25)ではラウンドを回しきれない


# ===== 補助 =====

def _relabel_next(out: dict, mapping: dict) -> dict:
    """
    graph.py のノードをそのまま使うと、traceの「次」がReAct版の遷移先
    （react）のままになる。ステートマシン側の遷移先に書き換える。
    """
    trace = out.get("trace") or []
    if not trace:
        return out
    last = dict(trace[-1])
    if last.get("next") in mapping:
        last["next"] = mapping[last["next"]]
    return {**out, "trace": list(trace[:-1]) + [last]}


def _urls_from_search(result: str) -> list[dict]:
    """検索結果テキストから URL とタイトルを取り出す。"""
    hits = []
    for block in (result or "").split("\n---\n"):
        url_m = re.search(r"^URL:\s*(\S+)", block, re.MULTILINE)
        if not url_m:
            continue
        title_m = re.search(r"^タイトル:\s*(.+)$", block, re.MULTILINE)
        hits.append({
            "url": url_m.group(1).strip(),
            "title": title_m.group(1).strip() if title_m else "",
        })
    return hits


_KEYWORD_SPLIT = re.compile(r"[\s、,。．・「」（）()\[\]]+")


def _keywords(text: str) -> list[str]:
    words = [w for w in _KEYWORD_SPLIT.split(text or "") if len(w) >= 2]
    return words[:8]


def relevant_excerpt(text: str, keywords: list[str], budget: int = EXCERPT_CHARS) -> str:
    """
    本文から、調査項目に関係しそうな段落だけを抜き出す。

    fetch_url は最大8000文字返すため、そのままプロンプトへ入れると
    回答を書く枠が消える。LLMに要約させると1ページごとに1回呼ぶことに
    なるので、ここは機械的に絞る。数字を含む段落を優先するのは、
    調査タスクで欲しいのがほぼ数値だから。
    """
    paragraphs = [p.strip() for p in re.split(r"\n{1,}", text or "") if p.strip()]
    scored = []
    for i, p in enumerate(paragraphs):
        if len(p) < 15:
            continue
        score = sum(1 for k in keywords if k and k in p)
        if re.search(r"\d", p):
            score += 1
        if re.search(r"\d+\s*(%|％|億|兆|万|円|ドル|ウォン)", p):
            score += 2
        if score > 0:
            scored.append((score, -i, p))
    scored.sort(reverse=True)

    out, used = [], 0
    for _score, _neg_i, p in scored:
        sep = 1 if out else 0          # join で入る改行も予算に数える
        if used + sep + len(p) > budget:
            p = p[: max(0, budget - used - sep)]
        if not p:
            break
        out.append(p)
        used += sep + len(p)
        if used >= budget:
            break
    return "\n".join(out)


# 決算・株価タスクで機械的に足すクエリ。LLMの計画は「決算」だけで
# 終わることが多く、発表前のプレビュー記事（予想）ばかり集まる。
EARNINGS_WORDS = ("決算", "業績", "営業利益", "純利益", "売上")
PRICE_WORDS = ("株価", "値動き", "騰落", "株")
_HAS_ACTUAL_QUERY = ("実績", "結果", "発表", "速報")
_HAS_SERIES_QUERY = ("推移", "時系列", "チャート")


# 主語になりうる語のかたまり。英数字（SK, TSMC）・カタカナ（ハイニックス）・
# 漢字2〜4字（半導体）を拾い、隣接するものは1語に繋ぐ（SK+ハイニックス）。
_SUBJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*|[ァ-ヶー]{2,}|[一-龥]{2,4}")


def _subject(task: str) -> str:
    """
    タスク文から検索の主語になりそうな語を取り出す。

    日本語のタスクは分かち書きされないため、空白分割だけでは
    「SKハイニックスの直近の決算と株価を調べて」が丸ごと1語になり、
    そのままクエリにすると検索が壊れる。
    """
    words = _keywords(task)
    if len(words) >= 2:
        return " ".join(words[:2])

    matches = list(_SUBJECT_RE.finditer(task or ""))
    if not matches:
        return (task or "")[:12]
    merged, start, end = [], None, None
    for m in matches:
        if start is not None and m.start() == end:
            end = m.end()
            continue
        if start is not None:
            merged.append(task[start:end])
        start, end = m.start(), m.end()
    if start is not None:
        merged.append(task[start:end])
    merged = [w for w in merged if len(w) >= 2]
    return merged[0] if merged else (task or "")[:12]


def query_relevant(task: str, query: str) -> bool:
    """
    クエリがタスクと繋がっているか。

    語の一致は部分一致で見る。日本語は分かち書きされないため、
    完全一致を要求すると正しいクエリまで無関係と判定してしまう。
    """
    words = [w for w in _keywords(query) if len(w) >= 2]
    if not words:
        return True
    task_words = [w for w in _keywords(task) if len(w) >= 2]
    task_words.append(_subject(task))
    for w in words:
        if w in (task or ""):
            return True
        if any(w in tw or tw in w for tw in task_words if tw):
            return True
    return False


def reinforce_plan(task: str, items: list[dict]) -> list[dict]:
    """
    計画に、取りこぼしがちな観点のクエリを機械的に足す。

    - 決算タスク: 「決算」だけだと予想記事に偏るため「決算 実績」を足す
    - 株価タスク: 単発のスナップショットで済ませないよう「株価 推移」を足す

    LLMに毎回言い聞かせるとプロンプトが膨らむので、計画の側で担保する。
    """
    items = [dict(i) for i in items]
    queries = " ".join(i.get("query", "") + i.get("question", "") for i in items)
    subject = _subject(task)
    if not subject:
        return items

    additions = []
    if any(w in task for w in EARNINGS_WORDS) and \
            not any(w in queries for w in _HAS_ACTUAL_QUERY):
        additions.append(("発表済みの実績値（予想ではないもの）", f"{subject} 決算 実績"))
    if any(w in task for w in PRICE_WORDS) and \
            not any(w in queries for w in _HAS_SERIES_QUERY):
        additions.append(("直近の株価の推移（単発の値ではなく時系列）", f"{subject} 株価 推移"))

    for question, query in additions:
        if len(items) >= MAX_ITEMS:
            break
        items.append({
            "id": len(items) + 1, "question": question, "query": query,
            "status": "open", "hits": [],
        })
    return items


def needs_result_recheck(state: dict) -> bool:
    """
    「◯日に発表予定」の情報を掴んでいるのに、発表結果を調べていない状態か。

    実測で、発表当日のコンセンサス（予想）を実績として書いた事故がある。
    予定を見つけたなら、結果が出ているかを一度は確かめる。
    """
    if not pending_event_warnings(state.get("findings", [])):
        return False
    done = " ".join(state.get("queries_done", []))
    return not any(w in done for w in ("結果", "実績", "速報"))


QUERY_ANGLES = ("結果", "実績", "速報", "発表後 反応", "推移", "見通し")


def fresh_query(base: str, question: str, queries_done: list) -> str:
    """
    まだ使っていない切り口を足した新しいクエリを作る。

    gap が「未充足」と判断しても、同じクエリしか出せなければ search は
    素通りする（実測で発生）。切り口を変えたクエリを必ず1つ用意する。
    """
    done = list(queries_done or [])
    subject = " ".join(_keywords(base or question)[:2]) or (base or question)[:20]
    for angle in QUERY_ANGLES:
        candidate = f"{subject} {angle}".strip()
        if not any(_normalize_query(candidate) == _normalize_query(q) for q in done):
            return candidate
    # 切り口を使い切ったら、問いの語を足して重複を外す
    extra = " ".join(_keywords(question)[:2])
    return f"{subject} {extra}".strip()


def _normalize_query(q: str) -> str:
    return " ".join(sorted((q or "").lower().split()))


def is_duplicate_query(query: str, queries_done: list) -> bool:
    norm = _normalize_query(query)
    return any(norm == _normalize_query(q) for q in (queries_done or []))


def drop_irrelevant_queries(task: str, items: list[dict]) -> list[dict]:
    """
    タスクと語がひとつも重ならないクエリを、タスク寄りに作り直す。

    実測で、半導体企業の調査に「バッテリー市場動向」のような無関係な
    クエリが紛れ込んでいた。plan の出力時点で弾く。
    """
    subject = _subject(task)
    if not subject:
        return items
    out = []
    for item in items:
        if query_relevant(task, item.get("query", "")):
            out.append(item)
            continue
        repaired = dict(item)
        repaired["query"] = f"{subject} {' '.join(_keywords(item.get('question', ''))[:1])}".strip()
        repaired["repaired_from"] = item.get("query", "")
        out.append(repaired)
    return out


def append_result_item(task: str, items: list[dict]) -> list[dict]:
    """発表結果を確かめるための調査項目を1件足す。"""
    items = [dict(i) for i in items]
    subject = _subject(task)
    suffix = "決算 結果" if any(w in task for w in EARNINGS_WORDS) else "結果"
    items.append({
        "id": len(items) + 1,
        "question": "発表予定だったものの結果（実績値が出ているか）",
        "query": f"{subject} {suffix}".strip(),
        "status": "open",
        "hits": [],
    })
    return items


def _sources_section(sources: list[dict]) -> str:
    """取得済みページの抜粋を、予算内でプロンプト用に整形する。"""
    if not sources:
        return ""
    lines, used = [], 0
    for s in reversed(sources):          # 新しいものを優先
        block = f"- {s.get('title') or s.get('url', '')}（{s.get('url', '')}）\n  {s.get('excerpt', '')}"
        if used + len(block) > SOURCES_CHAR_BUDGET:
            continue
        lines.append(block)
        used += len(block)
    if not lines:
        return ""
    return "## 取得したページの抜粋\n" + "\n".join(reversed(lines)) + "\n"


def _plan_section(plan_items: list[dict]) -> str:
    if not plan_items:
        return ""
    lines = []
    for it in plan_items:
        mark = "済" if it.get("status") == "filled" else "未"
        lines.append(f"{it['id']}. [{mark}] {it['question']}")
    return "## 調査項目\n" + "\n".join(lines) + "\n"


def _recent_feedback(history: list) -> str:
    """correct / critic が書いた差し戻し内容だけを拾う。

    以前は本文の書き出し（「（自動訂正チェック）」等）で見分けていたが、
    文言を変えた瞬間に静かに拾えなくなる。エントリの出所で選ぶ。
    """
    picked = []
    for content in reversed(internal_texts(history)):
        if content:
            picked.append(content[:900])
        if len(picked) >= 2:
            break
    if not picked:
        return ""
    return "## 前回の指摘（必ず反映すること）\n" + "\n".join(reversed(picked)) + "\n"


def _ask(prompt: str, system: str = "あなたは調査を担当するアシスタントです。指示の書式に厳密に従ってください。",
         boost: bool = False, max_continuations: int = 2) -> str:
    llm = get_llm(temperature=0.1, boost_tokens=boost)
    content, _resp = invoke_with_continuation(
        llm,
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        max_continuations=max_continuations,
    )
    return content or ""


# ===== ノード =====

def plan_step(state: AgentState) -> AgentState:
    """タスクを調査項目に分解する（LLM 1回）。"""
    if state.get("plan_items"):
        return _with_trace(state, {**state}, "plan", "計画は作成済み", "search",
                           note="再入のためスキップ", skipped=True)

    today = datetime.now().strftime("%Y年%m月%d日")
    prompt = (
        f"今日は{today}です。\n"
        f"次のタスクに答えるために、調べるべきことを{MAX_ITEMS}件以内に分解してください。\n\n"
        f"タスク: {state['task']}\n\n"
        "出力は次の形式のみ。説明文や前置きは書かないでください。\n"
        "1. 調べること | 検索クエリ\n"
        "2. 調べること | 検索クエリ\n\n"
        "検索クエリは2〜3語に絞ること。単語を詰め込むと検索精度が落ちます。\n"
        "「調べること」は、答えが数値や日付で埋まる粒度にすること。\n"
    )

    try:
        output = _ask(prompt)
    except Exception as e:
        output = ""
        err = str(e)[:80]
    else:
        err = ""

    items = []
    for line in (output or "").splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.+?)\s*[|｜]\s*(.+?)\s*$", line)
        if not m:
            continue
        items.append({
            "id": len(items) + 1,
            "question": m.group(2).strip()[:120],
            "query": m.group(3).strip()[:80],
            "status": "open",
            "hits": [],
        })
        if len(items) >= MAX_ITEMS:
            break

    fallback = not items
    if fallback:
        # 分解できなくても止めない。タスクそのものを1項目として進める。
        items = [{"id": 1, "question": state["task"][:120],
                  "query": " ".join(_keywords(state["task"])[:3]) or state["task"][:40],
                  "status": "open", "hits": []}]

    items = drop_irrelevant_queries(state["task"], reinforce_plan(state["task"], items))
    repaired = [i for i in items if i.get("repaired_from")]

    plan_text = "調査計画:\n" + "\n".join(
        f"{i['id']}. {i['question']}（クエリ: {i['query']}）" for i in items
    )
    note = "LLMが計画を返さなかったためタスクをそのまま調査する" if fallback else ""
    if repaired:
        note = (note + " / " if note else "") + (
            "タスクと無関係なクエリを是正: "
            + "、".join(i["repaired_from"] for i in repaired[:2])
        )
    if err:
        note = f"計画の生成に失敗（{err}）。タスクをそのまま調査する"

    return _with_trace(state, {
        **state,
        "history": state["history"] + [{"role": "assistant", "content": plan_text}],
        "plan_items": items,
        "status": "running",
        "step_count": state["step_count"] + 1,
    }, "plan", f"調査項目を{len(items)}件に分解", "search", note=note, skipped=fallback)


def search_step(state: AgentState) -> AgentState:
    """未充足の調査項目について検索する（LLMを呼ばない）。"""
    items = [dict(i) for i in state.get("plan_items", [])]
    targets = [i for i in items if i["status"] == "open"][:MAX_SEARCHES_PER_ROUND]

    if not targets:
        return _with_trace(state, {**state}, "search", "検索する項目がない", "gap",
                           note="全項目が充足済み", skipped=True)

    history = list(state["history"])
    findings = state.get("findings", [])
    queries_done = list(state.get("queries_done", []))
    ran = 0

    for item in targets:
        query = (item.get("query") or "").strip()
        if not query or query in queries_done:
            # 同じクエリの繰り返しは結果も同じ。ラウンドを無駄にしない
            continue
        try:
            result = web_search(query)
        except Exception as e:
            result = f"検索エラー: {e}"
        queries_done.append(query)
        ran += 1

        history.append({"role": "result", "origin": ORIGIN_SOURCE,
                        "content": f"[検索] {query}\n{result}"})
        findings = merge_findings(
            findings,
            collect_from_text(result, source=f"web_search({query})",
                              step=state["step_count"] + 1),
        )
        item["hits"] = _urls_from_search(result)

    for i, item in enumerate(items):
        for t in targets:
            if t["id"] == item["id"]:
                items[i] = t

    return _with_trace(state, {
        **state,
        "history": history,
        "plan_items": items,
        "findings": findings,
        "queries_done": queries_done,
        "status": "running",
        "step_count": state["step_count"] + 1,
    }, "search", f"{ran}件のクエリを実行（ラウンド{state.get('research_round', 0) + 1}）",
        "digest", note="" if ran else "全クエリが実行済みだった", skipped=(ran == 0))


def digest_step(state: AgentState) -> AgentState:
    """検索で得たURLの本文を取得し、関係する段落だけ抜き出す（LLMを呼ばない）。"""
    sources = list(state.get("sources", []))
    fetched = {s.get("url") for s in sources}

    queue = []
    for item in state.get("plan_items", []):
        if item.get("status") == "filled":
            continue
        for hit in item.get("hits", []):
            if hit["url"] in fetched:
                continue
            queue.append((item, hit))
    queue = queue[:MAX_FETCH_PER_ROUND]

    if not queue:
        return _with_trace(state, {**state}, "digest", "取得するURLがない", "gap",
                           note="検索結果にURLが無いか、取得済み", skipped=True)

    history = list(state["history"])
    findings = state.get("findings", [])
    ok = 0

    for item, hit in queue:
        try:
            body = fetch_url(hit["url"])
        except Exception as e:
            body = f"エラー: {e}"
        if body.startswith("エラー") or body.startswith("本文を抽出できません"):
            history.append({"role": "result", "origin": ORIGIN_SOURCE,
                            "content": f"[本文取得] {hit['url']}\n{body[:200]}"})
            continue

        excerpt = relevant_excerpt(body, _keywords(item["question"] + " " + item["query"]))
        if not excerpt:
            excerpt = body[:EXCERPT_CHARS]
        # 抜粋は段落を選んで詰めるので、本文末尾の切断の印は必ず落ちる。
        # 「元ページが切れていたか」はフラグで持つ（抜粋であること自体とは別）
        sources.append({
            "url": hit["url"],
            "title": hit.get("title", ""),
            "item_id": item["id"],
            "excerpt": excerpt,
            "source_truncated": TRUNCATION_MARK in body,
        })
        fetched.add(hit["url"])
        ok += 1

        history.append({"role": "result", "origin": ORIGIN_SOURCE,
                        "content": f"[本文取得] {hit['url']}\n{excerpt[:600]}"})
        findings = merge_findings(
            findings,
            collect_from_text(excerpt, source=f"fetch_url({hit['url']})",
                              step=state["step_count"] + 1),
        )

    return _with_trace(state, {
        **state,
        "history": history,
        "sources": sources,
        "findings": findings,
        "status": "running",
        "step_count": state["step_count"] + 1,
    }, "digest", f"{ok}/{len(queue)}件の本文を取得", "gap",
        note="" if ok else "全て取得に失敗した", skipped=(ok == 0))


def gap_step(state: AgentState) -> AgentState:
    """
    調査項目が埋まったかをLLMに判定させ、足りなければ次のクエリを決める。

    機械判定（findingsの件数など）では「数値は拾えているが問いに
    答えていない」ケースを見抜けないため、ここはLLMに任せている。
    ただし判定に失敗しても止めず、composeへ進める（fail-forward）。
    """
    items = [dict(i) for i in state.get("plan_items", [])]
    rnd = state.get("research_round", 0) + 1
    open_items = [i for i in items if i["status"] == "open"]

    # 「◯日に発表予定」を掴んでいるのに結果を調べていないなら、
    # LLMの判定を待たずに1ラウンド使って確認する。予想値を実績として
    # 書く事故は、ここを飛ばしたときに起きている。
    if rnd < state.get("max_rounds", 3) and needs_result_recheck(state):
        return _with_trace(state, {
            **state,
            "plan_items": append_result_item(state["task"], items),
            "research_round": rnd,
            "status": "running",
        }, "gap", "発表予定を検知。結果を確認しに戻る", "search",
            note="予定の情報があるのに、結果・実績を調べていない")

    if not open_items:
        return _with_trace(state, {**state, "research_round": rnd},
                           "gap", "全項目が充足", "compose")

    if rnd >= state.get("max_rounds", 3):
        for i in items:
            i["status"] = "filled"
        return _with_trace(state, {**state, "plan_items": items, "research_round": rnd},
                           "gap", f"ラウンド上限に到達（{rnd}/{state.get('max_rounds', 3)}）",
                           "compose", note=f"未充足{len(open_items)}件のまま執筆へ", skipped=True)

    prompt = (
        f"タスク: {state['task']}\n\n"
        + _plan_section(items)
        + "\n"
        + (format_findings(state.get("findings", [])) or "（取得済みの数値なし）")
        + "\n\n"
        + _sources_section(state.get("sources", []))
        + "\n各調査項目について、今ある情報だけで答えられるかを判定してください。\n"
        "出力は次の形式の行だけ。説明文は書かないでください。\n"
        "ITEM 1: OK\n"
        "ITEM 2: NG | 次に投げる検索クエリ（2〜3語）\n\n"
        "OKは「その項目に答える具体的な数値や事実が上にある」場合のみ。\n"
        "推測で補える、という理由でOKにしないでください。\n"
    )

    try:
        output = _ask(prompt)
    except Exception as e:
        for i in items:
            i["status"] = "filled"
        return _with_trace(state, {**state, "plan_items": items, "research_round": rnd},
                           "gap", "充足判定に失敗", "compose",
                           note=f"{str(e)[:80]}。執筆へ進む", skipped=True)

    verdicts = {}
    for line in (output or "").splitlines():
        m = re.match(r"\s*ITEM\s*(\d+)\s*[:：]\s*(OK|NG)\s*(?:[|｜]\s*(.*))?$",
                     line.strip(), re.IGNORECASE)
        if m:
            verdicts[int(m.group(1))] = (m.group(2).upper(), (m.group(3) or "").strip())

    if not verdicts:
        for i in items:
            i["status"] = "filled"
        return _with_trace(state, {**state, "plan_items": items, "research_round": rnd},
                           "gap", "充足判定の書式を解釈できなかった", "compose",
                           note="判定不能のため執筆へ進む", skipped=True)

    still_open = 0
    queries_done = state.get("queries_done", [])
    for item in items:
        verdict, next_query = verdicts.get(item["id"], ("OK", ""))
        if verdict == "OK":
            item["status"] = "filled"
            continue
        item["status"] = "open"
        still_open += 1
        candidate = (next_query or "")[:80].strip()
        # 未使用のクエリを必ず1つ持たせる。使い回しだと search が素通りして
        # 「未充足のまま何も起きない」ラウンドになる
        if not candidate or is_duplicate_query(candidate, queries_done):
            candidate = fresh_query(state["task"], item.get("question", ""), queries_done)
        item["query"] = candidate

    if still_open == 0:
        return _with_trace(state, {**state, "plan_items": items, "research_round": rnd},
                           "gap", "全項目が充足", "compose")

    return _with_trace(state, {
        **state, "plan_items": items, "research_round": rnd, "status": "running",
    }, "gap", f"未充足{still_open}件。再検索へ", "search",
        note=f"ラウンド{rnd}/{state.get('max_rounds', 3)}")


def compose_step(state: AgentState) -> AgentState:
    """
    集めた情報からレポートを書く（LLM 1回）。

    出力を 'DONE: ' で始めさせるのは、後段の correct / critic と
    executor の結果抽出が DONE を目印にしているため。ReAct版と
    同じ資産をそのまま使える。
    """
    composed = state.get("compose_count", 0)
    if composed >= state.get("max_composes", 3):
        return _with_trace(state, {**state, "status": "done"},
                           "compose", "執筆回数の上限に到達", "END",
                           note=f"{composed}/{state.get('max_composes', 3)}", skipped=True)

    today = datetime.now().strftime("%Y年%m月%d日")
    warnings = pending_event_warnings(state.get("findings", []))
    warn_text = ("\n".join(f"- {w}" for w in warnings) + "\n") if warnings else ""

    confirmed_text = format_confirmed(state.get("confirmed", []))

    prompt = (
        f"今日は{today}です。\n"
        f"タスク: {state['task']}\n\n"
        + _plan_section(state.get("plan_items", []))
        + "\n"
        + (format_findings(state.get("findings", [])) or "（取得済みの数値なし）")
        + "\n\n"
        + _sources_section(state.get("sources", []))
        + (f"\n## 注意\n{warn_text}" if warn_text else "")
        + (f"\n## 確定済み（検証済み。値もラベルも変えないこと）\n{confirmed_text}\n"
           if confirmed_text else "")
        + _recent_feedback(state["history"])
        + "\n上の情報だけを使って、タスクへの回答を書いてください。\n"
        "上に無い数値・固有名詞・因果関係を書かないこと。\n"
        "数値一覧の [実績] / [予想] は、そのまま同じ表記で書き写すこと。\n"
        + ("確定済みの値を落としたり書き換えたりする場合は、理由を1行で書くこと。\n"
           if confirmed_text else "")
        + "情報が足りない項目は、埋めずに「確認できず」と書くこと。\n"
        "回答は必ず 'DONE: ' から始めてください。\n"
    )

    system = (
        "あなたは調査結果をまとめる担当です。\n"
        + SECTIONS_ALWAYS[2]          # 数値の書き写しルール
        + "\n"
        + "\n".join(SECTIONS_WRITE)   # 執筆時の表記ルール
    )

    try:
        output = _ask(prompt, system=system, boost=True, max_continuations=3)
    except Exception as e:
        return _with_trace(state, {
            **state,
            "status": "error",
            "compose_count": composed + 1,
            "step_count": state["step_count"] + 1,
        }, "compose", "執筆に失敗", "END", note=str(e)[:80])

    if not output.strip():
        # 空応答の再試行は「差し戻し」とは別勘定にする。ここで compose_count を
        # 使うと、correct と critic のために確保した枠を空応答が食い潰し、
        # 予算の不変条件（1 + 訂正 + レビュー）が成り立たなくなる。
        retries = state.get("compose_retry_count", 0)
        if retries >= state.get("max_compose_retries", 1):
            return _with_trace(state, {
                **state,
                "status": "needs_revision",     # 直前の版があれば correct が拾う
                "compose_retry_count": retries + 1,
                "step_count": state["step_count"] + 1,
                "verification_notes": state.get("verification_notes", [])
                + ["レポートの生成が空応答を繰り返したため、この版は書き直せていません。"],
            }, "compose", "空応答が続いたため執筆を打ち切り", "correct",
                note=f"再試行上限（{state.get('max_compose_retries', 1)}）", skipped=True)
        return _with_trace(state, {
            **state,
            "history": state["history"] + [{
                "role": "result", "origin": ORIGIN_INTERNAL,
                "content": "レポートの生成が空応答でした。もう一度書いてください。",
            }],
            "status": "running",
            "compose_retry_count": retries + 1,
            "step_count": state["step_count"] + 1,
        }, "compose", "空応答", "compose", note=f"再執筆する（{retries + 1}回目）")

    text = output.strip()
    if "DONE:" not in text:
        text = f"DONE: {text}"

    return _with_trace(state, {
        **state,
        "history": state["history"] + [{"role": "assistant", "content": text}],
        "status": "needs_revision",
        "last_action_type": "done",
        "compose_count": composed + 1,
        "step_count": state["step_count"] + 1,
    }, "compose", f"レポートを執筆（{len(text)}文字）", "correct",
        note=f"{composed + 1}回目")


def correct_sm_step(state: AgentState) -> AgentState:
    """graph.py の correct をそのまま使い、traceの遷移先だけ書き換える。"""
    return _relabel_next(correct_step(state), {"react": "compose"})


def critic_sm_step(state: AgentState) -> AgentState:
    """
    graph.py の critic を使い、遷移先をステートマシン側に読み替える。

    加えて、compose 枠が残っていないのに差し戻そうとする場合は、
    指摘を verification_notes に移して終える。枠が無いまま react/compose へ
    戻しても、compose_step が上限で素通りして指摘が消えるだけになる。
    """
    out = _relabel_next(critic_step(state), {"react": "compose"})
    if out.get("status") != "running":
        return out

    if state.get("compose_count", 0) < state.get("max_composes", 3):
        return out

    issues = []
    for entry in out.get("history", [])[len(state.get("history", [])):]:
        if entry.get("role") == "result":
            issues.append(entry.get("content", "")[:400])
    fixed = {
        **out,
        "history": state["history"],          # 差し戻し文は履歴に残さない
        "status": "done",
        "verification_notes": state.get("verification_notes", [])
        + [f"（レビュー未反映）{i}" for i in issues],
    }
    trace = list(out.get("trace") or [])
    if trace:
        last = dict(trace[-1])
        last.update({
            "next": "END",
            "summary": "レビュー: 要修正だが執筆枠が残っていない",
            "note": "指摘を最終回答の注記に回す",
        })
        trace[-1] = last
    fixed["trace"] = trace
    return fixed


# ===== 遷移 =====

def route_after_plan(state: AgentState) -> str:
    return END if state["status"] == "error" else "search"


def route_after_search(state: AgentState) -> str:
    return END if state["status"] == "error" else "digest"


def route_after_digest(state: AgentState) -> str:
    return END if state["status"] == "error" else "gap"


def route_after_gap(state: AgentState) -> str:
    if state["status"] == "error":
        return END
    items = state.get("plan_items", [])
    if any(i.get("status") == "open" for i in items):
        return "search"
    return "compose"


def route_after_compose(state: AgentState) -> str:
    if state["status"] in ("error", "done"):
        return END
    if state["status"] == "running":
        return "compose"      # 空応答の再試行
    return "correct"


def route_after_correct(state: AgentState) -> str:
    if state["status"] == "error":
        return END
    if state["status"] == "running":
        return "compose"      # 数値の訂正を求めて書き直させる
    return "critic"


def route_after_critic(state: AgentState) -> str:
    if state["status"] == "done":
        return END
    return "compose"


class _ResearchApp:
    """
    LangGraphの既定の再帰上限（25）ではラウンドを回しきれないため、
    stream/invoke時に上限を引き上げる薄いラッパ。executor 側は
    ReAct版と同じ .stream(state) の呼び出しのままでよい。
    """

    def __init__(self, compiled):
        self._compiled = compiled

    def _config(self, config):
        cfg = {"recursion_limit": RECURSION_LIMIT}
        if config:
            cfg.update(config)
        return cfg

    def stream(self, state, config=None):
        return self._compiled.stream(state, self._config(config))

    def invoke(self, state, config=None):
        return self._compiled.invoke(state, self._config(config))


# ノードとその遷移先を1箇所にまとめる。ここから組み立てることで、
# 「ノードは足したがエッジを足し忘れた」という取りこぼしを防ぐ。
NODES = {
    "plan": plan_step,
    "search": search_step,
    "digest": digest_step,
    "gap": gap_step,
    "compose": compose_step,
    "correct": correct_sm_step,
    "critic": critic_sm_step,
}

ROUTES = {
    "plan": (route_after_plan, ["search"]),
    "search": (route_after_search, ["digest"]),
    "digest": (route_after_digest, ["gap"]),
    "gap": (route_after_gap, ["search", "compose"]),
    "compose": (route_after_compose, ["compose", "correct"]),
    "correct": (route_after_correct, ["compose", "critic"]),
    "critic": (route_after_critic, ["compose"]),
}

ENTRY_POINT = "plan"

workflow = StateGraph(AgentState)
for _name, _fn in NODES.items():
    workflow.add_node(_name, _fn)
workflow.set_entry_point(ENTRY_POINT)
for _name, (_router, _targets) in ROUTES.items():
    mapping = {t: t for t in _targets}
    mapping[END] = END
    workflow.add_conditional_edges(_name, _router, mapping)

app = _ResearchApp(workflow.compile())
