# -*- coding: utf-8 -*-
"""精读提示词（API 与 WebChat 共用）——由领域包 domain.json 驱动，代码零领域绑定。"""
from .domain import system_prompt

# 动态属性：每次访问按当前领域包生成（改配置后重启生效）
def __getattr__(name):
    if name == "SYSTEM_PROMPT":
        return system_prompt()
    raise AttributeError(name)
