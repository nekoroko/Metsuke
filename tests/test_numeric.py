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
        self.assertIn("取得済みの数値", feedback)
        self.assertIn("置き換えに使うこと", feedback)

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
