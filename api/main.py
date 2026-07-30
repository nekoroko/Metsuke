"""api/main.py — React Web UI を配る API サーバ。

既存の Python ロジック（db / executor / scheduler / graphs / llm_profiles）は
そのまま使い、ここは HTTP の口を足すだけにする。UI を差し替えても
エージェントの挙動が変わらないようにするため。

起動:
    uvicorn api.main:app --reload --port 8000     # 開発（Vite は別ポート）
    python -m api                                 # 本番相当（web/dist を配る）

Streamlit 版は app_streamlit.py として残してある。同じDBを見るので、
移行中は両方立ち上げて挙動を比べられる。
"""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from db import init_db
from api.routes_agents import router as agents_router
from api.routes_config import router as config_router
from api.routes_library import router as library_router

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_DIR = os.path.join(BASE_DIR, "web", "dist")

app = FastAPI(title="AI Agent Studio API", version="1.0")

# 開発時は Vite dev server（5173）から叩く。本番は同一オリジンなので
# この設定は効かない。許可先を絞っているのは、ローカルの別プロセスから
# APIキー操作を叩かれないようにするため。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(library_router, prefix="/api")
app.include_router(agents_router, prefix="/api")
app.include_router(config_router, prefix="/api")


@app.on_event("startup")
def _startup():
    """DBの初期化とスケジューラの起動。Streamlit 版の app.py と同じ順序。"""
    init_db()
    try:
        import scheduler
        scheduler.start()
        scheduler.load_schedules()
    except Exception as e:      # スケジューラが落ちてもAPIは使えるようにする
        print(f"[api] スケジューラを起動できませんでした: {e}")


@app.get("/api/health")
def health():
    return {"ok": True}


# --- 静的配信（vite build の成果物） ---
#
# ビルド前でもAPIだけで起動できるようにする。dist が無いときに
# StaticFiles をマウントすると起動時に落ちるため、存在確認してから。
if os.path.isdir(DIST_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(DIST_DIR, "assets")),
              name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        """
        SPA のフォールバック。/runs/xxx を直接開いてもリロードできるよう、
        API 以外のパスはすべて index.html を返す。
        """
        candidate = os.path.join(DIST_DIR, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(DIST_DIR, "index.html"))
