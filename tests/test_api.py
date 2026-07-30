# tests/test_api.py — React Web UI 用の HTTP API
#
# DBは一時ファイルへ差し替える（test_llm_profiles.py と同じ手口）。
# LLM・サンドボックス・スケジューラは呼ばない。実際に叩くのは
# 「DBの読み書き」「形の変換」「秘密を出さないこと」の3つ。

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stubs import install_llm_stubs  # noqa: E402

install_llm_stubs()

import db  # noqa: E402
import settings_store  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402


class _ApiCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self._orig = (db.DB_PATH, settings_store.DB_PATH)
        db.DB_PATH = self.path
        settings_store.DB_PATH = self.path
        db.init_db()
        # startup イベント（init_db + scheduler.start）は走らせない。
        # 実DBとAPSchedulerを掴んでしまうため
        self.client = TestClient(app, raise_server_exceptions=True)

    def tearDown(self):
        db.DB_PATH, settings_store.DB_PATH = self._orig
        if os.path.exists(self.path):
            os.unlink(self.path)


class TestTools(_ApiCase):
    def test_作成から削除まで(self):
        r = self.client.post("/api/tools", json={
            "name": "為替取得", "description": "USD/JPY", "code": "print(1)"})
        self.assertEqual(r.status_code, 201)
        tool_id = r.json()["id"]

        self.assertEqual(len(self.client.get("/api/tools").json()), 1)

        r = self.client.patch(f"/api/tools/{tool_id}", json={"status": "verified"})
        self.assertEqual(r.json()["status"], "verified")

        self.assertEqual(
            self.client.delete(f"/api/tools/{tool_id}").status_code, 204)
        self.assertEqual(self.client.get("/api/tools").json(), [])

    def test_無いIDは404(self):
        self.assertEqual(self.client.get("/api/tools/nope").status_code, 404)
        self.assertEqual(
            self.client.patch("/api/tools/nope", json={"name": "x"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/tools/nope").status_code, 404)


class TestAgentTasks(_ApiCase):
    def test_allowed_tool_idsは配列で返る(self):
        r = self.client.post("/api/agent-tasks", json={
            "name": "決算調査", "task_prompt": "調べて",
            "allowed_tool_ids": ["a1", "b2"]})
        task_id = r.json()["id"]
        got = self.client.get(f"/api/agent-tasks/{task_id}").json()
        self.assertEqual(got["allowed_tool_ids"], ["a1", "b2"])
        # 一覧側も同じ形
        self.assertEqual(
            self.client.get("/api/agent-tasks").json()[0]["allowed_tool_ids"],
            ["a1", "b2"])

    def test_ツール未指定なら空配列(self):
        r = self.client.post("/api/agent-tasks", json={
            "name": "t", "task_prompt": "p"})
        got = self.client.get(f"/api/agent-tasks/{r.json()['id']}").json()
        self.assertEqual(got["allowed_tool_ids"], [])

    def test_グラフとモデルを保存できる(self):
        pid = db.get_llm_profiles()[0]["id"]
        r = self.client.post("/api/agent-tasks", json={
            "name": "t", "task_prompt": "p",
            "graph_kind": "research", "llm_profile_id": pid})
        got = self.client.get(f"/api/agent-tasks/{r.json()['id']}").json()
        self.assertEqual(got["graph_kind"], "research")
        self.assertEqual(got["llm_profile_id"], pid)

    def test_既定に戻すためnullを送れる(self):
        # exclude_unset なので「送ったキーだけ」更新される
        r = self.client.post("/api/agent-tasks", json={
            "name": "t", "task_prompt": "p", "graph_kind": "research"})
        tid = r.json()["id"]
        self.client.patch(f"/api/agent-tasks/{tid}", json={"graph_kind": None})
        self.assertIsNone(
            self.client.get(f"/api/agent-tasks/{tid}").json()["graph_kind"])


class TestExecutions(_ApiCase):
    def _exec_with(self, history=None, trace=None):
        tid = db.add_agent_task("t", "", "p")
        eid = db.add_execution("agent", tid, "t")
        db.set_execution_graph_kind(eid, "react")
        db.set_execution_llm_info(eid, {"name": "Qwen", "model": "qwen3.5-4b",
                                        "provider": "local"})
        if history is not None or trace is not None:
            db.update_execution_progress(eid, history or [], trace or [])
        return eid

    def test_JSON列はパース済みで返る(self):
        eid = self._exec_with(
            history=[{"role": "assistant", "content": "THOUGHT: 調べる"}],
            trace=[{"seq": 1, "node": "react", "next": "verify_tool"}])
        got = self.client.get(f"/api/executions/{eid}").json()
        self.assertIsInstance(got["history"], list)
        self.assertIsInstance(got["trace"], list)
        self.assertIsInstance(got["llm_info"], dict)
        self.assertEqual(got["llm_info"]["model"], "qwen3.5-4b")

    def test_表示用ラベルが付く(self):
        got = self.client.get(f"/api/executions/{self._exec_with()}").json()
        self.assertEqual(got["graph_label"], "ReActループ")
        self.assertIn("qwen3.5-4b", got["model_label"])

    def test_パース済みステップが付く(self):
        # フロントに THOUGHT/ACTION/DONE の正規表現を書かせないため
        eid = self._exec_with(history=[
            {"role": "assistant", "content": "THOUGHT: 調べる\nACTION: web_search(x)"},
            {"role": "result", "content": "1. …"},
        ])
        steps = self.client.get(f"/api/executions/{eid}").json()["steps"]
        self.assertEqual(steps[0]["thought"], "調べる")
        self.assertEqual(steps[0]["action"], "web_search(x)")
        self.assertEqual(steps[1]["role"], "result")

    def test_中身が無くても壊れない(self):
        got = self.client.get(f"/api/executions/{self._exec_with()}").json()
        self.assertEqual(got["history"], [])
        self.assertEqual(got["trace"], [])

    def test_無いIDは404(self):
        self.assertEqual(self.client.get("/api/executions/nope").status_code, 404)
        self.assertEqual(
            self.client.post("/api/executions/nope/cancel").status_code, 404)

    def test_終わった実行は停止できない(self):
        eid = self._exec_with()
        db.finish_execution(eid, "done", stdout="おわり")
        r = self.client.post(f"/api/executions/{eid}/cancel").json()
        self.assertFalse(r["cancelled"])
        self.assertEqual(r["status"], "done")

    def test_実行中は停止できる(self):
        eid = self._exec_with()
        r = self.client.post(f"/api/executions/{eid}/cancel").json()
        self.assertTrue(r["cancelled"])
        self.assertEqual(db.get_execution(eid)["status"], "error")


class TestSchedules(_ApiCase):
    def test_cronが不正なら400(self):
        r = self.client.post("/api/schedules", json={
            "exec_type": "agent", "target_id": "x", "cron_expr": "毎日"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("cron式が不正", r.json()["detail"])

    def test_登録すると対象名が付いて返る(self):
        tid = db.add_agent_task("決算調査", "", "p")
        r = self.client.post("/api/schedules", json={
            "exec_type": "agent", "target_id": tid, "cron_expr": "0 9 * * *"})
        self.assertEqual(r.status_code, 201)
        rows = self.client.get("/api/schedules").json()
        self.assertEqual(rows[0]["target_name"], "決算調査")

    def test_対象が消えていても一覧は壊れない(self):
        tid = db.add_agent_task("消えるタスク", "", "p")
        self.client.post("/api/schedules", json={
            "exec_type": "agent", "target_id": tid, "cron_expr": "0 9 * * *"})
        db.delete_agent_task(tid)
        rows = self.client.get("/api/schedules").json()
        self.assertEqual(rows, [])      # タスク削除でスケジュールも消える

    def test_有効無効を切り替えられる(self):
        tid = db.add_agent_task("t", "", "p")
        r = self.client.post("/api/schedules", json={
            "exec_type": "agent", "target_id": tid, "cron_expr": "0 9 * * *"})
        sid = r.json()["id"]
        self.assertFalse(
            self.client.patch(f"/api/schedules/{sid}",
                              json={"enabled": False}).json()["enabled"])
        self.assertEqual(
            self.client.delete(f"/api/schedules/{sid}").status_code, 204)


class TestLlmProfilesApi(_ApiCase):
    """**APIキーがGETレスポンスに出ないこと**が最重要。"""

    SECRET = "sk-must-never-be-returned-99999"

    def _make(self):
        return self.client.post("/api/llm-profiles", json={
            "name": "Claude", "provider": "api", "provider_kind": "anthropic",
            "model": "claude-sonnet-5", "api_key": self.SECRET,
            "max_output_tokens": "8000"}).json()

    def test_作成レスポンスに鍵が出ない(self):
        out = self._make()
        self.assertNotIn("api_key", out)
        self.assertNotIn(self.SECRET, json.dumps(out))
        self.assertTrue(out["has_api_key"])

    def test_一覧に鍵が出ない(self):
        self._make()
        body = self.client.get("/api/llm-profiles").text
        self.assertNotIn(self.SECRET, body)
        self.assertNotIn("api_key", body.replace("has_api_key", ""))

    def test_鍵はDBには入っている(self):
        # 出さないだけで、保存はされている
        out = self._make()
        self.assertEqual(db.get_llm_profile(out["id"])["api_key"], self.SECRET)

    def test_空の鍵で更新しても消えない(self):
        # 画面は鍵の実値を持っていない。空欄のまま保存されると消えてしまう
        out = self._make()
        self.client.patch(f"/api/llm-profiles/{out['id']}",
                          json={"name": "Claude 改", "api_key": ""})
        row = db.get_llm_profile(out["id"])
        self.assertEqual(row["api_key"], self.SECRET)
        self.assertEqual(row["name"], "Claude 改")

    def test_既定フラグが付く(self):
        self._make()
        rows = self.client.get("/api/llm-profiles").json()
        self.assertEqual(sum(1 for r in rows if r["is_default"]), 1)

    def test_既定を切り替えられる(self):
        out = self._make()
        r = self.client.put("/api/settings/default-profile",
                            json={"profile_id": out["id"]})
        self.assertEqual(r.status_code, 200)
        rows = self.client.get("/api/llm-profiles").json()
        self.assertTrue(next(x for x in rows if x["id"] == out["id"])["is_default"])

    def test_最後の1件の削除は409(self):
        only = self.client.get("/api/llm-profiles").json()[0]
        r = self.client.delete(f"/api/llm-profiles/{only['id']}")
        self.assertEqual(r.status_code, 409)

    def test_2件あれば削除できる(self):
        out = self._make()
        self.assertEqual(
            self.client.delete(f"/api/llm-profiles/{out['id']}").status_code, 204)

    def test_thinkingは真偽値でやり取りする(self):
        out = self.client.post("/api/llm-profiles", json={
            "name": "L", "provider": "local", "base_url": "http://x/v1",
            "model": "m", "disable_thinking": False}).json()
        self.assertIs(out["disable_thinking"], False)
        self.assertEqual(db.get_llm_profile(out["id"])["disable_thinking"], "false")


class TestSettingsApi(_ApiCase):
    def test_秘密はマスクして返る(self):
        db.set_settings({"tavily_api_key": "tvly-secret", "search_provider": "tavily"})
        got = self.client.get("/api/settings").json()
        self.assertEqual(got["tavily_api_key"], "********")
        self.assertEqual(got["search_provider"], "tavily")

    def test_空の秘密は空のまま返る(self):
        got = self.client.get("/api/settings").json()
        self.assertEqual(got["brave_api_key"], "")

    def test_マスク値を送り返しても上書きしない(self):
        db.set_settings({"tavily_api_key": "tvly-secret"})
        self.client.put("/api/settings", json={"values": {
            "tavily_api_key": "********", "search_provider": "tavily"}})
        self.assertEqual(db.get_setting("tavily_api_key"), "tvly-secret")
        self.assertEqual(db.get_setting("search_provider"), "tavily")

    def test_新しい値なら上書きする(self):
        self.client.put("/api/settings", json={"values": {
            "tavily_api_key": "tvly-new"}})
        self.assertEqual(db.get_setting("tavily_api_key"), "tvly-new")


class TestMeta(_ApiCase):
    def test_グラフ種別と既定モデルが返る(self):
        got = self.client.get("/api/meta").json()
        self.assertEqual([g["value"] for g in got["graph_kinds"]],
                         ["react", "research"])
        self.assertIn("default_graph_kind", got)
        # has_api_key は真偽値だけ。鍵そのもののフィールドは無い
        self.assertNotIn("api_key", got["default_profile"].keys())

    def test_既定モデルにも鍵が出ない(self):
        db.update_llm_profile(db.get_llm_profiles()[0]["id"],
                              api_key="sk-secret-meta")
        self.assertNotIn("sk-secret-meta", self.client.get("/api/meta").text)


if __name__ == "__main__":
    unittest.main()
