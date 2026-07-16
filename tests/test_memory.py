"""W4-A 记忆系统单测（存储地基 + L1 System + L4 Working）。

分两类（沿用 test_retrieval.py 的 skip 模式）：
- 纯内存（不依赖 DB / 模型）：L1 动态 system 拼装、L4 工作记忆压缩 —— CI 必跑
- DB 集成：profile/episodic 表建表与读写，PG 连不上则 skip
"""

import json

import pytest

from opsagent.core.config import settings
from opsagent.core.memory.compactor import count_tokens, should_compact
from opsagent.core.memory.episodic import EpisodicContext, render_history
from opsagent.core.memory.profile import ProfileContext
from opsagent.core.memory.system import build_system_prompt
from opsagent.core.memory.working import (
    TOPK_KEYS,
    compact_record_for_prompt,
    compact_working_memory,
)

_TOOLS = "- search_logs(...): 查日志\n- search_sop(...): 查知识库"

# ---------------- 纯内存：L1 System 动态拼装 ----------------


def test_system_prompt_assembles_role_tone():
    """四种 role 生成不同语气，且都含工具列表 + 安全约束。"""
    sre = build_system_prompt(_TOOLS, role="SRE")
    newbie = build_system_prompt(_TOOLS, role="newbie")
    manager = build_system_prompt(_TOOLS, role="manager")
    base = build_system_prompt(_TOOLS, role=None)

    # 工具列表与安全约束在每种角色下都在
    for sp in (sre, newbie, manager, base):
        assert "search_logs" in sp and "search_sop" in sp
        assert "严禁编造" in sp

    # 语气段各不相同：SRE 允许术语、新人避行话、Manager 偏结论
    assert "专业术语" in sre
    assert "避免行话" in newbie
    assert "业务影响面" in manager
    # 四者两两不等（role 真的影响了输出）
    assert len({sre, newbie, manager, base}) == 4


def test_system_prompt_degrades_without_profile():
    """role=None / 未知 role 都退回基线（无语气段），但仍是完整 system。"""
    base = build_system_prompt(_TOOLS, role=None)
    unknown = build_system_prompt(_TOOLS, role="captain")
    # 未知角色不报错，等同基线
    assert base == unknown
    # 基线里不含任何角色语气句
    assert "专业术语" not in base and "避免行话" not in base
    assert "OpsAgent" in base and "输出格式" in base


def test_role_tone_case_insensitive():
    """role 大小写/空格不敏感。"""
    assert build_system_prompt(_TOOLS, role="sre") == build_system_prompt(_TOOLS, role=" SRE ")


# ---------------- 纯内存：L4 Working 压缩 ----------------


def test_working_memory_compact_topk():
    """压缩保留 Top-K 字段、截断大 result。"""
    big_result = {"rows": ["x" * 50 for _ in range(100)]}  # 远超截断上限
    rec = {
        "tool_name": "search_logs",
        "args": {"service": "edgectl-backend-http"},
        "result": big_result,
        "success": True,
        "latency_ms": 12,
        "error": None,
    }
    out = compact_record_for_prompt(rec, max_chars=100)
    # Top-K 关键字段都在
    for k in TOPK_KEYS:
        assert k in out
    # result 被截断并带标注
    assert "已截断" in out["result"]
    assert len(out["result"]) < len(json.dumps(big_result, ensure_ascii=False))
    # error 为 None 时不塞进精简版
    assert "error" not in out


def test_working_memory_compact_keeps_error():
    """失败记录保留 error 字段。"""
    rec = {
        "tool_name": "trace_query",
        "args": {},
        "result": None,
        "success": False,
        "latency_ms": 3,
        "error": "ToolValidationError: trace_id/service 至少给一个",
    }
    out = compact_record_for_prompt(rec)
    assert out["success"] is False
    assert "ToolValidationError" in out["error"]
    assert out.get("result") is None  # result 为 None 不出现


def test_compact_working_memory_batch():
    """批量压缩：空返回空，多条逐条压。"""
    assert compact_working_memory(None) == []
    assert compact_working_memory([]) == []
    recs = [
        {"tool_name": "a", "args": {}, "result": "ok", "success": True, "latency_ms": 1, "error": None},
        {"tool_name": "b", "args": {}, "result": "x" * 9999, "success": True, "latency_ms": 2, "error": None},
    ]
    out = compact_working_memory(recs, max_chars=50)
    assert len(out) == 2
    assert "已截断" in out[1]["result"]


# ---------------- 纯内存：L3 Episodic 装配与渲染 ----------------


def test_render_history_empty_gives_placeholder():
    """空历史（本会话首轮）渲染为占位句，不出现空段落。"""
    assert render_history(None) == render_history([])
    assert "第一轮" in render_history([])


def test_render_history_labels_roles():
    """user/assistant/system 三种角色渲染为中文标签，顺序保持。"""
    msgs = [
        {"role": "system", "content": "早期摘要"},
        {"role": "user", "content": "问题A"},
        {"role": "assistant", "content": "回答A"},
    ]
    text = render_history(msgs)
    lines = text.splitlines()
    assert lines[0].startswith("摘要：") and "早期摘要" in lines[0]
    assert lines[1] == "用户：问题A"
    assert lines[2] == "助手：回答A"


def test_episodic_context_to_messages_order():
    """to_messages：摘要在前（system 角色）、最近轮原文在后，顺序不乱。"""
    ctx = EpisodicContext(
        summaries=[{"covers_start": 1, "covers_end": 3, "summary": "压缩了前三轮"}],
        recent_turns=[
            {"turn": 4, "role": "user", "content": "q4"},
            {"turn": 4, "role": "assistant", "content": "a4"},
        ],
    )
    msgs = ctx.to_messages()
    assert msgs[0]["role"] == "system" and "压缩了前三轮" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "q4"}
    assert msgs[2] == {"role": "assistant", "content": "a4"}
    # 空上下文 → 空 messages
    assert EpisodicContext().to_messages() == []
    assert EpisodicContext().is_empty()


# ---------------- 纯内存：W4-C 压缩触发判断 ----------------


def test_should_compact_manual_always_true():
    """手动触发优先级最高，不看轮数 / token。"""
    ok, reason = should_compact(n_turns=0, context_tokens=0, manual=True)
    assert ok and reason == "manual"


def test_should_compact_by_round_count():
    """轮数达 memory_compact_round_count 即触发；差一轮不触发。"""
    n = settings.memory_compact_round_count
    ok, reason = should_compact(n_turns=n, context_tokens=0, max_context_tokens=100000)
    assert ok and "rounds" in reason
    ok2, reason2 = should_compact(n_turns=n - 1, context_tokens=0, max_context_tokens=100000)
    assert not ok2 and reason2 == "no"


def test_should_compact_by_token_ratio():
    """上下文 token 占比达阈值即触发；未达不触发。"""
    max_ctx = 1000
    ratio = settings.memory_compact_token_ratio
    over = int(max_ctx * ratio) + 1
    ok, reason = should_compact(n_turns=1, context_tokens=over, max_context_tokens=max_ctx)
    assert ok and "token_ratio" in reason
    under = int(max_ctx * ratio) - 1
    ok2, reason2 = should_compact(n_turns=1, context_tokens=under, max_context_tokens=max_ctx)
    assert not ok2 and reason2 == "no"


def test_count_tokens_positive():
    """token 估算返回正整数（tiktoken 不可用时退字符粗估，同样 > 0）。"""
    n = count_tokens([{"role": "user", "content": "你好，这是一段用于估算 token 的测试文本"}])
    assert isinstance(n, int) and n > 0


# ---------------- 纯内存：W4-D Profile role 提取与冲突可见 ----------------


def test_profile_context_role_and_conflict_block():
    """role 取最新版本；同字段多版本在 prompt 块里全列（冲突可见）。"""
    from datetime import datetime

    # 模拟 store.load_profile_versions 的输出：field ASC, created_at DESC
    versions = [
        {"field": "prefers", "value": "简洁结论", "source": "updater",
         "created_at": datetime(2026, 6, 12)},
        {"field": "role", "value": "SRE", "source": "updater",
         "created_at": datetime(2026, 6, 15)},
        {"field": "role", "value": "backend", "source": "explicit",
         "created_at": datetime(2026, 6, 10)},
    ]
    ctx = ProfileContext(versions=versions, fault_patterns=[])
    assert ctx.role == "SRE"  # 最新优先
    block = ctx.to_prompt_block()
    # 冲突可见：新值 + 旧值 + 历史值标记都在
    assert "SRE" in block and "backend" in block and "历史值" in block
    assert "简洁结论" in block


def test_profile_context_empty_and_fault_patterns():
    """空画像给占位；故障模式渲染带距离。"""
    assert ProfileContext().to_prompt_block() == "（暂无用户画像）"
    assert ProfileContext().role is None
    ctx = ProfileContext(
        versions=[], fault_patterns=[{"summary": "网关 5xx 飙升", "dist": 0.123}]
    )
    block = ctx.to_prompt_block()
    assert "网关 5xx 飙升" in block and "0.123" in block


# ---------------- DB 集成（PG 不通自动 skip）----------------


@pytest.fixture
def mem_db():
    """建好 memory 4 表的连接工厂；PG 不可用则 skip。"""
    try:
        from opsagent.core.memory.store import ensure_memory_schema
        from opsagent.core.retrieval.store import connect

        with connect() as conn:
            ensure_memory_schema(conn)
        return connect
    except Exception as exc:
        pytest.skip(f"PG 不可用，跳过 memory 集成测试: {exc}")


def test_ensure_memory_schema_idempotent(mem_db):
    """连跑两次建表不报错（幂等）。"""
    from opsagent.core.memory.store import ensure_memory_schema

    with mem_db() as conn:
        ensure_memory_schema(conn)
        ensure_memory_schema(conn)  # 第二次不应抛


def test_append_and_load_recent_turns(mem_db):
    """写 10 轮（每轮 user+assistant 两行），load_recent_turns(3) 取最近 3 轮且正序。"""
    from opsagent.core.memory.store import append_turn, count_turns, load_recent_turns

    sid = "test-sess-recent"
    with mem_db() as conn:
        # 清掉可能的历史残留，保证可重复
        conn.execute("DELETE FROM episodic_turns WHERE session_id = %s", (sid,))
        conn.commit()
        for t in range(1, 11):
            append_turn(conn, sid, t, "user", f"问题{t}")
            append_turn(conn, sid, t, "assistant", f"回答{t}")

        assert count_turns(conn, sid) == 10
        rows = load_recent_turns(conn, sid, 3)
        # 最近 3 轮 × 每轮 2 行 = 6 行
        assert len(rows) == 6
        turns = [r["turn"] for r in rows]
        assert turns == [8, 8, 9, 9, 10, 10]  # 正序 + 同轮 user 在 assistant 前
        assert rows[0]["role"] == "user" and rows[1]["role"] == "assistant"


def test_profile_upsert_multi_version(mem_db):
    """两次 upsert role（backend→SRE），多版本保留、最新优先。"""
    from opsagent.core.memory.store import load_profile_versions, upsert_profile_field

    uid = "test-user-conflict"
    with mem_db() as conn:
        conn.execute("DELETE FROM profile WHERE user_id = %s", (uid,))
        conn.commit()
        upsert_profile_field(conn, uid, "role", "backend", source="explicit")
        upsert_profile_field(conn, uid, "role", "SRE", source="updater")

        versions = [v for v in load_profile_versions(conn, uid) if v["field"] == "role"]
        assert len(versions) == 2  # 不覆盖，两个版本都在
        assert versions[0]["value"] == "SRE"  # created_at DESC，最新优先


def test_persist_then_load_episodic_sees_prior_turn(mem_db):
    """W4-B 核心验收：同一 session 第二次请求能在历史里看到上一轮。

    模拟两次请求：第一次 persist_turn 落库一轮，第二次 load_episodic 应读回该轮，
    渲染进 prompt 的历史文本里能看到上一轮的问与答。
    """
    from opsagent.core.memory.episodic import load_episodic, persist_turn
    from opsagent.core.memory.store import count_turns

    sid = "test-sess-multiturn"
    with mem_db() as conn:
        conn.execute("DELETE FROM episodic_turns WHERE session_id = %s", (sid,))
        conn.commit()

    # —— 第一次请求：落库一轮 ——
    turn1 = persist_turn(sid, "edgectl 网关报错率飙升怎么办", "建议先查 5xx 日志定位上游")
    assert turn1 == 1

    # —— 第二次请求：装配上下文 ——
    ctx = load_episodic(sid)
    assert not ctx.is_empty()
    history = render_history(ctx.to_messages())
    # 上一轮的问与答都出现在历史里 → 真多轮
    assert "网关报错率飙升" in history
    assert "查 5xx 日志" in history

    # 第二轮落库后轮号自增
    turn2 = persist_turn(sid, "那要看哪个时间段", "建议看最近 15 分钟")
    assert turn2 == 2
    with mem_db() as conn:
        assert count_turns(conn, sid) == 2


def test_load_episodic_keeps_only_recent_turns(mem_db):
    """load_episodic 只取最近 N 轮（keep_recent_turns），更早的不进 recent_turns。"""
    from opsagent.core.memory.episodic import load_episodic, persist_turn

    sid = "test-sess-keepn"
    with mem_db() as conn:
        conn.execute("DELETE FROM episodic_turns WHERE session_id = %s", (sid,))
        conn.commit()
    for _ in range(5):
        persist_turn(sid, "q", "a")

    ctx = load_episodic(sid, keep_recent_turns=2)
    turns = sorted({t["turn"] for t in ctx.recent_turns})
    assert turns == [4, 5]  # 只保留最近 2 轮
    assert len(ctx.recent_turns) == 4  # 2 轮 × 每轮 2 行


# ---------------- DB 集成：W4-C 压缩编排（LLM 边界用 monkeypatch 替掉）----------------
# 真实 updater LLM 不在单测里打，只验「DB 编排 + 兜底」逻辑：构造对话 → 压缩 → 查摘要表。


async def test_compact_session_creates_level1_summary(mem_db, monkeypatch):
    """9 轮对话触发压缩 → episodic_summaries 出一条一级摘要，覆盖区间正确。"""
    from opsagent.core.memory import compactor
    from opsagent.core.memory.store import append_turn, load_summaries

    sid = "test-sess-compact1"
    with mem_db() as conn:
        conn.execute("DELETE FROM episodic_turns WHERE session_id = %s", (sid,))
        conn.execute("DELETE FROM episodic_summaries WHERE session_id = %s", (sid,))
        conn.commit()
        for t in range(1, 10):  # 9 轮
            append_turn(conn, sid, t, "user", f"问题{t}")
            append_turn(conn, sid, t, "assistant", f"回答{t}")

    fake = {
        "rounds_compressed": 6,
        "topic": "测试压缩",
        "key_facts": ["事实1"],
        "tools_used": [],
        "decisions": [],
        "unresolved": [],
    }

    async def _fake_summarize(chunk):
        return fake

    monkeypatch.setattr(compactor, "_summarize_chunk", _fake_summarize)

    summary = await compactor.compact_session(sid, reason="manual")
    assert summary == fake
    keep = settings.memory_keep_recent_turns
    with mem_db() as conn:
        l1 = [s for s in load_summaries(conn, sid) if s["level"] == 1]
    assert len(l1) == 1
    # 待压缩区间 = [1, 最大轮号 9 - 保留轮数 keep]
    assert l1[0]["covers_start"] == 1
    assert l1[0]["covers_end"] == 9 - keep


async def test_compact_session_recursive_level2(mem_db, monkeypatch):
    """一级摘要累计达 merge_count → 合并出 level2，原 level1 全标 superseded。"""
    from opsagent.core.memory import compactor
    from opsagent.core.memory.store import insert_summary, load_summaries

    sid = "test-sess-compact2"
    merge_n = settings.memory_summary_merge_count
    with mem_db() as conn:
        conn.execute("DELETE FROM episodic_summaries WHERE session_id = %s", (sid,))
        conn.commit()
        for i in range(merge_n):  # 预置 merge_n 条一级摘要
            insert_summary(conn, sid, 1, i * 2 + 1, i * 2 + 2, '{"topic":"t"}')

    fake = {
        "rounds_compressed": merge_n,
        "topic": "合并摘要",
        "key_facts": [],
        "tools_used": [],
        "decisions": [],
        "unresolved": [],
    }

    async def _fake_summarize(chunk):
        return fake

    monkeypatch.setattr(compactor, "_summarize_chunk", _fake_summarize)

    await compactor._maybe_merge_summaries(sid)
    with mem_db() as conn:
        all_s = load_summaries(conn, sid, include_superseded=True)
    l2 = [s for s in all_s if s["level"] == 2]
    l1_active = [s for s in all_s if s["level"] == 1 and not s["superseded"]]
    assert len(l2) == 1  # 生成一条 level2
    assert l1_active == []  # 原 level1 全部被合并（superseded）


async def test_compact_session_failure_keeps_turns(mem_db, monkeypatch):
    """压缩中途 LLM 失败 → 返回 None、不写半成品摘要、原始对话一行不丢。"""
    from opsagent.core.memory import compactor
    from opsagent.core.memory.store import append_turn, count_turns, load_summaries

    sid = "test-sess-compactfail"
    with mem_db() as conn:
        conn.execute("DELETE FROM episodic_turns WHERE session_id = %s", (sid,))
        conn.execute("DELETE FROM episodic_summaries WHERE session_id = %s", (sid,))
        conn.commit()
        for t in range(1, 10):
            append_turn(conn, sid, t, "user", f"q{t}")
            append_turn(conn, sid, t, "assistant", f"a{t}")

    async def _boom(chunk):
        raise RuntimeError("模拟 LLM 不可用")

    monkeypatch.setattr(compactor, "_summarize_chunk", _boom)

    res = await compactor.compact_session(sid, reason="manual")
    assert res is None  # 失败兜底
    with mem_db() as conn:
        assert count_turns(conn, sid) == 9  # 原文一行不丢
        assert load_summaries(conn, sid) == []  # 没有半成品摘要


# ---------------- DB 集成：W4-D Profile 跨会话画像 + Updater ----------------


def test_load_profile_cross_session_role(mem_db):
    """跨会话画像：按 user_id 拉多版本，role 取最新（不传 query 不触发向量召回）。"""
    from opsagent.core.memory.profile import load_profile
    from opsagent.core.memory.store import upsert_profile_field

    uid = "test-user-profile-load"
    with mem_db() as conn:
        conn.execute("DELETE FROM profile WHERE user_id = %s", (uid,))
        conn.commit()
        upsert_profile_field(conn, uid, "role", "backend", source="explicit")
        upsert_profile_field(conn, uid, "role", "SRE", source="updater")
        upsert_profile_field(conn, uid, "prefers", "简洁结论", source="updater")

    profile = load_profile(uid)  # 不传 query → 不碰 embedder
    assert profile.role == "SRE"  # 最新版本
    block = profile.to_prompt_block()
    assert "SRE" in block and "backend" in block  # 冲突多版本可见
    assert profile.fault_patterns == []  # 未给 query，无召回


async def test_profile_updater_writes_fields_and_fault_pattern(mem_db, monkeypatch):
    """Updater 抽取：写多版本字段 + 故障模式向量入库，随后能按 query 召回。

    用 monkeypatch 替掉 LLM（profile.chat）与 embedder，离线验证「抽取→落库→召回」闭环。
    """
    import numpy as np

    from opsagent.core.memory import profile as profile_mod
    from opsagent.core.memory.profile import load_profile, run_profile_updater

    uid = "test-user-updater"
    with mem_db() as conn:
        conn.execute("DELETE FROM profile WHERE user_id = %s", (uid,))
        conn.execute("DELETE FROM profile_fault_patterns WHERE user_id = %s", (uid,))
        conn.commit()

    # 假 updater：返回稳定字段 + 一条故障模式
    fake_json = json.dumps(
        {
            "fields": [{"field": "role", "value": "SRE"}],
            "fault_pattern": {"summary": "网关 5xx 飙升常因上游限流变更"},
        },
        ensure_ascii=False,
    )

    async def _fake_chat(**kwargs):
        return {"choices": [{"message": {"content": fake_json}}]}

    class _FakeEmbedder:
        def embed_query(self, q):
            # 维度需与建表 vector(dim) 一致
            return np.zeros(settings.embedding_dim, dtype=np.float32)

    monkeypatch.setattr(profile_mod, "chat", _fake_chat)
    monkeypatch.setattr(
        "opsagent.core.retrieval.embedder.get_embedder", lambda: _FakeEmbedder()
    )

    data = await run_profile_updater(uid, "用户：网关报错怎么查\n助手：先看 5xx 日志")
    assert data is not None and data["fields"][0]["value"] == "SRE"

    # 字段已落库 + 故障模式可按 query 召回
    profile = load_profile(uid, query="网关报错")
    assert profile.role == "SRE"
    assert len(profile.fault_patterns) >= 1
    assert "限流" in profile.fault_patterns[0]["summary"]
