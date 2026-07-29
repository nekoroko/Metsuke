# tests/stubs.py — LLM関連の依存をテスト用に差し替える
#
# graph.py / ui は langchain_openai と langgraph を import するが、
# ここで検証したいのはLLMを呼ばない部分だけなので、形だけのモジュールを
# sys.modules に置いて import を通す。既にインストール済みの環境では
# 何もしない（本物を使う）。

import sys
import types


def install_llm_stubs():
    if "langchain_openai" not in sys.modules:
        try:
            import langchain_openai  # noqa: F401
        except ImportError:
            m = types.ModuleType("langchain_openai")
            m.ChatOpenAI = object
            sys.modules["langchain_openai"] = m

    if "langgraph.graph" not in sys.modules:
        try:
            import langgraph.graph  # noqa: F401
        except ImportError:
            lg = types.ModuleType("langgraph")
            lgg = types.ModuleType("langgraph.graph")

            class _StateGraph:
                def __init__(self, *a, **k): pass
                def add_node(self, *a, **k): pass
                def add_edge(self, *a, **k): pass
                def set_entry_point(self, *a, **k): pass
                def add_conditional_edges(self, *a, **k): pass
                def compile(self, *a, **k): return object()

            lgg.StateGraph = _StateGraph
            lgg.END = "END"
            lg.graph = lgg
            sys.modules["langgraph"] = lg
            sys.modules["langgraph.graph"] = lgg
