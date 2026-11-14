"""结构同步引擎（纯函数，可单测）：字段迁移规则 diff + 影响范围隔离。

口径：
1. 结构快照形如 [{"column": "store_id", "dtype": "int"}, ...]，列名是字段自然键。
2. 迁移规则（changes）由调用方显式声明改名；未声明改名时按「删除 + 新增」处理：
   - added：新结构有、旧结构没有，且没有改名指向它；
   - removed：旧结构有、新结构没有，且没有改名离开它；
   - renamed：changes 里显式给出 {old_name, name}，old_name 与 name 都必须存在；
   - type_changed：同名列类型变化。
3. 影响隔离：只有 removed / renamed / type_changed 会让依据失效；added 不影响既有结论。
4. 关系失效：边的任一端列（左列或右列）命中失效旧列名即 stale。
5. 建议失效：建议引用的任一列（粒度外键 / 度量 / 属性 / 主键）命中失效旧列名即 stale；
   仅新增列不会让既有建议过期。
"""
from __future__ import annotations

from typing import Any, Sequence

from src.constants.enums import (
    CHANGE_KIND_ORDER,
    INVALIDATING_CHANGE_KINDS,
    ChangeKind,
)
from src.utils.errors import AppError


def snapshot_from_rows(rows: Sequence[dict]) -> list[dict]:
    """从抽样行推导结构快照（列名 + 推断类型由剖析侧负责，这里只保列名与传入类型）。"""
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return [{"column": column, "dtype": ""} for column in columns]


def normalize_snapshot(snapshot: Sequence[dict]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in snapshot:
        column = str(item.get("column") or "").strip()
        if not column:
            continue
        result[column] = str(item.get("dtype") or "").strip()
    return result


def normalize_changes(changes: Sequence[dict] | None) -> list[dict]:
    normalized: list[dict] = []
    for item in changes or []:
        kind = str(item.get("change_kind") or "").strip()
        if kind not in CHANGE_KIND_ORDER:
            raise AppError("VALIDATION", 422, {"change_kind": kind, "allowed": CHANGE_KIND_ORDER})
        normalized.append(
            {
                "column": str(item.get("column") or "").strip(),
                "old_name": str(item.get("old_name") or "").strip() or None,
                "old_dtype": str(item.get("old_dtype") or "").strip() or None,
                "dtype": str(item.get("dtype") or "").strip() or None,
                "change_kind": kind,
            }
        )
    return normalized


def validate_renames(
    changes: Sequence[dict], before: dict[str, str], after: dict[str, str]
) -> None:
    for change in changes:
        if change["change_kind"] != ChangeKind.RENAMED.value:
            continue
        old_name = change.get("old_name")
        new_name = change.get("column")
        if not old_name or not new_name:
            raise AppError("VALIDATION", 422, {"reason": "改名规则必须同时提供 old_name 与新列名 column"})
        if old_name not in before:
            raise AppError("VALIDATION", 422, {"reason": f"改名源列 {old_name} 不在旧结构中"})
        if new_name not in after:
            raise AppError("VALIDATION", 422, {"reason": f"改名目标列 {new_name} 不在新结构中"})
        if old_name in after:
            raise AppError("VALIDATION", 422, {"reason": f"改名源列 {old_name} 仍存在于新结构，无法判定改名"})
        if new_name in before:
            raise AppError("VALIDATION", 422, {"reason": f"改名目标列 {new_name} 已存在于旧结构，无法判定改名"})


def diff_structure(
    before_snapshot: Sequence[dict],
    after_snapshot: Sequence[dict],
    declared_changes: Sequence[dict] | None = None,
) -> list[dict]:
    """计算字段级迁移规则。declared_changes 仅承载显式改名（renamed）。"""
    before = normalize_snapshot(before_snapshot)
    after = normalize_snapshot(after_snapshot)
    changes = normalize_changes(declared_changes)
    validate_renames(changes, before, after)

    renamed_away = {c["old_name"] for c in changes if c["change_kind"] == ChangeKind.RENAMED.value}
    renamed_to = {c["column"] for c in changes if c["change_kind"] == ChangeKind.RENAMED.value}

    result: list[dict] = list(changes)
    for old_name, old_dtype in before.items():
        if old_name in renamed_away:
            continue
        if old_name not in after:
            result.append(
                {
                    "column": old_name,
                    "old_name": None,
                    "old_dtype": old_dtype or None,
                    "dtype": None,
                    "change_kind": ChangeKind.REMOVED.value,
                }
            )
        elif after[old_name] and old_dtype and after[old_name] != old_dtype:
            result.append(
                {
                    "column": old_name,
                    "old_name": None,
                    "old_dtype": old_dtype,
                    "dtype": after[old_name],
                    "change_kind": ChangeKind.TYPE_CHANGED.value,
                }
            )
    for new_name, new_dtype in after.items():
        if new_name in renamed_to:
            continue
        if new_name not in before:
            result.append(
                {
                    "column": new_name,
                    "old_name": None,
                    "old_dtype": None,
                    "dtype": new_dtype or None,
                    "change_kind": ChangeKind.ADDED.value,
                }
            )

    order = {kind: index for index, kind in enumerate(CHANGE_KIND_ORDER)}
    result.sort(key=lambda item: (order[item["change_kind"]], item["column"]))
    return result


def invalidated_old_columns(changes: Sequence[dict]) -> set[str]:
    """变化后在旧结构里失效的列名（关系与建议按旧列名判定影响）。"""
    stale: set[str] = set()
    for change in changes:
        if change["change_kind"] not in INVALIDATING_CHANGE_KINDS:
            continue
        if change["change_kind"] == ChangeKind.RENAMED.value:
            stale.add(change["old_name"])
        else:
            stale.add(change["column"])
    return stale


def relation_is_affected(relation: Any, table_id: int, stale_columns: set[str]) -> bool:
    if not stale_columns:
        return False
    if relation.left_table_id == table_id and relation.left_column in stale_columns:
        return True
    if relation.right_table_id == table_id and relation.right_column in stale_columns:
        return True
    return False


def suggestion_columns(suggestion: Any) -> set[str]:
    """建议 DDL 依据的全部源列：粒度外键 / 度量 / 属性 / 关联边列。"""
    columns: set[str] = set()
    for name in suggestion.measures or []:
        columns.add(name)
    for name in suggestion.attributes or []:
        columns.add(name)
    for join in suggestion.joins or []:
        fact_column = join.get("fact_column")
        dim_column = join.get("dim_column")
        if fact_column:
            columns.add(fact_column)
        if dim_column:
            columns.add(dim_column)
    grain = suggestion.grain or ""
    return columns


def suggestion_is_affected(
    suggestion: Any,
    table_id: int,
    stale_columns: set[str],
    table_name_by_id: dict[int, str] | None = None,
) -> bool:
    if not stale_columns:
        return False
    # 本表建议：引用列命中即失效（覆盖事实建议 join 的 fact_column 与度量/属性）
    if suggestion.table_id == table_id:
        return bool(suggestion_columns(suggestion) & stale_columns)
    # 其它表建议：它的关联边引用了本次同步表的失效列。
    # 维度建议的 join 用 fact_table/fact_column；事实建议的 join 用 dim_table/dim_column。
    own_name = (table_name_by_id or {}).get(table_id)
    if not own_name:
        return False
    for join in suggestion.joins or []:
        if join.get("fact_table") == own_name and join.get("fact_column") in stale_columns:
            return True
        if join.get("dim_table") == own_name and join.get("dim_column") in stale_columns:
            return True
    return False


def summarize_changes(changes: Sequence[dict]) -> dict[str, int]:
    summary = {kind: 0 for kind in CHANGE_KIND_ORDER}
    for change in changes:
        summary[change["change_kind"]] += 1
    return summary
