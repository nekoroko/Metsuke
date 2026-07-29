# sandbox.py — 生成コードのサンドボックス実行（Podman版）
import subprocess
import tempfile
import os

WORKSPACE = "/tmp/agent_workspace"

# requirements-tools.txt のライブラリを同梱した専用イメージ（Containerfile参照）。
# 素の python:3.12-slim だと pandas/requests/ddgs 等が入っておらず、
# CREATOR_PROMPTがAIに許可しているライブラリを使うコードが軒並み
# ModuleNotFoundError で落ちるため、専用イメージを前提にする。
SANDBOX_IMAGE = os.environ.get("AGENT_SANDBOX_IMAGE", "agent-studio-sandbox:latest")

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
