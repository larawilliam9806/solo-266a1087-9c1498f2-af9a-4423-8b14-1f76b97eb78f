"""质检结论的录入与改判。

- 只能为已登记的进料批或工序产出批录入检验号和结论（pending/pass/fail）。
- 改判走乐观版号控制：请求须带上所见版号（未录入的批版号为 0），
  版号已变即拒绝，不产生任何写入。
- 整个"读版号 -> 校验 -> 写入"在单个 BEGIN IMMEDIATE 事务内完成：
  与发货登记并发时，先入账（先提交）的改判必然被发货校验看到；
  任一校验或写入失败即回滚，保留原账。
"""
from .db import connect
from .state import load_state


class BatchNotFoundError(Exception):
    """批号不存在。"""


class NotInspectableError(Exception):
    """批号存在，但不是已登记的进料批或工序产出批。"""


class VersionConflictError(Exception):
    """所见版号与当前版号不一致，改判被拒绝。"""

    def __init__(self, batch_no, expected, current):
        self.batch_no = batch_no
        self.expected = expected
        self.current = current
        super().__init__(
            f"批号 {batch_no} 的质检版号已变：所见版号 {expected}，"
            f"当前版号 {current}，请重新获取批次详情后再改判")


def record_inspection(batch_no, inspection_no, conclusion, expected_version) -> dict:
    """录入或改判某批的质检结论，返回最新结论与版号。

    expected_version 为调用方在批次详情中看到的版号（未录入的批为 0）。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = load_state(conn)
        if batch_no not in state.weights:
            raise BatchNotFoundError(f"批号 {batch_no} 不存在")
        if batch_no not in state.incoming and batch_no not in state.source_process():
            raise NotInspectableError(
                f"批号 {batch_no} 不是已登记的进料批或工序产出批，不能录入质检结论")
        current = state.version_of(batch_no)
        if current != expected_version:
            raise VersionConflictError(batch_no, expected_version, current)
        conn.execute(
            """INSERT INTO inspections(batch_no, inspection_no, conclusion, version)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(batch_no) DO UPDATE SET
                   inspection_no = excluded.inspection_no,
                   conclusion    = excluded.conclusion,
                   version       = inspections.version + 1""",
            (batch_no, inspection_no, conclusion))
        conn.commit()
        return {
            "batch_no": batch_no,
            "inspection_no": inspection_no,
            "conclusion": conclusion,
            "version": current + 1,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
