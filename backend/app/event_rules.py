"""统一事件规则：动作、状态序列、编号口径只在这里声明一次。

事件边界（``app.events``）与各业务写入方共用本模块的规则，禁止在服务或路由里
再各自维护一份动作映射，避免同一动作在业务页与审计页得出不同结论。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleRule:
    """单个业务模块接入统一事件边界所需的全部规则。"""

    module: str
    label: str
    code_field: str
    #: 允许出现的记录编号前缀；保留历史前缀是为了兼容改造前已落库的编号。
    code_prefixes: tuple[str, ...]
    status_order: tuple[str, ...]
    #: 动作 -> 目标状态（动作名称保持改造前口径不变）。
    action_targets: dict[str, str]
    #: 动作 -> 允许执行该动作时记录必须处于的源状态，重复推进在此被拦下。
    action_sources: dict[str, str]
    negative_actions: frozenset[str]
    #: audit_row：业务模块，成功事件投影成一条审计记录；
    #: self：审计模块自身，事件只回写同一条审计记录，不再新增审计行。
    projection: str


RULES: dict[str, ModuleRule] = {
    "lifecycle": ModuleRule(
        module="lifecycle",
        label="表生命周期管理",
        code_field="策略编号",
        code_prefixes=("LIFE-",),
        status_order=("草稿", "生效中", "已到期", "已归档"),
        action_targets={"启用策略": "生效中", "归档表": "已归档", "删除表": "已到期"},
        action_sources={"启用策略": "草稿", "归档表": "生效中", "删除表": "已到期"},
        negative_actions=frozenset(),
        projection="audit_row",
    ),
    "catalog": ModuleRule(
        module="catalog",
        label="数据资产盘点",
        code_field="资产编号",
        code_prefixes=("CATA-",),
        status_order=("草稿", "已盘点", "已复核", "已归档"),
        action_targets={"提交复核": "已盘点", "归档资产": "已归档", "废弃资产": "已归档"},
        action_sources={"提交复核": "草稿", "归档资产": "已复核", "废弃资产": "已复核"},
        negative_actions=frozenset(),
        projection="audit_row",
    ),
    "retention": ModuleRule(
        module="retention",
        label="数据保留策略",
        code_field="规则编号",
        # RETE- 是改造前种子数据使用的历史前缀，RETN- 是语义编号，两者都接受。
        code_prefixes=("RETN-", "RETE-"),
        status_order=("草稿", "生效中", "已到期", "已废止"),
        action_targets={"启用规则": "生效中", "废止规则": "已废止", "归档规则": "已到期"},
        action_sources={"启用规则": "草稿", "废止规则": "生效中", "归档规则": "已到期"},
        negative_actions=frozenset(),
        projection="audit_row",
    ),
    "recycle": ModuleRule(
        module="recycle",
        label="数据回收站",
        code_field="记录编号",
        code_prefixes=("RECY-",),
        status_order=("待恢复", "已恢复", "已清理", "已过期"),
        action_targets={"恢复数据": "已恢复", "永久清理": "已清理", "延期保留": "已过期"},
        action_sources={"恢复数据": "待恢复", "永久清理": "已恢复", "延期保留": "已过期"},
        negative_actions=frozenset(),
        projection="audit_row",
    ),
    "audit": ModuleRule(
        module="audit",
        label="变更审计追踪",
        code_field="记录编号",
        code_prefixes=("AUDI-",),
        status_order=("已记录", "已复核", "已归档"),
        action_targets={"标记复核": "已复核", "归档记录": "已归档"},
        action_sources={"标记复核": "已记录", "归档记录": "已复核"},
        negative_actions=frozenset(),
        projection="self",
    ),
}


def rule_of(module: str) -> ModuleRule:
    """取模块规则；未接入统一事件边界的模块直接报错，不允许静默走老路。"""
    try:
        return RULES[module]
    except KeyError as exc:
        raise KeyError(f"模块「{module}」未接入统一事件边界") from exc


def check_record_no(rule: ModuleRule, record_no: object) -> bool:
    """按模块编号前缀做兼容核对，历史前缀与语义前缀都视为合法。"""
    text = str(record_no or "")
    return any(text.startswith(prefix) for prefix in rule.code_prefixes)
