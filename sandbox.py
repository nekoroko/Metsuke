# sandbox.py — コード実行のサンドボックス（Podman版）
#
# 検証済みツール・AI生成コード・シェルコマンドを問わず、
# コードを実行する経路はすべてこのモジュールを通る。
#
# 以前は「検証済みツールはホスト直接実行、未検証コードのみPodman」という
# 使い分けだったが、以下の理由で全経路をサンドボックス化した。
#   - プレビューと本番の実行環境が一致し、「プレビューは通ったのに
#     本番で落ちる」が構造的に消える
#   - 人間のレビューは万能ではなく、検証済みコードにもバグはあり得る
#     （パス誤りによる削除など）ため、爆発半径を限定する価値がある
#   - run_shell だけが無防備に残る状態を避ける
import subprocess
import tempfile
import threading
import os

from paths import BASE_DIR, DB_PATH
from tool_runtime import requirements_hash
from settings_store import read_settings

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

# サンドボックスに渡してよい設定キー → 環境変数名の対応表。
#
# 設定DB（agent_studio.db）には検索APIキーだけでなく、LLMプロバイダの
# APIキー（api_key）も平文で同居している。DBそのものをコンテナに
# マウントすると、実行されるコードに全プロバイダのキーを渡すことになる。
# そのため「必要なキーだけを環境変数で個別に注入する」方式にしている。
SANDBOX_ENV_KEYS = {
    "tavily_api_key": "TAVILY_API_KEY",
    "google_api_key": "GOOGLE_API_KEY",
    "google_cse_id": "GOOGLE_CSE_ID",
    "brave_api_key": "BRAVE_API_KEY",
}

# 実行経路ごとの権限。どこまで許すかをここに集約する。
#
# 検証済みツールは人間のレビューを通っているため、通信とファイル出力を許可する。
# エージェントが実行時に組み立てるコード・コマンドは、レビューを経ていないため
# 通信を遮断する（調査が必要なら web_search / fetch_url ツールを使わせる）。
PROFILE_VERIFIED_TOOL = {"network": True, "writable_workspace": True, "timeout": 120}
PROFILE_AGENT_CODE = {"network": False, "writable_workspace": False, "timeout": 60}
PROFILE_AGENT_SHELL = {"network": False, "writable_workspace": True, "timeout": 30}

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


def build_sandbox_env() -> dict:
    """
    サンドボックスに渡す環境変数を組み立てる。
    SANDBOX_ENV_KEYS に列挙したキーのうち、値が入っているものだけを返す。
    LLMのAPIキーは意図的に含めない。
    """
    settings = read_settings()
    env = {}
    for setting_key, env_name in SANDBOX_ENV_KEYS.items():
        value = (settings.get(setting_key) or "").strip()
        if value:
            env[env_name] = value
    return env


def exposes_secrets(host_path: str) -> bool:
    """
    そのホストパスをマウントすると、設定DBがコンテナから見えるか。

    設定DBにはLLMプロバイダのAPIキーが平文で入っている。サンドボックスの
    中で動くのは**AIが生成したコード**なので、DBが見える状態にすると
    「生成コードが鍵を読んで外へ送る」経路が開く。ネットワークを許可した
    実行なら、そのまま持ち出せる。

    親ディレクトリの指定でも見えてしまうので、パスの前方一致で見る。
    ファイル単体の指定（DBそのもの）も同じ判定に含まれる。
    """
    if not host_path:
        return False
    try:
        target = os.path.realpath(os.path.expanduser(host_path))
        db = os.path.realpath(DB_PATH)
    except OSError:
        return False
    if target == db:
        return True
    # 親ディレクトリを渡された場合。os.path.commonpath は区切りの扱いを
    # 誤らないので、文字列の startswith ではなくこちらを使う
    try:
        return os.path.commonpath([target, db]) == target
    except ValueError:
        return False          # ドライブが違う等、比較できない場合は無関係


def parse_mounts(text: str) -> list:
    """
    追加マウント設定を解析する。1行1マウントで、以下の形式。

        /host/path:/container/path:ro
        /host/path:/container/path        （modeを省略すると ro）
        /host/path                        （コンテナ内も同じパス、ro）

    全実行がサンドボックス化されたことで、作業ディレクトリ以外のホスト
    ファイル（ログ、CSV等）に触るツールはマウント指定が必須になる。
    ツール個別ではなく全体設定にしているのは、実行経路ごとに権限が
    バラつくのを避けるため。

    **設定DBが見える指定は落とす。** 誤って親ディレクトリを書いた場合も
    含めて、ここで止める（DBには平文のAPIキーが入っており、コンテナの中で
    動くのはAIが生成したコード）。落としたことは戻り値に出ないので、
    UI 側は check_mounts で理由つきの一覧を出すこと。
    """
    mounts = []
    for host, container, mode, rejected in _parse_mount_lines(text):
        if rejected:
            continue
        mounts.append((host, container, mode))
    return mounts


def _parse_mount_lines(text: str):
    """1行ずつ (host, container, mode, 却下理由) を返す内部関数。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(":")]
        host = parts[0]
        if not host:
            continue
        container = parts[1] if len(parts) > 1 and parts[1] else host
        mode = parts[2] if len(parts) > 2 and parts[2] else "ro"
        if mode not in ("ro", "rw"):
            mode = "ro"
        rejected = ""
        if exposes_secrets(host):
            rejected = ("設定DB（APIキーを平文で保持）がコンテナから見えるため"
                        "マウントしません")
        yield host, container, mode, rejected


def check_mounts(text: str) -> list[dict]:
    """
    UI表示用。各行の解析結果と、却下した場合はその理由を返す。

    黙って落とすと「書いたのにマウントされない」という分かりにくい
    状態になるので、画面に理由を出せるようにしておく。
    """
    return [{"host": host, "container": container, "mode": mode,
             "rejected": rejected}
            for host, container, mode, rejected in _parse_mount_lines(text)]


def configured_mounts() -> list:
    """設定DBに保存された追加マウントを返す"""
    return parse_mounts(read_settings().get("sandbox_extra_mounts", ""))


def _build_podman_args(container_argv: list, network: bool,
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

    # 設定された追加マウント + 呼び出し側が指定した分
    for host_path, container_path, mount_mode in configured_mounts() + list(extra_mounts or []):
        args += ["-v", f"{host_path}:{container_path}:{mount_mode}{_MOUNT_LABEL}"]

    # 設定DB（agent_studio.db）にはLLMプロバイダのAPIキーも平文で同居しているため、
    # DBそのものは絶対にマウントしない。必要なキーだけを個別に環境変数で渡す。
    for key, value in (env or {}).items():
        args += ["-e", f"{key}={value}"]

    args += [SANDBOX_IMAGE] + list(container_argv)
    return args


def _run_in_container(container_argv: list, timeout: int, network: bool,
                      writable_workspace: bool, env: dict, extra_mounts: list) -> dict:
    """
    コンテナ内で任意のコマンドを実行する共通処理。
    Pythonコード実行もシェル実行もここを通る。
    """
    # 初回、および requirements-tools.txt 変更後は、ここで自動ビルドが走る
    ensured = ensure_sandbox_image()
    if not ensured["ok"]:
        return {"success": False, "stdout": "", "stderr": ensured["message"]}

    args = _build_podman_args(
        container_argv,
        network=network,
        writable_workspace=writable_workspace,
        env=env,
        extra_mounts=extra_mounts,
    )

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": f"タイムアウト（{timeout}秒）"}
    except FileNotFoundError:
        return {
            "success": False, "stdout": "",
            "stderr": "podmanが見つかりません。'sudo apt install podman' でインストールしてください。",
        }

    stderr = result.stderr
    if result.returncode != 0 and any(
        m in stderr.lower() for m in _IMAGE_MISSING_MARKERS
    ):
        stderr = (
            f"サンドボックスイメージ '{SANDBOX_IMAGE}' が見つかりません。\n"
            "リポジトリのルートで以下を実行してビルドしてください:\n"
            f"  podman build -t {SANDBOX_IMAGE} .\n\n"
            f"--- podmanの出力 ---\n{stderr}"
        )

    return {
        "success": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": stderr,
        "returncode": result.returncode,
    }


def execute_shell_in_sandbox(command: str, timeout: int = 30, network: bool = False,
                             writable_workspace: bool = True, env: dict = None,
                             extra_mounts: list = None) -> dict:
    """
    シェルコマンドをコンテナ内で実行する。

    以前 run_shell はホスト上で直接実行されており、generate_code の隔離を
    回避できる唯一の経路になっていた。ここを通すことでその穴を塞ぐ。
    """
    return _run_in_container(
        ["sh", "-c", command],
        timeout=timeout, network=network, writable_workspace=writable_workspace,
        env=env, extra_mounts=extra_mounts,
    )


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

    # 生成コードを一時ファイルに書き出す（ワークスペース経由でコンテナに渡す）
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.py', delete=False,
        encoding='utf-8', dir=WORKSPACE
    ) as f:
        f.write(code)
        temp_filename = os.path.basename(f.name)
        temp_path = f.name

    try:
        return _run_in_container(
            ["python3", f"{WORKSPACE}/{temp_filename}"],
            timeout=timeout, network=network,
            writable_workspace=writable_workspace,
            env=env, extra_mounts=extra_mounts,
        )
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
