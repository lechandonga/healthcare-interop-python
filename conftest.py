"""确保项目根目录在 ``sys.path`` 中（与安装方式无关）。"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
