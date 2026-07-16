# data/logs/real/ — 层 1：真实公开数据集

## 数据来源

本目录存放从 **LogHub**（CUHK 维护，AIOps 学界事实标准）下载的真实生产环境日志。

- 项目主页：<https://github.com/logpai/loghub>
- 论文引用：
  - Zhu et al. *Tools and Benchmarks for Automated Log Parsing*, ICSE-SEIP 2019
  - He et al. *Loghub: A Large Collection of System Log Datasets for AI-driven Log Analytics*, ISSRE 2023

## 已使用的子集

| 数据集 | 来源系统 | 规模 | 用途 |
|---|---|---|---|
| OpenStack | 真实 OpenStack 集群事件日志 | ~200K 行 | 运维场景检索底座 |
| Zookeeper | 真实 ZooKeeper 集群日志 | ~75K 行 | 分布式协调日志样本 |
| Spark | Spark 任务执行日志 | ~33K 行 | 大数据计算场景 |

## 如何下载

```bash
# 用项目内的下载脚本
uv run python data/synthesizer/download_loghub.py --datasets OpenStack,Zookeeper,Spark
```

## 注意

- 本目录已在 `.gitignore` 中——不提交到仓库
- 仅本文件入库，作为数据来源声明
- LogHub 数据集为 **CC BY 4.0** 许可，可以学术 + 商业使用，**引用论文即可**
