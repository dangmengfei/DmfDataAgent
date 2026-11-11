"""规则通道：相对/绝对时间表达解析（含农历、节气、闰月、交易日历）。

迁移自老系统 get_time 的规则思路，输出格式对齐老 LLM 通道约定
（beginTime/endTime 连续范围，times 离散节点，granularity 粒度）。

覆盖指标问答常见时间表达：
- 绝对范围/点：YYYY年M月D日、YYYY-MM-DD、YYYY年M月、YYYY年、YYYY-MM
- 相对范围：近N天/周/月/年/工作日、近N个交易日
- 相对点：今天/昨天/前天/大前天、本周/这周/上周/上上周、本月/这月/上月/上个月、
  今年/这年/去年/上一年、本季度/这季度/上季度、明天/后天/明年
- 月内位置：月初/月中/月末、月初至今/本月初至今、X月上旬/中旬/下旬
- 季度：YYYYQn / YYYY年Qn / YYYY年第n季度 / n季度(n 缺省按当前季)
- 无年份组合：M月D日（农历需显式"农历"前缀，见 _parse_lunar）、M月、多个 M月 离散点
- 节气：立春/清明/冬至等（见 solar_terms），含"节气前后N天"
- 农历节日/农历日期：春节/端午/中秋等（见 festival）、"农历三月十五"
- 粒度：day/week/month/quarter/year（英文，对齐老输出）

**匹配纪律（三条，都是从线上 bug 学来的）**：
1. 子串匹配按**最长优先**："上个月"不能被"上月"的 `in` 判断漏掉，"大前天"不能被
   "前天"抢走（`前天` 是 `大前天` 的子串）——所有相对词一律用最长别名优先。
2. 更具体的模式排在更泛的模式之前："三月十五"（明确到日）必须先于"X月"（整月）命中，
   否则被降级成整月。
3. 命不中返回空 ParsedTime，由调用方回退默认时间（不抛错、不猜）。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Callable, List, Optional

from dmf_agent.time.format import (
    cn_to_int,
    first_day_of_month,
    first_day_of_quarter,
    first_day_of_year,
    last_day_of_month,
    last_day_of_quarter,
    last_day_of_year,
    resolve_lunar,
    shift_month,
    to_simplified,
    to_ym,
)
from dmf_agent.time.holidays import nth_workday_backwards
from dmf_agent.time.model import ParsedTime, point_time, range_time
from dmf_agent.time.solar_terms import TERM_ORDER, solar_term_date

_RANGE_SEP = r"(?:[至到~\-—－]|至|到)"
_DAY = r"(\d{1,2})"
_MONTH = r"(\d{1,2})"
_YEAR = r"(\d{4})"
# 兼容中文数字（迁移自老系统 DELTA_NUM_STRING 支持范围）
_NUM_CN = r"(\d{1,3}|[零〇一二三四五六七八九十两俩百]+)"


def _to_int(text: str) -> Optional[int]:
    """阿拉伯或中文数字 -> int。"""
    try:
        return int(text)
    except ValueError:
        return cn_to_int(text)


def _contains_longest(query: str, aliases: List[str]) -> Optional[str]:
    """最长别名优先的包含判断——避免"上个月"漏配给"上月"、"大前天"被"前天"抢走。

    返回命中的别名（最长者），未命中返回 None。别名调用方需保证**先长后短**有序。
    """
    for alias in aliases:  # 调用方按 len 降序排好
        if alias in query:
            return alias
    return None


# 相对词别名表：长在前（_contains_longest 依赖此顺序）
_DAY_ALIASES = ["大前天", "三天前", "前天", "前日", "昨天", "昨日", "今天", "今日", "今儿"]
_MONTH_REL_ALIASES = ["上个月", "上一个月", "上月", "这个月", "本月", "当月"]
_YEAR_REL_ALIASES = ["上一年", "去年", "上一年", "这一年", "今年", "当年"]
_WEEK_REL_ALIASES = ["上上周", "上上个星期", "上个星期", "上周", "上一周", "这个星期", "本周", "这周", "本星期"]
# 相对词别名表：长在前（_contains_longest 依赖此顺序；调用处仍再 sorted 兜底）
_QUARTER_REL_ALIASES = [
    "上个季度", "上一季度", "这个季度", "上季度", "本季度", "这季度", "本季", "当季",
]


class RuleTimeParser:
    def __init__(self, today: Optional[date] = None) -> None:
        self.today = today or date.today()

    def parse(self, query: str) -> ParsedTime:
        """按优先级依次尝试规则，返回第一个命中（入口先做繁体->简体归一化）。"""
        query = to_simplified(query)
        for handler in self._handlers():
            result = handler(query)
            if result is not None and not result.is_empty:
                return result
        return ParsedTime()

    # ---------- 节气（立春/清明/冬至…，见 solar_terms.py） ----------
    def _parse_solar_term(self, query: str) -> Optional[ParsedTime]:

        for name in TERM_ORDER:
            if name not in query:
                continue
            target = solar_term_date(self.today.year, name)
            # 该节气今年已过 -> 取次年（"清明是几号"在清明后问，按下一清明算）
            if target < self.today:
                target = solar_term_date(self.today.year + 1, name)
            # "立春前7天"/"冬至后3天" -> 节气为中心的范围；分组 1=前后、2=数字
            m = re.search(name + r"(前|后)\s*" + _NUM_CN + r"\s*[天日]", query)
            if m:
                n = _to_int(m.group(2))
                if n is None or n <= 0:
                    continue
                if m.group(1) == "前":
                    return range_time(target - timedelta(days=n), target, "day")
                return range_time(target, target + timedelta(days=n), "day")
            return ParsedTime(times=[target.isoformat()], granularity="day", raw=name)
        return None

    def _handlers(self) -> List[Callable[[str], Optional[ParsedTime]]]:
        # 优先级设计（纪律 2：更具体的模式排更前）：
        # 节气带偏移（"清明前7天"）> 绝对范围 > 相对范围（近N天）> 显式季度 > 周/月/日相对词
        # > 节日 > 绝对点 > 月内位（月初/上旬，先于"X月"否则"3月上旬"被降级成3月）
        # > 农历日期（"农历三月十五"，先于公历月离散点）> 月离散点 > 相对年/季
        return [
            self._parse_solar_term,
            self._parse_absolute_range,
            self._parse_relative_range,  # 近N天/周/月/年/工作日/交易日
            self._parse_quarter_explicit,  # 2026Q3 / 2026年Q3 / 2026年第3季度
            # 农历先于月内位：「农历正月初一」里的"初一"含"月初"二字，若让 month_pos 先跑
            # 会被当成"本月上旬"（老系统把农历日期做成显式前缀，本就是更具体的信号）
            self._parse_lunar_date,
            self._parse_week_rel,  # 本周/上周/上上周…
            self._parse_quarter_rel,  # 本季度/上季度/当季
            self._parse_month_pos,  # 月初/月中/月末/上旬/中旬/下旬
            self._parse_month_rel,  # 本月/上月/上个月
            self._parse_day_rel,  # 今天/昨天/前天/大前天/明天/后天
            self._parse_future_rel,  # 明天/后天/明年/下个月/下月
            self._parse_festival,  # 元旦/春节/端午/中秋/国庆…
            self._parse_absolute_point,  # YYYY年M月D日 / YYYY-MM-DD / YYYY年M月 / YYYY年
            self._parse_quarter_num,  # "3季度"/"第3季度"（无年份）
            self._parse_month_day,  # [今年/去年]M月D日
            self._parse_month_points,  # [今年/去年]M月、M月和N月（离散点）
            self._parse_year_rel,  # 今年/去年/上一年
        ]

    def _year_hint(self, query: str) -> int:
        """无显式年份时按"去年/上一年"推断年份，否则今年。"""
        if _contains_longest(query, ["上一年", "去年"]):
            return self.today.year - 1
        return self.today.year

    # ---------- 绝对时间范围 ----------
    def _parse_absolute_range(self, query: str) -> Optional[ParsedTime]:
        m = re.search(
            rf"{_YEAR}年{_MONTH}月{_DAY}日{_RANGE_SEP}{_YEAR}年{_MONTH}月{_DAY}日", query
        )
        if m:
            return range_time(
                date(int(m[1]), int(m[2]), int(m[3])),
                date(int(m[4]), int(m[5]), int(m[6])),
                "day",
            )
        m = re.search(
            rf"{_YEAR}-{_MONTH}-{_DAY}\s*{_RANGE_SEP}\s*{_YEAR}-{_MONTH}-{_DAY}", query
        )
        if m:
            return range_time(
                date(int(m[1]), int(m[2]), int(m[3])),
                date(int(m[4]), int(m[5]), int(m[6])),
                "day",
            )
        return None

    # ---------- 相对范围：近N天/周/月/年/工作日/交易日（兼容中文数字与"N个"） ----------
    def _parse_relative_range(self, query: str) -> Optional[ParsedTime]:
        m = re.search(
            r"(?:最)?近" + _NUM_CN + r"个?(天|日|周|月|年|工作日|交易日)", query
        )
        if not m:
            return None
        n = _to_int(m[1])
        unit = m[2]
        if n is None or n <= 0:
            return None
        if unit in ("工作日", "交易日"):
            # 老系统 WORKDAY_DELTA + 交易日历：剔周末、剔法定节假日、含调休补班
            begin = nth_workday_backwards(self.today, n)
            return range_time(begin, self.today, "day")
        if unit in ("天", "日"):
            # "近N天"结束不含今天（对齐老 prompt 约定）
            end = self.today - timedelta(days=1)
            begin = self.today - timedelta(days=n)
            return range_time(begin, end, "day")
        if unit == "周":
            monday = self.today - timedelta(days=self.today.weekday())
            return range_time(monday - timedelta(weeks=n - 1), self.today, "week")
        if unit == "月":
            return range_time(
                first_day_of_month(shift_month(self.today, -(n - 1))), self.today, "month"
            )
        return range_time(date(self.today.year - (n - 1), 1, 1), self.today, "year")

    # ---------- 显式季度：2026Q3 / 2026年Q3 / 2026年第3季度 / 2026年第三季度 ----------
    # 注意：_YEAR 自带捕获组，故这里先用 (?:\d{4}) 做非捕获前缀，再单独捕获序号，
    # 避免分组编号随"年/Q/第"是否出现而漂移（曾因此把年份当季度取，引发 KeyError）。
    def _parse_quarter_explicit(self, query: str) -> Optional[ParsedTime]:
        ym = re.search(_YEAR, query)
        if ym is None:
            return None
        year = int(ym.group(1))
        q: Optional[int] = None
        # 形态一：Q3 / 第3季度 / 第3季 / Q3季度（序号在"季"之前）
        m = re.search(r"\d{4}\s*年?\s*[Qq第]?\s*([1-4])\s*[Qq]?季(?:度)?", query)
        if m:
            q = int(m.group(1))
        else:
            # 形态二：2026Q3 / 2026年Q3 —— Q 紧跟数字、无"季"字
            mq = re.search(r"\d{4}\s*年?\s*[Qq]\s*([1-4])", query)
            if mq:
                q = int(mq.group(1))
            else:
                # 形态三：2026年第三季度（中文序号）
                mc = re.search(r"\d{4}\s*年?\s*第?\s*([一二三四])\s*季(?:度)?", query)
                if mc is None:
                    return None
                q = {"一": 1, "二": 2, "三": 3, "四": 4}[mc.group(1)]
        if not 1 <= q <= 4:
            return None
        start = date(year, (q - 1) * 3 + 1, 1)
        return range_time(start, last_day_of_quarter(start), "quarter")

    # ---------- 无年份季度："Q3" / "3季度" / "第3季度" / "第三季度" ----------
    def _parse_quarter_num(self, query: str) -> Optional[ParsedTime]:
        q: Optional[int] = None
        # 形态一：Q3 / 3季度 / 第3季度 / Q3季度（序号在"季"之前）
        m = re.search(r"[Qq]?\s*([1-4])\s*[Qq]?季(?:度)?", query)
        if m:
            q = int(m.group(1))
        else:
            # 形态二：裸 "Q3"（无"季"字；老系统写法兼容）
            mq = re.search(r"\b[Qq]\s*([1-4])\b", query)
            if mq:
                q = int(mq.group(1))
            else:
                mc = re.search(r"第?\s*([一二三四])\s*季(?:度)?", query)
                if mc:
                    q = {"一": 1, "二": 2, "三": 3, "四": 4}[mc.group(1)]
        if q is None or not 1 <= q <= 4:
            return None
        # 无年份：该季度起点晚于今天则取去年同季（保证返回已发生的季度，不给未来）
        year = self.today.year
        if first_day_of_quarter(date(year, (q - 1) * 3 + 1, 1)) > self.today:
            year -= 1
        start = date(year, (q - 1) * 3 + 1, 1)
        return range_time(start, last_day_of_quarter(start), "quarter")

    # ---------- 相对季度：本季/上季/当季（含长别名，纪律 1） ----------
    def _parse_quarter_rel(self, query: str) -> Optional[ParsedTime]:
        hit = _contains_longest(
            query, sorted(_QUARTER_REL_ALIASES, key=len, reverse=True)
        )
        if hit is None:
            return None
        # "上季度"类 -> 上一个完整季度；当季 -> 起点本季 1 日，止于今天（同上：不给未过的未来）
        if hit.startswith("上"):
            start = first_day_of_quarter(shift_month(self.today, -3))
            return range_time(start, last_day_of_quarter(start), "quarter")
        return range_time(first_day_of_quarter(self.today), self.today, "quarter")

    # ---------- 周 ----------
    def _parse_week_rel(self, query: str) -> Optional[ParsedTime]:
        hit = _contains_longest(query, sorted(_WEEK_REL_ALIASES, key=len, reverse=True))
        if hit is None:
            return None
        monday = self.today - timedelta(days=self.today.weekday())
        if hit.startswith("上"):
            weeks_back = 2 if hit.startswith("上上") else 1
            start = monday - timedelta(weeks=weeks_back)
            return range_time(start, start + timedelta(days=6), "week")
        # 本周/这周：从周一到今天（今天可能未到周末）
        return range_time(monday, self.today, "week")

    # ---------- 月内位：月初/月中/月末/上旬/中旬/下旬（先于"X月"匹配） ----------
    def _parse_month_pos(self, query: str) -> Optional[ParsedTime]:
        month = self.today.month
        year = self.today.year
        # 明确指出某月的位序："3月上旬"
        m = re.search(rf"{_MONTH}\s*月\s*(上旬|中旬|下旬)", query)
        if m:
            month = int(m.group(1))
        else:
            if _contains_longest(query, ["上个月", "上一个月", "上月"]):
                shifted = shift_month(self.today, -1)
                year, month = shifted.year, shifted.month
            elif _contains_longest(query, ["去年", "上一年"]) and "月" not in query:
                year = self.today.year - 1
        has_pos = _contains_longest(query, ["上旬", "中旬", "下旬", "月初", "月中", "月末"])
        if not has_pos:
            return None
        first = first_day_of_month(date(year, month, 1))
        last = last_day_of_month(date(year, month, 1))
        if has_pos in ("上旬", "月初"):
            return range_time(first, min(first + timedelta(days=9), last), "day")
        if has_pos in ("中旬", "月中"):
            return range_time(first + timedelta(days=10), min(first + timedelta(days=19), last), "day")
        if has_pos in ("下旬", "月末"):
            return range_time(first + timedelta(days=20), last, "day")
        return None

    # ---------- 月 ----------
    def _parse_month_rel(self, query: str) -> Optional[ParsedTime]:
        hit = _contains_longest(query, sorted(_MONTH_REL_ALIASES, key=len, reverse=True))
        if hit is None:
            return None
        if hit.startswith("上"):
            last = shift_month(self.today, -1)
            return range_time(first_day_of_month(last), last_day_of_month(last), "month")
        # 本月/当月：起点本月 1 日，止于今天（月末未过完，不能给整月——否则统计出未来日期）
        return range_time(first_day_of_month(self.today), self.today, "month")

    # ---------- 未来：明天/后天/明年/下个月/下单月 ----------
    def _parse_future_rel(self, query: str) -> Optional[ParsedTime]:
        if "后天" in query:
            d = self.today + timedelta(days=2)
            return range_time(d, d, "day")
        if "明天" in query or "明日" in query:
            d = self.today + timedelta(days=1)
            return range_time(d, d, "day")
        if _contains_longest(query, ["下个月", "下一个月", "下月"]):
            nxt = shift_month(self.today, 1)
            nxt = date(nxt.year, nxt.month, 1)
            return range_time(nxt, last_day_of_month(nxt), "month")
        if "明年" in query:
            nxt = date(self.today.year + 1, 1, 1)
            return range_time(nxt, last_day_of_year(nxt), "year")
        return None

    # ---------- 相对年 ----------
    def _parse_year_rel(self, query: str) -> Optional[ParsedTime]:
        hit = _contains_longest(query, sorted(_YEAR_REL_ALIASES, key=len, reverse=True))
        if hit is None:
            return None
        if hit.startswith("上") or hit == "去年":
            y = self.today.year - 1
            return range_time(date(y, 1, 1), date(y, 12, 31), "year")
        return range_time(first_day_of_year(self.today), self.today, "year")

    # ---------- 日 ----------
    def _parse_day_rel(self, query: str) -> Optional[ParsedTime]:
        hit = _contains_longest(query, sorted(_DAY_ALIASES, key=len, reverse=True))
        if hit is None:
            return None
        offsets = {
            "大前天": 3, "三天前": 3, "前天": 2, "前日": 2,
            "昨天": 1, "昨日": 1, "今天": 0, "今日": 0, "今儿": 0,
        }
        d = self.today - timedelta(days=offsets[hit])
        return range_time(d, d, "day")

    # ---------- 节日 ----------
    def _parse_festival(self, query: str) -> Optional[ParsedTime]:
        from dmf_agent.time.festival import resolve_festival

        # 节日也遵守年份提示："去年端午"取去年，而非一律用今年
        if _contains_longest(query, ["上一年", "去年"]):
            return resolve_festival(query, year=self.today.year - 1)
        return resolve_festival(query, year=self.today.year)

    # ---------- 农历日期：农历X月X日 / 闰X月（老系统定制能力） ----------
    def _parse_lunar_date(self, query: str) -> Optional[ParsedTime]:
        """显式农历表达 -> 公历。

        支持「农历三月十五」「农历3月15」「农历三月初五」「闰二月」等。
        只到月（未给日）时返回该农历月的**整数月范围**（初一 ~ 下月初一前一日），
        与"正月"问整月的语义一致。
        """
        if not any(p in query for p in ("农历", "阴历", "夏历")):
            return None
        # 用命名组，避免日后增减正则项时分组编号漂移（曾因此把"十五"读成"三"）。
        # 日数支持三种写法：阿拉伯（15）/ 中文序数（初五、廿三、三十）/ 普通中文数字（十五）
        m = re.search(
            r"(?:农历|阴历|夏历)\s*(?P<leap>闰?)\s*"
            r"(?P<mon>[0-9]{1,2}|[正二三四五六七八九十冬腊]{1,3})"
            r"月(?:(?P<day>[0-9]{1,2}|[零〇二三四五六七八九十两]{1,3}|"
            r"初[一二三四五六七八九十]|廿[一二三四五六七八九]|三十)[日号]?)?",
            query,
        )
        if not m:
            return None
        leap = bool(m.group("leap"))
        mon_txt = m.group("mon")
        month = _to_int(mon_txt) if mon_txt.isdigit() else _cn_month(mon_txt)
        if month is None or not 1 <= month <= 12:
            return None
        day_txt = m.group("day")
        if not day_txt:
            day = 1
        elif day_txt.isdigit():
            day = int(day_txt)
        elif day_txt[0] in ("初", "廿") or day_txt == "三十":
            day = _cn_lunar_day(day_txt)  # 农历专用序数写法（初五/廿三/三十）
        else:
            day = _to_int(day_txt)  # 普通中文数字（十五、二十三）
        if day is None or not 1 <= day <= 30:
            return None

        year = self.today.year
        resolved = resolve_lunar(year, month, day, leap=leap)
        if resolved is None:
            # 该年无此农历月（如闰月不存在的年份）-> 试次年
            resolved = resolve_lunar(year + 1, month, day, leap=leap)
        if resolved is None:
            return None

        if day == 1:
            # 仅到月：整月范围。跨年只发生在腊月（下一个月是次年正月）
            nxt = resolve_lunar(
                resolved.year + 1 if month == 12 else year,
                1 if month == 12 else month + 1,
                1,
            )
            end = (nxt - timedelta(days=1)) if nxt is not None else last_day_of_month(resolved)
            return range_time(resolved, end, "day")
        return ParsedTime(
            times=[resolved.isoformat()], granularity="day", raw=query
        )

    # ---------- 绝对时间点 ----------
    def _parse_absolute_point(self, query: str) -> Optional[ParsedTime]:
        m = re.search(rf"{_YEAR}年{_MONTH}月{_DAY}日", query)
        if m:
            return point_time(date(int(m[1]), int(m[2]), int(m[3])), "day")
        m = re.search(rf"{_YEAR}-{_MONTH}-{_DAY}", query)
        if m:
            return point_time(date(int(m[1]), int(m[2]), int(m[3])), "day")
        m = re.search(rf"{_YEAR}年{_MONTH}月(?:份)?", query)
        if m:
            return point_time(date(int(m[1]), int(m[2]), 1), "month", to_ym)
        m = re.search(rf"{_YEAR}-{_MONTH}", query)
        if m:
            return point_time(date(int(m[1]), int(m[2]), 1), "month", to_ym)
        m = re.search(rf"{_YEAR}年", query)
        if m:
            return point_time(date(int(m[1]), 1, 1), "year")
        return None

    # ---------- 无年份 M月D日 / "三月十五"（兼容中文数字，"日/号"可省） ----------
    def _parse_month_day(self, query: str) -> Optional[ParsedTime]:
        # "日/号"后缀可选："3月15""三月十五"与"3月15日"同义。
        # 第二捕获组必须非空——否则"3月"（整月，无日数）会被这里抢走变成无效日。
        m = re.search(rf"({_NUM_CN[1:-1]})月({_NUM_CN[1:-1]})[日号]?", query)
        if not m or not m.group(2):
            return None
        month = _to_int(m.group(1))
        day = _to_int(m.group(2))
        if not month or not day:
            return None
        try:
            return point_time(date(self._year_hint(query), month, day), "day")
        except ValueError:
            return None

    # ---------- 无年份 M月 离散点（支持 1月、3月和6月、中文数字） ----------
    def _parse_month_points(self, query: str) -> Optional[ParsedTime]:
        months = re.findall(rf"({_NUM_CN[1:-1]})个?月(?:份)?", query)
        months = [_to_int(m) for m in months]
        months = [m for m in months if m and 1 <= m <= 12]
        if not months:
            return None
        year = self._year_hint(query)
        return ParsedTime(
            times=[to_ym(date(year, m, 1)) for m in months], granularity="month"
        )


def _cn_month(text: str) -> Optional[int]:
    """中文月名 -> 月数（正月/冬月/腊月兼容）。"""
    text = text.strip()
    mapping = {
        "正": 1, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
        "七": 7, "八": 8, "九": 9, "十": 10, "冬": 11, "十一": 11, "腊": 12, "十二": 12,
    }
    if text in mapping:
        return mapping[text]
    return _to_int(text) if re.fullmatch(r"\d+", text) else None


def _cn_lunar_day(text: str) -> Optional[int]:
    """农历日的中文写法 -> 日数（初一/十五/廿三/三十）。"""
    text = text.strip()
    if re.fullmatch(r"\d+", text):
        return int(text)
    base = {"初一": 1, "初二": 2, "初三": 3, "初四": 4, "初五": 5, "初六": 6, "初七": 7,
            "初八": 8, "初九": 9, "初十": 10}
    if text in base:
        return base[text]
    m = re.fullmatch(r"廿([一二三四五六七八九])", text)
    if m:
        return 20 + {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                     "六": 6, "七": 7, "八": 8, "九": 9}[m.group(1)]
    if text == "三十":
        return 30
    return None


def parse_time(query: str, today: Optional[date] = None) -> ParsedTime:
    """便捷入口：规则通道时间解析。"""
    return RuleTimeParser(today).parse(query)
