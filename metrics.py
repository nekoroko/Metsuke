# metrics.py — 実行の所要時間とトークン使用量を集める。
#
# 置き場所の理由
# --------------
# LLM呼び出しは config.invoke_with_retry の1箇所しか通らない
# （invoke_with_continuation も内部でこれを呼ぶので、継続分割・リトライも
#  ここで数えられる）。ノードの出入りは graph._with_trace が全部通る。
# つまり計測点は2つで足り、各ノードの実装には手を入れなくてよい。
#
# なぜスレッドローカルだけにしないか
# ----------------------------------
# 実行ごとの隔離にはスレッドローカルで足りる（run_agent_background は
# 実行1件=1スレッド）。だが後でレビュアーを ThreadPoolExecutor で
# 並列化すると、ワーカースレッドからは親のスレッドローカルが見えず、
# **そのぶんのトークンが黙って0になる**。数字が出ているのに実態と違う、
# という最悪の壊れ方なので、集計器そのものはロック付きの普通のオブジェクト
# にしておき、スレッドローカルは「いまどれを使うか」の参照だけを持つ。
# ワーカー側は use_collector(parent) で明示的に受け取る。
#
# トークン数を推定しない理由
# --------------------------
# usage を返さないプロバイダがある（OpenAI互換を名乗るローカルサーバに多い）。
# そこで文字数から推定すると、根拠のない数字が「計測値」として表示される。
# このプロジェクトが一貫して潰してきた失敗そのものなので、取れないときは
# 欠測として残し、reported=False で「返ってこなかった」ことを見せる。

import threading
import time
from contextlib import contextmanager

_active = threading.local()


def _usage_of(response) -> tuple[int, int, bool]:
    """
    応答から (入力トークン, 出力トークン, 取得できたか) を返す。

    langchain の AIMessage は usage_metadata を持つ。OpenAI互換の生の形は
    response_metadata["token_usage"] に入る。どちらも見る
    （config.extract_reasoning_tokens と同じ方針）。
    """
    try:
        um = getattr(response, "usage_metadata", None) or {}
        i, o = um.get("input_tokens"), um.get("output_tokens")
        if isinstance(i, int) or isinstance(o, int):
            return int(i or 0), int(o or 0), True
    except Exception:
        pass

    try:
        meta = getattr(response, "response_metadata", None) or {}
        usage = meta.get("token_usage") or meta.get("usage") or {}
        i = usage.get("prompt_tokens", usage.get("input_tokens"))
        o = usage.get("completion_tokens", usage.get("output_tokens"))
        if isinstance(i, int) or isinstance(o, int):
            return int(i or 0), int(o or 0), True
    except Exception:
        pass

    return 0, 0, False


class RunCollector:
    """1回の実行ぶんの集計。複数スレッドから積まれる前提でロックする。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._node = self._zero()
        self._total = self._zero()
        self._total["nodes"] = 0
        self._node_started = time.monotonic()

    @staticmethod
    def _zero() -> dict:
        return {
            "llm_calls": 0,        # LLMを呼んだ回数（継続分割・リトライも1回と数える）
            "llm_attempts": 0,     # 実際に送ったリクエスト数（リトライを含む）
            "llm_ms": 0,           # LLMの応答を待った合計
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "missing_usage": 0,    # usage が返らなかった呼び出しの数
        }

    # --- 記録 ---

    def record_llm(self, response, elapsed_ms: int, attempts: int = 1,
                   reasoning_tokens: int = 0):
        i, o, reported = _usage_of(response)
        with self._lock:
            for bucket in (self._node, self._total):
                bucket["llm_calls"] += 1
                bucket["llm_attempts"] += max(1, attempts)
                bucket["llm_ms"] += max(0, elapsed_ms)
                bucket["input_tokens"] += i
                bucket["output_tokens"] += o
                bucket["reasoning_tokens"] += max(0, reasoning_tokens)
                if not reported:
                    bucket["missing_usage"] += 1

    def begin_node(self):
        with self._lock:
            self._node = self._zero()
            self._node_started = time.monotonic()

    def take_node(self) -> dict:
        """いまのノードぶんを取り出して締める。_with_trace から呼ぶ。"""
        with self._lock:
            elapsed_ms = int((time.monotonic() - self._node_started) * 1000)
            out = dict(self._node)
            out["elapsed_ms"] = elapsed_ms
            out["tokens_reported"] = out["llm_calls"] > 0 and out["missing_usage"] == 0
            self._total["nodes"] += 1
            self._total["elapsed_ms"] = self._total.get("elapsed_ms", 0) + elapsed_ms
            # 同じノードぶんを二度足さない。取り出したら空にする
            self._node = self._zero()
            self._node_started = time.monotonic()
            return out

    def totals(self) -> dict:
        with self._lock:
            out = dict(self._total)
        out.setdefault("elapsed_ms", 0)
        out["tokens_reported"] = out["llm_calls"] > 0 and out["missing_usage"] == 0
        return out


# --- 実行スコープ ---

def current() -> RunCollector | None:
    return getattr(_active, "collector", None)


@contextmanager
def collect_run():
    """実行の入口で囲う。抜けると集計は捨てられる。"""
    collector = RunCollector()
    previous = current()
    _active.collector = collector
    try:
        yield collector
    finally:
        _active.collector = previous


@contextmanager
def use_collector(collector):
    """
    別スレッドで親の集計器を引き継ぐ。

    ThreadPoolExecutor でレビュアーを並列化するときは、ワーカーの中で
    これを使わないとそのぶんの計測が落ちる。
    """
    previous = current()
    _active.collector = collector
    try:
        yield collector
    finally:
        _active.collector = previous


# --- 計測点から呼ぶ薄いラッパ（集計器が無ければ何もしない）---

def record_llm(response, elapsed_ms: int, attempts: int = 1,
               reasoning_tokens: int = 0):
    c = current()
    if c is not None:
        c.record_llm(response, elapsed_ms, attempts, reasoning_tokens)


def begin_node():
    c = current()
    if c is not None:
        c.begin_node()


def take_node() -> dict:
    c = current()
    return c.take_node() if c is not None else {}


def totals() -> dict:
    c = current()
    return c.totals() if c is not None else {}


# --- 表示用 ---

def fmt_ms(ms) -> str:
    if not isinstance(ms, (int, float)) or ms < 0:
        return "—"
    if ms < 1000:
        return f"{int(ms)}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{int(ms // 60_000)}m{int((ms % 60_000) // 1000)}s"


def fmt_tokens(entry: dict) -> str:
    """
    トークンの表示。取れていないときは数字を作らない。

    「in 3,412 / out 512」または「未報告」。
    """
    if not entry or not entry.get("llm_calls"):
        return "—"
    if not entry.get("input_tokens") and not entry.get("output_tokens"):
        return "未報告"
    text = f"in {entry.get('input_tokens', 0):,} / out {entry.get('output_tokens', 0):,}"
    if entry.get("missing_usage"):
        text += f"（{entry['missing_usage']}件は未報告）"
    return text
