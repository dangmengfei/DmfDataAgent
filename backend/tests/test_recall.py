"""候选召回模块测试（纯 Python，无外部依赖）。"""

from dmf_agent.config.settings import RecallSettings
from dmf_agent.recall.black_talk import BlackTalkDictionary, DEFAULT_BLACK_TALK
from dmf_agent.recall.recall import MetricRecaller
from dmf_agent.recall.segment import remove_punctuation, split_str_by_cn
from dmf_agent.recall.similarity import part_jaccard_score

SAMPLE_NAMES = ["GMV", "日活跃用户数", "新增用户数", "付费转化率", "客单价"]


def test_split_str_by_cn():
    # 中文逐字成词，英文/数字连续
    assert split_str_by_cn("近7天GMV趋势") == ["近", "7", "天", "GMV", "趋", "势"]
    assert split_str_by_cn("GMV") == ["GMV"]
    assert split_str_by_cn("客单价") == ["客", "单", "价"]


def test_remove_punctuation():
    assert remove_punctuation("GMV, 近7天？") == "GMV 近7天"


def test_part_jaccard_score():
    # "GMV" 命中候选 GMV（分母为候选长度）
    scores = part_jaccard_score("查一下GMV", SAMPLE_NAMES, min_value=0.5, max_num=5)
    assert scores["GMV"] == 1.0
    # 明显不相关的不召回
    scores2 = part_jaccard_score("你好呀今天天气不错", SAMPLE_NAMES, min_value=0.5, max_num=5)
    assert scores2 == {}


def test_recall_full_match():
    recaller = MetricRecaller()
    result = recaller.recall("近7天GMV趋势", SAMPLE_NAMES)
    assert result[0].name == "GMV"
    assert result[0].score == 1.0
    assert result[0].matched_by == "full"


def test_recall_black_talk():
    recaller = MetricRecaller(black_talk=BlackTalkDictionary(DEFAULT_BLACK_TALK))
    # "成交额" 黑话 -> GMV
    result = recaller.recall("看一下成交额", SAMPLE_NAMES)
    assert any(r.name == "GMV" and r.matched_by == "black_talk" for r in result)
    # "日活" 黑话 -> 日活跃用户数
    result2 = recaller.recall("近7天日活怎么样", SAMPLE_NAMES)
    assert any(r.name == "日活跃用户数" and r.matched_by == "black_talk" for r in result2)
    # "新增用户" 黑话 -> 新增用户数（黑话优先于部分匹配）
    result3 = recaller.recall("新增用户大概多少", SAMPLE_NAMES)
    assert any(r.name == "新增用户数" and r.matched_by == "black_talk" for r in result3)


def test_recall_part_match_with_typo():
    # 口语/错字部分命中（不含黑话词）：query 含候选名的部分字符 -> Jaccard 命中
    recaller = MetricRecaller()
    result = recaller.recall("新增户数大概多少", SAMPLE_NAMES)
    assert any(r.name == "新增用户数" and r.matched_by == "part" for r in result)


def test_recall_no_match_returns_empty():
    recaller = MetricRecaller()
    assert recaller.recall("今天天气怎么样", SAMPLE_NAMES) == []


def test_recall_respects_max_nums():
    recaller = MetricRecaller()
    result = recaller.recall("用户GMV转化率客单价都看一下", SAMPLE_NAMES, max_nums=2)
    assert len(result) <= 2


def test_relative_threshold_filters_noise():
    """强命中时抑制加权后低于阈值的部分匹配噪声（迁移老 weight_sort >= 最高分/3）。"""
    recaller = MetricRecaller()
    result = recaller.recall("近5个工作日的新增用户数", SAMPLE_NAMES)
    names = [r.name for r in result]
    # 新增用户数：黑话 + 完全命中 1.0
    assert "新增用户数" in names
    # 日活跃用户数：Jaccard 0.667 * 权重0.4 = 0.267 < 1.0/3 -> 被过滤
    assert "日活跃用户数" not in names


def test_relative_threshold_keeps_weak_part_when_alone():
    """无强命中时，加权后的部分匹配候选不被相对阈值误杀（空黑话隔离场景）。"""
    recaller = MetricRecaller(black_talk=BlackTalkDictionary({}))
    result = recaller.recall("新增户数客单", SAMPLE_NAMES)
    names = [r.name for r in result]
    assert "新增用户数" in names
    assert "客单价" in names


def test_weight_configurable_part():
    """weight_part 可配置：调高后部分匹配不再被强命中抑制。"""
    recaller = MetricRecaller(settings=RecallSettings(weight_part=1.0))
    result = recaller.recall("近5个工作日的新增用户数", SAMPLE_NAMES)
    names = [r.name for r in result]
    assert "新增用户数" in names
    assert "日活跃用户数" in names  # 0.667 >= 1.0/3，保留


def test_relative_threshold_disablable():
    """enable_relative_threshold=false 时不做相对阈值过滤。"""
    recaller = MetricRecaller(settings=RecallSettings(enable_relative_threshold=False))
    result = recaller.recall("近5个工作日的新增用户数", SAMPLE_NAMES)
    names = [r.name for r in result]
    assert "日活跃用户数" in names
