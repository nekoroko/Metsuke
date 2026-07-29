# settings_store.py — 設定DBの読み取り専用アクセス
#
# config.py / tools.py / sandbox.py は、いずれも設定を読むだけで書き込まない。
# これらが db.py を import すると、import時に init_db() の副作用が走るうえ、
# agent-project 側（graph.py 経由）が agent-studio 側のDB実装に依存してしまう。
#
# そのため各ファイルが個別に sqlite を直接読む実装を持っていたが、
# 同じ関数が3箇所に複製される形になっていたため、ここへ集約する。
# 書き込みを伴う操作は従来どおり db.py 側にある。

import sqlite3
import os

from paths import DB_PATH


def read_settings() -> dict:
    """設定DBの settings テーブルを dict として読む。失敗時は空dict。"""
    try:
        if not os.path.exists(DB_PATH):
            return {}
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        conn.close()
        return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {}
