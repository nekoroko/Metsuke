# tests/test_research_graph.py — リサーチ用ステートマシンのテスト
#
# LLMもネットワークも使わない。_ask / web_search / fetch_url を差し替え、
# ノード単体の入出力と遷移だけを確認する。

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stubs import install_llm_stubs  # noqa: E402

install_llm_stubs()

import graph_research as gr  # noqa: E402
import graphs  # noqa: E402
from state import make_initial_state  # noqa: E402


SEARCH_RESULT = (
    "タイトル: SKハイニックス決算\n"
    "URL: https://example.com/a\n"
    "概要: 売上高は22.3兆ウォン、営業利益は9.2兆ウォンだった。\n"
    "---\n"
    "タイトル: 株価動向\n"
    "URL: https://example.com/b\n"
    "概要: 28日終値は-14.65%となった。"
)


def _state(**over):
    s = make_initial_state("SKハイニックスの決算と株価", **graphs.KIND_BUDGETS[graphs.RESEARCH])
    s.update(over)
    return s


class TestHelpers(unittest.TestCase):
    def test_urls_from_search(self):
        hits = gr._urls_from_search(SEARCH_RESULT)
        self.assertEqual([h["url"] for h in hits],
                         ["https://example.com/a", "https://example.com/b"])
        self.assertEqual(hits[0]["title"], "SKハイニックス決算")

    def test_urls_from_search_on_error_text(self):
        self.assertEqual(gr._urls_from_search("検索エラー: timeout"), [])

    def test_relevant_excerpt_prefers_numeric_paragraphs(self):
        body = (
            "このページはクッキーを使用しています。\n"
            "ナビゲーション ホーム 会社情報 採用\n"
            "SKハイニックスの2026年4-6月期の営業利益は9.2兆ウォンとなった。\n"
        )
        out = gr.relevant_excerpt(body, ["SKハイニックス", "営業利益"], budget=200)
        self.assertIn("9.2兆ウォン", out)
        self.assertNotIn("クッキー", out)

    def test_relevant_excerpt_respects_budget(self):
        body = "\n".join(f"売上は{i}億円だった。" * 5 for i in range(20))
        self.assertLessEqual(len(gr.relevant_excerpt(body, ["売上"], budget=300)), 300)

    def test_relabel_next_rewrites_last_entry_only(self):
        out = {"trace": [
            {"seq": 1, "node": "compose", "next": "correct"},
            {"seq": 2, "node": "correct", "next": "react"},
        ]}
        fixed = gr._relabel_next(out, {"react": "compose"})
        self.assertEqual(fixed["trace"][1]["next"], "compose")
        self.assertEqual(fixed["trace"][0]["next"], "correct")

    def test_recent_feedback_picks_only_review_entries(self):
        history = [
            {"role": "result", "content": "[検索] foo\n結果"},
            {"role": "result", "content": "（自動訂正チェック）最終回答に問題があります。\n- 単位"},
        ]
        fb = gr._recent_feedback(history)
        self.assertIn("自動訂正チェック", fb)
        self.assertNotIn("[検索]", fb)


class TestPlanStep(unittest.TestCase):
    def tearDown(self):
        gr._ask = self._orig

    def setUp(self):
        self._orig = gr._ask

    def test_parses_numbered_lines(self):
        gr._ask = lambda *a, **k: (
            "1. 直近の人口 | 東京 人口 推計\n"
            "2. 前年からの増減 | 東京 人口 増減\n"
        )
        # 決算・株価タスクは reinforce_plan が項目を足すため、
        # ここでは補強の対象にならないタスクでパースだけを見る
        out = gr.plan_step(_state(task="東京の人口推計"))
        self.assertEqual(len(out["plan_items"]), 2)
        self.assertEqual(out["plan_items"][0]["query"], "東京 人口 推計")
        self.assertEqual(out["plan_items"][0]["status"], "open")
        self.assertEqual(out["trace"][-1]["next"], "search")

    def test_falls_back_to_task_when_unparseable(self):
        gr._ask = lambda *a, **k: "承知しました。まず調査を始めます。"
        out = gr.plan_step(_state(task="東京の人口推計"))
        self.assertEqual(len(out["plan_items"]), 1)
        self.assertTrue(out["trace"][-1]["skipped"])

    def test_llm_failure_does_not_stop_the_graph(self):
        def boom(*a, **k):
            raise RuntimeError("接続エラー")
        gr._ask = boom
        out = gr.plan_step(_state(task="東京の人口推計"))
        self.assertEqual(out["status"], "running")
        self.assertEqual(len(out["plan_items"]), 1)
        self.assertIn("接続エラー", out["trace"][-1]["note"])

    def test_skips_when_plan_exists(self):
        gr._ask = lambda *a, **k: self.fail("計画済みならLLMを呼ばない")
        out = gr.plan_step(_state(plan_items=[{"id": 1, "question": "q",
                                               "query": "q", "status": "open", "hits": []}]))
        self.assertTrue(out["trace"][-1]["skipped"])


class TestSearchStep(unittest.TestCase):
    def setUp(self):
        self._orig = gr.web_search
        gr.web_search = lambda q: SEARCH_RESULT

    def tearDown(self):
        gr.web_search = self._orig

    def _planned(self, **over):
        return _state(plan_items=[
            {"id": 1, "question": "決算", "query": "SKハイニックス 決算", "status": "open", "hits": []},
            {"id": 2, "question": "株価", "query": "SKハイニックス 株価", "status": "filled", "hits": []},
        ], **over)

    def test_searches_only_open_items(self):
        out = gr.search_step(self._planned())
        self.assertEqual(out["queries_done"], ["SKハイニックス 決算"])
        self.assertEqual(out["plan_items"][0]["hits"][0]["url"], "https://example.com/a")
        self.assertEqual(out["plan_items"][1]["hits"], [])

    def test_collects_findings_from_results(self):
        out = gr.search_step(self._planned())
        raws = [f["raw"] for f in out["findings"]]
        self.assertTrue(any("22.3兆ウォン" in r for r in raws), raws)

    def test_does_not_repeat_a_query(self):
        out = gr.search_step(self._planned(queries_done=["SKハイニックス 決算"]))
        self.assertTrue(out["trace"][-1]["skipped"])
        self.assertEqual(out["queries_done"], ["SKハイニックス 決算"])

    def test_search_error_is_recorded_not_raised(self):
        def boom(q):
            raise RuntimeError("timeout")
        gr.web_search = boom
        out = gr.search_step(self._planned())
        self.assertIn("timeout", out["history"][-1]["content"])
        self.assertEqual(out["status"], "running")


class TestDigestStep(unittest.TestCase):
    def setUp(self):
        self._orig = gr.fetch_url
        gr.fetch_url = lambda url: (
            "メニュー ホーム\n"
            "SKハイニックスの営業利益は9.2兆ウォンで過去最高となった。\n"
        )

    def tearDown(self):
        gr.fetch_url = self._orig

    def _searched(self):
        return _state(plan_items=[{
            "id": 1, "question": "決算", "query": "決算", "status": "open",
            "hits": [{"url": "https://example.com/a", "title": "決算"}],
        }])

    def test_fetches_and_stores_excerpt(self):
        out = gr.digest_step(self._searched())
        self.assertEqual(len(out["sources"]), 1)
        self.assertIn("9.2兆ウォン", out["sources"][0]["excerpt"])
        self.assertEqual(out["trace"][-1]["next"], "gap")

    def test_skips_already_fetched_url(self):
        s = self._searched()
        s["sources"] = [{"url": "https://example.com/a", "excerpt": "x"}]
        out = gr.digest_step(s)
        self.assertTrue(out["trace"][-1]["skipped"])

    def test_fetch_error_does_not_add_source(self):
        gr.fetch_url = lambda url: "エラー: 403"
        out = gr.digest_step(self._searched())
        self.assertEqual(out["sources"], [])
        self.assertTrue(out["trace"][-1]["skipped"])


class TestGapStep(unittest.TestCase):
    def setUp(self):
        self._orig = gr._ask

    def tearDown(self):
        gr._ask = self._orig

    def _items(self):
        return [
            {"id": 1, "question": "決算", "query": "決算", "status": "open", "hits": []},
            {"id": 2, "question": "株価", "query": "株価", "status": "open", "hits": []},
        ]

    def test_ng_item_gets_new_query_and_loops_back(self):
        gr._ask = lambda *a, **k: "ITEM 1: OK\nITEM 2: NG | SKハイニックス 株価 今週\n"
        out = gr.gap_step(_state(plan_items=self._items()))
        self.assertEqual(out["plan_items"][0]["status"], "filled")
        self.assertEqual(out["plan_items"][1]["status"], "open")
        self.assertEqual(out["plan_items"][1]["query"], "SKハイニックス 株価 今週")
        self.assertEqual(gr.route_after_gap(out), "search")

    def test_all_ok_moves_to_compose(self):
        gr._ask = lambda *a, **k: "ITEM 1: OK\nITEM 2: OK"
        out = gr.gap_step(_state(plan_items=self._items()))
        self.assertEqual(gr.route_after_gap(out), "compose")

    def test_round_limit_forces_compose(self):
        gr._ask = lambda *a, **k: self.fail("上限到達時はLLMを呼ばない")
        out = gr.gap_step(_state(plan_items=self._items(), research_round=2, max_rounds=3))
        self.assertEqual(gr.route_after_gap(out), "compose")
        self.assertTrue(out["trace"][-1]["skipped"])

    def test_unparseable_verdict_moves_forward(self):
        gr._ask = lambda *a, **k: "まだ情報が足りないと思います。"
        out = gr.gap_step(_state(plan_items=self._items()))
        self.assertEqual(gr.route_after_gap(out), "compose")
        self.assertTrue(out["trace"][-1]["skipped"])

    def test_llm_failure_moves_forward(self):
        def boom(*a, **k):
            raise RuntimeError("落ちた")
        gr._ask = boom
        out = gr.gap_step(_state(plan_items=self._items()))
        self.assertEqual(gr.route_after_gap(out), "compose")


class TestComposeStep(unittest.TestCase):
    def setUp(self):
        self._orig = gr._ask

    def tearDown(self):
        gr._ask = self._orig

    def test_writes_done_prefixed_answer(self):
        gr._ask = lambda *a, **k: "DONE: 営業利益は9.2兆ウォン [実績] でした。"
        out = gr.compose_step(_state())
        self.assertEqual(out["status"], "needs_revision")
        self.assertTrue(out["history"][-1]["content"].startswith("DONE:"))
        self.assertEqual(gr.route_after_compose(out), "correct")

    def test_adds_done_prefix_when_missing(self):
        gr._ask = lambda *a, **k: "営業利益は9.2兆ウォンでした。"
        out = gr.compose_step(_state())
        self.assertTrue(out["history"][-1]["content"].startswith("DONE:"))

    def test_empty_response_retries_compose(self):
        gr._ask = lambda *a, **k: "   "
        out = gr.compose_step(_state())
        self.assertEqual(gr.route_after_compose(out), "compose")
        # 空応答は差し戻しとは別枠で数える（compose_count を食わない）
        self.assertEqual(out["compose_retry_count"], 1)
        self.assertEqual(out["compose_count"], 0)

    def test_empty_response_gives_up_at_retry_limit(self):
        gr._ask = lambda *a, **k: "   "
        out = gr.compose_step(_state(compose_retry_count=1, max_compose_retries=1))
        self.assertEqual(gr.route_after_compose(out), "correct")
        self.assertEqual(out["compose_count"], 0)
        self.assertTrue(any("空応答" in n for n in out["verification_notes"]))

    def test_stops_at_compose_limit(self):
        gr._ask = lambda *a, **k: self.fail("上限到達時はLLMを呼ばない")
        out = gr.compose_step(_state(compose_count=3, max_composes=3))
        self.assertEqual(out["status"], "done")
        self.assertEqual(gr.route_after_compose(out), gr.END)

    def test_feedback_is_included_in_prompt(self):
        seen = {}

        def capture(prompt, **k):
            seen["prompt"] = prompt
            return "DONE: 修正しました。"
        gr._ask = capture
        s = _state(history=[{"role": "result",
                             "content": "（自動訂正チェック）最終回答に問題があります。\n- 単位が違う"}])
        gr.compose_step(s)
        self.assertIn("単位が違う", seen["prompt"])


class TestRouting(unittest.TestCase):
    def test_correct_routes_back_to_compose_on_retry(self):
        self.assertEqual(gr.route_after_correct({"status": "running"}), "compose")
        self.assertEqual(gr.route_after_correct({"status": "needs_revision"}), "critic")

    def test_critic_routes_back_to_compose(self):
        self.assertEqual(gr.route_after_critic({"status": "running"}), "compose")
        self.assertEqual(gr.route_after_critic({"status": "done"}), gr.END)

    def test_error_short_circuits(self):
        for fn in (gr.route_after_plan, gr.route_after_search,
                   gr.route_after_digest, gr.route_after_gap):
            self.assertEqual(fn({"status": "error", "plan_items": []}), gr.END)

    def test_every_route_target_is_a_registered_node(self):
        self.assertEqual(set(gr.NODES), set(gr.ROUTES))
        self.assertIn(gr.ENTRY_POINT, gr.NODES)
        for name, (_router, targets) in gr.ROUTES.items():
            for t in targets:
                self.assertIn(t, gr.NODES, f"{name} → {t} が未登録")


class TestGraphRegistry(unittest.TestCase):
    def setUp(self):
        self._orig_settings = graphs.read_settings

    def tearDown(self):
        graphs.read_settings = self._orig_settings

    def test_normalize_unknown_kind(self):
        self.assertEqual(graphs.normalize_kind("nope"), graphs.REACT)
        self.assertEqual(graphs.normalize_kind(graphs.RESEARCH), graphs.RESEARCH)

    def test_default_from_settings(self):
        graphs.read_settings = lambda: {graphs.SETTING_KEY: graphs.RESEARCH}
        self.assertEqual(graphs.default_kind(), graphs.RESEARCH)

    def test_override_wins_over_everything(self):
        graphs.read_settings = lambda: {graphs.SETTING_KEY: graphs.RESEARCH}
        self.assertEqual(graphs.resolve_kind(None, graphs.REACT), graphs.REACT)

    def test_per_task_wins_over_default(self):
        graphs.read_settings = lambda: {graphs.SETTING_KEY: graphs.REACT}
        import db
        orig = db.get_agent_task
        db.get_agent_task = lambda tid: {"graph_kind": graphs.RESEARCH}
        try:
            self.assertEqual(graphs.resolve_kind("t1"), graphs.RESEARCH)
        finally:
            db.get_agent_task = orig

    def test_blank_per_task_falls_back_to_default(self):
        graphs.read_settings = lambda: {graphs.SETTING_KEY: graphs.RESEARCH}
        import db
        orig = db.get_agent_task
        db.get_agent_task = lambda tid: {"graph_kind": ""}
        try:
            self.assertEqual(graphs.resolve_kind("t1"), graphs.RESEARCH)
        finally:
            db.get_agent_task = orig

    def test_state_budget_differs_by_kind(self):
        react = graphs.make_state(graphs.REACT, "t")
        research = graphs.make_state(graphs.RESEARCH, "t")
        self.assertEqual(react["max_steps"], 10)
        self.assertEqual(research["max_steps"], graphs.KIND_BUDGETS[graphs.RESEARCH]["max_steps"])
        self.assertEqual(set(react), set(research))


if __name__ == "__main__":
    unittest.main()


class TestPlanReinforcement(unittest.TestCase):
    """計画に、取りこぼしがちな観点を機械的に足す"""

    def test_earnings_task_gets_actual_query(self):
        items = [{"id": 1, "question": "決算", "query": "SKハイニックス 決算",
                  "status": "open", "hits": []}]
        out = gr.reinforce_plan("SKハイニックスの決算を調べて", items)
        self.assertEqual(len(out), 2)
        self.assertIn("実績", out[1]["query"])

    def test_no_duplicate_when_plan_already_covers_it(self):
        items = [{"id": 1, "question": "決算", "query": "SKハイニックス 決算 実績",
                  "status": "open", "hits": []}]
        self.assertEqual(len(gr.reinforce_plan("SKハイニックスの決算", items)), 1)

    def test_price_task_gets_time_series_query(self):
        items = [{"id": 1, "question": "株価", "query": "SKハイニックス 株価",
                  "status": "open", "hits": []}]
        out = gr.reinforce_plan("SKハイニックスの株価を調べて", items)
        self.assertTrue(any("推移" in i["query"] for i in out))

    def test_unrelated_task_is_untouched(self):
        items = [{"id": 1, "question": "天気", "query": "東京 天気",
                  "status": "open", "hits": []}]
        self.assertEqual(gr.reinforce_plan("東京の天気", items), items)

    def test_does_not_exceed_item_cap(self):
        items = [{"id": i, "question": "q", "query": "決算 q", "status": "open", "hits": []}
                 for i in range(1, gr.MAX_ITEMS + 1)]
        self.assertEqual(len(gr.reinforce_plan("決算と株価を調べて", items)), gr.MAX_ITEMS)


class TestResultRecheck(unittest.TestCase):
    """発表予定を掴んだら、結果が出ているかを必ず一度確認する"""

    def _pending_state(self, **over):
        findings = [{"kind": "date", "raw": "7月29日",
                     "context": "29日に第2四半期決算の発表を控える", "source": "web_search(x)"}]
        return _state(findings=findings, plan_items=[
            {"id": 1, "question": "決算", "query": "SKハイニックス 決算",
             "status": "filled", "hits": []},
        ], **over)

    def test_detects_missing_result_query(self):
        self.assertTrue(gr.needs_result_recheck(self._pending_state()))

    def test_satisfied_once_result_query_ran(self):
        s = self._pending_state(queries_done=["SKハイニックス 決算 結果"])
        self.assertFalse(gr.needs_result_recheck(s))

    def test_no_pending_event_no_recheck(self):
        self.assertFalse(gr.needs_result_recheck(_state()))

    def test_gap_loops_back_without_calling_llm(self):
        orig = gr._ask
        gr._ask = lambda *a, **k: self.fail("結果確認の差し戻しでLLMは呼ばない")
        try:
            out = gr.gap_step(self._pending_state())
        finally:
            gr._ask = orig
        self.assertEqual(gr.route_after_gap(out), "search")
        self.assertTrue(any("結果" in i["query"] for i in out["plan_items"]))

    def test_round_limit_still_wins(self):
        s = self._pending_state(research_round=2, max_rounds=3)
        orig = gr._ask
        gr._ask = lambda *a, **k: self.fail("上限到達時はLLMを呼ばない")
        try:
            out = gr.gap_step(s)
        finally:
            gr._ask = orig
        self.assertEqual(gr.route_after_gap(out), "compose")


class TestQueryRegeneration(unittest.TestCase):
    """gap は未充足の項目に必ず未使用のクエリを与える"""

    def setUp(self):
        self._orig = gr._ask

    def tearDown(self):
        gr._ask = self._orig

    def test_重複クエリなら別の切り口に差し替える(self):
        gr._ask = lambda *a, **k: "ITEM 1: NG | SKハイニックス 決算"
        s = _state(plan_items=[{"id": 1, "question": "決算の実績", "query": "SKハイニックス 決算",
                                "status": "open", "hits": []}],
                   queries_done=["SKハイニックス 決算"])
        out = gr.gap_step(s)
        new_query = out["plan_items"][0]["query"]
        self.assertFalse(gr.is_duplicate_query(new_query, s["queries_done"]))
        self.assertEqual(gr.route_after_gap(out), "search")

    def test_クエリを返さなくても新しいクエリを作る(self):
        gr._ask = lambda *a, **k: "ITEM 1: NG"
        s = _state(plan_items=[{"id": 1, "question": "決算の実績", "query": "SKハイニックス 決算",
                                "status": "open", "hits": []}],
                   queries_done=["SKハイニックス 決算"])
        out = gr.gap_step(s)
        self.assertTrue(out["plan_items"][0]["query"])
        self.assertFalse(gr.is_duplicate_query(out["plan_items"][0]["query"],
                                               s["queries_done"]))

    def test_切り口が尽きても重複を返さない(self):
        done = [f"SKハイニックス {a}" for a in gr.QUERY_ANGLES]
        q = gr.fresh_query("SKハイニックスの決算", "直近の営業利益", done)
        self.assertFalse(gr.is_duplicate_query(q, done))


class TestIrrelevantQuery(unittest.TestCase):
    """タスクと無関係なクエリを plan の時点で是正する"""

    def test_無関係なクエリは作り直す(self):
        items = [{"id": 1, "question": "市場動向", "query": "バッテリー市場 動向",
                  "status": "open", "hits": []}]
        out = gr.drop_irrelevant_queries("SKハイニックスの決算と株価", items)
        self.assertIn("SKハイニックス", out[0]["query"])
        self.assertEqual(out[0]["repaired_from"], "バッテリー市場 動向")

    def test_関連するクエリはそのまま(self):
        items = [{"id": 1, "question": "決算", "query": "SKハイニックス 決算 実績",
                  "status": "open", "hits": []}]
        out = gr.drop_irrelevant_queries("SKハイニックスの決算と株価", items)
        self.assertEqual(out[0]["query"], "SKハイニックス 決算 実績")
        self.assertNotIn("repaired_from", out[0])

    def test_分かち書きされない日本語でも主語を取れる(self):
        self.assertEqual(gr._subject("SKハイニックスの直近の決算と株価を調べて"), "SKハイニックス")
        self.assertEqual(gr._subject("TSMCの決算"), "TSMC")


class TestComposeBudgetOrdering(unittest.TestCase):
    """critic の指摘は、compose 枠が無ければ注記に回す"""

    def setUp(self):
        self._orig_dispatch = gr.critic_step

    def tearDown(self):
        gr.critic_step = self._orig_dispatch

    def _critic_says_revise(self, state):
        return {**state,
                "history": state["history"] + [{"role": "result", "content": "指摘: 時点が無い"}],
                "status": "running",
                "critique_count": state.get("critique_count", 0) + 1,
                "trace": (state.get("trace") or []) + [
                    {"seq": 1, "node": "critic", "next": "react", "summary": "要修正",
                     "note": "", "skipped": False}]}

    def test_枠が残っていれば差し戻す(self):
        gr.critic_step = self._critic_says_revise
        out = gr.critic_sm_step(_state(compose_count=1, max_composes=3))
        self.assertEqual(gr.route_after_critic(out), "compose")

    def test_枠が無ければ注記にして終える(self):
        gr.critic_step = self._critic_says_revise
        out = gr.critic_sm_step(_state(compose_count=3, max_composes=3))
        self.assertEqual(out["status"], "done")
        self.assertTrue(any("レビュー未反映" in n for n in out["verification_notes"]))
        self.assertEqual(out["trace"][-1]["next"], "END")

    def test_予算の不変条件(self):
        s = graphs.make_state(graphs.RESEARCH, "t")
        self.assertLessEqual(s["max_critiques"], s["max_composes"] - 1)


class TestConfirmedInCompose(unittest.TestCase):
    """確定済みフィールドは compose のプロンプトに載る"""

    def setUp(self):
        self._orig = gr._ask

    def tearDown(self):
        gr._ask = self._orig

    def test_確定済みが渡り変更に理由を求める(self):
        seen = {}

        def capture(prompt, **k):
            seen["prompt"] = prompt
            return "DONE: 書きました。"
        gr._ask = capture
        gr.compose_step(_state(confirmed=[
            {"raw": "22.3兆ウォン", "label": "売上高", "value_type": "actual"}]))
        self.assertIn("確定済み", seen["prompt"])
        self.assertIn("22.3兆ウォン", seen["prompt"])
        self.assertIn("理由を1行で", seen["prompt"])


class TestComposeBudgetAccounting(unittest.TestCase):
    """
    compose 枠は、初稿・correct の差し戻し・critic の差し戻しの全部が消費する。

    doc27 では correct が枠を使い切り、最後の critic 指摘が
    「執筆枠が残っていない」で捨てられていた。
    """

    def test_初稿と両方の差し戻しが収まる枠がある(self):
        s = graphs.make_state(graphs.RESEARCH, "t")
        self.assertGreaterEqual(
            s["max_composes"], 1 + s["max_corrections"] + s["max_critiques"])

    def test_correctはcriticの枠を食わない(self):
        import graph
        from state import make_initial_state
        # critic が2回残っているのに compose 枠が2しかない状況
        state = make_initial_state("決算", max_composes=4, max_critiques=2,
                                   reserve_compose_for_critic=1)
        state["compose_count"] = 2
        state["findings"] = []
        state["history"] = [
            {"role": "result", "content": "売上高は22.3兆ウォン"},
            {"role": "assistant", "content": "DONE: 利益率は99.9% [実績] でした。"},
        ]
        out = graph.correct_step(state)
        self.assertEqual(out["status"], "needs_revision")   # 差し戻さず critic へ
        self.assertEqual(gr.route_after_correct(out), "critic")

    def test_枠があるうちは差し戻す(self):
        import graph
        from state import make_initial_state
        state = make_initial_state("決算", max_composes=5, max_critiques=2,
                                   reserve_compose_for_critic=1)
        state["compose_count"] = 1
        state["findings"] = []
        state["history"] = [
            {"role": "result", "content": "売上高は22.3兆ウォン"},
            {"role": "assistant", "content": "DONE: 利益率は99.9% [実績] でした。"},
        ]
        out = graph.correct_step(state)
        self.assertEqual(out["status"], "running")
        self.assertEqual(gr.route_after_correct(out), "compose")


class TestConfirmedRestoration(unittest.TestCase):
    """確定済みの値は、差し戻せない場合に本文へ機械的に補記する"""

    def test_消えた確定済みの値を末尾に戻す(self):
        import graph
        from state import make_initial_state
        from numeric import collect_from_text
        state = make_initial_state("決算", max_composes=3,
                                   reserve_compose_for_critic=1)
        state["findings"] = collect_from_text(
            "売上高は22.3兆ウォン。28日終値は-14.65%。", source="web_search(x)")
        state["confirmed"] = [{"raw": "-14.65%", "label": "終値", "value_type": "actual"}]
        state["history"] = [
            {"role": "result", "content": "売上高は22.3兆ウォン。28日終値は-14.65%。"},
            {"role": "assistant", "content": "DONE: 売上高は22.3兆ウォン [実績] のみ。"},
        ]
        state["step_count"] = 10          # 差し戻せない
        out = graph.correct_step(state)
        done = [e for e in out["history"] if e["role"] == "assistant"][-1]["content"]
        self.assertIn("検証済みだが本文に反映されなかった値", done)
        self.assertIn("-14.65%", done)
        self.assertTrue(any("補記" in n for n in out["verification_notes"]))


class TestBudgetInvariantGenerality(unittest.TestCase):
    """
    compose 枠の式が、今回のログだけを救う特殊解ではないことを確かめる。

    compose_count を増やす経路をコードから数え上げ、その最悪ケースを
    総当たりで検証する。経路が増えたらこのテストが落ちるようにしてある。
    """

    CONSUMERS = ("初稿", "correctの差し戻し", "criticの差し戻し")

    def test_compose_countを増やす箇所が増えていない(self):
        """
        枠を消費するのは「執筆が成立した1回」と「LLM例外で終了する1回」だけ。
        後者は END へ抜けるので差し戻し予算とは競合しない。
        新しい消費経路が足されたら、この件数が変わって落ちる。
        """
        import inspect
        src = inspect.getsource(gr)
        writes = [line.strip() for line in src.splitlines()
                  if '"compose_count":' in line and "state.get" not in line]
        self.assertEqual(len(writes), 2, f"compose_count の更新箇所が増えている: {writes}")
        self.assertTrue(all("composed + 1" in w for w in writes))

    def test_例外での消費は終了するので競合しない(self):
        orig = gr._ask

        def boom(*a, **k):
            raise RuntimeError("接続断")
        gr._ask = boom
        try:
            out = gr.compose_step(_state())
        finally:
            gr._ask = orig
        self.assertEqual(out["status"], "error")
        self.assertEqual(gr.route_after_compose(out), gr.END)

    def test_最悪ケースでも枠が足りる(self):
        for corrections in range(0, 4):
            for critiques in range(0, 4):
                need = graphs.required_composes(corrections, critiques)
                budgets = graphs._enforce_budget_invariant({
                    "max_corrections": corrections, "max_critiques": critiques})
                used = 1 + corrections + critiques      # 初稿 + 両方が上限まで差し戻す
                self.assertGreaterEqual(
                    budgets["max_composes"], used,
                    f"corrections={corrections}, critiques={critiques} で枠が足りない")
                self.assertEqual(need, used)

    def test_空応答は枠を食わない(self):
        # 空応答は compose_retry_count で数えるため、上の式に影響しない
        s = graphs.make_state(graphs.RESEARCH, "t")
        self.assertGreaterEqual(s["max_compose_retries"], 1)
        self.assertEqual(s["compose_retry_count"], 0)

    def test_指摘件数は枠を増やさない(self):
        # 1回の差し戻しで何件指摘しても、消費する枠は1つ
        import graph
        from state import make_initial_state
        from numeric import collect_from_text
        state = make_initial_state("決算", max_composes=5, max_corrections=2,
                                   reserve_compose_for_critic=1)
        state["findings"] = collect_from_text("売上高は22.3兆ウォン", source="s")
        state["history"] = [
            {"role": "result", "content": "売上高は22.3兆ウォン"},
            {"role": "assistant",
             "content": "DONE: 利益率は99.9% [実績]、成長率は88.8% [実績]、粗利は77.7% [実績]。"},
        ]
        out = graph.correct_step(state)
        feedback = out["history"][-1]["content"]
        self.assertGreaterEqual(feedback.count("見当たりません"), 3)   # 指摘は3件
        self.assertEqual(out["compose_count"], 0)                      # 枠の消費は執筆側で1回だけ


class TestBudgetInvariantBySimulation(unittest.TestCase):
    """
    式の再検証を、算術ではなくループを回して行う（M3）。

    `TestBudgetInvariantGenerality` の総当たりは `1+c+k >= 1+c+k` を
    確かめているだけで、実際に何回 compose されるかは見ていない。
    `correction_count` を「差し戻した分岐だけ数える」に変えたことで
    correct の実消費が増える方向になったため、ここでは本物のノードで
    compose → correct → critic を回し、消費の実測値を見る。

    最悪ケースを作るために、correct も critic も必ず指摘を出す状態にする。
    実運用では差し戻しの抑制（同じ本文なら諦める等）が効いて、ここまで
    使い切らない。抑制に頼らず式が成り立つことを確かめるのが目的。
    """

    def setUp(self):
        import graph
        self.graph = graph
        self._orig = (graph.numeric_checker, graph.dispatch_reviewers,
                      graph.run_reviewers, gr._ask)

    def tearDown(self):
        (self.graph.numeric_checker, self.graph.dispatch_reviewers,
         self.graph.run_reviewers, gr._ask) = self._orig

    def _always_complains(self):
        drafts = {"i": 0}

        def draft(*a, **k):
            drafts["i"] += 1
            return f"DONE: 売上高は{100 + drafts['i']}兆ウォン [実績] でした。"

        gr._ask = draft
        self.graph.numeric_checker = lambda **k: {
            "verdict": "NEEDS_REVISION", "issues": ["数値が出典と合わない"],
            "instruction": "その数値を削除してください"}
        self.graph.dispatch_reviewers = lambda *a, **k: ["fact_checker"]
        self.graph.run_reviewers = lambda *a, **k: [
            {"reviewer": "fact_checker", "verdict": "NEEDS_REVISION",
             "issues": ["出典と食い違う"], "instruction": "削除する"}]

    def _run(self, corrections, critiques, max_steps=18):
        budgets = graphs._enforce_budget_invariant({
            "max_corrections": corrections, "max_critiques": critiques,
            "reserve_compose_for_critic": 1})
        state = make_initial_state("決算", max_steps=max_steps, **budgets)
        state["history"] = [{"role": "result", "content": "売上高は22.3兆ウォンだった。"}]
        self._always_complains()

        node = "compose"
        for _ in range(80):      # 遷移が閉じなくてもテストが止まらないようにする
            if node == "compose":
                state = gr.compose_step(state)
                node = gr.route_after_compose(state)
            elif node == "correct":
                state = gr.correct_sm_step(state)
                node = gr.route_after_correct(state)
            elif node == "critic":
                state = gr.critic_sm_step(state)
                node = gr.route_after_critic(state)
            else:
                break
            state["step_count"] = state.get("step_count", 0) + 1
            if node in (gr.END, "__end__"):
                break
        return state, budgets

    def test_総当たりで枠が足りる(self):
        for c in range(0, 4):
            for k in range(0, 4):
                with self.subTest(corrections=c, critiques=k):
                    st, b = self._run(c, k)
                    self.assertLessEqual(
                        st["compose_count"], b["max_composes"],
                        "compose の実消費が式の枠を超えた")
                    # 上限そのものも守られているか（片方が食い合っていないか）
                    self.assertLessEqual(st["correction_count"], c)
                    self.assertLessEqual(st["critique_count"], k)

    def test_総当たりでcompose枠切れが起きない(self):
        # 枠が尽きて compose が素通りする／critic が指摘を捨てる、のどちらも
        # 起きてはいけない。doc27 で実際に起きた壊れ方がこれ。
        for c in range(0, 4):
            for k in range(0, 4):
                with self.subTest(corrections=c, critiques=k):
                    st, _ = self._run(c, k)
                    trace = st.get("trace", [])
                    self.assertFalse(
                        [t for t in trace if "執筆枠が残っていない" in (t.get("summary") or "")],
                        "critic が compose 枠切れで指摘を捨てた")
                    self.assertFalse(
                        [t for t in trace
                         if t.get("node") == "compose" and t.get("skipped")],
                        "compose が上限に当たって素通りした")

    def test_差し戻せなかった指摘は必ず注記に残る(self):
        # 枠でも歩数でも止まったとき、指摘が黙って消えないこと
        st, _ = self._run(1, 1, max_steps=6)
        self.assertTrue(st.get("verification_notes"))

    def test_実消費は式の値と一致する(self):
        # 式が「足りる」だけでなく「無駄に多くない」ことも見る。
        # 余分に積むと、そのぶん遅くなりトークンも食う。
        for c, k in ((0, 0), (1, 1), (2, 2)):
            with self.subTest(corrections=c, critiques=k):
                st, b = self._run(c, k)
                self.assertEqual(st["compose_count"], graphs.required_composes(c, k))
                self.assertEqual(b["max_composes"], st["compose_count"])


class TestDigestTruncationFlag(unittest.TestCase):
    """SM の digest 経路でも、元ページの切断がフラグとして残る"""

    def setUp(self):
        self._orig = gr.fetch_url

    def tearDown(self):
        gr.fetch_url = self._orig

    def _searched(self):
        return _state(plan_items=[{
            "id": 1, "question": "決算", "query": "決算", "status": "open",
            "hits": [{"url": "https://example.com/a", "title": "決算"}],
        }])

    def test_切断された本文はフラグが立つ(self):
        from numeric import TRUNCATION_MARK
        body = ("SKハイニックスの営業利益は9.2兆ウォンとなった。\n" * 60) + TRUNCATION_MARK
        gr.fetch_url = lambda url: body
        out = gr.digest_step(self._searched())
        self.assertTrue(out["sources"][0]["source_truncated"])
        # 抜粋自体には印は残らない（残す設計にしていない）
        self.assertNotIn(TRUNCATION_MARK, out["sources"][0]["excerpt"])

    def test_切れていない本文はフラグが立たない(self):
        gr.fetch_url = lambda url: "SKハイニックスの営業利益は9.2兆ウォンとなった。"
        out = gr.digest_step(self._searched())
        self.assertFalse(out["sources"][0]["source_truncated"])


class TestBudgetInvariantWithRealReviewers(unittest.TestCase):
    """
    予算式の再検証を、レビュアー経路を**実物のまま**回して行う（1-1）。

    既存の TestBudgetInvariantBySimulation は graph.run_reviewers ごと
    スタブに差し替えているため、ALWAYS_ON_REVIEWERS 経由で
    numeric_checker が呼ばれる経路をそもそも通っていなかった。
    つまり「M3の総当たりを再実行して green」でも、今回直した二重実行は
    一切カバーできていない。

    ここでは LLM を叩く入口（compose の _ask / dispatch_reviewers /
    LLMレビュアー）だけを差し替え、run_reviewers・ALWAYS_ON_REVIEWERS・
    numeric_checker は実物を通す。APIキーは要らない。
    """

    def setUp(self):
        import graph
        import reviewers
        self.graph = graph
        self.reviewers = reviewers
        self._orig = (graph.numeric_checker, graph.dispatch_reviewers,
                      graph.run_reviewers, gr._ask,
                      dict(reviewers.REVIEWER_REGISTRY))
        self.numeric_calls = []

        real_numeric = graph.numeric_checker

        def counting(**kw):
            self.numeric_calls.append(kw.get("output", ""))
            return real_numeric(**kw)

        # numeric_checker は実物。呼ばれた回数だけ数える
        graph.numeric_checker = counting
        reviewers.REVIEWER_REGISTRY["numeric_checker"] = counting

        # LLM を叩く入口だけ潰す。run_reviewers 本体は実物のまま
        graph.dispatch_reviewers = (
            lambda task, output, output_type="auto":
            reviewers._with_always_on(["fact_checker"]))
        reviewers.REVIEWER_REGISTRY["fact_checker"] = lambda **kw: {
            "reviewer": "fact_checker", "verdict": "NEEDS_REVISION",
            "issues": ["出典と食い違う"], "instruction": "削除する", "raw": ""}

        drafts = {"i": 0}

        def draft(*a, **k):
            # 出典に無い数値を必ず混ぜ、実物の numeric_checker が
            # 毎回 NEEDS_REVISION を返す最悪ケースを作る
            drafts["i"] += 1
            return f"DONE: 営業利益率は{40 + drafts['i']}.5%でした。"

        gr._ask = draft

    def tearDown(self):
        (self.graph.numeric_checker, self.graph.dispatch_reviewers,
         self.graph.run_reviewers, gr._ask, registry) = self._orig
        self.reviewers.REVIEWER_REGISTRY.clear()
        self.reviewers.REVIEWER_REGISTRY.update(registry)

    def _run(self, corrections, critiques, max_steps=18):
        budgets = graphs._enforce_budget_invariant({
            "max_corrections": corrections, "max_critiques": critiques,
            "reserve_compose_for_critic": 1})
        state = make_initial_state("決算", max_steps=max_steps, **budgets)
        state["history"] = [{"role": "result", "content": "売上高は22.3兆ウォンだった。"}]

        corrects = 0
        node = "compose"
        for _ in range(80):
            if node == "compose":
                state = gr.compose_step(state)
                node = gr.route_after_compose(state)
            elif node == "correct":
                state = gr.correct_sm_step(state)
                corrects += 1
                node = gr.route_after_correct(state)
            elif node == "critic":
                state = gr.critic_sm_step(state)
                node = gr.route_after_critic(state)
            else:
                break
            state["step_count"] = state.get("step_count", 0) + 1
            if node in (gr.END, "__end__"):
                break
        return state, budgets, corrects

    def test_総当たりで枠が足りる(self):
        for c in range(0, 4):
            for k in range(0, 4):
                with self.subTest(corrections=c, critiques=k):
                    st, b, _ = self._run(c, k)
                    self.assertLessEqual(
                        st["compose_count"], b["max_composes"],
                        "compose の実消費が式の枠を超えた")
                    self.assertLessEqual(st["correction_count"], c)
                    self.assertLessEqual(st["critique_count"], k)

    def test_実消費は式の値と一致する(self):
        for c, k in ((0, 0), (1, 1), (2, 2)):
            with self.subTest(corrections=c, critiques=k):
                st, b, _ = self._run(c, k)
                self.assertEqual(st["compose_count"], graphs.required_composes(c, k))
                self.assertEqual(b["max_composes"], st["compose_count"])

    def test_numeric_checkerはcorrect1回につき1回だけ(self):
        # これが今回の本丸。修正前はここが corrects の2倍近くになる
        # （correct が直接呼び、直後の critic が ALWAYS_ON 経由で再実行）。
        for c in range(0, 4):
            for k in range(0, 4):
                with self.subTest(corrections=c, critiques=k):
                    self.numeric_calls.clear()
                    _, _, corrects = self._run(c, k)
                    self.assertEqual(
                        len(self.numeric_calls), corrects,
                        "numeric_checker が correct 以外からも呼ばれている")

    def test_同じ指摘が注記に二重に載らない(self):
        # 訂正も差し戻しもできない状況を作る。修正前は correct が積んだ
        # 指摘と、critic が積む「（レビュー未反映）」が並んでいた。
        #
        # 文字列の一致では見つからない。correct は機械修正**前**の本文を、
        # critic は修正**後**の本文（「42.5%（出典未確認）」）を見るので、
        # 指摘に載る該当箇所の抜粋が変わるためである。同じ値について
        # 「出典に見当たらない」が2回出ていないか、値で見る。
        import re

        st, _, _ = self._run(1, 1, max_steps=6)
        notes = st.get("verification_notes", [])
        self.assertTrue(notes, "差し戻せなかった指摘が消えている")

        values = []
        for n in notes:
            if "見当たりません" not in n:
                continue
            m = re.search(r"「([^」]+)」", n)
            if m:
                values.append(m.group(1))
        self.assertEqual(len(values), len(set(values)),
                         f"同じ値の「出典に見当たらない」指摘が重複している: {values}")


class TestNumericCheckedOnce(unittest.TestCase):
    """
    critic への入口が correct からの1本だけであること（構造の見張り）。

    numeric_checker を critic から外せたのは「critic に来る前に必ず
    correct を通る」ことが前提。将来 react → critic のような辺が
    足されると、数値照合を一度も通らない回答が確定してしまう。
    """

    def test_criticへの辺はcorrectからだけ(self):
        import graph
        for status in ("running", "needs_revision", "done", "error"):
            self.assertNotEqual(
                graph.route_after_react({
                    "status": status, "last_action_type": "", "last_tool_name": ""}),
                "critic", f"react から critic へ直行する辺ができている（status={status}）")
        self.assertNotEqual(
            graph.route_after_verify_tool({"status": "running"}), "critic")
        self.assertEqual(
            graph.route_after_correct({"status": "needs_revision"}), "critic")

    def test_リサーチSMでも同じ(self):
        self.assertNotEqual(gr.route_after_compose({"status": "needs_revision"}), "critic")
        self.assertEqual(gr.route_after_correct({"status": "needs_revision"}), "critic")

    def test_結果が載っていなければcriticが保険で照合する(self):
        # 構造が変わって correct を通らなくなった場合でも、黙って
        # 素通りさせない（fail-safe）。
        import graph
        import reviewers
        orig = (graph.numeric_checker, graph.dispatch_reviewers,
                dict(reviewers.REVIEWER_REGISTRY))
        calls = []
        real = graph.numeric_checker

        def counting(**kw):
            calls.append(kw.get("output", ""))
            return real(**kw)

        graph.numeric_checker = counting
        graph.dispatch_reviewers = lambda *a, **k: ["fact_checker"]
        reviewers.REVIEWER_REGISTRY["fact_checker"] = lambda **kw: {
            "reviewer": "fact_checker", "verdict": "OK", "issues": [],
            "instruction": "", "raw": ""}
        try:
            state = make_initial_state("決算", **graphs.KIND_BUDGETS[graphs.RESEARCH])
            state["history"] = [{"role": "assistant",
                                 "content": "DONE: 営業利益率は41.2%でした。"}]
            state.pop("numeric_result")          # correct を通らなかった状態
            out = graph.critic_step(state)
            self.assertEqual(len(calls), 1, "保険の照合が働いていない")
            self.assertTrue(out.get("verification_notes")
                            or out["status"] == "running")
        finally:
            (graph.numeric_checker, graph.dispatch_reviewers, registry) = orig
            reviewers.REVIEWER_REGISTRY.clear()
            reviewers.REVIEWER_REGISTRY.update(registry)
