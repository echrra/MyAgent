"""eval/dataset 加载测试 —— 验证 YAML 可解析、字段完整、分布合理。

断言口径：数量 / 编号 / 分布类断言写成**契约**而非**快照**。原先硬编码「50 条 /
E001-E050 连续 / 10 类各 5 条 / 10-30-10 难度」，每次扩评测集都会红一次；真正要守的
性质是「不退化、格式合法、无孤例」——扩集不该改测试，误删才该报警。
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from eval.dataset import CASES_DIR, load_cases, load_quick_subset

# 全量加载一次供多个测试共享
ALL_CASES = load_cases(CASES_DIR)
# 单轮 case（E 前缀；编号不连续 —— F11-F13 的 case 从 E056 起编，E051-E055 留空）
SINGLE_TURN_CASES = [c for c in ALL_CASES if c.get("type") != "multi_turn"]
# 多轮 case（M 前缀）
MULTI_TURN_CASES = [c for c in ALL_CASES if c.get("type") == "multi_turn"]

# 单轮 case 必需字段
REQUIRED_FIELDS_SINGLE = {
    "id",
    "fault_pattern",
    "difficulty",
    "query",
    "user_profile",
    "expected_tool_sequence",
    "expected_citations",
    "expected_keywords",
    "expected_keywords_min",
    "forbidden_keywords",
}

# 多轮 case 必需字段
REQUIRED_FIELDS_MULTI = {
    "id",
    "fault_pattern",
    "difficulty",
    "type",
    "turns",
    "user_profile",
    "expected_tool_sequence",
    "expected_citations",
    "expected_keywords",
    "expected_keywords_min",
    "forbidden_keywords",
}


class TestLoadAll:
    """单轮 case 全量验证。"""

    def test_count_not_shrinking(self):
        """数量不退化，且加载结果与 cases 目录里的 E*.yaml 一一对应。

        取代原先的 `== 50`：下限守「误删 / 漏加载」，集合比对守「文件与 id 不一致」，
        两者都不会因为扩集而失效。
        """
        yaml_ids = {p.stem for p in Path(CASES_DIR).glob("E*.yaml")}
        assert len(SINGLE_TURN_CASES) >= 50, "单轮 case 数量低于历史基线，疑似误删"
        assert {c["id"] for c in SINGLE_TURN_CASES} == yaml_ids, "case id 与文件名不一致"

    def test_ids_unique(self):
        """ID 不重复。"""
        ids = [c["id"] for c in ALL_CASES]
        assert len(ids) == len(set(ids))

    def test_ids_format(self):
        """单轮 case ID 形如 E001。

        不断言编号连续 —— E051-E055 是留空的（F11-F13 从 E056 起编），
        「格式合法 + 唯一 + 与文件名一致」才是契约。
        """
        for case in SINGLE_TURN_CASES:
            assert re.fullmatch(r"E\d{3}", case["id"]), f"非法单轮 ID: {case['id']}"

    def test_required_fields(self):
        """每条单轮 case 含必需字段。"""
        for case in SINGLE_TURN_CASES:
            missing = REQUIRED_FIELDS_SINGLE - set(case.keys())
            assert not missing, f"{case['id']} 缺少字段: {missing}"

    def test_difficulty_values(self):
        """difficulty 只有 easy/medium/hard。"""
        for case in ALL_CASES:
            assert case["difficulty"] in ("easy", "medium", "hard"), (
                f"{case['id']} 非法 difficulty: {case['difficulty']}"
            )

    def test_difficulty_distribution(self):
        """三档难度都有足量样本，且没有单档独大。

        取代原先的 `10 / 30 / 10`：精确条数是快照，扩集必失效。要守的是「三档都在、
        分布不退化成单一难度」——否则评测会失去难度梯度的区分力。
        """
        cnt = Counter(c["difficulty"] for c in SINGLE_TURN_CASES)
        total = len(SINGLE_TURN_CASES)
        for level in ("easy", "medium", "hard"):
            assert cnt[level] >= 5, f"{level} 仅 {cnt[level]} 条，样本不足"
        assert max(cnt.values()) <= total * 0.7, f"单一难度占比过高: {dict(cnt)}"

    def test_fault_pattern_coverage(self):
        """故障类型覆盖不退化，且每类不是孤例。

        取代原先的 `10 类各 5 条`：F11-F13 接入后是 13 类、新类各 2 条。
        每类 ≥2 是为了让「跨 case 一致低分」能与「单例波动」区分开。
        """
        cnt = Counter(c["fault_pattern"] for c in SINGLE_TURN_CASES)
        assert len(cnt) >= 10, f"故障类型覆盖退化，仅 {len(cnt)} 类"
        for pattern, count in cnt.items():
            assert pattern, "存在空 fault_pattern"
            assert count >= 2, f"{pattern} 只有 {count} 条，孤例无法评估稳定性"

    def test_query_not_empty(self):
        """单轮 case query 非空字符串。"""
        for case in SINGLE_TURN_CASES:
            assert isinstance(case["query"], str) and len(case["query"]) > 5, (
                f"{case['id']} query 过短或为空"
            )

    def test_expected_tool_sequence_format(self):
        """expected_tool_sequence 是 list[dict]，每项有 tool 或 tools 字段。"""
        for case in ALL_CASES:
            seq = case["expected_tool_sequence"]
            assert isinstance(seq, list)
            for item in seq:
                has_tool = "tool" in item or "tools" in item
                assert has_tool, f"{case['id']} 工具序列缺少 tool/tools 字段"
                # tools 字段必须是非空列表
                if "tools" in item:
                    assert isinstance(item["tools"], list) and len(item["tools"]) > 0, (
                        f"{case['id']} tools 字段应为非空列表"
                    )

    def test_keywords_min_reasonable(self):
        """expected_keywords_min <= len(expected_keywords)。"""
        for case in ALL_CASES:
            kw_min = case["expected_keywords_min"]
            kw_list = case["expected_keywords"]
            assert kw_min <= len(kw_list), (
                f"{case['id']}: min={kw_min} > len(keywords)={len(kw_list)}"
            )


class TestQuickSubset:
    """Quick subset 验证。"""

    def test_count(self):
        """应有 10 条 quick。"""
        quick = load_quick_subset(CASES_DIR)
        assert len(quick) == 10

    def test_all_have_quick_tag(self):
        """每条都含 quick tag。"""
        quick = load_quick_subset(CASES_DIR)
        for case in quick:
            assert "quick" in case.get("tags", []), f"{case['id']} 缺 quick tag"

    def test_one_per_fault_pattern(self):
        """每种故障类型恰好 1 条 quick。"""
        quick = load_quick_subset(CASES_DIR)
        patterns = [c["fault_pattern"] for c in quick]
        assert len(patterns) == len(set(patterns))


class TestFilters:
    """过滤功能测试。"""

    def test_filter_by_difficulty(self):
        """按 difficulty 过滤（仅计单轮 easy）。"""
        easy = load_cases(CASES_DIR, difficulty="easy")
        assert all(c["difficulty"] == "easy" for c in easy)
        assert len(easy) >= 10

    def test_filter_by_ids(self):
        """按 ID 过滤。"""
        subset = load_cases(CASES_DIR, ids=["E001", "E050"])
        assert len(subset) == 2
        assert {c["id"] for c in subset} == {"E001", "E050"}

    def test_filter_by_tags(self):
        """按 tags 过滤。"""
        tagged = load_cases(CASES_DIR, tags=["quick"])
        assert len(tagged) == 10


class TestMultiTurn:
    """多轮对话 case 验证。"""

    def test_count(self):
        """应有 5 条多轮 case。"""
        assert len(MULTI_TURN_CASES) == 5

    def test_ids_format(self):
        """多轮 case ID 为 M001-M005。"""
        expected_ids = {f"M{i:03d}" for i in range(1, 6)}
        actual_ids = {c["id"] for c in MULTI_TURN_CASES}
        assert actual_ids == expected_ids

    def test_required_fields(self):
        """每条多轮 case 含必需字段。"""
        for case in MULTI_TURN_CASES:
            missing = REQUIRED_FIELDS_MULTI - set(case.keys())
            assert not missing, f"{case['id']} 缺少字段: {missing}"

    def test_turns_not_empty(self):
        """turns 至少有 2 轮。"""
        for case in MULTI_TURN_CASES:
            turns = case.get("turns", [])
            assert len(turns) >= 2, f"{case['id']} turns 不足 2 轮"

    def test_turns_have_query(self):
        """每轮 turn 要么是字符串，要么含 query 字段。"""
        for case in MULTI_TURN_CASES:
            for idx, turn in enumerate(case["turns"]):
                if isinstance(turn, str):
                    assert len(turn) > 3, f"{case['id']} turn[{idx}] 过短"
                else:
                    assert "query" in turn, f"{case['id']} turn[{idx}] 缺 query"

    def test_has_multi_turn_tag(self):
        """每条多轮 case 含 multi_turn tag。"""
        for case in MULTI_TURN_CASES:
            assert "multi_turn" in case.get("tags", []), f"{case['id']} 缺 multi_turn tag"

    def test_filter_by_tag(self):
        """按 multi_turn tag 过滤能找到 5 条。"""
        filtered = load_cases(CASES_DIR, tags=["multi_turn"])
        assert len(filtered) == 5
