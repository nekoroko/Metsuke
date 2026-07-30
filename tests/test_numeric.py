# tests/test_numeric.py — 数値の抽出・照合の回帰テスト
#
# LLMを呼ばないので決定的に回せる。
#   python3 -m unittest discover -s tests -v
#
# フィクスチャは実際の実行（exec ID ecc0bed2, 2026-07-29）から作っている。
# 単位の取り違え（83兆ウォン → 9兆ウォン）と、履歴トリミングで窓の外に落ちた
# -7.47% を、それぞれ検出できることを固定する。

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numeric  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "skhynix_20260729.json")


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


class TestExtraction(unittest.TestCase):

    def test_複合表記を1件として抽出する(self):
        got = [n["raw"] for n in numeric.extract_numbers(
            "第1四半期の売上高52兆5763億ウォン、営業利益37兆6103億ウォン")]
        self.assertEqual(got, ["52兆5763億ウォン", "37兆6103億ウォン"])

    def test_符号付きの割合を抽出する(self):
        got = numeric.extract_numbers("終値 07/27 143.020 -11.550 -7.47%")
        self.assertEqual([n["raw"] for n in got], ["-7.47%"])
        self.assertAlmostEqual(got[0]["value"], -7.47)

    def test_全角パーセントを正規化する(self):
        got = numeric.extract_numbers("営業利益が約650％増加")
        self.assertEqual(got[0]["unit"], "%")

    def test_単位のない数値は拾わない(self):
        # 日付・株価・IDと区別できないため、意図的に対象外にしている
        self.assertEqual(numeric.extract_numbers("2026年7月29日 前日終値1,919,000"), [])

    def test_日付は日付として抽出する(self):
        got = [d["raw"] for d in numeric.extract_dates(
            "29日に第2四半期決算の発表を控える")]
        self.assertIn("29日", got)


class TestConversionPairs(unittest.TestCase):

    def test_妥当な換算は通る(self):
        pairs = numeric.find_conversion_pairs("売上高約83兆ウォン（約9兆円）")
        self.assertEqual(len(pairs), 1)
        self.assertTrue(numeric.conversion_plausible(pairs[0]))

    def test_単位取り違えを検出する(self):
        # ウォンの位置に円の値が入ったケース。比率が1.0になり桁として成立しない
        pairs = numeric.find_conversion_pairs("売上高約9兆ウォン（約9兆円）")
        self.assertEqual(len(pairs), 1)
        self.assertFalse(numeric.conversion_plausible(pairs[0]))

    def test_同一単位で値が違えば検出する(self):
        pairs = numeric.find_conversion_pairs("100億円（約200億円）")
        self.assertFalse(numeric.conversion_plausible(pairs[0]))

    def test_未知の通貨ペアは判定しない(self):
        pairs = numeric.find_conversion_pairs("100ドル（約100ウォン）")
        self.assertTrue(numeric.conversion_plausible(pairs[0]))


class TestMatching(unittest.TestCase):

    def setUp(self):
        self.hist = numeric.extract_numbers(
            "コンセンサスは売上高約84兆1693億ウォン、営業利益64兆ウォン")

    def test_丸め表記は同一とみなす(self):
        entry = numeric.extract_numbers("約84兆ウォン")[0]
        self.assertTrue(numeric.matches_any(entry, self.hist))

    def test_桁が違えば別物と判定する(self):
        entry = numeric.extract_numbers("9兆ウォン")[0]
        self.assertFalse(numeric.matches_any(entry, self.hist))

    def test_単位が違えば別物と判定する(self):
        entry = numeric.extract_numbers("64兆円")[0]
        self.assertFalse(numeric.matches_any(entry, self.hist))


class TestLedger(unittest.TestCase):

    def test_台帳が窓の外に落ちた数値を保持する(self):
        """
        欠陥B: -7.47% は DONE 時点で履歴トリミングにより見えなくなっていた。
        台帳はトリミングされないので、全ステップぶんが残る。
        """
        fx = load_fixture()
        ledger = []
        for i, e in enumerate(fx["history"]):
            if e["role"] != "result":
                continue
            ledger = numeric.merge_findings(
                ledger, numeric.collect_from_text(e["content"], source=f"step{i}", step=i))
        raws = [f["raw"] for f in ledger]
        for expected in fx["expected"]["ledger_must_contain"]:
            self.assertIn(expected, raws, f"台帳に {expected} が無い")

    def test_重複は除かれ上限が効く(self):
        items = numeric.collect_from_text("100億円と100億円")
        merged = numeric.merge_findings([], items + items)
        self.assertEqual(len([m for m in merged if m["raw"] == "100億円"]), 1)
        many = [{"kind": "number", "raw": f"{i}億円", "context": f"c{i}"} for i in range(60)]
        self.assertEqual(len(numeric.merge_findings([], many, limit=40)), 40)


class TestNumericCheckerRegression(unittest.TestCase):
    """実際の実行データに対する回帰テスト"""

    def setUp(self):
        # reviewers は config 経由で langchain を読むため、ここで軽く回避する
        import types
        if "langchain_openai" not in sys.modules:
            m = types.ModuleType("langchain_openai")
            m.ChatOpenAI = object
            sys.modules["langchain_openai"] = m
        from reviewers import numeric_checker
        self.checker = numeric_checker
        self.fx = load_fixture()

    def test_単位取り違えを差し戻す(self):
        r = self.checker(output=self.fx["answer"], history=self.fx["history"])
        self.assertEqual(r["verdict"], self.fx["expected"]["verdict"])
        joined = " ".join(r["issues"])
        for token in self.fx["expected"]["must_flag"]:
            self.assertIn(token, joined, f"{token} が指摘されていない")

    def test_出典にある数値は指摘しない(self):
        r = self.checker(output=self.fx["answer"], history=self.fx["history"])
        joined = " ".join(r["issues"])
        for token in self.fx["expected"]["must_not_flag"]:
            self.assertNotIn(
                f"「{token}」は検索結果", joined,
                f"{token} は履歴に存在するのに未照合として指摘されている")

    def test_問題がなければOKを返す(self):
        clean = ("売上高は83兆ウォン [実績]（約9兆円）、"
                 "営業利益は64兆ウォン [実績]（約7兆円）でした。")
        r = self.checker(output=clean, history=self.fx["history"])
        self.assertEqual(r["verdict"], "OK", f"誤検出: {r['issues']}")



class TestCorrectNode(unittest.TestCase):
    """correct ノード（訂正チェック）のふるまい"""

    def setUp(self):
        import types
        for name, attrs in (("langchain_openai", {"ChatOpenAI": object}),):
            if name not in sys.modules:
                m = types.ModuleType(name)
                for k, v in attrs.items():
                    setattr(m, k, v)
                sys.modules[name] = m
        if "langgraph.graph" not in sys.modules:
            lg = types.ModuleType("langgraph")
            lgg = types.ModuleType("langgraph.graph")

            class _SG:
                def __init__(self, *a, **k): pass
                def add_node(self, *a, **k): pass
                def set_entry_point(self, *a, **k): pass
                def add_conditional_edges(self, *a, **k): pass
                def compile(self, *a, **k): return object()

            lgg.StateGraph = _SG
            lgg.END = "END"
            sys.modules["langgraph"] = lg
            sys.modules["langgraph.graph"] = lgg

        import graph
        from state import make_initial_state
        self.graph = graph
        self.fx = load_fixture()
        self.state = make_initial_state("SKハイニックスの最新の株価動向と業績")
        self.state["history"] = list(self.fx["history"]) + [
            {"role": "assistant", "content": "THOUGHT: まとめる\nDONE: " + self.fx["answer"]}
        ]
        self.state["step_count"] = 6

    def test_誤りがあれば差し戻す(self):
        out = self.graph.correct_step(self.state)
        self.assertEqual(out["status"], "running", "react へ差し戻されていない")
        self.assertEqual(out["correction_count"], 1)
        feedback = out["history"][-1]["content"]
        self.assertIn("自動訂正チェック", feedback)
        self.assertIn("9兆ウォン", feedback)

    def test_誤りがなければcriticへ進む(self):
        clean = ("売上高は83兆ウォン [実績]（約9兆円）、"
                 "営業利益は64兆ウォン [実績]（約7兆円）でした。")
        self.state["history"][-1] = {"role": "assistant", "content": "DONE: " + clean}
        out = self.graph.correct_step(self.state)
        self.assertEqual(out["status"], "needs_revision")
        self.assertEqual(self.graph.route_after_correct(out), "critic")

    def test_差し戻しに置き換え候補を添える(self):
        from numeric import collect_from_text, merge_findings
        for entry in self.state["history"]:
            if entry["role"] == "result":
                self.state["findings"] = merge_findings(
                    self.state["findings"],
                    collect_from_text(entry["content"], source="web_search(x)", step=1),
                )
        out = self.graph.correct_step(self.state)
        feedback = out["history"][-1]["content"]
        self.assertIn("【参考リスト】", feedback)
        self.assertIn("これは候補ではありません", feedback)

    def test_差し戻し上限で素通しする(self):
        self.state["correction_count"] = 2
        out = self.graph.correct_step(self.state)
        self.assertEqual(out["status"], "needs_revision")
        self.assertEqual(self.graph.route_after_correct(out), "critic")

    def test_ステップ予算がなければ素通しする(self):
        self.state["step_count"] = self.state["max_steps"] - 1
        out = self.graph.correct_step(self.state)
        self.assertEqual(out["status"], "needs_revision")

    def test_DONEはcorrectへ流れる(self):
        s = dict(self.state, status="needs_revision")
        self.assertEqual(self.graph.route_after_react(s), "correct")


class TestFetchUrl(unittest.TestCase):
    """HTMLからのテキスト抽出"""

    def setUp(self):
        import tools
        self.tools = tools

    def test_scriptとstyleを除去する(self):
        html = ('<html><head><style>.a{color:red}</style></head><body>'
                '<script>var x="9兆ウォン";</script>'
                '<p>売上高約83兆ウォン（約9兆円）。</p></body></html>')
        text = self.tools.html_to_text(html)
        self.assertNotIn("var x", text)
        self.assertNotIn("color:red", text)
        self.assertIn("83兆ウォン", text)

    def test_抽出後に数値照合ができる(self):
        html = "<p>コンセンサスは売上高約83兆ウォン（約9兆円）。</p>"
        text = self.tools.html_to_text(html)
        pairs = numeric.find_conversion_pairs(text)
        self.assertEqual(len(pairs), 1)
        self.assertTrue(numeric.conversion_plausible(pairs[0]))


class TestPendingEvents(unittest.TestCase):
    """予定日の到来をコード側で検知する"""

    def setUp(self):
        import datetime
        self.today = datetime.date(2026, 7, 29)

    def test_当日の予定を警告する(self):
        f = [{"kind": "date", "raw": "29日",
              "context": "SKハイニックスが29日に第2四半期決算の発表を控える中"}]
        w = numeric.pending_event_warnings(f, self.today)
        self.assertEqual(len(w), 1)
        self.assertIn("本日", w[0])

    def test_未来の予定は警告しない(self):
        f = [{"kind": "date", "raw": "2026年8月15日", "context": "8月15日に発表予定"}]
        self.assertEqual(numeric.pending_event_warnings(f, self.today), [])

    def test_予定を示す語がなければ警告しない(self):
        f = [{"kind": "date", "raw": "27日", "context": "27日の終値は143.020"}]
        self.assertEqual(numeric.pending_event_warnings(f, self.today), [])

    def test_数値エントリは対象外(self):
        f = [{"kind": "number", "raw": "-7.47%", "context": "発表"}]
        self.assertEqual(numeric.pending_event_warnings(f, self.today), [])


class TestParseAction(unittest.TestCase):
    """
    DONE のパース。実行 36dee1d5 で、書式そのものに言及した文
    （「最終回答を『DONE: 』形式で再構成します」）にヒットし、
    本文が「」形式で再構成します…」から始まる壊れた回答になっていた。
    """

    def setUp(self):
        import types
        if "langchain_openai" not in sys.modules:
            m = types.ModuleType("langchain_openai")
            m.ChatOpenAI = object
            sys.modules["langchain_openai"] = m
        if "langgraph.graph" not in sys.modules:
            lg = types.ModuleType("langgraph")
            lgg = types.ModuleType("langgraph.graph")

            class _SG:
                def __init__(self, *a, **k): pass
                def add_node(self, *a, **k): pass
                def set_entry_point(self, *a, **k): pass
                def add_conditional_edges(self, *a, **k): pass
                def compile(self, *a, **k): return object()

            lgg.StateGraph = _SG
            lgg.END = "END"
            sys.modules["langgraph"] = lg
            sys.modules["langgraph.graph"] = lgg
        import graph
        self.parse = graph.parse_action

    def test_行頭のDONEを本文とする(self):
        r = self.parse("THOUGHT: まとめる\nDONE: 最終結果です。")
        self.assertEqual(r["type"], "done")
        self.assertEqual(r["content"], "最終結果です。")

    def test_書式への言及を本文と誤認しない(self):
        text = ("THOUGHT: 最終回答を「DONE: 」形式で再構成します。\n"
                "ACTION: DONE\n"
                "THOUGHT: SKハイニックスの調査結果を報告します。")
        r = self.parse(text)
        self.assertEqual(r["type"], "done")
        self.assertFalse(r["content"].startswith("」形式"),
                         f"書式への言及を拾っている: {r['content'][:30]}")
        self.assertTrue(r["content"].startswith("SKハイニックス"))

    def test_ACTION_DONE形式を受け付ける(self):
        r = self.parse("THOUGHT: まとめる\nACTION: DONE\nTHOUGHT: 結果です。")
        self.assertEqual(r["type"], "done")
        self.assertEqual(r["content"], "結果です。")

    def test_ツール呼び出しは従来どおり(self):
        r = self.parse("THOUGHT: 調べる\nACTION: web_search(SKハイニックス 決算)")
        self.assertEqual(r["type"], "tool")
        self.assertEqual(r["name"], "web_search")
        self.assertEqual(r["arg"], "SKハイニックス 決算")

    def test_形式不明はunknownのまま(self):
        r = self.parse("なにも書式に従っていない文章です。")
        self.assertEqual(r["type"], "unknown")


class TestUnusedFindings(unittest.TestCase):
    """取得済みなのに使われていない数値の検出"""

    def setUp(self):
        self.findings = [
            {"kind": "number", "raw": "-7.47%", "context": "終値 07/27"},
            {"kind": "number", "raw": "83兆ウォン", "context": "コンセンサス"},
            {"kind": "date", "raw": "29日", "context": "発表"},
        ]

    def test_情報なしの記述を検知する(self):
        self.assertTrue(numeric.claims_absence("値動きは確認できませんでした。"))
        self.assertFalse(numeric.claims_absence("値動きは-7.47%でした。"))

    def test_未使用の数値を返す(self):
        ans = "売上高は83兆ウォンの見込みです。値動きは見つかりませんでした。"
        got = [f["raw"] for f in numeric.unused_numbers(ans, self.findings)]
        self.assertEqual(got, ["-7.47%"])

    def test_丸め表記でも使用済みとみなす(self):
        ans = "売上高は約83兆ウォンです。株価は-7.47%でした。"
        self.assertEqual(numeric.unused_numbers(ans, self.findings), [])

    def test_日付は対象外(self):
        ans = "情報が見つかりませんでした。-7.47%、83兆ウォン。"
        got = [f["raw"] for f in numeric.unused_numbers(ans, self.findings)]
        self.assertNotIn("29日", got)


class TestExecutorHelpers(unittest.TestCase):
    """executor の最終回答の取り出しと時点表示"""

    def setUp(self):
        import types
        for name in ("langchain_openai",):
            if name not in sys.modules:
                m = types.ModuleType(name)
                m.ChatOpenAI = object
                sys.modules[name] = m
        import executor
        self.executor = executor

    def test_書式への言及を本文と誤認しない(self):
        history = [{"role": "assistant", "content": (
            "THOUGHT: 最終回答を「DONE: 」形式で再構成します。\n"
            "ACTION: DONE\n"
            "THOUGHT: SKハイニックスの調査結果です。")}]
        got = self.executor._extract_done_text(history)
        self.assertTrue(got.startswith("SKハイニックス"), got[:30])

    def test_行頭のDONEを優先する(self):
        history = [{"role": "assistant", "content": "THOUGHT: x\nDONE: 本文です。"}]
        self.assertEqual(self.executor._extract_done_text(history), "本文です。")

    def test_時点表示を先頭に付ける(self):
        out = self.executor._stamp_result("本文")
        self.assertIn("時点で取得した情報に基づきます", out.splitlines()[0])
        self.assertTrue(out.rstrip().endswith("本文"))

    def test_空文字には何も付けない(self):
        self.assertEqual(self.executor._stamp_result(""), "")


def _stub_llm_modules():
    import types
    if "langchain_openai" not in sys.modules:
        m = types.ModuleType("langchain_openai")
        m.ChatOpenAI = object
        sys.modules["langchain_openai"] = m
    if "langgraph.graph" not in sys.modules:
        lg = types.ModuleType("langgraph")
        lgg = types.ModuleType("langgraph.graph")

        class _SG:
            def __init__(self, *a, **k): pass
            def add_node(self, *a, **k): pass
            def set_entry_point(self, *a, **k): pass
            def add_conditional_edges(self, *a, **k): pass
            def compile(self, *a, **k): return object()

        lgg.StateGraph = _SG
        lgg.END = "END"
        sys.modules["langgraph"] = lg
        sys.modules["langgraph.graph"] = lgg


class TestReasoningSalvage(unittest.TestCase):
    """
    本文が空で、回答が思考側に入りきってしまったケースの回収。

    実測（gemma-4-e4b / コンテキスト8192）で、プロンプト7160トークンに対し
    出力枠が1032しか残らず、その1029がreasoningに消費されて content が空に
    なった。reasoning_content には完成した DONE が入っていた。
    """

    def setUp(self):
        _stub_llm_modules()
        import config
        self.config = config

    def _resp(self, reasoning):
        return type("R", (), {
            "content": "",
            "additional_kwargs": {"reasoning_content": reasoning},
        })()

    def test_ACTION_DONEから回収する(self):
        got = self.config.salvage_from_reasoning(
            self._resp("検討中...\nACTION: DONE\nDONE: 本文です。"))
        self.assertTrue(got.startswith("ACTION: DONE"))

    def test_回収した内容がDONEとしてパースできる(self):
        import graph
        got = self.config.salvage_from_reasoning(
            self._resp("検討中...\nACTION: DONE\nDONE: レポート本文。"))
        act = graph.parse_action(got)
        self.assertEqual(act["type"], "done")
        self.assertEqual(act["content"], "レポート本文。")

    def test_指示が無ければ回収しない(self):
        # 単なる思考の断片を本文として採用してしまわないこと
        got = self.config.salvage_from_reasoning(
            self._resp("うーん、どう書こうか考えている。"))
        self.assertEqual(got, "")

    def test_思考が空なら回収しない(self):
        self.assertEqual(self.config.salvage_from_reasoning(self._resp("")), "")


class TestPromptBudget(unittest.TestCase):
    """プロンプトが出力の枠を食い潰さないこと"""

    def setUp(self):
        _stub_llm_modules()
        import graph
        self.graph = graph

    def test_局面ごとに不要な節を落とす(self):
        early = self.graph.build_system_prompt(0, 10, False, None)
        late = self.graph.build_system_prompt(8, 10, False, None)

        self.assertIn("検索クエリの組み立て方", early)
        self.assertNotIn("数値には出所と種別", early)

        self.assertIn("数値には出所と種別", late)
        self.assertNotIn("検索クエリの組み立て方", late)

    def test_常設の節はどちらにもある(self):
        for step in (0, 8):
            p = self.graph.build_system_prompt(step, 10, False, None)
            self.assertIn("## 回答形式", p)
            self.assertIn("数値の書き写し", p)

    def test_どの局面でも上限内に収まる(self):
        # コンテキスト8192の環境で、履歴と回答の枠を残せる範囲に抑える
        for step in (0, 5, 8, 9):
            p = self.graph.build_system_prompt(step, 10, True, None)
            self.assertLess(len(p), 4000, f"step={step} でプロンプトが大きすぎる: {len(p)}字")


class TestFindingsBudget(unittest.TestCase):
    """台帳がプロンプトを圧迫しないこと"""

    def test_文字数の上限が効く(self):
        many = [{"kind": "number", "raw": f"{i}兆ウォン",
                 "context": "x" * 80, "source": "web_search(長いクエリ)"}
                for i in range(40)]
        out = numeric.format_findings(many)
        self.assertLessEqual(len(out), numeric.FINDINGS_CHAR_BUDGET + 200)

    def test_新しいものを優先して残す(self):
        many = [{"kind": "number", "raw": f"{i}兆ウォン",
                 "context": "x" * 80, "source": "q"} for i in range(40)]
        kept = [l.split("（")[0].replace("- ", "")
                for l in numeric.format_findings(many).splitlines()]
        self.assertIn("39兆ウォン", kept)
        self.assertNotIn("0兆ウォン", kept)

    def test_少数なら全部残る(self):
        few = [{"kind": "number", "raw": "83兆ウォン", "context": "コンセンサス", "source": "q"}]
        self.assertIn("83兆ウォン", numeric.format_findings(few))


class TestKoreanNumbers(unittest.TestCase):
    """
    韓国語表記の数値。エージェントは対象国の言語で検索することがあり、
    実測（exec f7cc0de3）では NAVER金融・韓国メディアから得た数値が
    まるごと抽出できておらず、株価の暴落(-14.65%)が台帳に載らなかった。
    """

    def test_퍼센트と마이너스を解釈する(self):
        got = numeric.extract_numbers("전일대비 하락 266,000 마이너스 14.65 퍼센트")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["unit"], "%")
        self.assertAlmostEqual(got[0]["value"], -14.65)

    def test_조억원の複合表記を解釈する(self):
        got = numeric.extract_numbers("매출을 78조9680억원, 영업이익을 61조350억원으로 전망")
        self.assertEqual([n["raw"] for n in got], ["78조9680억원", "61조350억원"])
        self.assertAlmostEqual(got[0]["value"], 78.968e12)
        self.assertAlmostEqual(got[1]["value"], 61.035e12)

    def test_원をウォンとして正規化する(self):
        self.assertEqual(numeric.extract_numbers("1조원")[0]["unit"], "ウォン")

    def test_言語をまたいで照合できる(self):
        kr = numeric.extract_numbers("영업이익을 61조350억원으로 전망")
        jp = numeric.extract_numbers("営業利益は61兆350億ウォンの予想")[0]
        self.assertTrue(numeric.matches_any(jp, kr))


class TestFinalAnswerAlwaysChecked(unittest.TestCase):
    """
    ステップ上限際で出力された最終回答も検証すること。

    以前は残りステップが1以下だと correct が検証自体をスキップしており、
    「最後に出力される＝実際に読まれる回答こそ検証されない」という
    逆の構造になっていた。実測（exec f7cc0de3）で、履歴に存在しない
    「売上構成比20%以上」が素通りしている。
    """

    def setUp(self):
        _stub_llm_modules()
        import graph, executor
        from state import make_initial_state
        from numeric import collect_from_text, merge_findings
        self.graph = graph
        self.executor = executor

        result = "SK하이닉스 -9.35% 하락. 매출 78조9680억원 전망."
        led = merge_findings([], collect_from_text(result, source="web_search(x)", step=1))
        self.state = make_initial_state("株価動向")
        self.state["step_count"] = 9          # 残り1。以前はここでスキップしていた
        self.state["findings"] = led
        self.state["history"] = [
            {"role": "result", "content": result},
            {"role": "assistant", "content":
                "DONE: 売上構成比の20%以上を占めます。直近は-9.35%の下落です。"},
        ]

    def test_予算切れでも検証してメモを残す(self):
        out = self.graph.correct_step(self.state)
        self.assertEqual(out["status"], "needs_revision", "差し戻してはいけない")
        notes = out.get("verification_notes", [])
        self.assertTrue(notes, "検証メモが残っていない")
        self.assertTrue(any("20%" in n for n in notes))

    def test_履歴にある数値は指摘しない(self):
        out = self.graph.correct_step(self.state)
        joined = " ".join(out.get("verification_notes", []))
        self.assertNotIn("「-9.35%」は検索結果", joined)

    def test_メモが最終回答に出る(self):
        out = self.graph.correct_step(self.state)
        final = self.executor._finalize_result(out)
        self.assertIn("自動検証で確認できなかった点", final)
        self.assertIn("20%", final)

    def test_問題がなければ注記は付かない(self):
        self.state["history"][-1] = {
            "role": "assistant", "content": "DONE: 直近は-9.35% [実績] の下落です。"}
        out = self.graph.correct_step(self.state)
        self.assertEqual(out.get("verification_notes", []), [])
        self.assertNotIn("自動検証で確認できなかった点",
                         self.executor._finalize_result(out))


class TestForecastVsActual(unittest.TestCase):
    """予想値を実績として書いていないかの機械チェック"""

    def setUp(self):
        _stub_llm_modules()
        from reviewers import numeric_checker
        import numeric
        self.checker = numeric_checker
        self.numeric = numeric
        self.findings = [
            {"kind": "number", "raw": "84.1兆ウォン",
             "context": "証券14社のコンセンサスでは84.1兆ウォン"},
            {"kind": "number", "raw": "22.3兆ウォン",
             "context": "第2四半期の売上高は22.3兆ウォンだった"},
        ]

    def test_予想の数値に実績タグを付けたら指摘する(self):
        r = self.checker(output="売上高は84.1兆ウォン [実績] でした。",
                         findings=self.findings)
        self.assertEqual(r["verdict"], "NEEDS_REVISION")
        self.assertIn("出典の文脈は予想", " ".join(r["issues"]))

    def test_実績の数値に実績タグは指摘しない(self):
        r = self.checker(output="売上高は22.3兆ウォン [実績] でした。",
                         findings=self.findings)
        self.assertNotIn("出典の文脈は予想", " ".join(r["issues"]))

    def test_予想タグなら指摘しない(self):
        r = self.checker(output="売上高は84.1兆ウォン [予想] の見込みです。",
                         findings=self.findings)
        self.assertNotIn("出典の文脈は予想", " ".join(r["issues"]))

    def test_同じ文脈に実績と予想が同居しても取り違えない(self):
        findings = [
            {"kind": "number", "raw": "-9.35%",
             "context": "SK하이닉스 -9.35% 하락. 매출 78조9680억원 전망."},
        ]
        r = self.checker(output="直近は-9.35% [実績] の下落です。", findings=findings)
        self.assertNotIn("出典の文脈は予想", " ".join(r["issues"]))


class TestTagRequirement(unittest.TestCase):
    """[実績]/[予想] の欠落は、予想値が混ざっているときだけ差し戻す"""

    def setUp(self):
        _stub_llm_modules()
        from reviewers import numeric_checker
        self.checker = numeric_checker

    def test_予想が混ざっていればタグ欠落を指摘する(self):
        history = [{"role": "result", "content": "コンセンサスでは84.1兆ウォンの見通し"}]
        r = self.checker(output="売上高は84.1兆ウォンでした。", history=history)
        self.assertIn("[実績] / [予想] が付いていません", " ".join(r["issues"]))

    def test_予想が無ければタグ欠落では差し戻さない(self):
        history = [{"role": "result", "content": "第2四半期の売上高は22.3兆ウォンだった"}]
        r = self.checker(output="売上高は22.3兆ウォンでした。", history=history)
        self.assertNotIn("[実績] / [予想] が付いていません", " ".join(r["issues"]))

    def test_一部にでもタグがあれば指摘しない(self):
        history = [{"role": "result", "content": "コンセンサスは84.1兆ウォンの見通し。売上は22.3兆ウォン"}]
        r = self.checker(output="84.1兆ウォン [予想]、22.3兆ウォン。", history=history)
        self.assertNotIn("[実績] / [予想] が付いていません", " ".join(r["issues"]))


class TestSignConflict(unittest.TestCase):
    """変動率と変動額の符号が食い違う記述を拾う"""

    def setUp(self):
        _stub_llm_modules()
        from reviewers import numeric_checker
        self.checker = numeric_checker

    def test_率と額の符号が逆なら指摘する(self):
        history = [{"role": "result", "content": "終値は+5.2%、前日比-1,200円"}]
        r = self.checker(output="終値は+5.2% [実績]（-1,200円）でした。", history=history)
        self.assertIn("符号が食い違っています", " ".join(r["issues"]))

    def test_同じ向きなら指摘しない(self):
        history = [{"role": "result", "content": "終値は-7.47%、前日比-11,550円"}]
        r = self.checker(output="終値は-7.47% [実績]（-11,550円）でした。", history=history)
        self.assertNotIn("符号が食い違っています", " ".join(r["issues"]))

    def test_率同士の符号違いは指摘しない(self):
        history = [{"role": "result", "content": "前日比-3.2%、年初来+12%"}]
        r = self.checker(output="前日比-3.2% [実績]、年初来+12% [実績] です。", history=history)
        self.assertNotIn("符号が食い違っています", " ".join(r["issues"]))


class TestNoRegressionOnRewrite(unittest.TestCase):
    """訂正で情報量が減ったら、それ自体を指摘する"""

    def setUp(self):
        _stub_llm_modules()
        from reviewers import numeric_checker
        self.checker = numeric_checker
        self.findings = [
            {"kind": "number", "raw": "22.3兆ウォン", "context": "売上高は22.3兆ウォン"},
            {"kind": "number", "raw": "-14.65%", "context": "28日終値は-14.65%"},
        ]

    def test_裏付けのある数値が消えたら指摘する(self):
        r = self.checker(
            output="売上高は22.3兆ウォン [実績] でした。",
            previous_output="売上高は22.3兆ウォン [実績]、株価は-14.65% [実績] でした。",
            findings=self.findings,
            history=[{"role": "result", "content": "売上高は22.3兆ウォン、28日終値は-14.65%"}],
        )
        self.assertIn("前回の回答にあった「-14.65%」が消えています", " ".join(r["issues"]))

    def test_裏付けのない数値が消えても指摘しない(self):
        r = self.checker(
            output="売上高は22.3兆ウォン [実績] でした。",
            previous_output="売上高は22.3兆ウォン [実績]、利益率は99.9% [実績] でした。",
            findings=self.findings,
            history=[{"role": "result", "content": "売上高は22.3兆ウォン、28日終値は-14.65%"}],
        )
        self.assertNotIn("99.9%", " ".join(i for i in r["issues"] if "消えています" in i))


class TestCriticNeverDropsIssues(unittest.TestCase):
    """差し戻す予算が無くても、レビューの指摘は最終回答に残す"""

    def setUp(self):
        _stub_llm_modules()
        import graph
        from state import make_initial_state
        self.graph = graph
        self.state = make_initial_state("株価動向")
        self.state["history"] = [
            {"role": "result", "content": "売上高は22.3兆ウォンだった"},
            {"role": "assistant", "content": "DONE: 売上高は22.3兆ウォン [実績] でした。"},
        ]
        self.state["step_count"] = 10      # 残りステップ0 → 差し戻せない
        self._orig_dispatch = graph.dispatch_reviewers
        self._orig_run = graph.run_reviewers
        graph.dispatch_reviewers = lambda *a, **k: ["generic_reviewer"]

    def tearDown(self):
        self.graph.dispatch_reviewers = self._orig_dispatch
        self.graph.run_reviewers = self._orig_run

    def _reviewers_say(self, verdict, issues):
        self.graph.run_reviewers = lambda *a, **k: [{
            "reviewer": "generic_reviewer", "verdict": verdict,
            "issues": issues, "instruction": "", "raw": "",
        }]

    def test_予算切れでも指摘を注記に残す(self):
        self._reviewers_say("NEEDS_REVISION", ["株価の時点が書かれていない"])
        out = self.graph.critic_step(self.state)
        self.assertEqual(out["status"], "done")
        notes = " ".join(out["verification_notes"])
        self.assertIn("株価の時点が書かれていない", notes)
        self.assertIn("レビュー未反映", notes)

    def test_予算切れでもOKなら注記は付かない(self):
        self._reviewers_say("OK", [])
        out = self.graph.critic_step(self.state)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out.get("verification_notes", []), [])


class TestReviewerConstraints(unittest.TestCase):
    """レビュアーが結論や物語構造を指示しないこと"""

    def setUp(self):
        _stub_llm_modules()
        import reviewers
        self.prompt = reviewers.FACT_CHECKER_PROMPT

    def test_結論の方向を指示させない(self):
        self.assertIn("結論の方向", self.prompt)
        self.assertIn("指示してはいけない", self.prompt)

    def test_物語を求めさせない(self):
        for word in ("ストーリー", "対比構造", "説得力"):
            self.assertIn(word, self.prompt)
        self.assertIn("報告書であって物語ではない", self.prompt)

    def test_根拠のない記述は削除を指示させる(self):
        self.assertIn("加筆ではなく削除", self.prompt)

    def test_欠落の在り処を示させる(self):
        self.assertIn("どこにあるかを示すこと", self.prompt)

    def test_削除だけでなく置き換えまで指示させる(self):
        self.assertIn("正しい値への置き換えまで指示すること", self.prompt)

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestLabelValueMatching(unittest.TestCase):
    """ラベルと数値の対応が出典と合っているか（意味照合）"""

    def setUp(self):
        _stub_llm_modules()
        from reviewers import numeric_checker
        self.checker = numeric_checker
        # 区切りの無い株価データ。ラベルは値の後ろに来る
        # 実物の形（区切りの消えた表）を使う。短く切り詰めたサンプルだと
        # 「表かどうか」の判定境界に載らず、検出経路が変わってしまう
        self.raw = _noisy("stock_table_concat")["raw"]
        self.findings = [{"kind": "number", "raw": "0.55%",
                          "context": self.raw, "value_type": "unknown"}]

    def test_別ラベルに付け替えたら指摘する(self):
        r = self.checker(output="配当利回りは0.55% [実績] です。",
                         findings=self.findings,
                         history=[{"role": "result", "content": self.raw}])
        joined = " ".join(r["issues"])
        self.assertIn("売買回転率", joined)
        self.assertIn("配当利回り", joined)

    def test_正しいラベルなら指摘しない(self):
        r = self.checker(output="売買回転率は0.55% [実績] です。",
                         findings=self.findings,
                         history=[{"role": "result", "content": self.raw}])
        self.assertNotIn("ラベルの対応", " ".join(r["issues"]))

    def test_同じラベルで値が違えば指摘する(self):
        findings = [{"kind": "number", "raw": "22.3兆ウォン",
                     "context": "売上高は22.3兆ウォンだった", "value_type": "actual"}]
        r = self.checker(output="売上高は9.2兆ウォン [実績] でした。", findings=findings,
                         history=[{"role": "result", "content": "売上高は22.3兆ウォンだった。営業利益は9.2兆ウォン"}])
        self.assertIn("出典と違います", " ".join(r["issues"]))

    def test_出典に無いラベルは言い換えとみなす(self):
        findings = [{"kind": "number", "raw": "-14.65%",
                     "context": "28日終値は-14.65%", "value_type": "actual"}]
        r = self.checker(output="株価は-14.65% [実績] でした。", findings=findings,
                         history=[{"role": "result", "content": "28日終値は-14.65%"}])
        self.assertNotIn("ラベルの対応", " ".join(r["issues"]))

    def test_文章の隣接ラベル違いは指摘しない(self):
        # 「28日の終値は前日比-14.65%」を「終値」と書くのは言い換えであって誤りではない
        findings = [{"kind": "number", "raw": "-14.65%",
                     "context": "28日の終値は前日比-14.65%", "value_type": "actual"}]
        r = self.checker(output="終値は-14.65% [実績] でした。", findings=findings,
                         history=[{"role": "result", "content": "28日の終値は前日比-14.65%"}])
        self.assertNotIn("ラベルの対応", " ".join(r["issues"]))

    def test_言語が違うラベルは食い違い扱いしない(self):
        findings = [{"kind": "number", "raw": "9.2兆ウォン",
                     "context": "영업이익은 9.2조원을 기록", "value_type": "actual"}]
        r = self.checker(output="営業利益は9.2兆ウォン [実績] でした。", findings=findings,
                         history=[{"role": "result", "content": "영업이익은 9.2조원을 기록"}])
        self.assertNotIn("ラベルの対応", " ".join(r["issues"]))


class TestMechanicalFixes(unittest.TestCase):
    """差し戻せないときに機械だけで直せる分を適用する"""

    def setUp(self):
        _stub_llm_modules()
        import numeric
        self.numeric = numeric
        self.findings = numeric.collect_from_text(
            "売上高は22.3兆ウォンだった。コンセンサスは84.1兆ウォンの見通し。",
            source="web_search(x)")
        self.history = [{"role": "result",
                         "content": "売上高は22.3兆ウォンだった。コンセンサスは84.1兆ウォンの見通し。"}]

    def test_種別タグを台帳に合わせて直す(self):
        text, applied = self.numeric.mechanical_fixes(
            "予想は84.1兆ウォン [実績] です。", self.findings, self.history)
        self.assertIn("84.1兆ウォン [予想]", text)
        self.assertTrue(applied)

    def test_未照合の数値に注記を付ける(self):
        text, applied = self.numeric.mechanical_fixes(
            "利益率は99.9%でした。", self.findings, self.history)
        self.assertIn("99.9%（出典未確認）", text)

    def test_正しい記述は書き換えない(self):
        original = "売上高は22.3兆ウォン [実績] でした。"
        text, applied = self.numeric.mechanical_fixes(original, self.findings, self.history)
        self.assertEqual(text, original)
        self.assertEqual(applied, [])

    def test_correctが予算切れで機械修正を適用する(self):
        import graph
        from state import make_initial_state
        state = make_initial_state("決算")
        state["findings"] = self.findings
        state["history"] = self.history + [
            {"role": "assistant", "content": "DONE: 予想は84.1兆ウォン [実績] です。"}]
        state["step_count"] = 10          # 差し戻せない
        out = graph.correct_step(state)
        done = [e for e in out["history"] if e["role"] == "assistant"][-1]["content"]
        self.assertIn("84.1兆ウォン [予想]", done)
        self.assertTrue(any("種別を" in n for n in out["verification_notes"]))


class TestConfirmedFields(unittest.TestCase):
    """検証済みの数値は次の書き直しで守る"""

    def setUp(self):
        _stub_llm_modules()
        import graph
        from state import make_initial_state
        from numeric import collect_from_text
        self.graph = graph
        self.findings = collect_from_text(
            "売上高は22.3兆ウォンだった。28日終値は-14.65%。", source="web_search(x)")
        self.state = make_initial_state("決算と株価")
        self.state["findings"] = self.findings
        self.state["history"] = [
            {"role": "result", "content": "売上高は22.3兆ウォンだった。28日終値は-14.65%。"},
            {"role": "assistant",
             "content": "DONE: 売上高は22.3兆ウォン [実績]、株価は-14.65% [実績]。"},
        ]
        self.state["step_count"] = 3

    def test_検証済みの数値が確定済みに積まれる(self):
        out = self.graph.correct_step(self.state)
        raws = [c["raw"] for c in out["confirmed"]]
        self.assertIn("22.3兆ウォン", raws)
        self.assertIn("-14.65%", raws)

    def test_確定済みが消えたら指摘する(self):
        out = self.graph.correct_step(self.state)
        nxt = {**self.state, "confirmed": out["confirmed"]}
        nxt["history"] = self.state["history"] + [
            {"role": "assistant", "content": "DONE: 売上高は22.3兆ウォン [実績] のみ。"}]
        out2 = self.graph.correct_step(nxt)
        feedback = out2["history"][-1]["content"]
        self.assertIn("確定済みの値「-14.65%」", feedback)

    def test_書き直しの差分がtraceに残る(self):
        # 1回目のDONEでは比較対象が無いので差分は空。2回目から出る
        self.state["history"].append(
            {"role": "assistant", "content": "DONE: 売上高は22.3兆ウォン [実績] のみ。"})
        out = self.graph.correct_step(self.state)
        note = out["trace"][-1]["note"]
        self.assertIn("削除: -14.65%", note)


class TestReactSearchGuards(unittest.TestCase):
    """ReActループの検索まわり（重複抑止・本文自動取得・見落とし防止）"""

    def setUp(self):
        _stub_llm_modules()
        import graph
        self.graph = graph
        self.result = (
            "タイトル: SKハイニックス、営業益が急増\n"
            "URL: https://example.com/a\n"
            "概要: 営業利益は9.2兆ウォンだった。\n"
            "---\n"
            "タイトル: 株価はRockets 14%\n"
            "URL: https://example.com/b\n"
            "概要: 28日終値は-14.65%。"
        )

    def test_重複クエリの判定は語順を無視する(self):
        self.assertTrue(self.graph.is_duplicate_query(
            "決算 SKハイニックス", ["SKハイニックス 決算"]))
        self.assertFalse(self.graph.is_duplicate_query(
            "SKハイニックス 決算 実績", ["SKハイニックス 決算"]))

    def test_未使用の切り口を返す(self):
        angles = self.graph.unused_angles("SKハイニックス 決算", ["SKハイニックス 決算 結果"])
        self.assertNotIn("結果", angles)
        self.assertIn("実績", angles)

    def test_見出しと数値の自動抽出(self):
        from numeric import collect_from_text
        summary = self.graph.summarize_hits(
            self.result, collect_from_text(self.result, source="web_search(x)"))
        self.assertIn("Rockets 14%", summary)
        self.assertIn("9.2兆ウォン", summary)

    def test_本文を自動取得して履歴に積む(self):
        import tools
        orig = tools.fetch_url
        tools.fetch_url = lambda url: f"{url} の本文。営業利益は9.2兆ウォンで過去最高。"
        try:
            blocks, attempted = self.graph.auto_fetch_sources(self.result, {"fetched_urls": []})
        finally:
            tools.fetch_url = orig
        self.assertEqual(len(blocks), 2)
        self.assertIn("https://example.com/a", attempted)

    def test_失敗したURLは再訪しない(self):
        import tools
        orig = tools.fetch_url
        tools.fetch_url = lambda url: "エラー: 403"
        try:
            blocks, attempted = self.graph.auto_fetch_sources(
                self.result, {"fetched_urls": ["https://example.com/a"]})
        finally:
            tools.fetch_url = orig
        self.assertNotIn("https://example.com/a", [b["url"] for b in blocks])

    def test_発表予定を過ぎていれば結果確認を促す(self):
        from numeric import collect_from_text
        findings = collect_from_text("7月29日に第2四半期決算の発表を控える",
                                     source="web_search(x)")
        nudge = self.graph._pending_result_nudge(
            {"findings": findings, "queries_done": ["SKハイニックス 決算"]})
        self.assertIn("結果", nudge)

    def test_結果を検索済みなら促さない(self):
        from numeric import collect_from_text
        findings = collect_from_text("7月29日に第2四半期決算の発表を控える",
                                     source="web_search(x)")
        nudge = self.graph._pending_result_nudge(
            {"findings": findings, "queries_done": ["SKハイニックス 決算 結果"]})
        self.assertEqual(nudge, "")


class TestVerifyBudgetNote(unittest.TestCase):
    """品質判定の予算切れ後は「未検証」であることを残す"""

    def setUp(self):
        _stub_llm_modules()
        import graph
        from state import make_initial_state
        self.graph = graph
        self.state = make_initial_state("株価")
        self.state["tool_verify_count"] = 6
        self.state["max_tool_verifies"] = 6

    def test_未検証である旨を注記に積む(self):
        out = self.graph.verify_tool_step(self.state)
        self.assertTrue(any("未検証" in n for n in out["verification_notes"]))

    def test_同じ注記を重複させない(self):
        out = self.graph.verify_tool_step(self.state)
        out2 = self.graph.verify_tool_step({**self.state, **out})
        self.assertEqual(len(out2["verification_notes"]), 1)


def _noisy(case_id):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "noisy_sources.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for c in data["cases"]:
        if c["id"] == case_id:
            return c
    raise KeyError(case_id)


class TestNoisyRealText(unittest.TestCase):
    """
    実行で問題を起こしたテキストの形（連結・複数出現・切断）で検証する。

    理想化したサンプルだけでは、この種の誤検出・見落としは再現しない。
    """

    def setUp(self):
        _stub_llm_modules()
        from reviewers import numeric_checker
        import numeric
        self.checker = numeric_checker
        self.numeric = numeric

    def _ledger(self, raw):
        return self.numeric.collect_from_text(raw, source="web_search(x)", step=1)

    def test_同じラベルと約が何度も出る記事で誤検出しない(self):
        case = _noisy("article_multiple_approx")
        led = self._ledger(case["raw"])
        hist = [{"role": "result", "content": case["raw"]}]
        for statement in case["true_statements"]:
            r = self.checker(output=statement, history=hist, findings=led)
            self.assertEqual(r["verdict"], "OK", f"{statement} → {r['issues']}")

    def test_連結された株価表で正しい組は通す(self):
        case = _noisy("stock_table_concat")
        led = self._ledger(case["raw"])
        hist = [{"role": "result", "content": case["raw"]}]
        for label, value in case["correct_pairs"]:
            r = self.checker(output=f"{label}は{value} [実績] です。",
                             history=hist, findings=led)
            self.assertNotIn("ラベルの対応", " ".join(r["issues"]),
                             f"{label}={value} が誤検出された: {r['issues']}")

    def test_連結された株価表で付け替えは捕まえる(self):
        case = _noisy("stock_table_concat")
        led = self._ledger(case["raw"])
        hist = [{"role": "result", "content": case["raw"]}]
        for label, value in case["wrong_pairs"]:
            r = self.checker(output=f"{label}は{value} [実績] です。",
                             history=hist, findings=led)
            self.assertIn("ラベルの対応", " ".join(r["issues"]),
                          f"{label}={value} の付け替えを見逃した")

    def test_韓国語ソースと日本語回答を食い違い扱いしない(self):
        case = _noisy("korean_mixed")
        led = self._ledger(case["raw"])
        hist = [{"role": "result", "content": case["raw"]}]
        for statement in case["true_statements"]:
            r = self.checker(output=statement, history=hist, findings=led)
            self.assertEqual(r["verdict"], "OK", f"{statement} → {r['issues']}")

    def test_途中切断は捏造と区別して伝える(self):
        case = _noisy("truncated_body")
        body = case["raw_head"] + self.numeric.TRUNCATION_MARK
        r = self.checker(output=f"為替は{case['invented_number']} [実績] でした。",
                         history=[{"role": "result", "content": body}])
        joined = " ".join(r["issues"])
        self.assertIn("見当たりません", joined)
        self.assertIn("途中で切れて", joined)

    def test_切断が無ければ切断の注記は出ない(self):
        r = self.checker(output="為替は1,380ウォン [実績] でした。",
                         history=[{"role": "result", "content": "15日のソウル外国為替市場。"}])
        self.assertNotIn("途中で切れて", " ".join(r["issues"]))


class TestCandidateFiltering(unittest.TestCase):
    """置換候補は単位・桁・ラベルで絞る"""

    def setUp(self):
        _stub_llm_modules()
        import numeric
        self.numeric = numeric
        self.led = numeric.collect_from_text(
            "株価は266,000ウォン。営業利益は9.2兆ウォン。売上高は22.3兆ウォン。"
            "セクター指数は0.55%。前日比は-14.65%。",
            source="web_search(x)")

    def test_桁が違う値は候補にしない(self):
        entry = self.numeric.extract_numbers("9.9兆ウォン")[0]
        cands = self.numeric.candidates_for(entry, self.led)
        self.assertNotIn("266,000ウォン", cands)

    def test_単位が違う値は候補にしない(self):
        entry = self.numeric.extract_numbers("9.9兆ウォン")[0]
        self.assertFalse([c for c in self.numeric.candidates_for(entry, self.led)
                          if "%" in c])

    def test_ラベルが一致する候補を先に出す(self):
        entry = self.numeric.extract_numbers("9.9兆ウォン")[0]
        cands = self.numeric.candidates_for(entry, self.led, label="営業利益")
        self.assertEqual(cands[0], "9.2兆ウォン")


class TestTabularBoundary(unittest.TestCase):
    """
    表形式の判定境界。自然文に隣接順序のロジックが及ばないことを確かめる。

    「助詞が無い」だけを条件にすると、見出し風の短い断片まで表と判定され、
    自然文での誤爆が再発する。
    """

    PROSE = [
        "営業利益は9.2兆ウォンだった",
        "SKハイニックスの第2四半期の営業利益は前年同期比で約650%増加した",
        "28日の終値は前日比-14.65%となり、時価総額は97兆ウォンに減少した",
        "第2四半期の売上高は52兆5,763億ウォンで、四半期として過去最高となった",
        "2026年7月29日発表9.2兆ウォン",
        "売上高22.3兆ウォン営業利益9.2兆ウォン",
        "ROE18.5%PER8.42PBR1.95",
        "前年同期比650%増",
        "純利益9.2兆ウォン",
        "株価-14.65%",
    ]

    def setUp(self):
        _stub_llm_modules()
        import numeric
        from reviewers import numeric_checker
        self.numeric = numeric
        self.checker = numeric_checker

    def test_自然文は表と判定しない(self):
        for text in self.PROSE:
            self.assertFalse(self.numeric.looks_tabular(text), f"表と誤判定: {text}")

    def test_実物の表は表と判定する(self):
        self.assertTrue(self.numeric.looks_tabular(_noisy("stock_table_concat")["raw"]))

    def test_空白が混ざっても表と判定する(self):
        # 「時価総額97,146,675,000千 KRW」のように空白が1つ入るのは実際にある
        raw = _noisy("stock_table_concat")["raw"]
        self.assertIn(" ", raw)

    def test_自然文で言い換えラベルを誤検出しない(self):
        for text in self.PROSE:
            led = self.numeric.collect_from_text(text, source="web_search(x)")
            nums = self.numeric.extract_numbers(text)
            if not nums:
                continue
            r = self.checker(output=f"当期の指標は{nums[0]['raw']} [実績] です。",
                             history=[{"role": "result", "content": text}], findings=led)
            self.assertNotIn("ラベルの対応", " ".join(r["issues"]),
                             f"自然文で付け替え扱いされた: {text}")


class TestSuggestionFormatting(unittest.TestCase):
    """置換候補と参考リストが、読んで見分けられる形式になっているか"""

    def setUp(self):
        _stub_llm_modules()
        from reviewers import numeric_checker
        self.checker = numeric_checker

    def test_同じ項目名の値だけを置換候補にする(self):
        import numeric
        led = numeric.collect_from_text("営業利益は9.2兆ウォン。売上高は22.3兆ウォン。",
                                        source="web_search(x)")
        r = self.checker(output="営業利益は9.9兆ウォン [実績] でした。",
                         history=[{"role": "result", "content": "営業利益は9.2兆ウォン。"}],
                         findings=led)
        joined = " ".join(r["issues"])
        self.assertIn("【置換候補】9.2兆ウォン", joined)
        self.assertNotIn("22.3兆ウォン", joined)

    def test_項目名が違う値は候補に混ぜない(self):
        # doc26: 営業利益の候補に「世界のメモリ市場規模1,500兆ウォン」が並んでいた
        import numeric
        led = numeric.collect_from_text(
            "世界のメモリ市場規模は1,500兆ウォンに拡大する見通し。", source="web_search(x)")
        r = self.checker(
            output="営業利益は9.9兆ウォン [実績] でした。",
            history=[{"role": "result", "content": "世界のメモリ市場規模は1,500兆ウォン。"}],
            findings=led)
        joined = " ".join(r["issues"])
        self.assertNotIn("1,500兆ウォン", joined)
        self.assertIn("【置換候補なし】", joined)

    def test_候補が無いときも明示する(self):
        r = self.checker(output="利益率は99.9% [実績] でした。",
                         history=[{"role": "result", "content": "本文に数値なし"}])
        self.assertIn("【置換候補なし】", " ".join(r["issues"]))

    def test_台帳ダンプは参考リストとして別枠で出る(self):
        import graph
        from state import make_initial_state
        from numeric import collect_from_text
        state = make_initial_state("決算")
        state["findings"] = collect_from_text(
            "営業利益は9.2兆ウォン。株価は266,000ウォン。指数は0.55%。", source="web_search(x)")
        state["history"] = [
            {"role": "result", "content": "営業利益は9.2兆ウォン。"},
            {"role": "assistant", "content": "DONE: 利益率は99.9% [実績] でした。"},
        ]
        state["step_count"] = 3
        out = graph.correct_step(state)
        feedback = out["history"][-1]["content"]
        self.assertIn("【参考リスト】", feedback)
        self.assertIn("これは候補ではありません", feedback)
        # 参考リストは候補より後ろに置く
        self.assertLess(feedback.index("【置換候補"), feedback.index("【参考リスト】"))


def _raw_fixture(case_id):
    """実物の生テキストを読む（noisy_sources.json の raw_file を辿る）。"""
    case = _noisy(case_id)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", case["raw_file"])
    with open(path, encoding="utf-8") as f:
        return case, f.read()


class TestRealDoc23Moomoo(unittest.TestCase):
    """doc23 の moomoo 連結データ（実物）"""

    def setUp(self):
        _stub_llm_modules()
        import numeric
        from reviewers import numeric_checker
        self.numeric = numeric
        self.checker = numeric_checker
        self.case, self.raw = _raw_fixture("doc23_moomoo_concat")
        self.led = numeric.collect_from_text(self.raw, source="fetch_url(moomoo)")
        self.hist = [{"role": "result", "content": self.raw}]

    def test_実物のデータ行は表と判定される(self):
        data_line = [l for l in self.raw.splitlines() if len(l) > 100][0]
        self.assertTrue(self.numeric.looks_tabular(data_line))

    def test_単位のある値だけが台帳に載る(self):
        # 高値・安値・出来高などは単位が無く、台帳には入らない（設計どおり）
        raws = [f["raw"] for f in self.led if f["kind"] == "number"]
        self.assertEqual(sorted(raws), ["0.55%", "6.84%"])

    def test_値の直後にラベルが来る並びである(self):
        got = {lv["raw"]: (lv["label_before"], lv["label_after"])
               for lv in self.numeric.labeled_values(self.raw)}
        self.assertEqual(got["6.84%"], ("配当利回", "振幅"))
        self.assertEqual(got["0.55%"], ("売買代金", "売買回転率"))

    def test_正しいラベルの記述は通る(self):
        for label, value in self.case["correct_pairs"]:
            r = self.checker(output=f"{label}は{value} [実績] です。",
                             history=self.hist, findings=self.led)
            self.assertNotIn("ラベルの対応", " ".join(r["issues"]),
                             f"{label}={value} が誤検出された: {r['issues']}")

    @unittest.expectedFailure
    def test_値の後ろにラベルが来る表での付け替えを検出する(self):
        """
        未対応。doc23 の実物は「値→ラベル」の並びだが、_adjacent_label は
        前後どちらの一致も受け入れるため、「配当利回6.84%振幅」の 6.84% を
        「配当利回り」と書いても通ってしまう。表の向きを判定する必要がある。
        """
        for label, value in self.case["wrong_pairs"]:
            r = self.checker(output=f"{label}は{value} [実績] です。",
                             history=self.hist, findings=self.led)
            self.assertIn("ラベルの対応", " ".join(r["issues"]),
                          f"{label}={value} の付け替えを見逃した")


class TestRealDoc25Biggo(unittest.TestCase):
    """doc25 の biggo 記事全文（実物）。「約」が6回出てくる"""

    def setUp(self):
        _stub_llm_modules()
        import numeric
        from reviewers import numeric_checker
        self.numeric = numeric
        self.checker = numeric_checker
        self.case, self.raw = _raw_fixture("doc25_biggo_article")
        self.led = numeric.collect_from_text(self.raw, source="fetch_url(biggo)")
        self.hist = [{"role": "result", "content": self.raw}]

    def test_約は複数回登場する(self):
        self.assertGreaterEqual(self.raw.count("約"), 6)

    def test_約はラベルとして採られない(self):
        labels = {lv["label_before"] for lv in self.numeric.labeled_values(self.raw)}
        self.assertNotIn("約", labels)
        for label in labels:
            self.assertFalse(label.endswith("約"), f"修飾語がラベルに残っている: {label}")

    def test_出典に書かれている記述は却下されない(self):
        for statement in self.case["true_statements"]:
            r = self.checker(output=statement, history=self.hist, findings=self.led)
            self.assertEqual(r["verdict"], "OK", f"{statement} → {r['issues']}")

    def test_出典に無い数値は却下される(self):
        r = self.checker(output="営業利益は99兆9,999億ウォン [実績] でした。",
                         history=self.hist, findings=self.led)
        self.assertIn("見当たりません", " ".join(r["issues"]))

    def test_換算の併記が矛盾していれば捕まえる(self):
        # 実物にある「83兆ウォン（約9兆円）」は妥当。桁をずらすと捕まる
        ok = self.checker(output="売上高は83兆ウォン（約9兆円） [予想] です。",
                          history=self.hist, findings=self.led)
        self.assertNotIn("換算が矛盾", " ".join(ok["issues"]))
        ng = self.checker(output="売上高は83兆ウォン（約83兆円） [予想] です。",
                          history=self.hist, findings=self.led)
        self.assertIn("換算が矛盾", " ".join(ng["issues"]))


class TestRealDoc27Truncated(unittest.TestCase):
    """doc27 の切断された fetch 結果（実物）"""

    def setUp(self):
        _stub_llm_modules()
        import numeric
        from reviewers import numeric_checker
        self.numeric = numeric
        self.checker = numeric_checker
        self.case, self.raw = _raw_fixture("doc27_biggo_truncated")

    def test_切れた先の数値は本文に無い(self):
        self.assertNotIn(self.case["invented_number"], self.raw)
        self.assertIn(self.case["still_present_number"], self.raw)

    def test_構造化フラグがあれば切断として伝える(self):
        led = self.numeric.collect_from_text(self.raw, source="fetch_url(biggo)")
        r = self.checker(
            output=f"前営業日比{self.case['invented_number']} [実績] 下落しました。",
            history=[{"role": "result", "content": self.raw}],
            findings=led,
            sources=[{"url": "https://finance.biggo.jp/news/2694607f",
                      "excerpt": self.raw[:700], "source_truncated": True}])
        joined = " ".join(r["issues"])
        self.assertIn("見当たりません", joined)
        self.assertIn("途中で切れて", joined)

    def test_フラグが無ければ切断とは言わない(self):
        led = self.numeric.collect_from_text(self.raw, source="fetch_url(biggo)")
        r = self.checker(
            output=f"前営業日比{self.case['invented_number']} [実績] 下落しました。",
            history=[{"role": "result", "content": self.raw}],
            findings=led,
            sources=[{"url": "x", "excerpt": self.raw[:700], "source_truncated": False}])
        self.assertNotIn("途中で切れて", " ".join(r["issues"]))


class TestTruncationFlagPropagation(unittest.TestCase):
    """
    元ページの切断は、テキストではなく構造化フィールドで運ぶ。

    本文中の印は抜粋（ReActは1200字、SMは700字）で必ず落ちるため、
    テキストに埋めたままでは検証側に届かない。
    """

    def setUp(self):
        _stub_llm_modules()
        import graph, numeric
        self.graph = graph
        self.numeric = numeric
        self.result = ("タイトル: 記事\nURL: https://example.com/a\n概要: 概要文。")

    def _fetch_returning(self, body):
        import tools
        orig = tools.fetch_url
        tools.fetch_url = lambda url: body
        self.addCleanup(lambda: setattr(tools, "fetch_url", orig))

    def test_修正前の問題_印は抜粋で落ちる(self):
        body = "あ" * 8000 + self.numeric.TRUNCATION_MARK
        self.assertNotIn(self.numeric.TRUNCATION_MARK, body[:1200])

    def test_ReAct経路でフラグが立つ(self):
        self._fetch_returning("あ" * 8000 + self.numeric.TRUNCATION_MARK)
        blocks, _ = self.graph.auto_fetch_sources(self.result, {"fetched_urls": []})
        self.assertTrue(blocks[0]["source_truncated"])

    def test_切れていなければフラグは立たない(self):
        self._fetch_returning("短い本文。営業利益は9.2兆ウォン。")
        blocks, _ = self.graph.auto_fetch_sources(self.result, {"fetched_urls": []})
        self.assertFalse(blocks[0]["source_truncated"])

    def test_フラグはsourcesに積まれ検証側まで届く(self):
        from reviewers import numeric_checker
        sources = [{"url": "https://example.com/a", "excerpt": "抜粋のみ",
                    "source_truncated": True}]
        r = numeric_checker(output="為替は1,380ウォン [実績] でした。",
                            history=[{"role": "result", "content": "抜粋のみ"}],
                            sources=sources)
        self.assertIn("途中で切れて", " ".join(r["issues"]))


class TestCorrectionCountSemantics(unittest.TestCase):
    """correction_count は差し戻した回数だけを数える"""

    def setUp(self):
        _stub_llm_modules()
        import graph
        from state import make_initial_state
        from numeric import collect_from_text
        self.graph = graph
        self.make = make_initial_state
        self.collect = collect_from_text

    def _state(self, done, **over):
        s = self.make("決算", **over)
        s["findings"] = self.collect("売上高は22.3兆ウォン。", source="web_search(x)")
        s["history"] = [
            {"role": "result", "content": "売上高は22.3兆ウォン。"},
            {"role": "assistant", "content": f"DONE: {done}"},
        ]
        s["step_count"] = 3
        return s

    def test_問題なしの通過は数えない(self):
        out = self.graph.correct_step(self._state("売上高は22.3兆ウォン [実績] でした。"))
        self.assertEqual(out["correction_count"], 0)

    def test_差し戻したときだけ数える(self):
        out = self.graph.correct_step(self._state("利益率は99.9% [実績] でした。"))
        self.assertEqual(out["status"], "running")
        self.assertEqual(out["correction_count"], 1)

    def test_差し戻せなかった場合も数えない(self):
        s = self._state("利益率は99.9% [実績] でした。")
        s["step_count"] = 10                      # 差し戻せない
        out = self.graph.correct_step(s)
        self.assertEqual(out["status"], "needs_revision")
        self.assertEqual(out["correction_count"], 0)

    def test_素通りで訂正の予算が減らない(self):
        # 「問題なし」で2回通過しても、その後に差し戻せる
        s = self._state("売上高は22.3兆ウォン [実績] でした。", max_corrections=2)
        for _ in range(2):
            out = self.graph.correct_step(s)
            s = {**s, **out}
        s["history"] = s["history"] + [
            {"role": "assistant", "content": "DONE: 利益率は99.9% [実績] でした。"}]
        out = self.graph.correct_step(s)
        self.assertEqual(out["status"], "running", "素通りで予算を使い切っている")


class TestOhlcTimeSeries(unittest.TestCase):
    """
    株価の時系列テーブル（単位も区切りも無い）専用の抽出。

    実測（2026-07-30 / qwen3.5-4b / ReAct）で、原文にある終値・高値・安値を
    「出典に見当たらない」と却下し、本文に（出典未確認）を付けた事故の回帰。
    """

    def setUp(self):
        _stub_llm_modules()
        import numeric
        from reviewers import numeric_checker
        self.numeric = numeric
        self.checker = numeric_checker
        self.case, self.raw = _raw_fixture("doc28_yahoo_timeseries")

    def test_ヘッダから欄の並びを読む(self):
        self.assertEqual(self.numeric.parse_ohlc_header(self.raw),
                         self.case["header_order"])

    def test_7日分のOHLCを復元する(self):
        rows = self.numeric.parse_ohlc_rows(self.raw)
        self.assertEqual(len(rows), len(self.case["expected_rows"]))
        for got, want in zip(rows, self.case["expected_rows"]):
            self.assertEqual(got["date"], want["date"])
            for field in ("始値", "高値", "安値", "終値"):
                self.assertEqual(got["fields"].get(field), want[field],
                                 f"{want['date']} の {field}")

    def test_ヘッダが無ければ欄名を付けない(self):
        # 推測で欄名を割り当てると、ラベルの付け替えと同じ事故になる
        no_header = "2026/7/28135.91136.49128.29130.17\n"
        rows = self.numeric.parse_ohlc_rows(no_header)
        self.assertEqual(rows[0]["fields"], {})
        self.assertEqual(rows[0]["values"][:4],
                         ["135.91", "136.49", "128.29", "130.17"])

    def test_実在する株価は却下されない(self):
        hist = [{"role": "result", "content": self.raw}]
        for value in self.case["recovered_by_ohlc"]:
            r = self.checker(output=f"終値は{value} [実績] でした。", history=hist)
            self.assertEqual(r["verdict"], "OK", f"{value} が却下された: {r['issues']}")

    def test_隣の日の終値では通さない(self):
        # 128.29 と 127.29 の差は0.78%。丸め用の許容幅（1%）を当てると通ってしまう
        hist = [{"role": "result", "content": self.raw}]
        r = self.checker(output="終値は127.29ドル [実績] でした。", history=hist)
        self.assertIn("見当たりません", " ".join(r["issues"]))

    def test_対象外として整理した値は変わらず却下される(self):
        # 「52週安値124.80」はラベル隣接の裸数値。案2の領域なので今回は未対応
        hist = [{"role": "result", "content": self.raw}]
        for value in self.case["out_of_scope"]["values"]:
            r = self.checker(output=f"安値は{value} [実績] でした。", history=hist)
            self.assertIn("見当たりません", " ".join(r["issues"]))

    def test_出典に無い株価は却下される(self):
        hist = [{"role": "result", "content": self.raw}]
        r = self.checker(output="終値は999.99ドル [実績] でした。", history=hist)
        self.assertIn("見当たりません", " ".join(r["issues"]))

    def test_時系列以外のページから価格を作らない(self):
        # 記事・気配値表・ページ装飾には、この解釈を広げない
        for case_id in ("doc25_biggo_article", "doc28_moomoo_quote", "doc23_moomoo_concat"):
            _, raw = _raw_fixture(case_id)
            self.assertEqual(self.numeric.parse_ohlc_rows(raw), [],
                             f"{case_id} から時系列行を誤検出した")

    def test_台帳には入れない(self):
        # 1ページで数十件になり、台帳の上限とプロンプト予算を食い潰すため
        led = self.numeric.collect_from_text(self.raw, source="fetch_url(yahoo)")
        self.assertEqual([f for f in led if f["kind"] == "number"], [])


class TestHeadlineValueType(unittest.TestCase):
    """
    実LLM検証で拾った実物の見出し・本文で、実績/予想の型付けを1件ずつ固定する。

    「予想を下回った」は予想の話ではなく実績の話なのに、単に「予想」という
    語が入っているだけで forecast になっていた（報告B）。期待値は
    noisy_sources.json の expectations 側に置き、テストはそれを読むだけに
    する。どの数値をどう判定すべきかは実装の都合ではなくデータの性質なので、
    データと一緒に置いておかないと後から動かされてしまう。

    unknown を期待している4件は「まだ決められないので unknown が正解」。
    実績と書けるだけの語がその文に無いものを、雰囲気で actual にはしない。
    """

    def setUp(self):
        _stub_llm_modules()
        import numeric
        self.numeric = numeric

    def _lines(self, case_id):
        case, raw = _raw_fixture(case_id)
        return case, raw.splitlines()

    def _check(self, case_id):
        case, lines = self._lines(case_id)
        wrong = []
        for exp in case["expectations"]:
            line = lines[exp["line"]]
            sentence = self.numeric.sentence_containing(line, exp["raw"])
            self.assertTrue(sentence,
                            f"{case_id} L{exp['line']} の {exp['raw']} を含む文が取れない")
            got = self.numeric.classify_value_type(sentence)
            if got != exp["value_type"]:
                wrong.append(f"L{exp['line']} {exp['raw']}: "
                             f"{exp['value_type']} を期待したが {got} "
                             f"（{exp['why']}）")
        self.assertEqual(wrong, [], "\n".join([""] + wrong))

    def test_実績を報じる文の型付け(self):
        self._check("doc28_headlines_actual")

    def test_予想を語る文の型付け(self):
        self._check("doc28_headlines_forecast")

    def test_期待値の内訳が実測どおりに固定されている(self):
        # 期待値そのものが後から緩められていないかを見る。
        # 「テストは通るが期待値が下がっていた」を防ぐための番人。
        case = _noisy("doc28_headlines_actual")
        counts = {}
        for exp in case["expectations"]:
            counts[exp["value_type"]] = counts.get(exp["value_type"], 0) + 1
        self.assertEqual(counts, {"actual": 9, "unknown": 3, "forecast": 1})

        fc = _noisy("doc28_headlines_forecast")
        self.assertEqual(len(fc["expectations"]), 12)
        self.assertTrue(all(e["value_type"] == "forecast" for e in fc["expectations"]))

    def test_期待値が実物の数値をもれなく覆っている(self):
        # 抽出できた数値だけを都合よく拾っていないか。
        for case_id in ("doc28_headlines_actual", "doc28_headlines_forecast"):
            case, lines = self._lines(case_id)
            listed = {(e["line"], e["raw"]) for e in case["expectations"]}
            found = set()
            for i, line in enumerate(lines):
                for x in self.numeric.extract_numbers(line):
                    found.add((i, x["raw"]))
            self.assertEqual(found, listed, f"{case_id} の期待値が実物と食い違う")


def _module_path(name):
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)


def _result_entry_literals(path):
    """
    ソースを構文木で読み、role="result" の辞書リテラルを全部返す。

    正規表現で「"role": "result"」の周辺を見る手もあるが、隣の
    エントリの origin を誤って拾う。辞書ごとに見る。
    """
    import ast
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {}
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant):
                keys[k.value] = v
        if keys.get("role") is None:
            continue
        role = keys["role"]
        if isinstance(role, ast.Constant) and role.value == "result":
            out.append((node.lineno, keys))
    return out


class TestHistoryOriginCoverage(unittest.TestCase):
    """
    履歴に role="result" を積む箇所が、全部 origin を書いているか（報告H）。

    出所の既定値は source（fail-open）にしてある。付け忘れても実在の数値が
    却下される事故にはならない代わり、付け忘れた箇所は黙って従来の挙動、
    つまり差し戻し文が出典に化けたままになる。静かに残るので、
    ここで構造として落とす。

    新しく role="result" を積む箇所を足すと、このテストが失敗する。
    """

    MODULES = ("graph.py", "graph_research.py")

    def test_全ての追加箇所がoriginを持つ(self):
        import ast
        import numeric
        allowed = {numeric.ORIGIN_SOURCE, numeric.ORIGIN_COMPUTED, numeric.ORIGIN_INTERNAL}
        missing = []
        for mod in self.MODULES:
            for lineno, keys in _result_entry_literals(_module_path(mod)):
                origin = keys.get("origin")
                if origin is None:
                    missing.append(f"{mod}:{lineno} に origin がない")
                    continue
                # 値は定数（Nameで間接参照していても定数に解決できること）
                if isinstance(origin, ast.Name):
                    value = getattr(numeric, origin.id, None)
                elif isinstance(origin, ast.Constant):
                    value = origin.value
                else:
                    value = None
                if value not in allowed:
                    missing.append(f"{mod}:{lineno} の origin が不明な値: {value!r}")
        self.assertEqual(missing, [], "\n".join([""] + missing))

    def test_洗い出した箇所数が変わっていない(self):
        # 内訳が変わったら、分類をやり直す必要がある
        counts = {mod: len(_result_entry_literals(_module_path(mod)))
                  for mod in self.MODULES}
        self.assertEqual(counts, {"graph.py": 13, "graph_research.py": 4})

    def test_検知テスト自体が機能する(self):
        # origin の無いエントリを混ぜたソースを作り、上のチェックが
        # 実際に拾うことを確かめる。番人が動くことを番人自身で見る。
        import ast
        import tempfile
        src = 'x = {"role": "result", "content": "しるしの無いエントリ"}\n'
        with tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8",
                                         delete=False) as f:
            f.write(src)
            path = f.name
        try:
            found = _result_entry_literals(path)
            self.assertEqual(len(found), 1)
            self.assertIsNone(found[0][1].get("origin"))
        finally:
            os.unlink(path)


class TestOriginFiltering(unittest.TestCase):
    """
    差し戻し文が出典として数え直されない（報告H の本体）。

    correct は自分の指摘文を role="result" で履歴に積む。指摘文には
    却下した数値がそのまま引用されているので、出所を見ないと
    「1回却下した値が2回目には出典にある」ことになる。
    """

    def setUp(self):
        _stub_llm_modules()
        import numeric
        from reviewers import numeric_checker
        self.numeric = numeric
        self.checker = numeric_checker

    def _history(self, source_text, feedback):
        n = self.numeric
        return [
            {"role": "result", "origin": n.ORIGIN_SOURCE, "content": source_text},
            {"role": "assistant", "content": "DONE: 売上高は777.77兆ウォン [実績] でした。"},
            {"role": "result", "origin": n.ORIGIN_INTERNAL, "content": feedback},
        ]

    def test_却下した数値は書き直しても却下される(self):
        _, src = _raw_fixture("doc25_biggo_article")
        feedback = ("（自動訂正チェック）「777.77兆ウォン」は検索結果・出典のどこにも"
                    "見当たりません。訂正した上で、再度DONEで最終回答を出してください。")
        hist = self._history(src, feedback)
        r = self.checker(output="売上高は777.77兆ウォン [実績] でした。", history=hist)
        self.assertIn("見当たりません", " ".join(r["issues"]))

    def test_許容幅の中でずらしても通らない(self):
        # 901.99 を却下したあとの 902.99（差0.11%）は MATCH_TOLERANCE の中に入る。
        # 指摘文を出典に数えていると、値を少し動かすだけで素通りしていた。
        _, src = _raw_fixture("doc25_biggo_article")
        feedback = "（自動訂正チェック）「901.99兆ウォン」は検索結果・出典のどこにも見当たりません。"
        hist = self._history(src, feedback)
        r = self.checker(output="売上高は902.99兆ウォン [実績] でした。", history=hist)
        self.assertIn("見当たりません", " ".join(r["issues"]))

    def test_印の無い履歴は従来どおり出典として扱う(self):
        # fail-open。付け忘れがあっても、実在の数値を却下する側には倒れない
        _, src = _raw_fixture("doc25_biggo_article")
        hist = [{"role": "result", "content": src}]
        r = self.checker(output="売上高は52兆5,763億ウォン [実績] でした。", history=hist)
        self.assertNotIn("見当たりません", " ".join(r["issues"]))

    def test_サンドボックスの出力は照合に使える(self):
        # computed は source と分けてあるが、当面は照合の母集団に入れる。
        # 計算した値を「出典に無い」と却下すると、報告A と同じ事故になる
        hist = [{"role": "result", "origin": self.numeric.ORIGIN_COMPUTED,
                 "content": "前年比: 650%"}]
        r = self.checker(output="成長率は650% [実績] でした。", history=hist)
        self.assertNotIn("見当たりません", " ".join(r["issues"]))

    def test_置換候補に却下済みの値を出さない(self):
        # _history_numbers は候補の母集団でもある。絞らないと、correct が
        # 却下したばかりの値を候補として提案し返す
        import reviewers
        feedback = "（自動訂正チェック）「777.77兆ウォン」は見当たりません。"
        hist = [{"role": "result", "origin": self.numeric.ORIGIN_INTERNAL,
                 "content": feedback}]
        self.assertEqual(reviewers._history_numbers(hist), [])


class TestOriginRegressionOnRealData(unittest.TestCase):
    """
    実データで、本物の数値が却下されないことを固定する（報告H の副作用対策）。

    報告A で起きたのは「正しい株価9件を出典に無いと却下した」事故だった。
    照合を厳しくする変更は、同じ形の事故を作りやすい。特に
    digest / summarize_hits が積む要約は role="result" なので、
    出所の分類を誤ると実在の数値が落ちる。
    """

    def setUp(self):
        _stub_llm_modules()
        import numeric
        from reviewers import numeric_checker
        self.numeric = numeric
        self.checker = numeric_checker
        n = numeric
        _, article = _raw_fixture("doc25_biggo_article")
        _, series = _raw_fixture("doc28_yahoo_timeseries")
        _, heads = _raw_fixture("doc28_headlines_actual")
        # ReAct が実際に積む並びをそのまま作る（graph.py の該当行と同じ出所）
        self.history = [
            {"role": "result", "origin": n.ORIGIN_SOURCE, "content": article},
            {"role": "result", "origin": n.ORIGIN_SOURCE,          # summarize_hits
             "content": "検索結果の要点: 第1四半期の営業利益は37兆6,103億ウォン。"},
            {"role": "result", "origin": n.ORIGIN_SOURCE,          # 自動取得した本文
             "content": f"[本文取得] https://y.example/\n{series}"},
            {"role": "result", "origin": n.ORIGIN_SOURCE,
             "content": f"[本文取得] https://n.example/\n{heads}"},
            {"role": "result", "origin": n.ORIGIN_INTERNAL,        # nudge
             "content": "（自動チェック）7月28日に決算発表の予定がありますが、まだ結果を調べていません。"},
            {"role": "result", "origin": n.ORIGIN_INTERNAL,        # correct の差し戻し
             "content": "（自動訂正チェック）「777.77兆ウォン」は見当たりません。"
                        "【置換候補】52兆5,763億ウォン。"},
            {"role": "result", "origin": n.ORIGIN_INTERNAL,        # critic の差し戻し
             "content": "## 指摘\n- 999.99ドルの根拠が不明です\n"},
        ]

    def _rejected(self, value):
        r = self.checker(output=f"{value} [実績] でした。", history=self.history)
        return any("見当たりません" in i for i in r["issues"])

    def test_本物の数値は通る(self):
        cases = [
            ("52兆5,763億ウォン", "検索結果の本文"),
            ("37兆6,103億ウォン", "summarize_hits の要約"),
            ("1,484.7ウォン", "検索結果の本文"),
            ("６０兆５０００億ウォン", "自動取得した見出し"),
            ("７９兆３０００億ウォン", "自動取得した見出し"),
            ("130.17ドル", "時系列表（OHLC経路）"),
            ("154.57ドル", "時系列表（OHLC経路）"),
        ]
        for value, where in cases:
            with self.subTest(value=value):
                self.assertFalse(self._rejected(value),
                                 f"{where} 由来の {value} を却下した")

    def test_捏造値は却下される(self):
        # どちらも差し戻し文の中にしか出てこない値
        for value in ("777.77兆ウォン", "999.99ドル"):
            with self.subTest(value=value):
                self.assertTrue(self._rejected(value), f"{value} が通ってしまう")

    def test_台帳は内部メッセージから作られない(self):
        # confirmed が減らない根拠。台帳への書き込みは出典由来だけ
        import inspect
        import graph
        import graph_research
        for mod in (graph, graph_research):
            src = inspect.getsource(mod)
            self.assertNotIn("collect_from_text(feedback", src)
            self.assertNotIn("collect_from_text(nudge", src)


class TestUrlNoise(unittest.TestCase):
    """
    URLの数値を拾わない（報告I）。

    検索結果は「URL: https://…」の行を含んだまま collect_from_text に渡る。
    パーセントエンコーディングの %XX が全部「XX%」として拾われるため、
    1本のURLで9件のゴミが台帳に入っていた。

    URLごとテキストを弾くのではなく、URLの範囲だけを同じ長さの空白に潰す。
    「URL: …\n概要: 営業利益は9.2兆ウォン」のように1つの文字列にURLと本文が
    同居しているので、まとめて弾くと本物の数値まで落ちる（報告A と同じ事故）。
    """

    def setUp(self):
        _stub_llm_modules()
        import numeric
        from reviewers import numeric_checker
        self.numeric = numeric
        self.checker = numeric_checker
        self.case, self.raw = _raw_fixture("doc29_search_with_urls")

    def _ledger(self, text=None):
        return self.numeric.collect_from_text(text if text is not None else self.raw,
                                              source="web_search(x)")

    def _numbers(self, led):
        return [f["raw"] for f in led if f["kind"] == "number"]

    def test_位置を保つために長さを変えない(self):
        masked = self.numeric.mask_urls(self.raw)
        self.assertEqual(len(masked), len(self.raw))
        self.assertNotIn("https://", masked)

    def test_URL由来の数値は台帳に載らない(self):
        got = self._numbers(self._ledger())
        for junk in self.case["url_noise"]["values_before_fix"]:
            self.assertNotIn(junk, got, f"URL由来の {junk} が台帳に入っている")

    def test_同じテキストの本物の数値は落ちない(self):
        # URLと本文が同居している。テキストごと弾く実装では全部消える
        got = self._numbers(self._ledger())
        for value in self.case["expected_numbers"]:
            self.assertIn(value, got, f"本物の {value} が落ちた")

    def test_呼び出し元4箇所ぶんの入力で台帳がきれいになる(self):
        # collect_from_text は graph.py 2箇所 / graph_research.py 2箇所から
        # 呼ばれる。入口で塞いでいるので、どの形の入力でも同じ結果になる
        forms = {
            "graph.py:605 web_search": self.raw,
            "graph.py:638 fetch本文": f"[本文取得] https://ex.example/a?q=%E6%B1%BA%E7%AE%97\n{self.raw}",
            "graph_research.py:455 検索": f"[検索] SK決算\n{self.raw}",
            "graph_research.py:529 抜粋": self.raw[:800],
        }
        for where, text in forms.items():
            with self.subTest(where=where):
                got = self._numbers(self._ledger(text))
                for junk in self.case["url_noise"]["values_before_fix"]:
                    self.assertNotIn(junk, got, f"{where} で {junk} が入った")

    def test_照合でURLのバイト列に一致しない(self):
        n = self.numeric
        hist = [{"role": "result", "origin": n.ORIGIN_SOURCE, "content": self.raw}]
        led = self._ledger()
        for junk in self.case["url_noise"]["must_not_pass"]:
            with self.subTest(value=junk):
                r = self.checker(output=f"利益率は{junk} [実績] でした。",
                                 history=hist, findings=led)
                self.assertIn("見当たりません", " ".join(r["issues"]),
                              f"{junk} がURLのバイト列と一致して通った")

    def test_照合で本物の数値は通る(self):
        n = self.numeric
        hist = [{"role": "result", "origin": n.ORIGIN_SOURCE, "content": self.raw}]
        led = self._ledger()
        for value in ("9.2兆ウォン", "557%", "130.17ドル"):
            with self.subTest(value=value):
                r = self.checker(output=f"値は{value} [実績] でした。",
                                 history=hist, findings=led)
                self.assertNotIn("見当たりません", " ".join(r["issues"]),
                                 f"本物の {value} を却下した")

    def test_置換候補にURL由来の値を出さない(self):
        import reviewers
        n = self.numeric
        hist = [{"role": "result", "origin": n.ORIGIN_SOURCE, "content": self.raw}]
        raws = [x["raw"] for x in reviewers._history_numbers(hist)]
        for junk in self.case["url_noise"]["values_before_fix"]:
            self.assertNotIn(junk, raws, f"候補に {junk} が並んでいる")

    def test_confirmedはこの修正だけで汚染されなくなる(self):
        # confirmed_from は findings しか見ないので、台帳が
        # きれいになれば confirmed 側の個別対応は要らない
        led = self._ledger()
        for junk in self.case["url_noise"]["must_not_pass"]:
            with self.subTest(value=junk):
                self.assertEqual(
                    self.numeric.confirmed_from(f"利益率は{junk} [実績] でした。", led), [],
                    f"URL由来の {junk} が confirmed に入る")
        # 本物は今までどおり確定済みになる
        conf = self.numeric.confirmed_from("営業利益は9.2兆ウォン [実績] でした。", led)
        self.assertEqual([c["raw"] for c in conf], ["9.2兆ウォン"])

    def test_台帳の枠をゴミが食わない(self):
        # merge_findings の上限は40件。URLのゴミが入ると本物が押し出される
        led = self._ledger()
        self.assertLessEqual(len(self._numbers(led)), 10,
                             "1ページの数値が多すぎる（URLのゴミが残っている）")
