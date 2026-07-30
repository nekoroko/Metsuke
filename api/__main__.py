"""python -m api で本番相当の起動（web/dist を配る1プロセス）。"""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", "8000")), reload=False)
