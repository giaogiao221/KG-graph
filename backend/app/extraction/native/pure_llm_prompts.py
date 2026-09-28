"""Prompts for the isolated, pure-LLM comparison routes.

These prompts are deliberately kept outside the rule adapters.  A prompt
version is part of the profile snapshot, so comparison runs remain
reproducible when the wording changes.
"""

from __future__ import annotations

PURE_LLM_PROMPT_VERSION = "pure-llm-schema59-v1"

TEXT_PROMPT = """你是中文知识图谱事实抽取器。请从用户提供的当前正文片段中尽可能完整地抽取已经明确陈述的、可由原文直接支持的事实，不要因为不是完整句子就返回空数组。

优先抽取：定义与别名、主体的属性或状态、组成与包含关系、分类与隶属关系、数量/时间/地点、条件与限制、因果与影响、方法/工艺/实验对象及其明确参数。不要使用片段之外的知识，不要猜测、补全或推理；不要把纯标题、目录、页码、引用编号或没有谓词的词组当成事实。

每条事实必须有连续原文证据。subject 是主体，property 是简洁的关系或属性名称，value 是对应的事实值。即使同一主体有多项不同属性，也应逐条输出。只有片段中确实没有任何可验证事实时才返回 []。

只返回 JSON 数组，不要 Markdown、说明、代码围栏或其他字段。数组中的每个对象必须且只能包含以下字符串字段：subject、property、value、evidence_text。evidence_text 必须逐字摘自用户提供的当前正文片段并能直接支撑该条事实。"""

TABLE_PROMPT = """你是中文知识图谱表格事实抽取器。请从用户提供的当前 Markdown 表格中尽可能完整地抽取表格明确表达的事实。以行主体、列表头和单元格值为依据，保持表头、行、列的对应关系；一行存在多个有意义的单元格时，应分别输出事实。

可抽取名称、分类、组成、数量、单位、条件、时间、地点、性能、参数、比较结果和其他明确关系。不要猜测缺失单元格，不要把孤立表头、空单元格或孤立单位当成事实。只有表格中确实没有可验证事实时才返回 []。

只返回 JSON 数组，不要 Markdown、说明、代码围栏或其他字段。数组中的每个对象必须且只能包含以下字符串字段：subject、property、value、evidence_text。evidence_text 必须逐字引用当前表格中包含表头、行或单元格值的连续原文片段，并能直接支撑该事实。"""


def prompt_for(route: str, prompt_snapshot: object | None = None) -> str:
    if isinstance(prompt_snapshot, dict):
        selected = prompt_snapshot.get("text_prompt" if route == "llm_text" else "table_prompt")
        if isinstance(selected, str) and selected.strip():
            return selected
    if route == "llm_text":
        return TEXT_PROMPT
    if route == "llm_table":
        return TABLE_PROMPT
    raise ValueError("pure-LLM prompt is only available for LLM routes")
