"""api/routes_library.py — ツール（Type 1）とAI生成。

既存の db / executor / ai_creator を薄く包むだけで、ロジックはここに置かない。
Streamlit 版と挙動がずれると、移行中に「どちらが正しいか」を判断できなくなる。
"""

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

import db
import executor
from api.events import generator_stream
from api.schemas import FixIn, GenerateIn, PreviewIn, ToolIn, ToolPatch

router = APIRouter()


@router.get("/tools")
def list_tools():
    return db.get_all_tools()


@router.get("/tools/{tool_id}")
def get_tool(tool_id: str):
    tool = db.get_tool(tool_id)
    if not tool:
        raise HTTPException(404, f"ツールが見つかりません: {tool_id}")
    return tool


@router.post("/tools", status_code=201)
def create_tool(body: ToolIn):
    tool_id = db.add_tool(body.name, body.description, body.code, body.status)
    return {"id": tool_id}


@router.patch("/tools/{tool_id}")
def patch_tool(tool_id: str, body: ToolPatch):
    if not db.get_tool(tool_id):
        raise HTTPException(404, f"ツールが見つかりません: {tool_id}")
    fields = body.model_dump(exclude_none=True)
    if fields:
        db.update_tool(tool_id, **fields)
    return db.get_tool(tool_id)


@router.delete("/tools/{tool_id}", status_code=204)
def remove_tool(tool_id: str):
    if not db.get_tool(tool_id):
        raise HTTPException(404, f"ツールが見つかりません: {tool_id}")
    db.delete_tool(tool_id)


@router.post("/tools/{tool_id}/run")
def run_tool(tool_id: str):
    tool = db.get_tool(tool_id)
    if not tool:
        raise HTTPException(404, f"ツールが見つかりません: {tool_id}")
    return executor.run_tool(tool["id"], tool["name"], tool["code"])


@router.post("/preview")
def preview(body: PreviewIn):
    """サンドボックスで1回だけ動かす。保存はしない。"""
    return executor.run_preview(
        body.code, network=body.network,
        writable_workspace=body.writable_workspace,
    )


@router.post("/tools/generate")
def generate(body: GenerateIn):
    import ai_creator
    return EventSourceResponse(
        generator_stream(ai_creator.generate_tool(body.prompt)))


@router.post("/tools/fix")
def fix(body: FixIn):
    import ai_creator
    return EventSourceResponse(generator_stream(ai_creator.fix_code(
        body.code, body.stdout, body.stderr, body.original_prompt)))
