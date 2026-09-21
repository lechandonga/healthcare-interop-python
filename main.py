"""服务入口：uv run uvicorn main:app --reload"""

from interop.api import app

__all__ = ["app"]
