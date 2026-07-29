# paths.py — 設定DBの場所を一元管理する
#
# 以前は db.py が相対パス（実行時のCWD基準）、config.py / tools.py が
# ~/agent-studio/ 固定の絶対パスを、それぞれ独立に持っていた。
# この2つは「リポジトリが物理的に $HOME/agent-studio に置かれ、かつ
# そこがCWD」のときしか一致しないため、クローン先が違うだけで
# 「UI（db.py）が書き込むDB」と「エージェント（config.py/tools.py）が
# 読み込むDB」が別ファイルになっていた。
#
# その場合、設定タブでLLMプロバイダや検索APIキーを変更しても反映されず、
# エラーも出さずに黙ってデフォルト（ローカルLM Studio / DuckDuckGo）へ
# フォールバックし続ける、という原因の分かりにくい不具合になる。
#
# ここを唯一の定義箇所とし、db.py / config.py / tools.py はこれを参照する。
# agent-project と agent-studio を別ディレクトリに分けて運用する場合は、
# 環境変数 AGENT_STUDIO_DB で明示的にパスを指定する。

import os

# リポジトリのルート。Containerfileのビルドコンテキストや
# requirements-tools.txt の解決にも使う。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 実行時のCWDに依存させないため、このファイルの位置を基準にした絶対パスにする
DB_PATH = os.environ.get("AGENT_STUDIO_DB") or os.path.join(BASE_DIR, "agent_studio.db")
