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
        self.assertEqual(out["compose_count"], 1)

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
