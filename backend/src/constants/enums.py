"""共享枚举（后端唯一定义处）。前端副本见 frontend/site/js/config.js。

README「共享枚举出现位置」一节列出全部出现点，改动需两侧同步。
"""
from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"


class TableDomain(StrEnum):
    """贴源表所属主题域（禾丰零售数仓分层前的业务域）。"""

    SALES = "sales"
    INVENTORY = "inventory"
    MEMBER = "member"
    PRODUCT = "product"
    SUPPLY = "supply"
    ORG = "org"
    LOG = "log"
    CONFIG = "config"


TABLE_DOMAIN_LABELS: dict[str, str] = {
    TableDomain.SALES.value: "销售",
    TableDomain.INVENTORY.value: "库存",
    TableDomain.MEMBER.value: "会员",
    TableDomain.PRODUCT.value: "商品",
    TableDomain.SUPPLY.value: "供应",
    TableDomain.ORG.value: "组织",
    TableDomain.LOG.value: "日志",
    TableDomain.CONFIG.value: "配置",
}

TABLE_DOMAIN_ORDER: list[str] = [item.value for item in TableDomain]


class DataType(StrEnum):
    INT = "int"
    DECIMAL = "decimal"
    DATE = "date"
    STRING = "string"
    BOOL = "bool"


DTYPE_LABELS: dict[str, str] = {
    DataType.INT.value: "整数",
    DataType.DECIMAL.value: "数值",
    DataType.DATE.value: "日期",
    DataType.STRING.value: "文本",
    DataType.BOOL.value: "布尔",
}


class RelationKind(StrEnum):
    """包含依赖关系类型：pk-fk 为强主外键（包含度 100%），fk 为弱外键候选。"""

    PK_FK = "pk-fk"
    FK = "fk"


RELATION_KIND_LABELS: dict[str, str] = {
    RelationKind.PK_FK.value: "主外键",
    RelationKind.FK.value: "弱外键候选",
}

RELATION_KIND_ORDER: list[str] = [RelationKind.PK_FK.value, RelationKind.FK.value]


class SuggestionRole(StrEnum):
    FACT = "fact"
    DIM = "dim"


SUGGESTION_ROLE_LABELS: dict[str, str] = {
    SuggestionRole.FACT.value: "事实表",
    SuggestionRole.DIM.value: "维度表",
}

SUGGESTION_ROLE_ORDER: list[str] = [SuggestionRole.FACT.value, SuggestionRole.DIM.value]


class SuggestionStatus(StrEnum):
    PENDING = "pending"
    ADOPTED = "adopted"
    DISMISSED = "dismissed"


SUGGESTION_STATUS_LABELS: dict[str, str] = {
    SuggestionStatus.PENDING.value: "待采纳",
    SuggestionStatus.ADOPTED.value: "已采纳",
    SuggestionStatus.DISMISSED.value: "已驳回",
}

SUGGESTION_STATUS_ORDER: list[str] = [
    SuggestionStatus.PENDING.value,
    SuggestionStatus.ADOPTED.value,
    SuggestionStatus.DISMISSED.value,
]


class ModelRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


MODEL_RUN_STATUS_LABELS: dict[str, str] = {
    ModelRunStatus.QUEUED.value: "排队中",
    ModelRunStatus.RUNNING.value: "剖析中",
    ModelRunStatus.SUCCESS.value: "成功",
    ModelRunStatus.PARTIAL.value: "部分成功",
    ModelRunStatus.FAILED.value: "失败",
}


class TriggerKind(StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    SEED = "seed"
    SYNC = "sync"


TRIGGER_LABELS: dict[str, str] = {
    TriggerKind.MANUAL.value: "手动",
    TriggerKind.SCHEDULE.value: "定时",
    TriggerKind.SEED.value: "初始化",
    TriggerKind.SYNC.value: "结构同步",
}


class ChangeKind(StrEnum):
    """结构同步的字段迁移规则（按字段级隔离影响范围）。"""

    ADDED = "added"          # 新增列：不影响既有建议的导出资格
    REMOVED = "removed"      # 删除列：引用它的关系 / 建议立即失效
    RENAMED = "renamed"      # 改名（迁移规则显式声明 old_name → name）：按删除+新增隔离
    TYPE_CHANGED = "type_changed"  # 类型变更：引用它的建议立即失效


CHANGE_KIND_LABELS: dict[str, str] = {
    ChangeKind.ADDED.value: "新增",
    ChangeKind.REMOVED.value: "删除",
    ChangeKind.RENAMED.value: "改名",
    ChangeKind.TYPE_CHANGED.value: "类型变更",
}

CHANGE_KIND_ORDER: list[str] = [item.value for item in ChangeKind]

# 会让依据失效的迁移规则（新增列不影响既有结论）
INVALIDATING_CHANGE_KINDS = frozenset(
    {ChangeKind.REMOVED.value, ChangeKind.RENAMED.value, ChangeKind.TYPE_CHANGED.value}
)


class SyncStatus(StrEnum):
    """结构同步单状态：失败留痕但不生效，成功后等待管理员确认重建。"""

    SUCCESS = "success"
    FAILED = "failed"
    REBUILT = "rebuilt"  # 管理员已确认并按新结构重新建模


SYNC_STATUS_LABELS: dict[str, str] = {
    SyncStatus.SUCCESS.value: "已同步·待确认",
    SyncStatus.FAILED.value: "同步失败",
    SyncStatus.REBUILT.value: "已重建",
}


class SuggestionFreshness(StrEnum):
    """建议与当前贴源结构的新鲜度（与人工采纳状态正交）。

    current 可导出；stale 依据的字段已变化，导出资格立即失效，需按新结构重建后重新采纳。
    """

    CURRENT = "current"
    STALE = "stale"


SUGGESTION_FRESHNESS_LABELS: dict[str, str] = {
    SuggestionFreshness.CURRENT.value: "依据有效",
    SuggestionFreshness.STALE.value: "依据过期",
}
