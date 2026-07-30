# llm_profiles.py — LLM接続設定を「プロファイル」として複数持つ
#
# 以前は settings テーブルの local_* / api_* を直接見ていたため、接続先は
# 常に1つだけだった。モデルを変えて試すには設定を上書きするしかなく、
# 前の設定は失われる。実測の比較（gemma-4-12b-qat と qwen3.5-4b など）を
# やるには、保存しておいて実行時に選べる必要がある。
#
# 役割は graphs.py と対になっている。graphs.py が「どのグラフで走らせるか」を
# 決めるのと同じ形で、ここは「どのモデルで走らせるか」を決める。
# 優先順位も同じ: 実行時の指定 > タスク個別 > 設定画面の既定。
#
# 書き込みは db.py 側にある（settings_store と同じ切り分け。config.py や
# graph.py 側から db.py を import すると init_db() の副作用が走る）。

from settings_store import read_llm_profile, read_llm_profiles, read_settings

# 設定画面で選んだ既定プロファイルのID
SETTING_KEY = "default_llm_profile_id"

# タスク個別・実行時指定で「既定に従う」を表す値。graphs.py と同じ綴り。
DEFAULT = "default"

PROVIDER_LOCAL = "local"
PROVIDER_API = "api"

KIND_OPENAI = "openai_compatible"
KIND_ANTHROPIC = "anthropic"

PROVIDER_LABELS = {
    PROVIDER_LOCAL: "ローカル",
    PROVIDER_API: "API",
}

KIND_LABELS = {
    KIND_OPENAI: "OpenAI互換",
    KIND_ANTHROPIC: "Anthropic",
}


def profile_label(profile: dict) -> str:
    """一覧・バッジ用の短い表示名。"""
    if not profile:
        return ""
    name = (profile.get("name") or "").strip() or "(名前なし)"
    model = (profile.get("model") or "").strip()
    if not model or model == name:
        return name
    return f"{name}（{model}）"


def profile_summary(profile: dict) -> str:
    """プロバイダ種別まで含めた1行の説明。APIキーは出さない。"""
    if not profile:
        return ""
    provider = PROVIDER_LABELS.get(profile.get("provider"), profile.get("provider") or "")
    kind = KIND_LABELS.get(profile.get("provider_kind"), "")
    parts = [p for p in (provider, kind) if p]
    base = (profile.get("base_url") or "").strip()
    if base:
        parts.append(base)
    return " / ".join(parts)


def profiles() -> list[dict]:
    return read_llm_profiles()


def default_profile_id() -> str:
    """設定画面で選んだ既定プロファイルのID。無効なら先頭にフォールバック。"""
    wanted = (read_settings().get(SETTING_KEY) or "").strip()
    known = {p["id"] for p in profiles()}
    if wanted and wanted in known:
        return wanted
    # 既定が消されている場合に実行が止まらないよう、あるものを使う
    available = profiles()
    return available[0]["id"] if available else ""


def resolve_profile_id(task_id: str = None, override: str = None) -> str:
    """
    使用するプロファイルのIDを決める。優先順位は override > タスク個別 > 既定。

    graphs.resolve_kind と同じ形。タスク個別の指定が空文字・NULL・"default"
    のときは既定に従う。存在しないIDを指していた場合も既定に落とす
    （プロファイルを削除した後にタスク側の指定が残るため）。
    """
    known = {p["id"] for p in profiles()}
    if override and override != DEFAULT and override in known:
        return override
    if task_id:
        try:
            from db import get_agent_task
            task = get_agent_task(task_id)
        except Exception:
            task = None
        if task:
            per_task = (task.get("llm_profile_id") or "").strip()
            if per_task and per_task != DEFAULT and per_task in known:
                return per_task
    return default_profile_id()


def resolve_profile(task_id: str = None, override: str = None) -> dict | None:
    pid = resolve_profile_id(task_id, override)
    return read_llm_profile(pid) if pid else None


def profile_settings(profile: dict) -> dict:
    """
    プロファイルを、従来の settings キーの形に展開する。

    config.get_llm() の分岐（provider / provider_kind ごとの読み分け）を
    そのまま使い回すため、専用の経路を作らずキー名を合わせる。
    プロファイルを増やしても get_llm 側は変わらない。
    """
    if not profile:
        return {}
    provider = (profile.get("provider") or PROVIDER_LOCAL).strip() or PROVIDER_LOCAL
    kind = (profile.get("provider_kind") or KIND_OPENAI).strip() or KIND_OPENAI
    out = {
        "llm_provider": provider,
        "api_provider_kind": kind,
        "disable_thinking": profile.get("disable_thinking") or "true",
    }
    tokens = str(profile.get("max_output_tokens") or "").strip()
    if provider == PROVIDER_API:
        out["api_base_url"] = (profile.get("base_url") or "").strip()
        out["api_model"] = (profile.get("model") or "").strip()
        out["api_key"] = (profile.get("api_key") or "").strip()
        out["api_max_output_tokens"] = tokens
    else:
        out["local_base_url"] = (profile.get("base_url") or "").strip()
        out["local_model"] = (profile.get("model") or "").strip()
        out["local_api_key"] = (profile.get("api_key") or "").strip()
        out["local_max_output_tokens"] = tokens
    return out


# 実行履歴に残すフィールド。**api_key は絶対に含めない。**
#
# 履歴は「まとめてコピー」でそのまま外へ貼られる前提のテキストになる。
# 鍵が1度でも混ざると、貼った先すべてから消さなければならない。
SNAPSHOT_FIELDS = ("name", "provider", "provider_kind", "model", "base_url",
                   "max_output_tokens", "disable_thinking")


def snapshot(profile: dict) -> dict:
    """
    実行時に使ったモデルを履歴へ残すための写し。

    IDだけを記録すると、後でプロファイルを編集・削除したときに
    「何で実行したか」が分からなくなる。値をその場で写して持つ。
    """
    if not profile:
        return {}
    out = {k: profile.get(k) or "" for k in SNAPSHOT_FIELDS}
    out["id"] = profile.get("id") or ""
    return out


def snapshot_label(info: dict) -> str:
    """履歴の写しから、表示・コピー用の1行を作る。"""
    if not info:
        return ""
    head = profile_label(info)
    tail = profile_summary(info)
    return f"{head} / {tail}" if head and tail else (head or tail)
