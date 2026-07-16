"""eval/dataset 加载测试 —— 验证 YAML 可解析、字段完整、分布正确。"""

from __future__ import annotations

from collections import Counter

from eval.dataset import CASES_DIR, load_cases, load_quick_subset

# 全量加载一次供多个测试共享
ALL_CASES = load_cases(CASES_DIR)
# 单轮 case（E001-E050）
SINGLE_TURN_CASES = [c for c in ALL_CASES if c.get("type") != "multi_turn"]
# 多轮 case（M001-M005）
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
    """单轮 case（E001-E050）全量验证。"""

    def test_count(self):
        """应有 50 条单轮 case。"""
        assert len(SINGLE_TURN_CASES) == 50

    def test_ids_unique(self):
        """ID 不重复。"""
        ids = [c["id"] for c in ALL_CASES]
        assert len(ids) == len(set(ids))

    def test_ids_sequential(self):
        """E001-E050 连续。"""
        expected_ids = {f"E{i:03d}" for i in range(1, 51)}
        actual_ids = {c["id"] for c in SINGLE_TURN_CASES}
        assert actual_ids == expected_ids

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
        """单轮 case 难度分布: 10 easy / 30 medium / 10 hard。"""
        cnt = Counter(c["difficulty"] for c in SINGLE_TURN_CASES)
        assert cnt["easy"] == 10
        assert cnt["medium"] == 30
        assert cnt["hard"] == 10

    def test_fault_pattern_coverage(self):
        """单轮 case 10 种故障类型各 5 条。"""
        cnt = Counter(c["fault_pattern"] for c in SINGLE_TURN_CASES)
        assert len(cnt) == 10
        for pattern, count in cnt.items():
            assert count == 5, f"{pattern} 只有 {count} 条"

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
