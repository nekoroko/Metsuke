# config.py — LLM接続設定（Local / API 切り替え、Thinking制御対応）
#
# 設定はagent-studioのSQLite（settingsテーブル）に一元化されている。
# UIの「⚙️ 設定」タブから切り替え可能。
# DBが読めない/未初期化の場合は、ローカルLLM（LM Studio）にフォールバックする。
#
# プロバイダの扱い方針:
# - OpenAI互換で問題なく動くもの（LM Studio、OpenAI本家、Bedrock Gateway、Gemini等）
#   は ChatOpenAI のまま扱う。
# - ネイティブなLangChain統合が存在するプロバイダ（Anthropic）は、
#   そちらを使う。OpenAI互換レイヤーはAnthropic公式が
#   「本番用途には非推奨」と明言しており、Extended Thinking等の主要機能を
#   フルに使えないため。
#
# 最大出力トークン数（max_tokens）の扱い方針:
# 接続先のAI基盤がローカルかAPIかに関わらず、実際の上限は環境によって様々
# （ローカルはサーバー側のContext Length設定次第、APIはモデル・プロバイダ次第）。
# API接続でも自動検出に失敗するケースはあるし、ローカル接続でも自動検出できる
# サーバーソフトはある。そのため「APIだから大きい値」「ローカルだから小さい値」と
# コード側で決め打ちにせず、UIの「⚙️ 設定」タブで値を持たせ、可能であれば
# 接続テストで自動取得、失敗すればユーザーが手入力する設計にしている。
# コード内の定数は、設定値が空/不正な場合の最終フォールバックに過ぎない。

import os
import time
import random
from langchain_openai import ChatOpenAI
from paths import DB_PATH as AGENT_STUDIO_DB  # db.py（UI側）と同一のDBを指す
from settings_store import read_settings as _read_settings

FALLBACK_LOCAL_BASE_URL = "http://10.0.2.2:1234/v1"
FALLBACK_LOCAL_MODEL = "gemma-4-12b-qat"

# 設定値が空/不正な場合にのみ使われる最終フォールバック（保守的な値）
ABSOLUTE_FALLBACK_LOCAL_MAX_TOKENS = 3000
ABSOLUTE_FALLBACK_API_MAX_TOKENS = 4000




def _resolve_max_tokens(configured: str, absolute_fallback: int,
                         boost: bool) -> int:
    """
    設定された最大出力トークン数を解決する。
    - 設定値が有効な数値ならそれを使う（boost時はそのまま、非boost時は
      半分程度に抑えて通常時のトークン消費を節約する）
    - 設定値が空/不正なら、最終フォールバック定数を使う
    """
    try:
        base = int(configured)
        if base <= 0:
            raise ValueError
    except (TypeError, ValueError):
        base = absolute_fallback

    if boost:
        return base
    return max(500, int(base * 0.5))


def fetch_anthropic_max_tokens(api_key: str, model: str) -> int | None:
    """
    Anthropic Models APIを叩いて、指定モデルの最大出力トークン数を動的に取得する。
    取得できた場合はその値、できなければNoneを返す（呼び出し側でハードコード値に
    フォールバックする）。

    Anthropicは公式Models APIでこの情報を確実に取得できる。
    他のプロバイダ（OpenAI互換全般）は fetch_openai_compatible_max_tokens を使う
    （そちらは対応状況がプロバイダ次第でベストエフォート）。
    """
    try:
        import urllib.request
        import json as _json

        req = urllib.request.Request(
            f"https://api.anthropic.com/v1/models/{model}",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        max_tokens = data.get("max_tokens")
        if isinstance(max_tokens, int) and max_tokens > 0:
            return max_tokens
    except Exception:
        pass
    return None


def fetch_openai_compatible_max_tokens(base_url: str, model: str, api_key: str = "") -> int | None:
    """
    OpenAI互換エンドポイント（ローカルLLMサーバー、OpenAI互換API全般）に対して、
    モデルの最大出力/コンテキスト長の情報を得られないかベストエフォートで試行する。

    標準のOpenAI API仕様では /v1/models はこの情報を含まないが、
    LocalAI・一部のプロキシ・vLLMの一部バージョン等、実装によっては
    'context_length' 'max_model_len' 'n_ctx' 等の独自フィールドで
    含めていることがある。そうした情報があれば拾い、無ければNoneを返す
    （呼び出し側はユーザーに手入力してもらう）。

    ローカル/APIを問わず「ダメ元で試す」位置づけの関数。
    """
    try:
        import urllib.request
        import json as _json

        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # まず /v1/models/{model} を試す
        for path in (f"/models/{model}", "/models"):
            url = base_url.rstrip("/") + path
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = _json.loads(resp.read().decode("utf-8"))
            except Exception:
                continue

            candidates = []
            if isinstance(data, dict):
                candidates.append(data)
                if isinstance(data.get("data"), list):
                    for item in data["data"]:
                        if isinstance(item, dict) and item.get("id") == model:
                            candidates.append(item)

            for c in candidates:
                for key in ("max_output_tokens", "max_tokens", "context_length",
                            "max_model_len", "n_ctx"):
                    val = c.get(key)
                    if isinstance(val, int) and val > 0:
                        return val
    except Exception:
        pass
    return None


def _get_anthropic_llm(settings: dict, temperature: float, max_tokens: int, disable_thinking: bool):
    """
    Anthropicネイティブのクライアントを返す。
    langchain_anthropic が未インストールの場合は分かりやすいエラーを出す。

    Anthropic APIのThinking仕様（2026年時点）:
    - 旧方式 {"type": "enabled", "budget_tokens": N} は新しいモデル世代で非推奨/廃止されている
    - 新方式は {"type": "adaptive"} + output_config: {"effort": "low"|"medium"|"high"|"xhigh"|"max"}
    - モデルによってはThinkingが常時ONで、明示的な無効化（disabled指定）が400エラーになる
    - Opus 4.7/4.8, Fable 5等の新しいモデルは、Thinkingの有無にかかわらず
      temperature/top_p/top_k自体を一切受け付けない（サンプリングが自動管理されるため）

    温度パラメータの互換性がモデルごとに割れているため、Anthropic接続では
    temperatureを送らない方針にしている（送っても無視されるだけの旧モデルもあれば、
    400エラーになる新モデルもあるため、送らないのが最も安全）。
    """
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError as e:
        raise ImportError(
            "Anthropicプロバイダを使うには langchain_anthropic が必要です。"
            "`pip install langchain-anthropic` を実行してください。"
        ) from e

    model = settings.get("api_model", "").strip()
    api_key = settings.get("api_key", "").strip()

    kwargs = dict(
        model=model,
        api_key=api_key,
        max_tokens=max_tokens,
    )

    if not disable_thinking:
        # Adaptive Thinking（新方式）。effortで深さを制御する。
        # disable_thinking=Falseでこの分岐に来ている＝Thinkingを積極的に使う設定
        # なので、深さは高めに倒す。呼び出し元でboost_tokensが立っていた場合も
        # （＝直前にThinkingが検知された等）highにする。
        effort = "high"
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["model_kwargs"] = {"output_config": {"effort": effort}}
    # disable_thinking=True の場合は thinking パラメータ自体を省略する。
    # 省略時の挙動はモデル依存（旧世代は無効、Fable 5等の新世代は
    # 常時Adaptiveがデフォルトで有効になる）だが、明示的にdisabledを
    # 送るより安全（disabled指定自体が400になるモデルがあるため）。

    return ChatAnthropic(**kwargs)


def get_llm(temperature: float = 0.1, boost_tokens: bool = False):
    """
    設定に応じてLLMインスタンスを返す。

    - provider=local または api_provider_kind=openai_compatible:
      ChatOpenAI（LM Studio, OpenAI本家, Bedrock Gateway, Gemini等）
    - provider=api かつ api_provider_kind=anthropic:
      ChatAnthropic（ネイティブ、Extended Thinking等をフル活用）

    Thinking/Reasoningの抑制はUIの「⚙️ 設定」タブでOn/Off切り替え可能。
    boost_tokens=True の場合、直前の呼び出しでThinkingが検知された等の理由で
    一時的にmax_tokensを引き上げたい場合に使う（呼び出し側の耐性ロジック用）。

    max_tokensは「⚙️ 設定」タブでユーザーが設定した値（接続テストでの
    自動取得結果、または手入力値）を使う。Local/APIどちらであっても、
    設定値が入っていればそれを尊重し、空/不正な場合のみコード内の
    保守的なフォールバック値を使う。
    """
    settings = _read_settings()
    provider = settings.get("llm_provider", "local")
    api_provider_kind = settings.get("api_provider_kind", "openai_compatible")
    disable_thinking = settings.get("disable_thinking", "true") == "true"
    use_boost = boost_tokens or not disable_thinking

    # --- API: Anthropicネイティブ ---
    if provider == "api" and api_provider_kind == "anthropic":
        model = settings.get("api_model", "").strip()
        api_key = settings.get("api_key", "").strip()
        if model and api_key:
            max_tokens = _resolve_max_tokens(
                settings.get("api_max_output_tokens", ""),
                ABSOLUTE_FALLBACK_API_MAX_TOKENS,
                use_boost,
            )
            return _get_anthropic_llm(settings, temperature, max_tokens, disable_thinking)
        provider = "local"

    # --- API: OpenAI互換（OpenAI本家/Bedrock Gateway/Gemini/LiteLLM Proxy等） ---
    extra_body = {}
    if disable_thinking:
        extra_body = {
            "chat_template_kwargs": {"enable_thinking": False},
            "reasoning_effort": "none",
        }

    if provider == "api":
        base_url = settings.get("api_base_url", "").strip()
        model = settings.get("api_model", "").strip()
        api_key = settings.get("api_key", "").strip()

        if base_url and model:
            max_tokens = _resolve_max_tokens(
                settings.get("api_max_output_tokens", ""),
                ABSOLUTE_FALLBACK_API_MAX_TOKENS,
                use_boost,
            )
            kwargs = dict(
                model=model,
                base_url=base_url,
                api_key=api_key or "unused",
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if extra_body:
                kwargs["extra_body"] = extra_body
            return ChatOpenAI(**kwargs)
        provider = "local"

    # --- local ---
    max_tokens = _resolve_max_tokens(
        settings.get("local_max_output_tokens", ""),
        ABSOLUTE_FALLBACK_LOCAL_MAX_TOKENS,
        use_boost,
    )
    base_url = settings.get("local_base_url") or FALLBACK_LOCAL_BASE_URL
    model = settings.get("local_model") or FALLBACK_LOCAL_MODEL
    api_key = settings.get("local_api_key") or "lm-studio"

    kwargs = dict(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if extra_body:
        kwargs["extra_body"] = extra_body
    return ChatOpenAI(**kwargs)


def get_current_provider_info() -> dict:
    """現在の設定情報を返す（UI表示・デバッグ用）"""
    settings = _read_settings()
    provider = settings.get("llm_provider", "local")
    api_provider_kind = settings.get("api_provider_kind", "openai_compatible")
    disable_thinking = settings.get("disable_thinking", "true") == "true"

    if provider == "api" and settings.get("api_model"):
        if api_provider_kind == "anthropic" and settings.get("api_key"):
            info = {
                "provider": "api",
                "provider_kind": "anthropic",
                "base_url": "api.anthropic.com（ネイティブ）",
                "model": settings.get("api_model", ""),
            }
        elif settings.get("api_base_url"):
            info = {
                "provider": "api",
                "provider_kind": "openai_compatible",
                "base_url": settings.get("api_base_url", ""),
                "model": settings.get("api_model", ""),
            }
        else:
            info = {
                "provider": "local",
                "provider_kind": "openai_compatible",
                "base_url": settings.get("local_base_url") or FALLBACK_LOCAL_BASE_URL,
                "model": settings.get("local_model") or FALLBACK_LOCAL_MODEL,
            }
    else:
        info = {
            "provider": "local",
            "provider_kind": "openai_compatible",
            "base_url": settings.get("local_base_url") or FALLBACK_LOCAL_BASE_URL,
            "model": settings.get("local_model") or FALLBACK_LOCAL_MODEL,
        }
    info["thinking_disabled"] = disable_thinking
    return info


def extract_finish_reason(response) -> str:
    """LLM応答からfinish_reasonを可能な限り抽出する。取得できなければ空文字。"""
    try:
        meta = getattr(response, "response_metadata", None) or {}
        reason = meta.get("finish_reason") or meta.get("stop_reason")
        if reason:
            # Anthropicは "end_turn"/"max_tokens" 等の独自語彙を使うため、
            # OpenAI語彙の "length" に正規化する
            if reason == "max_tokens":
                return "length"
            return reason
    except Exception:
        pass
    try:
        gen_info = getattr(response, "generation_info", None) or {}
        reason = gen_info.get("finish_reason")
        if reason:
            return reason
    except Exception:
        pass
    return ""


def invoke_with_retry(llm, messages, max_retries: int = 3):
    """
    一時的なサーバーエラー（529 Overloaded, 503 Service Unavailable,
    レート制限等）に対して指数バックオフでリトライする。

    コード側のバグではなく、API側の一時的な過負荷状態への対処。
    リトライ対象外のエラー（認証エラー、不正なリクエスト等）は
    即座に再送出する。
    """
    retryable_markers = (
        "529", "overloaded",
        "503", "service unavailable",
        "429", "rate_limit", "rate limit",
        "502", "504", "timeout",
    )

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return llm.invoke(messages)
        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            is_retryable = any(marker in error_str for marker in retryable_markers)

            if not is_retryable or attempt >= max_retries:
                raise

            wait_time = (2 ** attempt) + random.uniform(0, 1)
            time.sleep(wait_time)

    raise last_error


def invoke_with_continuation(llm, messages, max_continuations: int = 3):
    """
    LLM呼び出しを行い、finish_reason='length'（max_tokens到達で打ち切り）を検知したら、
    自動的に追加リクエストを送り、続きを結合して返す。

    継続リクエストは元のメッセージ履歴を丸ごと再送しない（軽量プロンプトのみ）。
    履歴を毎回再送すると、継続するたびにプロンプト側のトークンが増え、
    残りの完了トークン枠が狭くなる悪循環に陥るため。
    Thinking機能が有効なモデルでは、継続のたびに再び思考が走り枠を圧迫しやすいので、
    この軽量化が特に重要になる。

    本文（content）が完全に空のまま finish_reason='length' で切れるケース
    （Thinkingだけでmax_tokensを使い切り、本文を書き始める前に打ち切られた）
    についても、max_tokens自体は変更しない方針で対応する。
    llmが使えるコンテキスト長・出力上限はユーザーが設定した値のままとし、
    代わりに「直前の思考の続きから検討を再開させる」形で、複数回に分けて
    最終的に本文へたどり着かせる（max_continuations回まで）。

    戻り値: (結合済みcontent: str, 最後のresponseオブジェクト)
    """
    full_content = ""
    last_response = None
    tail_chars = 400  # 直前の出力からどれだけ末尾を継続プロンプトに含めるか

    for i in range(max_continuations + 1):
        if i == 0:
            current_messages = messages
        elif not full_content.strip():
            # 直前の呼び出しが finish_reason='length' で切れたにも
            # かかわらず、本文（content）が完全に空だったケース。
            # Thinkingだけでmax_tokensを使い切り、実際の出力が
            # 1文字も生成されなかった場合に発生する。
            #
            # max_tokensは変更しない（LLMが使えるコンテキスト長は
            # ユーザー設定のまま尊重する）。代わりに、直前の思考の
            # 内容（reasoning_content）を取り出し、その続きから
            # 検討を再開させる。これにより、1回あたりの予算は
            # 変えずに、複数回に分けて最終的に本文へ到達させる。
            reasoning_tail = (
                extract_reasoning_content_text(last_response) if last_response else ""
            )
            if reasoning_tail.strip():
                tail = reasoning_tail.strip()[-tail_chars:]
                current_messages = [
                    {"role": "system", "content": (
                        "あなたは自律型タスク実行エージェントです。"
                        "直前の思考（内部検討）がトークン上限に達し、"
                        "本文を書き始める前に打ち切られました。"
                        "これまでの思考の続きから検討を再開し、"
                        "早めに結論をまとめて、指定された形式"
                        "（THOUGHT/ACTION/DONE等）で本文を出力してください。"
                        "同じ検討を繰り返さず、簡潔に結論へ向かってください。"
                    )},
                    {"role": "user", "content": (
                        f"ここまでの思考の末尾:\n...{tail}\n\n"
                        "この続きから検討を進め、最終的な回答を出力してください。"
                    )},
                ]
            else:
                # reasoning_contentも取得できない場合の最終手段として、
                # 元のリクエストをそのまま再試行する
                current_messages = messages
        else:
            # 2回目以降は軽量プロンプトのみ。元の巨大な履歴は再送しない。
            tail = full_content[-tail_chars:]
            current_messages = [
                {"role": "system", "content": (
                    "あなたは自律型タスク実行エージェントです。"
                    "直前の回答がトークン上限で途中で切れました。"
                    "続きだけを、同じ内容を繰り返さず省略せずに出力してください。"
                    "THOUGHT/ACTION/DONE等の見出しは繰り返さず、本文の続きのみを書いてください。"
                )},
                {"role": "user", "content": (
                    f"ここまでの回答の末尾:\n...{tail}\n\n"
                    "この続きから書いてください。"
                )},
            ]

        response = invoke_with_retry(llm, current_messages)
        last_response = response
        piece = extract_text_content(response)
        full_content += piece

        finish_reason = extract_finish_reason(response)

        if finish_reason != "length":
            break
        if i >= max_continuations:
            break

    return full_content, last_response


def extract_text_content(response) -> str:
    """
    LLM応答からテキスト本文を文字列として取り出す。

    OpenAI互換モデルは response.content が常に文字列だが、
    AnthropicはThinking有効時に response.content が
    [{"type": "thinking", ...}, {"type": "text", "text": "..."}] のような
    ブロックのリストになる。この違いを吸収し、常に文字列を返す。

    thinkingブロックは無視し、textブロックのtextだけを結合する。
    """
    content = getattr(response, "content", None)

    if content is None:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_type = block.get("type")
                if block_type == "text":
                    parts.append(block.get("text", ""))
                # thinking等のtext以外のブロックは本文には含めない
        return "".join(parts)

    # 想定外の型の場合は文字列化して壊れないようにする
    return str(content)


def extract_reasoning_content_text(response) -> str:
    """
    LLM応答から reasoning_content（Thinkingの内容）を生テキストとして取り出す。
    取得できない場合は空文字を返す。

    本文（content）が空のまま finish_reason='length' で切れた場合、
    このテキストを使って「思考の続きから再開させる」継続処理に使う
    （invoke_with_continuation参照）。
    """
    try:
        content = getattr(response, "additional_kwargs", {}).get("reasoning_content", "")
        if isinstance(content, str):
            return content
    except Exception:
        pass
    return ""


def extract_reasoning_tokens(response) -> int:
    """
    LLM応答からreasoning_tokens（Thinkingに消費されたトークン数）を可能な限り抽出する。
    抑制設定に関わらず、実際にThinkingが行われたかを検知するための耐性ロジック用。
    取得できない場合は0を返す（エラーにしない）。
    """
    try:
        meta = getattr(response, "response_metadata", None) or {}
        usage = meta.get("token_usage") or meta.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        val = details.get("reasoning_tokens")
        if isinstance(val, int):
            return val
    except Exception:
        pass

    try:
        usage_meta = getattr(response, "usage_metadata", None) or {}
        output_details = usage_meta.get("output_token_details") or {}
        val = output_details.get("reasoning")
        if isinstance(val, int):
            return val
    except Exception:
        pass

    # Anthropicのthinkingブロックを検知（response.contentがブロックのリストになるケース）
    try:
        content = getattr(response, "content", None)
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "thinking":
                    return 1
    except Exception:
        pass

    # reasoning_content フィールドが非空なら、トークン数は取れなくても
    # 「Thinkingが行われた」事実だけは検知できるようにする
    try:
        content = getattr(response, "additional_kwargs", {}).get("reasoning_content", "")
        if content and content.strip():
            return 1  # 数値は不明だが検知フラグとして1を返す
    except Exception:
        pass

    return 0