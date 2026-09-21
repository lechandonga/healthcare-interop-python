import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


def log_case(logger: logging.Logger, case: str, sample, expect: str) -> None:
    """单测日志：打印输入样本与判定依据。"""
    logger.info("CASE %s | 输入样本=%r | 判定依据=%s", case, sample, expect)
