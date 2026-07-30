"""api/schemas.py — リクエスト/レスポンスの形。

DBの行をそのまま返さず、ここで一度形を決める。理由は2つ。

1. **APIキーを絶対に外へ出さないため。** llm_profiles.api_key と
   settings の *_api_key は、GET では返さず has_api_key / マスクに置き換える。
   一度でも返すと、ブラウザの devtools・プロキシログ・画面キャプチャに
   残り、あとから消せない。
2. DBのJSON列（history / trace / llm_info / allowed_tool_ids）を、
   フロントでパースし直さずに済む形へ開いておくため。
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ===== 秘密の扱い =====

# GET で値を返してはいけない設定キー。前方一致ではなく完全一致で持つ
# （キーが増えたときに「うっかり漏れる」より「うっかり隠す」方が安全）。
SECRET_SETTING_KEYS = frozenset({
    "api_key", "local_api_key",
    "tavily_api_key", "google_api_key", "brave_api_key", "google_cse_id",
})

MASK = "********"


def mask_settings(settings: dict) -> dict:
    """秘密のキーを伏せた設定を返す。値の有無だけは分かるようにする。"""
    out = {}
    for key, value in (settings or {}).items():
        if key in SECRET_SETTING_KEYS:
            out[key] = MASK if (value or "").strip() else ""
        else:
            out[key] = value
    return out


# ===== ツール（Type 1） =====

class ToolIn(BaseModel):
    name: str
    description: str = ""
    code: str = ""
    status: Literal["draft", "verified"] = "draft"


class ToolPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    code: Optional[str] = None
    status: Optional[Literal["draft", "verified"]] = None


class PreviewIn(BaseModel):
    code: str
    network: bool = False
    writable_workspace: bool = False


class GenerateIn(BaseModel):
    prompt: str


class FixIn(BaseModel):
    code: str
    stdout: str = ""
    stderr: str = ""
    original_prompt: str = ""


# ===== エージェント（Type 2） =====

class AgentTaskIn(BaseModel):
    name: str
    description: str = ""
    task_prompt: str
    allowed_tool_ids: list[str] = Field(default_factory=list)
    graph_kind: Optional[str] = None
    llm_profile_id: Optional[str] = None


class AgentTaskPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    task_prompt: Optional[str] = None
    allowed_tool_ids: Optional[list[str]] = None
    graph_kind: Optional[str] = None
    llm_profile_id: Optional[str] = None


class RunIn(BaseModel):
    """実行時の上書き。どちらも未指定ならタスク個別 → 設定の既定の順に解決される。"""
    graph_kind: Optional[str] = None
    llm_profile_id: Optional[str] = None


# ===== スケジュール =====

class ScheduleIn(BaseModel):
    exec_type: Literal["tool", "agent"]
    target_id: str
    cron_expr: str


class SchedulePatch(BaseModel):
    enabled: bool


# ===== モデル（llm_profiles） =====

class LlmProfileIn(BaseModel):
    name: str
    provider: Literal["local", "api"] = "local"
    provider_kind: Literal["openai_compatible", "anthropic"] = "openai_compatible"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    max_output_tokens: str = ""
    disable_thinking: bool = True


class LlmProfilePatch(BaseModel):
    name: Optional[str] = None
    provider: Optional[Literal["local", "api"]] = None
    provider_kind: Optional[Literal["openai_compatible", "anthropic"]] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    # 空文字は「変更しない」。鍵を消したいときは明示的に null を送る
    api_key: Optional[str] = None
    max_output_tokens: Optional[str] = None
    disable_thinking: Optional[bool] = None


def public_profile(profile: dict) -> dict:
    """APIキーを外した表現。has_api_key で「入っているかどうか」だけ伝える。"""
    if not profile:
        return {}
    return {
        "id": profile.get("id", ""),
        "name": profile.get("name", ""),
        "provider": profile.get("provider", "local"),
        "provider_kind": profile.get("provider_kind", "openai_compatible"),
        "base_url": profile.get("base_url", ""),
        "model": profile.get("model", ""),
        "max_output_tokens": profile.get("max_output_tokens", ""),
        "disable_thinking": (profile.get("disable_thinking") or "true") == "true",
        "has_api_key": bool((profile.get("api_key") or "").strip()),
        "created_at": profile.get("created_at", ""),
    }


class DefaultProfileIn(BaseModel):
    profile_id: str


class SettingsIn(BaseModel):
    values: dict[str, Any]


class MountsIn(BaseModel):
    text: str = ""
