"""上下文 token 估算（确定性、零外部依赖）。

OpenAI 兼容模型（qwen/deepseek 等）不暴露统一 tokenizer，这里用字符级启发式估算
进入模型的上下文规模，仅用于"上下文占比圆环"的容量提示（非精确计费）：

- 中日韩等宽字符（含中文标点）按约 1 token/字计；
- 其余 ASCII（英文/数字/空格）按约 4 字符/token 计（0.25 token/字符）。

需要精确值时可在后续替换为各模型官方 tokenizer，接口保持 `estimate_tokens(text) -> int`。
"""

from __future__ import annotations

import math
import re
from typing import Iterable

# 中日韩统一表意文字 + 中文标点/全角符号
_CJK_RE = re.compile(r"[一-鿿　-〿＀-￯]")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return int(math.ceil(cjk + other * 0.25))


def estimate_tokens_many(texts: Iterable[str]) -> int:
    return sum(estimate_tokens(t) for t in texts if t)
