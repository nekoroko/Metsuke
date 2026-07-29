# sandbox.py — 生成コードのサンドボックス実行（Podman版）
import subprocess
import tempfile
import os

WORKSPACE = "/tmp/agent_workspace"

def execute_in_sandbox(code: str, timeout: int = 60) -> dict:
    """
    生成されたPythonコードをPodmanコンテナ内で実行する。
    - コンテナは使い捨て（--rm）
    - ネットワーク無効（--network none）
    - ファイルシステムは読み取り専用（--read-only）、作業ディレクトリのみ書き込み可
    - メモリ制限あり（256MB）
    - プロセス数制限あり（--pids-limit 32）
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

    try:
        result = subprocess.run(
            [
                "podman", "run",
                "--rm",                              # 終了後にコンテナを自動削除
                "--network", "none",                 # ネットワーク遮断
                "--read-only",                       # ファイルシステム読み取り専用
                "--tmpfs", "/tmp:rw,size=64m",       # /tmpだけ書き込み可（64MB制限）
                "--memory", "256m",                  # メモリ上限256MB
                "--pids-limit", "32",                # プロセス数上限
                "--cpus", "1",                       # CPU 1コアに制限
                "-v", f"{WORKSPACE}:/tmp/agent_workspace:ro",  # コンテナ内でも同じパスでアクセス
                "docker.io/library/python:3.12-slim",
                "python3", f"{WORKSPACE}/{temp_filename}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
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