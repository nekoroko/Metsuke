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
import shutil

# リポジトリのルート。Containerfileのビルドコンテキストや
# requirements-tools.txt の解決にも使う。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 以前の置き場所（リポジトリ直下）。移行元としてのみ参照する。
LEGACY_DB_PATH = os.path.join(BASE_DIR, "agent_studio.db")


def _default_db_path() -> str:
    """
    既定のDBの置き場所。

    リポジトリ直下に置かない。DBにはLLMプロバイダのAPIキーが平文で入るので、
    作業ディレクトリごと扱われる操作に巻き込まれると鍵まで一緒に運ばれる。

      - サンドボックスへのマウント（BASE_DIR を渡すと中身ごと見える）
      - ディレクトリ単位のバックアップ・クラウド同期
      - リポジトリのコピー・zip 化

    XDG の作法に合わせて ~/.local/share/agent-studio/ に置く。
    AGENT_STUDIO_DB を設定すればそちらが優先される（複数構成の使い分け用）。
    """
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "agent-studio", "agent_studio.db")


DB_PATH = os.environ.get("AGENT_STUDIO_DB") or _default_db_path()

# DBのパーミッション。所有者だけが読み書きできるようにする。
# sqlite3 が作るファイルは既定で 0644 で、同じマシンの他ユーザーから
# APIキーが読めてしまう（aws-cli / gh / npm はいずれも 0600）。
DB_MODE = 0o600


def secure_db_file(path: str = None) -> None:
    """DBファイルの権限を 0600 に落とす。存在しなければ何もしない。

    毎回呼んでよい（既に 0600 なら chmod は無害）。作成のたびに
    呼ぶのではなく、接続のたびに呼ぶ。sqlite は WAL や journal を
    後から作ることがあるため。
    """
    target = path or DB_PATH
    for candidate in (target, target + "-wal", target + "-shm", target + "-journal"):
        try:
            if os.path.exists(candidate):
                os.chmod(candidate, DB_MODE)
        except OSError:
            pass          # 権限を落とせなくても動作は続ける（Windows等）


def ensure_db_location() -> str:
    """
    DBの置き場所を用意し、旧位置にDBがあれば引っ越す。

    引っ越しは移動（コピーではない）。両方に残ると、どちらが使われて
    いるのか分からない状態になる。旧位置に残骸があると、
    リポジトリを配った先に鍵が混ざる事故にもつながる。
    """
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass

    if (not os.path.exists(DB_PATH)
            and os.path.exists(LEGACY_DB_PATH)
            and os.path.abspath(LEGACY_DB_PATH) != os.path.abspath(DB_PATH)):
        shutil.move(LEGACY_DB_PATH, DB_PATH)
        for suffix in ("-wal", "-shm", "-journal"):
            if os.path.exists(LEGACY_DB_PATH + suffix):
                shutil.move(LEGACY_DB_PATH + suffix, DB_PATH + suffix)
        print(f"[paths] 設定DBを移動しました: {LEGACY_DB_PATH} → {DB_PATH}")

    secure_db_file()
    return DB_PATH
