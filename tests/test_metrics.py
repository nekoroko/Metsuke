# tests/test_metrics.py — 所要時間とトークンの計測
#
# 要点は「取れなかったものを作らない」こと。usage を返さないプロバイダで
# 0 や推定値が入ると、根拠のない数字が計測値として画面に並ぶ。

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stubs import install_llm_stubs  # noqa: E402

install_llm_stubs()

import config  # noqa: E402
import graph  # noqa: E402
import metrics  # noqa: E402
from executor import run_totals  # noqa: E402


class FakeResponse:
    """langchain の AIMessage が返す形だけを真似る。"""

    def __init__(self, usage_metadata=None, response_metadata=None):
        self.usage_metadata = usage_metadata or {}
        self.response_metadata = response_metadata or {}
        self.content = "ok"


class FakeLLM:
    def __init__(self, response, delay=0.0, fail_times=0):
        self.response = response
        self.delay = delay
        self.fail_times = fail_times
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("529 overloaded")
        if self.delay:
            time.sleep(self.delay)
        return self.response


class TestUsageExtraction(unittest.TestCase):
    def test_usage_metadataから読む(self):
        r = FakeResponse(usage_metadata={"input_tokens": 100, "output_tokens": 20})
        self.assertEqual(metrics._usage_of(r), (100, 20, True))

    def test_openai互換のtoken_usageから読む(self):
        r = FakeResponse(response_metadata={
            "token_usage": {"prompt_tokens": 300, "completion_tokens": 40}})
        self.assertEqual(metrics._usage_of(r), (300, 40, True))

    def test_返ってこなければ報告なしとして返す(self):
        self.assertEqual(metrics._usage_of(FakeResponse()), (0, 0, False))
        self.assertEqual(metrics._usage_of(None), (0, 0, False))

    def test_片方だけでも報告ありとして扱う(self):
        r = FakeResponse(usage_metadata={"output_tokens": 7})
        self.assertEqual(metrics._usage_of(r), (0, 7, True))


class TestCollector(unittest.TestCase):
    def test_ノードごとに締めて合計にも積む(self):
        with metrics.collect_run() as c:
            metrics.begin_node()
            c.record_llm(FakeResponse({"input_tokens": 10, "output_tokens": 2}), 50)
            c.record_llm(FakeResponse({"input_tokens": 5, "output_tokens": 1}), 30)
            first = metrics.take_node()

            metrics.begin_node()
            c.record_llm(FakeResponse({"input_tokens": 7, "output_tokens": 3}), 20)
            second = metrics.take_node()

            self.assertEqual(first["llm_calls"], 2)
            self.assertEqual(first["input_tokens"], 15)
            self.assertEqual(second["llm_calls"], 1)
            self.assertEqual(second["input_tokens"], 7)
            self.assertEqual(c.totals()["llm_calls"], 3)
            self.assertEqual(c.totals()["input_tokens"], 22)

    def test_取り出したら空になる_同じぶんを二度数えない(self):
        with metrics.collect_run() as c:
            metrics.begin_node()
            c.record_llm(FakeResponse({"input_tokens": 10, "output_tokens": 2}), 5)
            metrics.take_node()
            again = metrics.take_node()
            self.assertEqual(again["llm_calls"], 0)
            self.assertEqual(again["input_tokens"], 0)

    def test_未報告は数えるが数字は作らない(self):
        with metrics.collect_run() as c:
            metrics.begin_node()
            c.record_llm(FakeResponse(), 40)
            out = metrics.take_node()
        self.assertEqual(out["input_tokens"], 0)
        self.assertEqual(out["output_tokens"], 0)
        self.assertEqual(out["missing_usage"], 1)
        self.assertFalse(out["tokens_reported"])

    def test_所要時間が入る(self):
        with metrics.collect_run():
            metrics.begin_node()
            time.sleep(0.03)
            out = metrics.take_node()
        self.assertGreaterEqual(out["elapsed_ms"], 25)

    def test_集計器が無ければ何も起きない(self):
        # テストや CLI から素でノードを呼ぶ経路。例外を出さず空を返す
        metrics.record_llm(FakeResponse({"input_tokens": 1, "output_tokens": 1}), 5)
        self.assertEqual(metrics.take_node(), {})
        self.assertEqual(metrics.totals(), {})


class TestThreadSafety(unittest.TestCase):
    """
    レビュアーを ThreadPoolExecutor で並列化したときに落ちないこと。

    スレッドローカルだけで作ると、ワーカーぶんが黙って0になる。
    """

    def test_ワーカーがuse_collectorで引き継げる(self):
        with metrics.collect_run() as c:
            metrics.begin_node()

            def worker():
                with metrics.use_collector(c):
                    metrics.record_llm(
                        FakeResponse({"input_tokens": 100, "output_tokens": 10}), 10)

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            out = metrics.take_node()

        self.assertEqual(out["llm_calls"], 4)
        self.assertEqual(out["input_tokens"], 400)

    def test_引き継がないと落ちる_これが並列化のときの罠(self):
        with metrics.collect_run() as c:
            metrics.begin_node()

            def worker():
                # use_collector を忘れた場合。例外にはならず、静かに漏れる
                metrics.record_llm(
                    FakeResponse({"input_tokens": 100, "output_tokens": 10}), 10)

            t = threading.Thread(target=worker)
            t.start()
            t.join()
            out = metrics.take_node()

        self.assertEqual(out["llm_calls"], 0,
                         "この挙動が変わったら use_collector の説明を直すこと")
        self.assertEqual(c.totals()["llm_calls"], 0)

    def test_実行どうしは混ざらない(self):
        results = {}

        def run(name, tokens):
            with metrics.collect_run() as c:
                metrics.begin_node()
                c.record_llm(FakeResponse({"input_tokens": tokens, "output_tokens": 1}), 5)
                time.sleep(0.02)
                results[name] = metrics.take_node()["input_tokens"]

        a = threading.Thread(target=run, args=("a", 111))
        b = threading.Thread(target=run, args=("b", 222))
        a.start(), b.start()
        a.join(), b.join()
        self.assertEqual(results, {"a": 111, "b": 222})


class TestInvokeInstrumentation(unittest.TestCase):
    """計測点が invoke_with_retry の1箇所で足りていること"""

    def test_呼び出しが記録される(self):
        llm = FakeLLM(FakeResponse({"input_tokens": 800, "output_tokens": 120}), delay=0.02)
        with metrics.collect_run():
            metrics.begin_node()
            config.invoke_with_retry(llm, [])
            out = metrics.take_node()
        self.assertEqual(out["llm_calls"], 1)
        self.assertEqual(out["input_tokens"], 800)
        self.assertGreaterEqual(out["llm_ms"], 15)

    def test_リトライは1回の呼び出しとして数え_試行数は別に持つ(self):
        llm = FakeLLM(FakeResponse({"input_tokens": 10, "output_tokens": 1}), fail_times=2)
        with metrics.collect_run():
            metrics.begin_node()
            config.invoke_with_retry(llm, [])
            out = metrics.take_node()
        self.assertEqual(out["llm_calls"], 1)
        self.assertEqual(out["llm_attempts"], 3)

    def test_継続分割はそれぞれ1回として数える(self):
        # invoke_with_continuation は内部で invoke_with_retry を呼ぶ。
        # 分割ぶんが個別に数えられていないと、長文の実コストが見えない
        responses = [
            FakeResponse({"input_tokens": 900, "output_tokens": 300},
                         {"finish_reason": "length"}),
            FakeResponse({"input_tokens": 120, "output_tokens": 80},
                         {"finish_reason": "stop"}),
        ]

        class Seq:
            def __init__(self): self.i = 0

            def invoke(self, messages):
                r = responses[min(self.i, len(responses) - 1)]
                self.i += 1
                return r

        with metrics.collect_run():
            metrics.begin_node()
            config.invoke_with_continuation(Seq(), [])
            out = metrics.take_node()
        self.assertEqual(out["llm_calls"], 2)
        self.assertEqual(out["input_tokens"], 1020)


class TestTraceIntegration(unittest.TestCase):
    def test_traceエントリに混ざる(self):
        with metrics.collect_run() as c:
            metrics.begin_node()
            c.record_llm(FakeResponse({"input_tokens": 50, "output_tokens": 5}), 12)
            out = graph._with_trace({"trace": []}, {"status": "running"},
                                    "react", "考えた", "correct")
        entry = out["trace"][0]
        self.assertEqual(entry["llm_calls"], 1)
        self.assertEqual(entry["input_tokens"], 50)
        self.assertIn("elapsed_ms", entry)

    def test_計測していなければ何も足さない(self):
        out = graph._with_trace({"trace": []}, {"status": "running"},
                                "react", "考えた", "correct")
        entry = out["trace"][0]
        self.assertNotIn("elapsed_ms", entry)
        self.assertNotIn("input_tokens", entry)

    def test_measuredがノードを包んでも戻り値は変わらない(self):
        calls = []

        def node(state):
            calls.append(state)
            return {"ok": True}

        wrapped = graph.measured("x", node)
        self.assertEqual(wrapped({"a": 1}), {"ok": True})
        self.assertEqual(calls, [{"a": 1}])


class TestRunTotals(unittest.TestCase):
    def test_traceから合計する(self):
        state = {"trace": [
            {"elapsed_ms": 100, "llm_ms": 80, "llm_calls": 1,
             "input_tokens": 10, "output_tokens": 2, "missing_usage": 0},
            {"elapsed_ms": 50, "llm_ms": 0, "llm_calls": 0,
             "input_tokens": 0, "output_tokens": 0, "missing_usage": 0},
        ]}
        out = run_totals(state)
        self.assertEqual(out["elapsed_ms"], 150)
        self.assertEqual(out["input_tokens"], 10)
        self.assertEqual(out["nodes"], 2)
        self.assertTrue(out["tokens_reported"])

    def test_未報告があれば報告済みとは言わない(self):
        state = {"trace": [
            {"elapsed_ms": 10, "llm_calls": 2, "missing_usage": 2},
        ]}
        self.assertFalse(run_totals(state)["tokens_reported"])

    def test_計測前の実行でも落ちない(self):
        state = {"trace": [{"seq": 1, "node": "react", "summary": "x"}]}
        out = run_totals(state)
        self.assertEqual(out["elapsed_ms"], 0)
        self.assertFalse(out["tokens_reported"])
        self.assertEqual(run_totals({})["nodes"], 0)
        self.assertEqual(run_totals(None)["nodes"], 0)


class TestFormatting(unittest.TestCase):
    def test_時間の単位(self):
        self.assertEqual(metrics.fmt_ms(430), "430ms")
        self.assertEqual(metrics.fmt_ms(1500), "1.5s")
        self.assertEqual(metrics.fmt_ms(95_000), "1m35s")
        self.assertEqual(metrics.fmt_ms(None), "—")

    def test_未報告のときは数字を出さない(self):
        self.assertEqual(metrics.fmt_tokens({}), "—")
        self.assertEqual(
            metrics.fmt_tokens({"llm_calls": 2, "input_tokens": 0, "output_tokens": 0}),
            "未報告")
        self.assertEqual(
            metrics.fmt_tokens({"llm_calls": 1, "input_tokens": 1234, "output_tokens": 56}),
            "in 1,234 / out 56")

    def test_一部だけ未報告なら断り書きを付ける(self):
        text = metrics.fmt_tokens({
            "llm_calls": 3, "input_tokens": 100, "output_tokens": 20, "missing_usage": 1})
        self.assertIn("1件は未報告", text)

    def test_表の桁が全角でずれない(self):
        from verify_run import format_cost_table

        trace = [
            {"node": "search", "elapsed_ms": 60, "llm_ms": 60, "llm_calls": 2,
             "input_tokens": 2400, "output_tokens": 600, "missing_usage": 0},
            {"node": "compose", "elapsed_ms": 60, "llm_ms": 60, "llm_calls": 1,
             "input_tokens": 5000, "output_tokens": 900, "missing_usage": 0},
        ]
        lines = format_cost_table(trace).splitlines()
        from verify_run import _width

        widths = {_width(line) for line in lines if line.strip() and "---" not in line}
        self.assertEqual(len(widths), 1, f"行の表示幅が揃っていない: {widths}")

    def test_計測なしの実行はそう書く(self):
        from trace_view import totals_text, cost_text

        self.assertIn("計測していません", totals_text([{"seq": 1, "node": "react"}]))
        self.assertEqual(cost_text({"seq": 1, "node": "react"}), "")


if __name__ == "__main__":
    unittest.main()
