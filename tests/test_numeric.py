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
        clean = "売上高は83兆ウォン（約9兆円）、営業利益は64兆ウォン（約7兆円）でした。"
        r = self.checker(output=clean, history=self.fx["history"])
        self.assertEqual(r["verdict"], "OK", f"誤検出: {r['issues']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
