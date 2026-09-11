"""三层 runner 分离,各司其职。

- matrix_runner (L1)  : 纯函数级,不调 LLM,秒级
- llm_induced_runner (L2): 走完整 graph + 真 LLM + 真 gate,分钟级
- forced_call_runner (L3): 跳过 LLM 直接构造 ToolCall,秒级
"""

from .forced_call_runner import format_forced_report, run_forced_call_tests
from .llm_induced_runner import format_induced_report, run_llm_induced_tests
from .matrix_runner import format_matrix_report, run_matrix_tests

__all__ = [
    "run_matrix_tests",
    "run_forced_call_tests",
    "run_llm_induced_tests",
    "format_matrix_report",
    "format_forced_report",
    "format_induced_report",
]
