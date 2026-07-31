# tests/test_parsing.py — DONE本文の抽出・書き戻し（parsing.py）
#
# 1-2 の回帰。以前は行頭限定の厳密版と `split("DONE:")` の素朴版が
# 3系統7箇所に散っていて、**数値検証は壊れた文字列に対して走り、
# 利用者には正しい本文が返る**という状態になっていた。

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stubs import install_llm_stubs  # noqa: E402

install_llm_stubs()

import executor  # noqa: E402
import graph  # noqa: E402
import reviewers  # noqa: E402
from parsing import (  # noqa: E402
    extract_done, extract_previous_done, find_done_entry, parse_done,
    replace_done_body,
)
from state import make_initial_state  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _load_cases():
    with open(os.path.join(FIXTURES, "done_format_mention.json"), encoding="utf-8") as f:
        return json.load(f)["cases"]


class TestDoneFormatMention(unittest.TestCase):
    """実測ケース: モデルが DONE の書式そのものに言及する"""

    def test_厳密版は本文を正しく取り出す(self):
        for case in _load_cases():
            with self.subTest(case["name"]):
                self.assertEqual(parse_done(case["content"]), case["expected_body"])

    def test_素朴版は壊れる_これが直した理由(self):
        # 「なぜ split を捨てたか」を残す。ここが失敗するようになったら
        # fixture の想定が古い（＝素朴版でも通る文になっている）。
        for case in _load_cases():
            if case["naive_body"] is None:
                continue
            with self.subTest(case["name"]):
                naive = case["content"].split("DONE:")[1].strip()
                self.assertEqual(naive, case["naive_body"])
                self.assertNotEqual(naive, case["expected_body"])

    def test_履歴経由でも同じ結果になる(self):
        for case in _load_cases():
            with self.subTest(case["name"]):
                history = [{"role": "assistant", "content": case["content"]}]
                self.assertEqual(extract_done(history), case["expected_body"])
                self.assertEqual(
                    executor._extract_done_text(history), case["expected_body"])
                self.assertEqual(graph._extract_done(history), case["expected_body"])


class TestFindDoneEntry(unittest.TestCase):
    def test_見つからなければマイナス1(self):
        self.assertEqual(find_done_entry([]), (-1, ""))
        self.assertEqual(
            find_done_entry([{"role": "result", "content": "DONE: これは result"}]),
            (-1, ""))

    def test_assistant以外は見ない(self):
        history = [
            {"role": "assistant", "content": "DONE: 本物の回答"},
            {"role": "result", "content": "DONE: これは差し戻しメッセージ"},
        ]
        self.assertEqual(find_done_entry(history), (0, "本物の回答"))

    def test_本文が取れないエントリは飛ばして前を見る(self):
        history = [
            {"role": "assistant", "content": "DONE: 一次案"},
            {"role": "assistant", "content": "THOUGHT: まだ調べます"},
        ]
        self.assertEqual(find_done_entry(history), (0, "一次案"))


class TestExtractPreviousDone(unittest.TestCase):
    def test_1つ前の版を返す(self):
        history = [
            {"role": "assistant", "content": "DONE: 売上高は10兆ウォン"},
            {"role": "assistant", "content": "DONE: 売上高は12兆ウォン"},
        ]
        self.assertEqual(extract_previous_done(history), "売上高は10兆ウォン")

    def test_書式に言及しただけのメッセージは版として数えない(self):
        # 素朴版は "DONE:" を含むだけで1版と数えていたため、
        # ここで「1つ前」が1つずれていた
        history = [
            {"role": "assistant", "content": "DONE: 売上高は10兆ウォン"},
            {"role": "assistant", "content": "THOUGHT: 「DONE: 」形式で書きます"},
            {"role": "assistant", "content": "DONE: 売上高は12兆ウォン"},
        ]
        self.assertEqual(extract_previous_done(history), "売上高は10兆ウォン")


class TestReplaceDoneBody(unittest.TestCase):
    """書き戻し。読み側だけ直すと、ここが最終回答を空にする"""

    def test_前置きを残して本文だけ差し替える(self):
        content = "THOUGHT: まとめた\nDONE: 古い本文"
        out = replace_done_body(content, "新しい本文")
        self.assertEqual(out, "THOUGHT: まとめた\nDONE: 新しい本文")

    def test_書式に言及した文があっても本文を拾い直せる(self):
        # 旧実装は split("DONE:")[0] を head にしていたため、
        # 差し替え後の DONE: が行頭に来ず、以後どのパーサも本文を拾えなかった
        content = "THOUGHT: 最終回答を「DONE: 」形式で再構成します\nDONE: 古い本文"
        out = replace_done_body(content, "新しい本文")
        self.assertEqual(parse_done(out), "新しい本文")

        legacy = f'{content.split("DONE:")[0]}DONE: 新しい本文'
        self.assertEqual(parse_done(legacy), "", "旧実装は本文を失う（この差が修正点）")

    def test_ACTION_DONE形式でも書き戻せる(self):
        content = "THOUGHT: まとめた\nACTION: DONE\n古い本文"
        out = replace_done_body(content, "新しい本文")
        self.assertEqual(parse_done(out), "新しい本文")

    def test_DONEが無ければ付けて返す(self):
        self.assertEqual(replace_done_body("", "本文"), "DONE: 本文")

    def test_何度書き戻しても壊れない(self):
        content = "THOUGHT: 最終回答を「DONE: 」形式で再構成します\nDONE: v1"
        for i in range(2, 6):
            content = replace_done_body(content, f"v{i}")
            self.assertEqual(parse_done(content), f"v{i}")


class TestCheckedTextMatchesDelivered(unittest.TestCase):
    """
    correct_step が検証した文字列と、利用者に返る文字列が同一であること。

    ここが食い違うと、検証は通ったのに実際の回答は未検証、という
    最悪の組み合わせになる（しかもログ上は「問題なし」に見える）。
    """

    def setUp(self):
        self._orig = graph.numeric_checker
        self.checked = []

        def spy(**kw):
            self.checked.append(kw.get("output", ""))
            return self._orig(**kw)

        graph.numeric_checker = spy

    def tearDown(self):
        graph.numeric_checker = self._orig

    def _state(self, content):
        s = make_initial_state("SKハイニックスの決算を調べて", max_steps=10)
        s["history"] = [
            {"role": "result", "origin": "source",
             "content": "SKハイニックスの2025年第2四半期の売上高は12兆5000億ウォン。"},
            {"role": "assistant", "content": content},
        ]
        s["findings"] = [{
            "raw": "12兆5000億ウォン", "value": 12.5, "unit": "兆ウォン",
            "label": "売上高", "context": "2025年第2四半期の売上高は12兆5000億ウォン",
            "value_type": "actual",
        }]
        s["step_count"] = 4
        return s

    def test_検証した本文と返る本文が一致する(self):
        for case in _load_cases():
            with self.subTest(case["name"]):
                self.checked.clear()
                state = self._state(case["content"])
                out = graph.correct_step(state)
                self.assertEqual(len(self.checked), 1)
                self.assertEqual(self.checked[0], case["expected_body"])
                self.assertEqual(
                    executor._extract_done_text(out["history"]),
                    case["expected_body"])

    def test_機械修正を書き戻しても本文が消えない(self):
        # 出典に無い数値を混ぜて mechanical_fixes を発火させ、
        # 訂正予算を切らして書き戻し経路に入れる
        content = ("THOUGHT: 最終回答を「DONE: 」形式で再構成します\n"
                   "DONE: 売上高は12兆5000億ウォン、営業利益率は41.2%でした。")
        state = self._state(content)
        state["correction_count"] = 2
        state["max_corrections"] = 2
        out = graph.correct_step(state)

        body = executor._extract_done_text(out["history"])
        self.assertTrue(body, "書き戻しで最終回答が空になった")
        self.assertIn("12兆5000億ウォン", body)
        self.assertIn("出典未確認", body, "機械修正が本文に反映されていない")


class TestNoNaiveSplitLeftBehind(unittest.TestCase):
    """
    `split("DONE:")` が復活していないことを構造的に見張る。

    同じ規則を各所にコピーして片方だけ直す、というのが元の事故なので、
    grep 相当の検査をテストとして残す。
    """

    TARGETS = ["graph.py", "graph_research.py", "executor.py", "run.py",
               "reviewers.py", "parsing.py"]

    def test_素朴なsplitが残っていない(self):
        # 文字列の grep だと、経緯を説明したコメント・docstring まで拾って
        # しまう。構文木で「実際に呼んでいる箇所」だけを見る。
        import ast

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for name in self.TARGETS:
            path = os.path.join(root, name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (isinstance(fn, ast.Attribute) and fn.attr == "split"):
                    continue
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and arg.value == "DONE:":
                        offenders.append(f"{name}:{node.lineno}")
        self.assertEqual(offenders, [], "DONE本文の抽出は parsing.py に集約する")


class TestNumericCheckerNotAlwaysOn(unittest.TestCase):
    """critic 側の常時レビュアーから numeric_checker が外れていること"""

    def test_ALWAYS_ONに入っていない(self):
        self.assertNotIn("numeric_checker", reviewers.ALWAYS_ON_REVIEWERS)

    def test_明示指定なら今も呼べる(self):
        # 枠から外しただけで、レジストリからは消していない
        self.assertIn("numeric_checker", reviewers.REVIEWER_REGISTRY)
        results = reviewers.run_reviewers(
            ["numeric_checker"], output="売上高は10兆ウォンでした。",
            history=[], sources=[], findings=[])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["reviewer"], "numeric_checker")


if __name__ == "__main__":
    unittest.main()
