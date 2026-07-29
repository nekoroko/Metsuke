# sandbox.py — 生成コードのサンドボックス実行（Podman版）
import subprocess
import tempfile
import threading
import os

from paths import BASE_DIR
from tool_runtime import requirements_hash

WORKSPACE = "/tmp/agent_workspace"

# requirements-tools.txt のライブラリを同梱した専用イメージ（Containerfile参照）。
# 素の python:3.12-slim だと pandas/requests/ddgs 等が入っておらず、
# CREATOR_PROMPTがAIに許可しているライブラリを使うコードが軒並み
# ModuleNotFoundError で落ちるため、専用イメージを前提にする。
SANDBOX_IMAGE = os.environ.get("AGENT_SANDBOX_IMAGE", "agent-studio-sandbox:latest")

# AGENT_SANDBOX_IMAGE で明示指定された場合は、ユーザーが自前で管理している
# イメージとみなし、自動ビルドの対象外にする（勝手に上書きしないため）。
_IMAGE_IS_USER_MANAGED = bool(os.environ.get("AGENT_SANDBOX_IMAGE"))

# requirements-tools.txt の内容ハッシュを焼き込むラベル名。
# イメージのラベルと現在の定義を突き合わせ、ズレていれば再ビルドする。
IMAGE_LABEL = "agent-studio.tools-hash"

BUILD_TIMEOUT = 900

# 複数のプレビューが同時に走ったときに、ビルドが二重起動しないようにする
_build_lock = threading.Lock()

# pandas/numpyはimportしただけで100MB近く消費するため、
# 従来の256mでは実データ処理でOOMしやすい。
DEFAULT_MEMORY = os.environ.get("AGENT_SANDBOX_MEMORY", "512m")

# SELinux有効環境（Fedora/RHEL系）ではbind mountにラベル再付与が必要になり、
# 付けないと Permission denied になる。逆に不要な環境で付ける意味はないため、
# 環境変数で明示的に有効化する。
_MOUNT_LABEL = ",Z" if os.environ.get("AGENT_SANDBOX_SELINUX") == "1" else ""

_IMAGE_MISSING_MARKERS = (
    "image not known",
    "unable to find image",
    "no such image",
    "manifest unknown",
)


def _image_tools_hash() -> str | None:
    """
    既存イメージに焼き込まれたライブラリ定義のハッシュを返す。
    イメージが無い / ラベルが無い / podmanが無い場合は None。
    """
    try:
        result = subprocess.run(
            ["podman", "image", "inspect", "--format",
             f'{{{{index .Config.Labels "{IMAGE_LABEL}"}}}}', SANDBOX_IMAGE],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def ensure_sandbox_image(force: bool = False) -> dict:
    """
    サンドボックスイメージが最新の定義でビルド済みであることを保証する。

    - イメージが存在しない（初回）      → ビルドする
    - requirements-tools.txt が変わった → 再ビルドする
    - 一致している                      → 何もしない

    これにより、利用者が事前に `podman build` を実行する必要がなくなる。
    以前は素の python:3.12-slim を指定していたためpodmanが自動pullしており、
    事前準備は不要だった。専用イメージに変えた分をここで埋め合わせている。

    戻り値: {"ok": bool, "built": bool, "message": str}
    """
    if _IMAGE_IS_USER_MANAGED and not force:
        # ユーザー管理のイメージには触れない
        return {"ok": True, "built": False, "message": ""}

    want = requirements_hash()

    with _build_lock:
        # ロック待ちの間に他スレッドがビルドを終えている可能性があるため、
        # ロック取得後に改めて確認する
        if not force and _image_tools_hash() == want:
            return {"ok": True, "built": False, "message": ""}

        try:
            result = subprocess.run(
                [
                    "podman", "build",
                    "-t", SANDBOX_IMAGE,
                    "-f", os.path.join(BASE_DIR, "Containerfile"),
                    "--label", f"{IMAGE_LABEL}={want}",
                    BASE_DIR,
                ],
                capture_output=True, text=True, timeout=BUILD_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "built": False,
                    "message": f"サンドボックスイメージのビルドがタイムアウトしました（{BUILD_TIMEOUT}秒）"}
        except FileNotFoundError:
            return {"ok": False, "built": False,
                    "message": "podmanが見つかりません。'sudo apt install podman' でインストールしてください。"}

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "")[-2000:]
            return {
                "ok": False, "built": False,
                "message": (
                    f"サンドボックスイメージ '{SANDBOX_IMAGE}' のビルドに失敗しました。\n"
                    "requirements-tools.txt のパッケージ名や、ネットワーク接続を確認してください。\n\n"
                    f"--- podman build の出力 ---\n{detail}"
                ),
            }

        return {"ok": True, "built": True, "message": ""}


def image_status() -> dict:
    """
    UI表示用。イメージの状態を返す。
      state: "ready"（最新） / "stale"（再ビルド要） / "missing"（未ビルド）
             / "user_managed"（ユーザー管理のため対象外）
    """
    if _IMAGE_IS_USER_MANAGED:
        return {"state": "user_managed", "image": SANDBOX_IMAGE}

    want = requirements_hash()
    current = _image_tools_hash()
    if current is None:
        state = "missing"
    elif current == want:
        state = "ready"
    else:
        state = "stale"
    return {"state": state, "image": SANDBOX_IMAGE,
            "current_hash": current, "expected_hash": want}


def _build_podman_args(script_path_in_container: str, network: bool,
                       writable_workspace: bool, env: dict, extra_mounts: list) -> list:
    """podman run の引数列を組み立てる"""
    args = [
        "podman", "run", "--rm",
        "--read-only",                    # ルートFSは常に読み取り専用
        "--tmpfs", "/tmp:rw,size=64m",    # /tmpのみ書き込み可（64MB）
        "--memory", DEFAULT_MEMORY,
        "--pids-limit", "32",
        "--cpus", "1",
    ]

    # network=True のときは --network 自体を省略し、Podmanの既定
    # （rootlessならslirp4netns/pasta、rootfulならbridge）に任せる。
    # 明示的に "bridge" と書くとrootlessで動かないため。
    if not network:
        args += ["--network", "none"]

    mode = "rw" if writable_workspace else "ro"
    args += ["-v", f"{WORKSPACE}:{WORKSPACE}:{mode}{_MOUNT_LABEL}"]

    for host_path, container_path, mount_mode in (extra_mounts or []):
        args += ["-v", f"{host_path}:{container_path}:{mount_mode}{_MOUNT_LABEL}"]

    # 設定DB（agent_studio.db）にはLLMプロバイダのAPIキーも平文で同居しているため、
    # DBそのものは絶対にマウントしない。必要なキーだけを個別に環境変数で渡す。
    for key, value in (env or {}).items():
        args += ["-e", f"{key}={value}"]

    args += [SANDBOX_IMAGE, "python3", script_path_in_container]
    return args


def execute_in_sandbox(code: str, timeout: int = 60, network: bool = False,
                       writable_workspace: bool = False, env: dict = None,
                       extra_mounts: list = None) -> dict:
    """
    生成されたPythonコードをPodmanコンテナ内で実行する。

    常に有効な制限:
    - 使い捨てコンテナ（--rm）
    - ルートFS読み取り専用（--read-only）、/tmpのみtmpfsで書き込み可
    - メモリ制限（既定512MB）、プロセス数制限（32）、CPU 1コア

    呼び出し側で選択する制限:
    - network=False（既定）  … --network none で通信遮断
      network=True           … 外向き通信を許可（Tavily等のAPIアクセス用）
    - writable_workspace=False（既定） … 作業ディレクトリを読み取り専用でマウント
      writable_workspace=True          … 書き込み可でマウント（ファイル出力するツール用）

    ネットワークを開けてもサンドボックスの価値の大半は残る点に注意:
    ファイルシステムのスコープ制限（~/.ssh や agent_studio.db に触れない）、
    リソース制限、非永続性はいずれも有効なままである。

    env: コンテナに渡す環境変数（検索APIキー等）。
         設定DBをマウントする代わりに、必要なキーだけをここで渡す。
    extra_mounts: [(ホストパス, コンテナ内パス, "ro"|"rw"), ...]
    """
    # 初回、および requirements-tools.txt 変更後は、ここで自動ビルドが走る
    ensured = ensure_sandbox_image()
    if not ensured["ok"]:
        return {"success": False, "stdout": "", "stderr": ensured["message"]}

    os.makedirs(WORKSPACE, exist_ok=True)

    # 生成コードを一時ファイルに書き出す
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.py', delete=False,
        encoding='utf-8', dir=WORKSPACE
    ) as f:
        f.write(code)
        temp_filename = os.path.basename(f.name)
        temp_path = f.name

    args = _build_podman_args(
        f"{WORKSPACE}/{temp_filename}",
        network=network,
        writable_workspace=writable_workspace,
        env=env,
        extra_mounts=extra_mounts,
    )

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)

        stderr = result.stderr
        if result.returncode != 0 and any(
            m in stderr.lower() for m in _IMAGE_MISSING_MARKERS
        ):
            stderr = (
                f"サンドボックスイメージ '{SANDBOX_IMAGE}' が見つかりません。\n"
                "リポジトリのルートで以下を実行してビルドしてください（初回のみ）:\n"
                f"  podman build -t {SANDBOX_IMAGE} .\n\n"
                f"--- podmanの出力 ---\n{stderr}"
            )

        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": stderr,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "stdout": "",
            "stderr": f"タイムアウト（{timeout}秒）",
        }
    except FileNotFoundError:
        return {
            "success": False,
            "stdout": "",
            "stderr": "podmanが見つかりません。'sudo apt install podman' でインストールしてください。",
        }
    finally:
        os.unlink(temp_path)
