"""支持 python -m eval.runner 方式运行评测。

在所有业务 import 之前限制底层 ML 库的线程数，防止 torch/BLAS/tokenizers
各自开满 CPU 核数导致线程爆炸（eval 并发 × 模型推理 × 满核并行 → CPU 500%+）。
"""

import os

# 必须在 import torch / transformers / FlagEmbedding 之前设置，否则不生效
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")

# torch.set_num_threads 需要 import 后调用，放在 main() 入口最前面
from eval.runner import main as _main  # noqa: E402


def main() -> None:
    try:
        import torch
        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)
    except ImportError:
        pass
    _main()


if __name__ == "__main__":
    main()
