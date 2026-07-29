# Containerfile — AI生成コードのサンドボックス実行用イメージ
#
# 素の python:3.12-slim には標準ライブラリしか入っていないため、
# ai_creator.py の CREATOR_PROMPT がAIに使用を許可しているライブラリ
# （requirements-tools.txt）を同梱したイメージを用意する。
#
# これが無いと、pandas/requests/ddgs 等を使うツールは
# サンドボックス内で必ず ModuleNotFoundError になる
# （--network none のためコンテナ内での pip install もできない）。
#
# ビルドは初回のみ。ビルド時のみネットワークが必要で、
# 以降の実行はオフラインで完結する。
#
#   podman build -t agent-studio-sandbox:latest .
#
# 別名でビルドした場合は、環境変数 AGENT_SANDBOX_IMAGE で指定できる。

FROM docker.io/library/python:3.12-slim

COPY requirements-tools.txt /tmp/requirements-tools.txt
RUN pip install --no-cache-dir -r /tmp/requirements-tools.txt \
    && rm -f /tmp/requirements-tools.txt

# 実行時は --read-only + --tmpfs /tmp で起動するため、
# 書き込み可能な場所はマウントしたパスと /tmp のみになる。
WORKDIR /tmp/agent_workspace
