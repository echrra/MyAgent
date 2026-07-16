"""LogHub 公开数据集下载脚本（数据策略层 1）。

设计:
- 默认拉 LogHub 的 2k 标注样本（github raw，~MB 级，无需登录）—— 足以做"真实日志锚点 + few-shot 范例"
- 数据集来源：https://github.com/logpai/loghub  论文：Zhu et al, ICSE-SEIP 2019
- 文件落到 `data/logs/real/<dataset>/`，结构化 CSV + 原始 .log 都拉

用法:
    uv run python data/synthesizer/download_loghub.py
    uv run python data/synthesizer/download_loghub.py --datasets OpenStack,Zookeeper
    uv run python data/synthesizer/download_loghub.py --force   # 覆盖重下

注意:
- 2k 标注样本是子集；如需百万级全量，需自行去 Zenodo 下载完整 zip 后解压到 real/<dataset>/
"""

import argparse
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REAL_LOG_DIR = PROJECT_ROOT / "data" / "logs" / "real"

# LogHub github raw 主干（master 分支稳定）
_GITHUB_BASE = "https://raw.githubusercontent.com/logpai/loghub/master"

# 已知可用数据集 → 每个数据集要下的文件列表
# 选择 _2k.log（原始）+ _2k.log_structured.csv（已解析），同时拿到原始文本和模板信号
DATASETS: dict[str, list[str]] = {
    "OpenStack": [
        "OpenStack/OpenStack_2k.log",
        "OpenStack/OpenStack_2k.log_structured.csv",
        "OpenStack/OpenStack_2k.log_templates.csv",
    ],
    "Zookeeper": [
        "Zookeeper/Zookeeper_2k.log",
        "Zookeeper/Zookeeper_2k.log_structured.csv",
        "Zookeeper/Zookeeper_2k.log_templates.csv",
    ],
    "Spark": [
        "Spark/Spark_2k.log",
        "Spark/Spark_2k.log_structured.csv",
        "Spark/Spark_2k.log_templates.csv",
    ],
    "HDFS": [
        "HDFS/HDFS_2k.log",
        "HDFS/HDFS_2k.log_structured.csv",
        "HDFS/HDFS_2k.log_templates.csv",
    ],
    "Apache": [
        "Apache/Apache_2k.log",
        "Apache/Apache_2k.log_structured.csv",
        "Apache/Apache_2k.log_templates.csv",
    ],
}


def _download_one(url: str, out_path: Path, force: bool = False) -> tuple[bool, str]:
    """下载单个文件，返回 (是否真的下载了, 消息)。"""
    if out_path.exists() and not force:
        return False, f"已存在，跳过 → {out_path.name}"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "opsagent-loghub-fetcher"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} {exc.reason} → {url}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc} → {url}"

    out_path.write_bytes(data)
    return True, f"✓ {out_path.name} ({len(data):,} bytes)"


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as f:
        return sum(1 for _ in f)


def _summary(dataset: str) -> str:
    """对已下载的数据集做一次完整性 + 行数检查。"""
    base = REAL_LOG_DIR / dataset
    log_file = base / f"{dataset}_2k.log"
    if not log_file.exists():
        return f"   ⚠️ 主文件缺失 {log_file.name}"
    return f"   📊 {dataset}_2k.log: {_count_lines(log_file)} 行"


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 LogHub 公开真实日志数据集")
    parser.add_argument(
        "--datasets",
        default="OpenStack,Zookeeper,Spark",
        help="逗号分隔的数据集名（可选: " + ", ".join(DATASETS.keys()) + "）",
    )
    parser.add_argument("--force", action="store_true", help="已存在文件也覆盖重下")
    args = parser.parse_args()

    requested = [d.strip() for d in args.datasets.split(",") if d.strip()]
    unknown = [d for d in requested if d not in DATASETS]
    if unknown:
        print(f"❌ 未知数据集: {unknown}\n   可选: {list(DATASETS.keys())}")
        return 2

    print("=" * 70)
    print(f"LogHub 数据集下载  → {REAL_LOG_DIR}")
    print("=" * 70)
    started = time.perf_counter()
    n_ok = n_skip = n_fail = 0

    for ds in requested:
        print(f"\n▶ {ds}")
        for relpath in DATASETS[ds]:
            url = f"{_GITHUB_BASE}/{relpath}"
            out_path = REAL_LOG_DIR / ds / Path(relpath).name
            downloaded, msg = _download_one(url, out_path, force=args.force)
            print(f"   {msg}")
            if "✓" in msg:
                n_ok += 1
            elif "已存在" in msg:
                n_skip += 1
            else:
                n_fail += 1
        print(_summary(ds))

    elapsed = time.perf_counter() - started
    print("\n" + "=" * 70)
    print(f"完成: 新下载 {n_ok} / 跳过 {n_skip} / 失败 {n_fail}   耗时 {elapsed:.1f}s")
    if n_fail > 0:
        print("⚠️  有文件失败，可能 github 网络问题，重跑或加 --force")
        return 1
    print("🎉 全部就绪。下一步: 用 data/synthesizer/schema_from_seeds.py 抽 schema")
    return 0


if __name__ == "__main__":
    sys.exit(main())
