"""科目名规范化（单一事实来源）。

M4.10 审查修复（L2/L4）：此前"同一套规则"存在三份拷贝
（``app/modules/extractor/handler.py``、``scripts/eval_m2_f1.py``、
``scripts/rebuild_moutai_gt.py``）且已实际分叉——GT 侧剥尾随附注号而
预测侧不剥，严格相等匹配下预测「货币资金1」永远匹配不上 GT
「货币资金」；GT 重建的括号碎片截断不跨行，产出
「信用减值损失损失以-」类损坏科目名（已核实存在于交付 GT）。

本模块为唯一实现：抽取链路（Extractor 落库前、handler 后置安全网）、
F1 评估脚本与 GT 重建脚本共同引用，任一规则改动三处口径同步变化。
仅依赖 ``re``，保证 scripts/ 无 app 依赖环境下也可导入。
"""

from __future__ import annotations

import re

# 行号/行性质前缀（一、营业收入 / （一） / 1. / 减： / 其中：）。
_NAME_PREFIX_RE = re.compile(
    r"^(?:[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\d+[、.]|(?:减|加|其中)：)"
)
# 完整括号注释（（损失以“－”号填列） 等）。
_NAME_PAREN_RE = re.compile(r"（[^）]*）")
# 尾随附注号（科目名末尾 1-3 位数字，如「货币资金1」；A 股三表科目名
# 不以数字结尾，剥离是安全的——与 GT 重建规则对齐）。
_TRAILING_NOTE_RE = re.compile(r"\d{1,3}$")


def normalize_item_name(raw: str) -> str:
    """去行号/前缀/括号注释/尾随附注号/全角符号/空格，得到规范科目名。

    规则（改动任一条将同时影响抽取落库、F1 评估与 GT 重建三处口径）：

    1. 「号填」碎片截断（括号注释跨行拆分只剩半截时按标记截断）；
    2. 行号/行性质前缀剥离；
    3. 完整括号注释移除；
    4. 未闭合开括号截断（跨行碎片「信用减值损失（损失以“-」→
       「信用减值损失」；完整括号对已在上一步移除，剩余「（」必为
       跨行碎片起点，其后内容不可信）；
    5. 尾随附注号剥离（「货币资金1」→「货币资金」）；
    6. 全角符号转半角、去内部空格。

    Args:
        raw: 原始科目名（LLM 输出 / PDF 文本层 / benchmark 行）。

    Returns:
        规范化后的科目名；无有效内容时返回空串（调用方应跳过）。
    """
    name = raw.strip()
    # 括号注释碎片截断（“损失以“－”号填列” 跨行拆分时只剩半截括号）。
    mark = name.find("号填")
    if mark != -1:
        paren = name.rfind("（", 0, mark)
        name = name[:paren] if paren != -1 else name[:mark]
    name = _NAME_PREFIX_RE.sub("", name)
    name = _NAME_PAREN_RE.sub("", name)
    # 未闭合开括号：截断其后全部内容（L4 根因——GT 侧曾产出
    # 「信用减值损失损失以-」，模型输出正确名称也匹配不上）。
    if name.count("（") > name.count("）"):
        name = name[: name.find("（")]
    name = name.replace("（", "").replace("）", "")
    name = _TRAILING_NOTE_RE.sub("", name)
    name = name.replace("－", "-").replace("—", "-").replace("“", "").replace("”", "")
    return re.sub(r"\s+", "", name).strip()


__all__ = ["normalize_item_name"]
