# tests/test_llm_profiles.py — LLM接続プロファイル（複数保存 + 実行時選択）
#
# DBは一時ファイルに差し替える。db / settings_store は import 時に
# paths.DB_PATH をモジュール変数へ取り込むので、その変数を差し替えれば
# reload なしで隔離できる（reload すると他のテストが掴んでいる
# モジュールと別インスタンスになり、順序依存の壊れ方をする）。

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stubs import install_llm_stubs  # noqa: E402

install_llm_stubs()

import config  # noqa: E402
import db  # noqa: E402
import llm_profiles  # noqa: E402
import settings_store  # noqa: E402


class _TempDB(unittest.TestCase):
    """一時DBを使うテストの共通土台。"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)          # init_db に作らせる
        self._orig = (db.DB_PATH, settings_store.DB_PATH)
        db.DB_PATH = self.path
        settings_store.DB_PATH = self.path

    def tearDown(self):
        db.DB_PATH, settings_store.DB_PATH = self._orig
        if os.path.exists(self.path):
            os.unlink(self.path)


class TestMigration(_TempDB):
    """従来の単一設定を、プロファイル1件へ移行する"""

    def test_初回起動で既存設定が1件に移る(self):
        db.init_db()
        profiles = db.get_llm_profiles()
        self.assertEqual(len(profiles), 1)
        p = profiles[0]
        # defaults の値（local / LM Studio）がそのまま入っている
        self.assertEqual(p["provider"], "local")
        self.assertEqual(p["model"], "gemma-4-12b-qat")
        self.assertEqual(p["base_url"], "http://10.0.2.2:1234/v1")
        self.assertEqual(p["max_output_tokens"], "3000")
        self.assertEqual(db.get_setting("default_llm_profile_id"), p["id"])

    def test_api設定ならapi側のキーから移る(self):
        db.init_db()
        # 従来の設定を「API + Anthropic」に書き換えて、プロファイルを消してから作り直す
        db.set_settings({
            "llm_provider": "api", "api_provider_kind": "anthropic",
            "api_model": "claude-sonnet-5", "api_key": "sk-test",
            "api_max_output_tokens": "8000",
        })
        conn = db.get_connection()
        conn.execute("DELETE FROM llm_profiles")
        conn.commit()
        conn.close()
        db.init_db()
        p = db.get_llm_profiles()[0]
        self.assertEqual(p["provider"], "api")
        self.assertEqual(p["provider_kind"], "anthropic")
        self.assertEqual(p["model"], "claude-sonnet-5")
        self.assertEqual(p["api_key"], "sk-test")
        self.assertEqual(p["max_output_tokens"], "8000")

    def test_2回目の起動では増えない(self):
        db.init_db()
        db.init_db()
        db.init_db()
        self.assertEqual(len(db.get_llm_profiles()), 1)

    def test_移行後も1件は必ずある(self):
        # 設定が空でもプロファイル0件にはしない。0件だと実行時に選ぶものが無い
        db.init_db()
        conn = db.get_connection()
        conn.execute("DELETE FROM llm_profiles")
        conn.execute("UPDATE settings SET value = '' WHERE key LIKE 'local_%'")
        conn.commit()
        conn.close()
        db.init_db()
        self.assertEqual(len(db.get_llm_profiles()), 1)


class TestProfileSettings(_TempDB):
    """プロファイルを従来の settings キーへ展開する"""

    def setUp(self):
        super().setUp()
        db.init_db()

    def test_ローカルはlocalキーに入る(self):
        out = llm_profiles.profile_settings({
            "provider": "local", "base_url": "http://x/v1", "model": "m",
            "api_key": "k", "max_output_tokens": "3000",
        })
        self.assertEqual(out["llm_provider"], "local")
        self.assertEqual(out["local_model"], "m")
        self.assertEqual(out["local_base_url"], "http://x/v1")
        self.assertEqual(out["local_max_output_tokens"], "3000")
        self.assertNotIn("api_model", out)

    def test_APIはapiキーに入る(self):
        out = llm_profiles.profile_settings({
            "provider": "api", "provider_kind": "anthropic",
            "model": "claude-sonnet-5", "api_key": "sk", "max_output_tokens": "8000",
        })
        self.assertEqual(out["llm_provider"], "api")
        self.assertEqual(out["api_provider_kind"], "anthropic")
        self.assertEqual(out["api_model"], "claude-sonnet-5")
        self.assertNotIn("local_model", out)

    def test_空のプロファイルは何も返さない(self):
        self.assertEqual(llm_profiles.profile_settings(None), {})


class TestResolution(_TempDB):
    """既定 / タスク個別 / 実行時 の優先順位（graphs.resolve_kind と同じ形）"""

    def setUp(self):
        super().setUp()
        db.init_db()
        self.base = db.get_llm_profiles()[0]["id"]
        self.qwen = db.add_llm_profile("Qwen", provider="local", model="qwen3.5-4b",
                                       base_url="http://x/v1")
        self.claude = db.add_llm_profile("Claude", provider="api",
                                         provider_kind="anthropic",
                                         model="claude-sonnet-5", api_key="sk")

    def test_既定に従う(self):
        self.assertEqual(llm_profiles.resolve_profile_id(), self.base)

    def test_タスク個別が既定に勝つ(self):
        tid = db.add_agent_task("t", "", "p", llm_profile_id=self.qwen)
        self.assertEqual(llm_profiles.resolve_profile_id(tid), self.qwen)

    def test_実行時指定がタスク個別に勝つ(self):
        tid = db.add_agent_task("t", "", "p", llm_profile_id=self.qwen)
        self.assertEqual(
            llm_profiles.resolve_profile_id(tid, self.claude), self.claude)

    def test_defaultという指定は既定に落ちる(self):
        tid = db.add_agent_task("t", "", "p", llm_profile_id=llm_profiles.DEFAULT)
        self.assertEqual(llm_profiles.resolve_profile_id(tid), self.base)

    def test_消えたプロファイルを指していても実行は止まらない(self):
        # タスク側の指定は残るので、既定へ落とす
        tid = db.add_agent_task("t", "", "p", llm_profile_id=self.qwen)
        db.delete_llm_profile(self.qwen)
        self.assertEqual(llm_profiles.resolve_profile_id(tid), self.base)

    def test_既定IDが壊れていても先頭を使う(self):
        db.set_setting("default_llm_profile_id", "存在しないID")
        self.assertIn(llm_profiles.resolve_profile_id(),
                      {p["id"] for p in db.get_llm_profiles()})


class TestDeletion(_TempDB):
    def setUp(self):
        super().setUp()
        db.init_db()
        self.base = db.get_llm_profiles()[0]["id"]

    def test_最後の1件は削除できない(self):
        self.assertFalse(db.delete_llm_profile(self.base))
        self.assertEqual(len(db.get_llm_profiles()), 1)

    def test_既定を削除すると残りに付け替わる(self):
        other = db.add_llm_profile("Other", provider="local", model="m",
                                   base_url="http://x/v1")
        db.set_setting("default_llm_profile_id", other)
        self.assertTrue(db.delete_llm_profile(other))
        self.assertEqual(db.get_setting("default_llm_profile_id"), self.base)


class TestActiveProfile(_TempDB):
    """実行中プロファイルの切り替え（get_llm がどの設定を読むか）"""

    def setUp(self):
        super().setUp()
        db.init_db()
        self.claude = db.get_llm_profile(db.add_llm_profile(
            "Claude", provider="api", provider_kind="anthropic",
            model="claude-sonnet-5", api_key="sk", max_output_tokens="8000"))

    def test_ブロックの中だけ切り替わる(self):
        before = config.get_current_provider_info()["model"]
        with config.use_profile(self.claude):
            self.assertEqual(config.get_current_provider_info()["model"],
                             "claude-sonnet-5")
        self.assertEqual(config.get_current_provider_info()["model"], before)

    def test_入れ子は内側が勝つ(self):
        qwen = db.get_llm_profile(db.add_llm_profile(
            "Qwen", provider="local", model="qwen3.5-4b", base_url="http://x/v1"))
        with config.use_profile(self.claude):
            with config.use_profile(qwen):
                self.assertEqual(config.get_current_provider_info()["model"],
                                 "qwen3.5-4b")
            # 抜けたら外側に戻る
            self.assertEqual(config.get_current_provider_info()["model"],
                             "claude-sonnet-5")

    def test_例外でも元に戻る(self):
        before = config.active_profile()
        with self.assertRaises(RuntimeError):
            with config.use_profile(self.claude):
                raise RuntimeError("boom")
        self.assertIs(config.active_profile(), before)

    def test_別スレッドに漏れない(self):
        # スケジューラのジョブと画面が同時に走る。モジュール変数だと
        # 片方の実行がもう片方のモデルを差し替えてしまう
        seen = {}

        def other():
            seen["model"] = config.get_current_provider_info()["model"]

        with config.use_profile(self.claude):
            t = threading.Thread(target=other)
            t.start()
            t.join()
            self.assertEqual(config.get_current_provider_info()["model"],
                             "claude-sonnet-5")
        self.assertEqual(seen["model"], "gemma-4-12b-qat")

    def test_実行中でなければ既定のプロファイルが効く(self):
        # 設定はプロファイル側へ一本化してある。従来の local_* キーを
        # 直接書いても、プロファイルがある限りそちらが勝つ
        db.set_settings({"local_model": "使われないはずの値"})
        self.assertEqual(config.get_current_provider_info()["model"],
                         "gemma-4-12b-qat")


class TestApiKeyNeverLeaves(_TempDB):
    """
    履歴・エクスポートにAPIキーを残さない。

    「まとめてコピー」はそのまま外へ貼られる前提のテキストになる。
    鍵が1度でも混ざると、貼った先すべてから消さなければならない。
    """

    def setUp(self):
        super().setUp()
        db.init_db()
        self.secret = "sk-should-never-appear-12345"
        self.pid = db.add_llm_profile("Claude", provider="api",
                                       provider_kind="anthropic",
                                       model="claude-sonnet-5",
                                       api_key=self.secret,
                                       max_output_tokens="8000")

    def test_snapshotの対象フィールドにapi_keyが無い(self):
        self.assertNotIn("api_key", llm_profiles.SNAPSHOT_FIELDS)

    def test_snapshotにapi_keyが入らない(self):
        info = llm_profiles.snapshot(db.get_llm_profile(self.pid))
        self.assertNotIn("api_key", info)
        self.assertNotIn(self.secret, str(info))
        # 出したい情報は入っている
        self.assertEqual(info["model"], "claude-sonnet-5")
        self.assertEqual(info["name"], "Claude")

    def test_DBの実行記録にapi_keyが入らない(self):
        import executor
        tid = db.add_agent_task("t", "", "p", llm_profile_id=self.pid)
        eid = db.add_execution("agent", tid, "t")
        executor._prepare_agent_run(eid, tid, "p")
        row = db.get_execution(eid)
        self.assertNotIn(self.secret, row["llm_info"] or "")
        self.assertIn("claude-sonnet-5", row["llm_info"])

    def test_コピー用テキストにapi_keyが入らない(self):
        import executor
        from ui.page_history import build_export_text
        tid = db.add_agent_task("t", "", "p", llm_profile_id=self.pid)
        eid = db.add_execution("agent", tid, "t")
        executor._prepare_agent_run(eid, tid, "p")
        row = dict(db.get_execution(eid))
        row["stdout"] = "本文"
        text = build_export_text(row, None, None)
        self.assertNotIn(self.secret, text)
        self.assertIn("claude-sonnet-5", text)


class TestExportText(_TempDB):
    """まとめてコピーにモデル名が出る"""

    def setUp(self):
        super().setUp()
        db.init_db()

    def _text(self, profile_id=None):
        import executor
        from ui.page_history import build_export_text
        tid = db.add_agent_task("決算調査", "", "SKハイニックスの決算",
                                llm_profile_id=profile_id)
        eid = db.add_execution("agent", tid, "決算調査")
        executor._prepare_agent_run(eid, tid, "p")
        row = dict(db.get_execution(eid))
        row["stdout"] = "本文"
        return build_export_text(row, None, None)

    def test_モデル名と接続先が出る(self):
        text = self._text()
        self.assertIn("- モデル: ", text)
        self.assertIn("gemma-4-12b-qat", text)
        self.assertIn("ローカル", text)

    def test_最大出力トークンとThinkingが出る(self):
        text = self._text()
        self.assertIn("- 最大出力トークン: 3000", text)
        self.assertIn("- Thinking: 無効", text)

    def test_グラフ名も出る(self):
        self.assertIn("- グラフ: ", self._text())

    def test_選んだプロファイルが反映される(self):
        pid = db.add_llm_profile("Qwen", provider="local", model="qwen3.5-4b",
                                 base_url="http://x/v1", max_output_tokens="2048")
        text = self._text(pid)
        self.assertIn("qwen3.5-4b", text)
        self.assertNotIn("gemma-4-12b-qat", text)

    def test_モデル情報が無い古い履歴でも壊れない(self):
        from ui.page_history import build_export_text
        row = {"id": "x", "target_name": "t", "status": "done",
               "started_at": "2026-07-30T00:00:00", "stdout": "本文"}
        text = build_export_text(row, None, None)
        self.assertIn("本文", text)
        self.assertNotIn("- モデル:", text)

    def test_壊れたllm_infoでも落ちない(self):
        from ui.page_history import parse_llm_info
        self.assertEqual(parse_llm_info("{壊れたJSON"), {})
        self.assertEqual(parse_llm_info(None), {})
        self.assertEqual(parse_llm_info("[1,2]"), {})


class TestExecutorWiring(unittest.TestCase):
    """
    3つの実行経路すべてでプロファイルが効くか（構造チェック）。

    get_llm() はグラフの各ノードから直接呼ばれるので、実行の外側で
    プロファイルを立て忘れると、黙って既定のモデルで走ってしまう。
    経路ごとに with を書くと書き漏らしに気づけないため、回すところを
    1箇所（_agent_steps）に寄せてある。それが崩れたらここで落とす。
    """

    def _source(self):
        import inspect
        import executor
        return inspect.getsource(executor)

    def test_streamを直接回している箇所が無い(self):
        src = self._source()
        # _agent_steps の中の1回だけ
        self.assertEqual(src.count(".stream("), 1)

    def test_use_profileは_agent_stepsの中にある(self):
        src = self._source()
        self.assertEqual(src.count("use_profile("), 1)
        head = src.index("def _agent_steps(")
        tail = src.index("\ndef ", head)
        self.assertIn("use_profile(", src[head:tail])

    def test_3経路すべてが_agent_stepsを通る(self):
        src = self._source()
        # 定義1 + 呼び出し3
        self.assertEqual(src.count("_agent_steps("), 4)
        self.assertEqual(src.count("_prepare_agent_run("), 4)


if __name__ == "__main__":
    unittest.main()
