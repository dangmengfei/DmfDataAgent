"""时间规则解析测试（固定 today 保证确定性）。"""

from datetime import date, timedelta

from dmf_agent.time.format import cn_to_int, quarter_format, week_format
from dmf_agent.time.holidays import nth_workday_backwards
from dmf_agent.time.rules import ParsedTime, parse_time

TODAY = date(2026, 8, 17)


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def test_absolute_range_cn():
    r = parse_time("查询2026年6月1日到2026年6月30日的数据", today=TODAY)
    assert r.begin_time == "2026-06-01"
    assert r.end_time == "2026-06-30"
    assert r.granularity == "day"


def test_absolute_range_iso():
    r = parse_time("2026-06-01 ~ 2026-06-30", today=TODAY)
    assert r.begin_time == "2026-06-01"
    assert r.end_time == "2026-06-30"


def test_absolute_points():
    assert parse_time("2026年6月1日", today=TODAY).times == ["2026-06-01"]
    assert parse_time("2026年6月", today=TODAY).times == ["2026-06"]
    assert parse_time("2026年", today=TODAY).times == ["2026-01-01"]
    assert parse_time("2026-06", today=TODAY).times == ["2026-06"]
    assert parse_time("2026-06-15", today=TODAY).times == ["2026-06-15"]


def test_near_days():
    r = parse_time("近7天GMV趋势", today=TODAY)
    assert r.begin_time == (TODAY - timedelta(days=7)).isoformat()
    # "近N天"结束不含今天
    assert r.end_time == (TODAY - timedelta(days=1)).isoformat()
    assert r.granularity == "day"


def test_near_week_and_month():
    r = parse_time("最近4周", today=TODAY)
    assert r.granularity == "week"
    assert r.end_time == TODAY.isoformat()

    r2 = parse_time("近3个月", today=TODAY)
    assert r2.granularity == "month"
    assert r2.end_time == TODAY.isoformat()


def test_this_last_week():
    monday = _monday(TODAY)
    r = parse_time("本周GMV", today=TODAY)
    assert r.begin_time == monday.isoformat()
    assert r.end_time == TODAY.isoformat()
    assert r.granularity == "week"

    r2 = parse_time("上周GMV", today=TODAY)
    assert r2.begin_time == (monday - timedelta(days=7)).isoformat()
    assert r2.end_time == (monday - timedelta(days=1)).isoformat()


def test_this_last_month():
    r = parse_time("本月新增用户", today=TODAY)
    assert r.begin_time == "2026-08-01"
    assert r.end_time == TODAY.isoformat()
    assert r.granularity == "month"

    r2 = parse_time("上月", today=TODAY)
    assert r2.begin_time == "2026-07-01"
    assert r2.end_time == "2026-07-31"


def test_this_last_year():
    r = parse_time("今年GMV", today=TODAY)
    assert r.begin_time == "2026-01-01"
    assert r.end_time == TODAY.isoformat()
    assert r.granularity == "year"

    r2 = parse_time("去年GMV", today=TODAY)
    assert r2.begin_time == "2025-01-01"
    assert r2.end_time == "2025-12-31"


def test_day_rel():
    # 老系统语义：今天/昨天/前天为 begin==end 的时间点范围
    r = parse_time("今天", today=TODAY)
    assert r.begin_time == TODAY.isoformat()
    assert r.end_time == TODAY.isoformat()

    y = TODAY - timedelta(days=1)
    r2 = parse_time("昨天的销售额", today=TODAY)
    assert r2.begin_time == y.isoformat() and r2.end_time == y.isoformat()

    q = TODAY - timedelta(days=2)
    r3 = parse_time("前天", today=TODAY)
    assert r3.begin_time == q.isoformat() and r3.end_time == q.isoformat()


def test_month_day_and_points():
    r = parse_time("6月15日", today=TODAY)
    assert r.times == ["2026-06-15"]
    assert r.granularity == "day"

    r2 = parse_time("今年1月、3月和6月", today=TODAY)
    assert r2.times == ["2026-01", "2026-03", "2026-06"]
    assert r2.granularity == "month"


def test_no_time_returns_empty():
    r = parse_time("查一下GMV趋势", today=TODAY)
    assert isinstance(r, ParsedTime)
    assert r.is_empty is True
    assert r.to_elements() is None


def test_to_elements():
    r = parse_time("近7天GMV", today=TODAY)
    elems = r.to_elements()
    assert elems["start"] == (TODAY - timedelta(days=7)).isoformat()
    assert elems["granularity"] == "day"


def test_format_helpers():
    assert quarter_format(date(2026, 8, 17)) == "2026Q3"
    assert week_format(date(2026, 8, 17)) == "2026-W34"


def test_cn_to_int():
    # 时间解析实际场景：1~31、简单百/千组合（"二百五"式口语省略十位不在范围）
    assert cn_to_int("十五") == 15
    assert cn_to_int("三十") == 30
    assert cn_to_int("三百") == 300
    assert cn_to_int("两百零五") == 205
    assert cn_to_int("两万三千五百六十七") == 23567
    assert cn_to_int("一千零五") == 1005
    assert cn_to_int("俩") == 2
    assert cn_to_int("十一") == 11
    assert cn_to_int("二十一") == 21
    assert cn_to_int("ABC") is None


def test_workdays():
    expected = nth_workday_backwards(TODAY, 3)
    r = parse_time("近3个工作日", today=TODAY)
    assert r.begin_time == expected.isoformat()
    assert r.end_time == TODAY.isoformat()
    assert r.granularity == "day"

    r2 = parse_time("近三个工作日", today=TODAY)
    assert r2.begin_time == expected.isoformat()

    r3 = parse_time("近5个交易日", today=TODAY)
    assert r3.begin_time == nth_workday_backwards(TODAY, 5).isoformat()


def test_cn_month_day():
    r = parse_time("三月十五日", today=TODAY)
    assert r.times == ["2026-03-15"]

    r2 = parse_time("去年六月", today=TODAY)
    assert r2.times == ["2025-06"]


def test_solar_festival():
    r = parse_time("元旦GMV", today=TODAY)
    assert r.times == ["2026-01-01"]
    assert r.granularity == "day"

    r2 = parse_time("国庆节期间", today=TODAY)
    assert r2.begin_time == "2026-10-01"
    assert r2.end_time == "2026-10-07"

    r3 = parse_time("五一销量", today=TODAY)
    assert r3.times == ["2026-05-01"]


def test_lunar_festival():
    r = parse_time("春节假期", today=TODAY)
    # 2026 春节（正月初一）= 2026-02-17，假期范围除夕~初六 = 02-16 ~ 02-22
    assert r.begin_time == "2026-02-16"
    assert r.end_time == "2026-02-22"

    r2 = parse_time("中秋销量", today=TODAY)
    assert r2.times == ["2026-09-25"]


def test_regular_festival():
    r = parse_time("今年母亲节", today=TODAY)
    assert r.times == ["2026-05-10"]


def test_traditional_chinese():
    # 繁体输入经归一化后命中（v2 增强，老系统仅支持简体）
    r = parse_time("近5個工作日", today=TODAY)
    r2 = parse_time("近5个工作日", today=TODAY)
    assert r.begin_time == r2.begin_time
    assert r.end_time == r2.end_time

    r3 = parse_time("昨天的銷售額", today=TODAY)
    y = TODAY - timedelta(days=1)
    assert r3.begin_time == y.isoformat()


async def test_parse_time_combined_fallback_to_rules():
    """LLM 不可用时组合入口回退规则通道。"""
    from dmf_agent.time import parse_time_combined

    parsed = await parse_time_combined("近7天", today=TODAY, get_chat_model=lambda: None)
    assert parsed.begin_time == (TODAY - timedelta(days=7)).isoformat()
    assert parsed.end_time == (TODAY - timedelta(days=1)).isoformat()
    assert parsed.granularity == "day"


def test_to_parsed_time():
    """LLM 通道输出结构 -> ParsedTime 映射。"""
    from dmf_agent.time.llm import TimeLLMResult, _to_parsed_time

    r = _to_parsed_time(
        TimeLLMResult(bt="2026-06-01", et="2026-06-30", g="month", thoughts="6月份"), TODAY
    )
    assert r.begin_time == "2026-06-01"
    assert r.end_time == "2026-06-30"
    assert r.granularity == "month"

    r2 = _to_parsed_time(TimeLLMResult(t=["2024Q2", "2024Q3"], g="quarter"), TODAY)
    assert r2.times == ["2024Q2", "2024Q3"]
    assert r2.granularity == "quarter"

    # g 兼容逗号多值取最后一个（老系统语义）
    r3 = _to_parsed_time(TimeLLMResult(t=["2026-01"], g="month,day"), TODAY)
    assert r3.granularity == "day"

    # 空结果
    r4 = _to_parsed_time(TimeLLMResult(), TODAY)
    assert r4.is_empty is True


# ---------- 回归：substring 抢匹配（大前天） ----------
def test_dayqiantian_not_matched_by_qiantian():
    """「大前天」含子串「前天」——最长别名优先，否则错成昨天。"""
    r = parse_time("大前天", today=TODAY)
    d = TODAY - timedelta(days=3)
    assert r.begin_time == d.isoformat()
    assert r.end_time == d.isoformat()

    r2 = parse_time("大大前天", today=TODAY)
    assert r2.begin_time == d.isoformat()

    # 对照：单独「前天」仍是 -2
    assert parse_time("前天", today=TODAY).begin_time == (TODAY - timedelta(days=2)).isoformat()


def test_shanggeyue_and_variants():
    """「上个月/上一个月」必须命中（旧版只判「上月」子串导致静默回落近 7 天）。"""
    for q in ("上个月", "上一个月", "上月"):
        r = parse_time(q, today=TODAY)
        assert r.begin_time == "2026-07-01", f"{q} 起点应为上月 1 日"
        assert r.end_time == "2026-07-31", f"{q} 止于上月月末"
        assert r.granularity == "month"
    # 问句里带指标名同样命中
    assert parse_time("上个月的GMV", today=TODAY).begin_time == "2026-07-01"


# ---------- 回归：M月D日 优先于 M月离散点 ----------
def test_month_day_beats_month_points():
    """「三月十五」必须解析到 3 月 15 日，不能被「X月」离散点降级成整月。"""
    r = parse_time("三月十五", today=TODAY)
    assert r.times == ["2026-03-15"]
    assert r.granularity == "day"

    assert parse_time("3月15", today=TODAY).times == ["2026-03-15"]
    assert parse_time("3月15日", today=TODAY).times == ["2026-03-15"]

    # 对照：无日数的「3月」仍是整月离散点（不能被上面抢走）
    assert parse_time("3月", today=TODAY).times == ["2026-03"]
    assert parse_time("3月", today=TODAY).granularity == "month"


# ---------- 补齐：季度各形态 ----------
def test_quarter_forms():
    year = 2026
    # 显式年份（含裸 Q3 无「季」字）
    for q in ("2026Q3", "2026年Q3", "2026年第3季度", "2026年第3季", "2026年第三季度"):
        r = parse_time(q, today=TODAY)
        assert r.begin_time == f"{year}-07-01", q
        assert r.end_time == f"{year}-09-30", q
        assert r.granularity == "quarter", q
    # 无年份：Q3 / 第3季度 / 3季度 / 第三季度 —— 2026-08-17 时 Q3 未过完，取今年
    for q in ("Q3", "第3季度", "3季度", "第三季度"):
        r = parse_time(q, today=TODAY)
        assert r.begin_time == "2026-07-01", q
        assert r.end_time == "2026-09-30", q
    # 未到的季度回落去年（不给未来）
    r = parse_time("Q4", today=TODAY)
    assert r.begin_time == "2025-10-01" and r.end_time == "2025-12-31"
    # 相对季
    r = parse_time("本季度", today=TODAY)
    assert r.begin_time == "2026-07-01" and r.end_time == TODAY.isoformat()
    r = parse_time("上季度", today=TODAY)
    assert r.begin_time == "2026-04-01" and r.end_time == "2026-06-30"
    r = parse_time("这季度", today=TODAY)
    assert r.begin_time == "2026-07-01"


# ---------- 补齐：月内位 / 未来相对词 ----------
def test_month_position():
    r = parse_time("月初", today=TODAY)
    assert r.begin_time == "2026-08-01" and r.end_time == "2026-08-10"
    assert parse_time("上旬", today=TODAY).begin_time == "2026-08-01"
    assert parse_time("中旬", today=TODAY).begin_time == "2026-08-11"
    assert parse_time("下旬", today=TODAY).begin_time == "2026-08-21"
    assert parse_time("月末", today=TODAY).end_time == "2026-08-31"
    # 指定月的位序
    assert parse_time("3月上旬", today=TODAY).begin_time == "2026-03-01"
    # 上个月的月初
    assert parse_time("上个月初", today=TODAY).begin_time == "2026-07-01"


def test_future_relative_words():
    assert parse_time("明天", today=TODAY).begin_time == "2026-08-18"
    assert parse_time("后天", today=TODAY).begin_time == "2026-08-19"
    r = parse_time("明年", today=TODAY)
    assert r.begin_time == "2027-01-01" and r.end_time == "2027-12-31"
    r = parse_time("下个月", today=TODAY)
    assert r.begin_time == "2026-09-01" and r.end_time == "2026-09-30"


# ---------- 节气 ----------
def test_solar_terms():
    r = parse_time("清明", today=TODAY)
    assert r.granularity == "day"

    # TODAY=2026-08-17 已过清明 -> 取次年（"下一个清明"）
    assert parse_time("清明", today=TODAY).times == ["2027-04-05"]
    assert parse_time("立春", today=TODAY).times == ["2027-02-04"]

    # 节气前后 N 天（同样按"下一个清明"即 2027-04-05 定位）
    r = parse_time("清明前7天", today=TODAY)
    assert r.begin_time == "2027-03-29" and r.end_time == "2027-04-05"
    # 冬至（2026-12-22）在今年 TODAY 之后 -> 不跳到明年
    r = parse_time("冬至后3天", today=TODAY)
    assert r.begin_time == "2026-12-22" and r.end_time == "2026-12-25"


def test_solar_term_module_dates():
    """校正表年份以真实值为准（表外回落算法）。"""
    from dmf_agent.time.solar_terms import solar_term_date

    assert solar_term_date(2026, "春分").isoformat() == "2026-03-20"
    assert solar_term_date(2026, "冬至").isoformat() == "2026-12-22"
    assert solar_term_date(2025, "清明").isoformat() == "2025-04-04"
    assert solar_term_date(2024, "立春").isoformat() == "2024-02-04"
    # 表外年份仍可估算（且落在合理窗口内）
    d = solar_term_date(2030, "春分")
    assert d is not None and d.month == 3 and 19 <= d.day <= 21
    assert solar_term_date(2026, "不存在的节气") is None


# ---------- 农历日期 / 闰月 ----------
def test_lunar_dates():
    from lunardate import LunarDate

    # 明确到日 -> 离散点；与 lunardate 权威换算对齐
    for q, (ly, lm, ld) in (
        ("农历三月十五", (2026, 3, 15)),
        ("农历三月初五", (2026, 3, 5)),
    ):
        exp = LunarDate(ly, lm, ld).to_solar_date().isoformat()
        assert parse_time(q, today=TODAY).times == [exp], q
    # 阿拉伯数字写法同义
    assert parse_time("农历3月15", today=TODAY).times == ["2026-05-01"]

    # 只到月 -> 整个农历月范围（初一 ~ 下月初一前一日）
    r = parse_time("农历正月初一", today=TODAY)
    assert r.begin_time == "2026-02-17"
    assert r.end_time == "2026-03-18"
    assert parse_time("农历正月", today=TODAY).begin_time == "2026-02-17"

    # 闰月：2025 有闰六月，2026 无
    from dmf_agent.time.format import resolve_lunar

    assert resolve_lunar(2026, 3, 15) == date(2026, 5, 1)
    assert resolve_lunar(2025, 6, 1, leap=True) == date(2025, 7, 25)
    assert resolve_lunar(2026, 6, 1, leap=True) is None


# ---------- 交易日历 ----------
def test_holidays_calendar():
    from dmf_agent.time.holidays import (
        holiday_span,
        is_covered_year,
        is_workday,
        nth_workday_backwards,
    )

    # 2026-10-10 周六为国庆调休补班 -> 工作日
    assert is_workday(date(2026, 10, 10)) is True
    # 2026 国庆 10-01~10-07 放假（含周六 10-03）
    assert is_workday(date(2026, 10, 1)) is False
    assert is_workday(date(2026, 10, 3)) is False
    # 普通周末休
    assert is_workday(date(2026, 10, 31)) is False
    assert is_workday(date(2026, 10, 30)) is True
    # 表外年份降级为仅剔周末（不猜节假日）
    assert is_covered_year(2099) is False
    assert is_workday(date(2099, 10, 31)) is False  # 周六
    assert is_workday(date(2099, 10, 30)) is True  # 周五
    # 放假区间
    spans = {name: (s, e) for s, e, name in holiday_span(2026)}
    assert spans["国庆"] == (date(2026, 10, 1), date(2026, 10, 7))
    # 真实交易日回溯：2026-10-29（周四）前 3 个交易日 -> 10-27（周二）
    assert nth_workday_backwards(date(2026, 10, 29), 3) == date(2026, 10, 27)


def test_relative_workdays_use_calendar():
    """「近N个工作日」走交易日历（含节假日/调休），非仅剔周末。"""
    # 2026-10-10（周六）是国庆调休补班 -> 计入近 3 个工作日
    # 10-12(周一) 往前：10-12、10-10(调休)、10-09 -> 起点 10-09
    r = parse_time("近3个工作日", today=date(2026, 10, 12))
    assert r.begin_time == "2026-10-09"
    assert r.end_time == "2026-10-12"


# ---------- 与默认兜底的关系 ----------
def test_no_time_still_empty():
    """无时间词仍返回空（调用方回退默认近 7 天）。"""
    assert parse_time("GMV是多少", today=TODAY).is_empty


def test_festival_honors_year_hint():
    """「去年端午」取去年（旧版一律用今年，给出错误年份）。"""
    assert parse_time("去年端午", today=TODAY).times == ["2025-05-31"]
    assert parse_time("去年春节", today=TODAY).begin_time == "2025-01-28"
    assert parse_time("今年国庆", today=TODAY).begin_time == "2026-10-01"
