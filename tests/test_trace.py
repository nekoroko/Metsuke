# tests/test_trace.py — ノード遷移トレースのテスト
#
# LLMを呼ばない。graph.py / ui は langchain_openai・langgraph を import するため、
# 未導入の環境では tests/stubs.py の空モジュールで import だけ通す。

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stubs import install_llm_stubs  # noqa: E402

install_llm_stubs()

from graph import _with_trace  # noqa: E402
from trace_view import (  # noqa: E402
    counters_text, format_trace_lines, node_label, parse_trace,
    skipped_count, trace_rows,
)


SAMPLE = [
    {"seq": 1, "node": "react", "from": "(開始)", "summary": "web_search を呼び出し",
     "next": "verify_tool", "note": "", "skipped": False, "status": "running",
     "step_count": 1, "critique_count": 0, "correction_count": 0, "tool_verify_count": 0},
    {"seq": 2, "node": "verify_tool", "from": "react", "summary": "検算を省略",
     "next": "react", "note": "検算の予算切れ", "skipped": True, "status": "running",
     "step_count": 1, "critique_count": 0, "correction_count": 0, "tool_verify_count": 6},
]


class TestParseTrace(unittest.TestCase):
    def test_none_and_garbage(self):
        for raw in (None, "", "not json", "{}", "[1, 2]", 123):
            self.assertEqual(parse_trace(raw), [], repr(raw))

    def test_json_string(self):
        import json
        self.assertEqual(parse_trace(json.dumps(SAMPLE)), SAMPLE)

    def test_passthrough_list(self):
        self.assertEqual(parse_trace(SAMPLE), SAMPLE)

    def test_drops_non_dict_entries(self):
        self.assertEqual(parse_trace([SAMPLE[0], "x", None]), [SAMPLE[0]])


class TestFormatting(unittest.TestCase):
    def test_node_label_falls_back_to_raw_name(self):
        self.assertIn("react", node_label("react"))
        self.assertEqual(node_label("mystery"), "mystery")
        self.assertEqual(node_label(""), "?")

    def test_counters_text(self):
        self.assertEqual(counters_text(SAMPLE[1]), "step=1/verify=6/correct=0/critique=0")

    def test_rows_mark_skip(self):
        rows = trace_rows(SAMPLE)
        self.assertEqual(rows[0]["実行"], "実行")
        self.assertEqual(rows[1]["実行"], "スキップ")
        self.assertEqual(rows[0]["遷移元"], "(開始)")

    def test_rows_show_end_when_no_next(self):
        rows = trace_rows([{**SAMPLE[0], "next": ""}])
        self.assertEqual(rows[0]["次"], "(終了)")

    def test_lines_include_reason_only_when_present(self):
        lines = format_trace_lines(SAMPLE)
        text = "\n".join(lines)
        self.assertIn("#1 (開始) → react [実行] : web_search を呼び出し", text)
        self.assertIn("理由: 検算の予算切れ", text)
        self.assertEqual(text.count("理由:"), 1)

    def test_skipped_count(self):
        self.assertEqual(skipped_count(SAMPLE), 1)
        self.assertEqual(skipped_count([]), 0)


class TestExportText(unittest.TestCase):
    def test_trace_section_included(self):
        from ui.page_history import build_export_text
        exec_row = {"id": "abc", "target_name": "調査", "status": "done",
                    "started_at": "2026-07-29T10:00:00", "stdout": "本文"}
        text = build_export_text(exec_row, [{"role": "assistant", "content": "考えた"}], SAMPLE)
        self.assertIn("## ノード遷移（2件）", text)
        self.assertIn("[スキップ]", text)
        # ノード遷移は履歴より前に置く
        self.assertLess(text.index("## ノード遷移"), text.index("## ReActループ履歴"))

    def test_no_trace_no_section(self):
        from ui.page_history import build_export_text
        exec_row = {"id": "abc", "target_name": "調査", "status": "done",
                    "started_at": "2026-07-29T10:00:00", "stdout": "本文"}
        self.assertNotIn("ノード遷移", build_export_text(exec_row, None, []))


class TestPersistence(unittest.TestCase):
    """trace列の保存と、エラー終了時に消えないこと"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        os.environ["AGENT_STUDIO_DB"] = os.path.join(self.tmp, "t.db")
        for mod in ("paths", "db"):
            sys.modules.pop(mod, None)
        import db
        self.db = db
        db.init_db()

    def tearDown(self):
        import shutil
        os.environ.pop("AGENT_STUDIO_DB", None)
        for mod in ("paths", "db"):
            sys.modules.pop(mod, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _new_exec(self):
        return self.db.add_execution("agent", "t1", "調査", trigger="manual")

    def test_progress_then_finish_roundtrip(self):
        eid = self._new_exec()
        self.db.update_execution_progress(eid, [{"role": "assistant", "content": "x"}], SAMPLE)
        self.db.finish_execution(eid, "done", stdout="本文", history=[], trace=SAMPLE)
        row = self.db.get_execution(eid)
        self.assertEqual(parse_trace(row["trace"]), SAMPLE)

    def test_error_finish_keeps_progress_trace(self):
        eid = self._new_exec()
        self.db.update_execution_progress(eid, [{"role": "assistant", "content": "x"}], SAMPLE)
        # エラー終了は trace / history を渡さない
        self.db.finish_execution(eid, "error", stderr="落ちた")
        row = self.db.get_execution(eid)
        self.assertEqual(row["status"], "error")
        self.assertEqual(parse_trace(row["trace"]), SAMPLE)
        self.assertIsNotNone(row["history"])


class TestWithTrace(unittest.TestCase):
    def test_sequence_and_from_chain(self):
        state = {"trace": [], "status": "running", "step_count": 0}
        out1 = _with_trace(state, {"step_count": 1}, "react", "検索", next_node="verify_tool")
        state2 = {**state, **out1}
        out2 = _with_trace(state2, {}, "verify_tool", "検算OK", next_node="react")

        t = out2["trace"]
        self.assertEqual([e["seq"] for e in t], [1, 2])
        self.assertEqual(t[0]["from"], "(開始)")
        self.assertEqual(t[1]["from"], "react")

    def test_counters_taken_from_output_then_state(self):
        state = {"trace": [], "status": "running", "step_count": 3, "correction_count": 1}
        out = _with_trace(state, {"status": "needs_revision"}, "correct", "差し戻し",
                          next_node="react")
        e = out["trace"][0]
        self.assertEqual(e["status"], "needs_revision")   # 出力側が優先
        self.assertEqual(e["step_count"], 3)              # 出力に無ければ現状態
        self.assertEqual(e["correction_count"], 1)

    def test_skip_is_recorded(self):
        state = {"trace": [], "status": "running"}
        out = _with_trace(state, {}, "verify_tool", "検算を省略",
                          next_node="react", note="予算切れ", skipped=True)
        self.assertTrue(out["trace"][0]["skipped"])
        self.assertEqual(out["trace"][0]["note"], "予算切れ")

    def test_original_output_keys_preserved(self):
        out = _with_trace({"trace": []}, {"history": ["h"], "status": "done"},
                          "react", "完了")
        self.assertEqual(out["history"], ["h"])
        self.assertEqual(out["status"], "done")


if __name__ == "__main__":
    unittest.main()
