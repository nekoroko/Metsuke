"""api/routes_config.py — モデル（llm_profiles）と設定。

**APIキーはGETで返さない。** 返すのは has_api_key（入っているかどうか）だけ。
一度でも返すと devtools・プロキシログ・画面キャプチャに残り、あとから
消せない。設定側の *_api_key も同じ理由でマスクする。
"""

import time

from fastapi import APIRouter, HTTPException

import config
import db
import graphs
import llm_profiles
from api.schemas import (
    DefaultProfileIn, LlmProfileIn, LlmProfilePatch, MountsIn, SECRET_SETTING_KEYS,
    SettingsIn, mask_settings, public_profile,
)

router = APIRouter()


# ===== メタ情報（起動時に1回だけ読む） =====

@router.get("/meta")
def meta():
    info = config.get_current_provider_info()
    settings = db.get_all_settings()
    # image_status() が返すのは state（ready / stale / missing / user_managed）。
    # 以前ここで存在しないキー exists を見ていたため、構築済みでも常に
    # 「未構築」と表示されていた。
    sandbox_state = "unavailable"
    try:
        from sandbox import image_status
        sandbox_state = image_status().get("state", "unavailable")
    except Exception:
        pass                     # podman が無い環境でも画面は出す
    podman_ok = sandbox_state in ("ready", "user_managed")
    return {
        "provider_label": f"{info.get('profile_name', '')} / {info.get('model', '')}",
        "default_profile": public_profile(llm_profiles.resolve_profile() or {}),
        "graph_kinds": [
            {"value": kind, "label": graphs.GRAPH_LABELS[kind],
             "description": graphs.GRAPH_DESCRIPTIONS[kind]}
            for kind in (graphs.REACT, graphs.RESEARCH)
        ],
        "default_graph_kind": graphs.default_kind(),
        "search_provider": settings.get("search_provider", "duckduckgo"),
        "podman_ok": podman_ok,
        "sandbox_state": sandbox_state,
    }


# ===== モデル =====

@router.get("/llm-profiles")
def list_profiles():
    default_id = llm_profiles.default_profile_id()
    out = []
    for p in db.get_llm_profiles():
        item = public_profile(p)
        item["is_default"] = p["id"] == default_id
        out.append(item)
    return out


@router.post("/llm-profiles", status_code=201)
def create_profile(body: LlmProfileIn):
    profile_id = db.add_llm_profile(
        body.name, provider=body.provider, provider_kind=body.provider_kind,
        base_url=body.base_url, model=body.model, api_key=body.api_key,
        max_output_tokens=body.max_output_tokens,
        disable_thinking="true" if body.disable_thinking else "false",
    )
    return public_profile(db.get_llm_profile(profile_id))


@router.patch("/llm-profiles/{profile_id}")
def patch_profile(profile_id: str, body: LlmProfilePatch):
    if not db.get_llm_profile(profile_id):
        raise HTTPException(404, f"プロファイルが見つかりません: {profile_id}")
    fields = body.model_dump(exclude_unset=True)
    # 空文字の api_key は「変更しない」。画面は鍵を持っていないので、
    # 入力欄が空のまま保存されると既存の鍵が消えてしまう
    if "api_key" in fields and not (fields["api_key"] or "").strip():
        fields.pop("api_key")
    if "disable_thinking" in fields:
        fields["disable_thinking"] = "true" if fields["disable_thinking"] else "false"
    if fields:
        db.update_llm_profile(profile_id, **fields)
    return public_profile(db.get_llm_profile(profile_id))


@router.delete("/llm-profiles/{profile_id}", status_code=204)
def remove_profile(profile_id: str):
    if not db.get_llm_profile(profile_id):
        raise HTTPException(404, f"プロファイルが見つかりません: {profile_id}")
    if not db.delete_llm_profile(profile_id):
        # 0件になると実行時に選ぶものが無くなる（db 側の仕様）
        raise HTTPException(409, "最後の1件は削除できません")


@router.post("/llm-profiles/{profile_id}/test")
def test_profile(profile_id: str):
    """
    接続テスト。max_output_tokens の自動取得 → 実際に1回呼ぶ、の順。

    自動取得は失敗しても致命ではない（多くのプロバイダが対応していない）。
    実際に呼べたかどうかが本命なので、そちらの結果を ok に載せる。
    """
    profile = db.get_llm_profile(profile_id)
    if not profile:
        raise HTTPException(404, f"プロファイルが見つかりません: {profile_id}")

    max_tokens = None
    try:
        if (profile.get("provider") == "api"
                and profile.get("provider_kind") == "anthropic"):
            max_tokens = config.fetch_anthropic_max_tokens(
                (profile.get("api_key") or "").strip(),
                (profile.get("model") or "").strip())
        else:
            max_tokens = config.fetch_openai_compatible_max_tokens(
                (profile.get("base_url") or "").strip(),
                (profile.get("model") or "").strip(),
                (profile.get("api_key") or "").strip())
    except Exception:
        max_tokens = None

    started = time.time()
    try:
        with config.use_profile(profile):
            llm = config.get_llm(temperature=0.0)
            resp = llm.invoke([{"role": "user",
                                "content": "テスト。'OK'とだけ返してください。"}])
        return {
            "ok": True,
            "max_tokens": max_tokens,
            "latency_ms": int((time.time() - started) * 1000),
            "sample": str(getattr(resp, "content", ""))[:100],
        }
    except Exception as e:
        return {
            "ok": False,
            "max_tokens": max_tokens,
            "latency_ms": int((time.time() - started) * 1000),
            "error": f"{type(e).__name__}: {e}",
        }


@router.put("/settings/default-profile")
def set_default_profile(body: DefaultProfileIn):
    if not db.get_llm_profile(body.profile_id):
        raise HTTPException(404, f"プロファイルが見つかりません: {body.profile_id}")
    db.set_setting(llm_profiles.SETTING_KEY, body.profile_id)
    return {"profile_id": body.profile_id}


# ===== 設定 =====

@router.post("/settings/check-mounts")
def check_mounts(body: MountsIn):
    """
    追加マウントの下見。設定DBが見える行は却下し、その理由を返す。

    黙って落とすと「書いたのにマウントされない」という分かりにくい
    状態になるので、保存前に画面へ理由を出せるようにしておく。
    パスの解決はサーバ側でしかできない（realpath が要る）。
    """
    from sandbox import check_mounts as _check
    return _check(body.text)


@router.get("/settings")
def get_settings():
    return mask_settings(db.get_all_settings())


@router.put("/settings")
def put_settings(body: SettingsIn):
    """
    設定を更新する。マスク値をそのまま送り返してきた場合は無視する。

    画面は秘密の実値を持っていない。フォームを素直に往復させると
    「********」という文字列で鍵が上書きされる。
    """
    from api.schemas import MASK
    values = {}
    for key, value in (body.values or {}).items():
        if key in SECRET_SETTING_KEYS and value == MASK:
            continue
        values[key] = "" if value is None else str(value)
    if values:
        db.set_settings(values)
    return mask_settings(db.get_all_settings())
