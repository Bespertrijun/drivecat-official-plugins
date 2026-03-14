"""
RenameRuleEngine — 8 种重命名规则引擎。

纯函数式设计，每种规则实现 apply(filename, **kwargs) -> str。
RenameRuleEngine.apply_rules(filename, rules, **kwargs) 串联所有规则。
"""

import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


# ── 规则基类 ──


class RenameRule(ABC):
    """重命名规则接口。"""

    @abstractmethod
    def apply(self, filename: str, **kwargs: Any) -> str:
        """
        对文件名应用规则。

        Args:
            filename: 不含扩展名的文件名
            **kwargs: 额外上下文（如 index 用于序号规则）

        Returns:
            修改后的文件名
        """
        ...

    @abstractmethod
    def describe(self) -> str:
        """返回规则的中文描述。"""
        ...


# ── 具体规则 ──


class InsertRule(RenameRule):
    """在指定位置插入文本。"""

    def __init__(self, text: str, position: int = 0):
        """
        Args:
            text: 要插入的文本
            position: 插入位置（0=开头，-1=末尾，正数=从左起）
        """
        self.text = text
        self.position = position

    def apply(self, filename: str, **kwargs) -> str:
        if self.position == -1 or self.position >= len(filename):
            return filename + self.text
        return filename[: self.position] + self.text + filename[self.position :]

    def describe(self) -> str:
        pos = "开头" if self.position == 0 else ("末尾" if self.position == -1 else f"位置{self.position}")
        return f"在{pos}插入 '{self.text}'"


class ReplaceRule(RenameRule):
    """简单查找替换。"""

    def __init__(self, pattern: str, replacement: str):
        self.pattern = pattern
        self.replacement = replacement

    def apply(self, filename: str, **kwargs) -> str:
        return filename.replace(self.pattern, self.replacement)

    def describe(self) -> str:
        return f"将 '{self.pattern}' 替换为 '{self.replacement}'"


class RegexRule(RenameRule):
    """正则表达式替换。"""

    def __init__(self, pattern: str, replacement: str, flags: str = ""):
        self.pattern = pattern
        self.replacement = replacement
        self.flags = self._parse_flags(flags)

    @staticmethod
    def _parse_flags(flags_str: str) -> int:
        flag_map = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}
        result = 0
        for c in flags_str:
            if c in flag_map:
                result |= flag_map[c]
        return result

    def apply(self, filename: str, **kwargs) -> str:
        return re.sub(self.pattern, self.replacement, filename, flags=self.flags)

    def describe(self) -> str:
        return f"正则 /{self.pattern}/ → '{self.replacement}'"


class DeleteRule(RenameRule):
    """删除指定字符。"""

    def __init__(
        self,
        target: str = "",
        position: str = "all",
        count: int = 0,
    ):
        """
        Args:
            target: 要删除的文本（position='all' 时使用）
            position: 'all'=删除所有匹配, 'start'=删除前 count 个字符, 'end'=删除后 count 个字符
            count: 删除字符数（position='start'/'end' 时使用）
        """
        self.target = target
        self.position = position
        self.count = count

    def apply(self, filename: str, **kwargs) -> str:
        if self.position == "start" and self.count > 0:
            return filename[self.count :]
        elif self.position == "end" and self.count > 0:
            return filename[: -self.count] if self.count < len(filename) else ""
        else:
            return filename.replace(self.target, "")

    def describe(self) -> str:
        if self.position == "start":
            return f"删除前 {self.count} 个字符"
        elif self.position == "end":
            return f"删除后 {self.count} 个字符"
        return f"删除所有 '{self.target}'"


class SequenceRule(RenameRule):
    """添加序号。"""

    def __init__(
        self,
        start_num: int = 1,
        step: int = 1,
        padding: int = 3,
        position: int = -1,
        separator: str = "_",
    ):
        self.start_num = start_num
        self.step = step
        self.padding = padding
        self.position = position
        self.separator = separator

    def apply(self, filename: str, **kwargs) -> str:
        index = kwargs.get("index", 0)
        num = self.start_num + index * self.step
        seq = str(num).zfill(self.padding)

        if self.position == 0:
            return seq + self.separator + filename
        else:
            return filename + self.separator + seq

    def describe(self) -> str:
        return f"添加序号（从 {self.start_num} 开始，步长 {self.step}）"


class PadRule(RenameRule):
    """数字填充/补齐。"""

    def __init__(self, target_digits: int = 3, pad_char: str = "0"):
        self.target_digits = target_digits
        self.pad_char = pad_char

    def apply(self, filename: str, **kwargs) -> str:
        def pad_match(m: re.Match) -> str:
            return m.group().zfill(self.target_digits)

        return re.sub(r"\d+", pad_match, filename)

    def describe(self) -> str:
        return f"数字补齐到 {self.target_digits} 位"


class CaseRule(RenameRule):
    """大小写转换。"""

    def __init__(self, case_type: str = "lower"):
        """
        Args:
            case_type: 'upper', 'lower', 'title', 'capitalize', 'swap'
        """
        self.case_type = case_type

    def apply(self, filename: str, **kwargs) -> str:
        funcs = {
            "upper": str.upper,
            "lower": str.lower,
            "title": str.title,
            "capitalize": str.capitalize,
            "swap": str.swapcase,
        }
        func = funcs.get(self.case_type, str.lower)
        return func(filename)

    def describe(self) -> str:
        labels = {
            "upper": "全部大写",
            "lower": "全部小写",
            "title": "首字母大写",
            "capitalize": "句首大写",
            "swap": "大小写互换",
        }
        return labels.get(self.case_type, "大小写转换")


class DateRule(RenameRule):
    """基于日期插入文本。"""

    def __init__(
        self,
        format: str = "%Y%m%d",
        position: int = 0,
        separator: str = "_",
    ):
        self.format = format
        self.position = position
        self.separator = separator

    def apply(self, filename: str, **kwargs) -> str:
        # 优先用 kwargs 中的 mtime，否则用当前时间
        mtime = kwargs.get("mtime")
        if mtime:
            dt = datetime.fromtimestamp(mtime)
        else:
            dt = datetime.now()

        date_str = dt.strftime(self.format)

        if self.position == 0:
            return date_str + self.separator + filename
        else:
            return filename + self.separator + date_str

    def describe(self) -> str:
        return f"添加日期（格式: {self.format}）"


# ── 规则引擎 ──

# 规则类型 → 类映射
RULE_TYPE_MAP = {
    "insert": InsertRule,
    "replace": ReplaceRule,
    "regex": RegexRule,
    "delete": DeleteRule,
    "sequence": SequenceRule,
    "pad": PadRule,
    "case": CaseRule,
    "date": DateRule,
}


class RuleSpec(BaseModel):
    """规则描述（从 API 接收）。"""

    type: str  # insert / replace / regex / delete / sequence / pad / case / date
    params: Dict[str, Any] = {}


def create_rule(spec: RuleSpec) -> RenameRule:
    """从 RuleSpec 创建规则实例。"""
    cls = RULE_TYPE_MAP.get(spec.type)
    if cls is None:
        raise ValueError(f"Unknown rule type: {spec.type}")
    return cls(**spec.params)


class RenameRuleEngine:
    """规则引擎，串联多条规则。"""

    @staticmethod
    def apply_rules(
        filename: str,
        rules: List[RenameRule],
        **kwargs: Any,
    ) -> str:
        """
        对文件名依次应用所有规则。

        规则操作的是不含扩展名的文件名部分。
        """
        # 分离文件名和扩展名
        dot_pos = filename.rfind(".")
        if dot_pos > 0:
            name_part = filename[:dot_pos]
            ext_part = filename[dot_pos:]
        else:
            name_part = filename
            ext_part = ""

        for rule in rules:
            name_part = rule.apply(name_part, **kwargs)

        return name_part + ext_part
