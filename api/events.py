"""api/events.py — SSE（Server-Sent Events）の組み立て。

実行の追従だけは、実行しているプロセスと画面が別なので工夫が要る。

エージェント実行は APScheduler のジョブとして走り、進捗は
`executor.run_agent_background` が `update_execution_progress` で
DBへ書く。API サーバのリクエストハンドラはその外側にいるので、
generator を直接受け取れない。DBを1秒ごとに見て差分を送る。

「差分」は history と trace の**件数**で持つ。どちらも追記しかされない
（executor 側で丸ごと書き直すが、内容は前回分＋新着）。件数で持てば、
同じイベントを二度送らない。

ツール生成（ai_creator）は同一プロセスの generator なので、そちらは
yield をそのまま流す。
"""

import asyncio
import json
from typing import AsyncIterator

from db import get_execution
from executor import parse_history_for_display

# 実行中DBを見に行く間隔。短くすると負荷、長くすると画面が飛ぶ。
POLL_SECONDS = 1.0

# 実行が終わってからも少しだけ見る。finish_execution の書き込みと
# ポーリングが競合したときに、最後の1回を取りこぼさないため。
TAIL_POLLS = 2


def sse(event: dict) -> dict:
    """sse_starlette に渡す形。data は必ずJSON文字列にする。"""
    return {"data": json.dumps(event, ensure_ascii=False)}


def _loads(raw):
    if not raw:
        return []
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def step_events(history: list, start: int) -> list[dict]:
    """
    history の start 番目以降を、画面が読める形のイベントにする。

    THOUGHT / ACTION / DONE のパースは `parse_history_for_display` に
    任せる。フロントに同じ正規表現を書くと、片方だけ直す事故が起きる。
    """
    parsed = parse_history_for_display(history)
    return [{"type": "step" if e.get("role") != "result" else "result", **e}
            for e in parsed[start:]]


def trace_events(trace: list, start: int) -> list[dict]:
    out = []
    for entry in trace[start:]:
        out.append({
            "type": "trace",
            "seq": entry.get("seq"),
            "node": entry.get("node"),
            "from": entry.get("from"),
            "next": entry.get("next"),
            "summary": entry.get("summary"),
            "note": entry.get("note"),
            "skipped": bool(entry.get("skipped")),
            # 所要時間とトークン。計測していない実行では入っていないので
            # そのまま欠測（undefined）として流す。0 を作らない
            "elapsed_ms": entry.get("elapsed_ms"),
            "llm_ms": entry.get("llm_ms"),
            "llm_calls": entry.get("llm_calls"),
            "input_tokens": entry.get("input_tokens"),
            "output_tokens": entry.get("output_tokens"),
            "reasoning_tokens": entry.get("reasoning_tokens"),
            "missing_usage": entry.get("missing_usage"),
        })
    return out


async def execution_stream(exec_id: str) -> AsyncIterator[dict]:
    """
    1つの実行を追う。実行中でなくても、いまの中身を1回流してから終わる。

    ページを離れて戻ってきた場合もこの経路で復元できる（履歴が
    最初から順に流れ、running なら続きが来る）。
    """
    sent_steps = 0
    sent_trace = 0
    last_status = None
    tail = TAIL_POLLS

    while True:
        row = get_execution(exec_id)
        if row is None:
            yield sse({"type": "error", "message": f"実行が見つかりません: {exec_id}"})
            return

        history = _loads(row.get("history"))
        trace = _loads(row.get("trace"))

        for event in step_events(history, sent_steps):
            yield sse(event)
        sent_steps = len(parse_history_for_display(history))

        for event in trace_events(trace, sent_trace):
            yield sse(event)
        sent_trace = len(trace)

        status = row.get("status")
        if status != last_status:
            yield sse({
                "type": "status",
                "status": status,
                "graph_kind": row.get("graph_kind"),
                "started_at": row.get("started_at"),
                "finished_at": row.get("finished_at"),
            })
            last_status = status

        if status in ("done", "error"):
            # 終了後にもう数回だけ見る。finish_execution の書き込みと
            # ポーリングが行き違ったときの取りこぼしを拾う
            if tail <= 0:
                yield sse({
                    "type": "final",
                    "status": status,
                    "stdout": row.get("stdout") or "",
                    "stderr": row.get("stderr") or "",
                })
                return
            tail -= 1

        await asyncio.sleep(POLL_SECONDS)


async def generator_stream(gen) -> AsyncIterator[dict]:
    """
    同一プロセスの同期 generator（ai_creator）を SSE へ流す。

    LLM呼び出しでブロックするので、イベントループを止めないよう
    ワーカースレッドで next() を回す。
    """
    loop = asyncio.get_running_loop()
    iterator = iter(gen)

    def _next():
        try:
            return next(iterator)
        except StopIteration:
            return _DONE

    while True:
        try:
            item = await loop.run_in_executor(None, _next)
        except Exception as e:                      # 生成側の例外を画面へ
            yield sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
            return
        if item is _DONE:
            yield sse({"type": "final"})
            return
        yield sse({"type": "chunk", **(item if isinstance(item, dict) else {"value": item})})


class _Done:
    pass


_DONE = _Done()
