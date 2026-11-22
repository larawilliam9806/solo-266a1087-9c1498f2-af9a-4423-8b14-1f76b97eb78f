"""从 SQLite 载入完整账簿状态，供校验与追溯计算使用。"""
from dataclasses import dataclass, field


@dataclass
class State:
    weights: dict = field(default_factory=dict)      # batch_no -> 克
    incoming: set = field(default_factory=set)       # 已登记进料的批号
    processes: dict = field(default_factory=dict)    # pid -> {"inputs": [...], "outputs": [...]}
    shipments: dict = field(default_factory=dict)    # batch_no -> shipment_id
    inspections: dict = field(default_factory=dict)  # batch_no -> {"inspection_no", "conclusion", "version"}

    def source_process(self) -> dict:
        """batch_no -> 产出它的工序 id"""
        return {b: pid for pid, p in self.processes.items() for b in p["outputs"]}

    def dest_process(self) -> dict:
        """batch_no -> 消耗它的工序 id"""
        return {b: pid for pid, p in self.processes.items() for b in p["inputs"]}

    def conclusion_of(self, batch_no) -> str:
        """质检结论；未录入的批按待检处理。"""
        rec = self.inspections.get(batch_no)
        return rec["conclusion"] if rec else "pending"

    def version_of(self, batch_no) -> int:
        """质检版号；未录入为 0，首次录入为 1，每次改判 +1。"""
        rec = self.inspections.get(batch_no)
        return rec["version"] if rec else 0

    def upstream_closure(self, batch_no) -> set:
        """沿来源向上的全部上游批号（不含本批）。账簿无环，遍历必终止。"""
        src = self.source_process()
        seen, stack = set(), [batch_no]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            pid = src.get(x)
            if pid is not None:
                stack.extend(self.processes[pid]["inputs"])
        seen.discard(batch_no)
        return seen

    def unqualified_in_chain(self, batch_no) -> list:
        """本批及全部上游中结论不是 pass（待检/不合格）的批号，升序。"""
        chain = self.upstream_closure(batch_no) | {batch_no}
        return sorted(b for b in chain if self.conclusion_of(b) != "pass")


def load_state(conn) -> State:
    weights = {r["batch_no"]: r["weight"]
               for r in conn.execute("SELECT batch_no, weight FROM batches")}
    incoming = {r["batch_no"] for r in conn.execute("SELECT batch_no FROM incoming")}
    processes = {r["id"]: {"inputs": [], "outputs": []}
                 for r in conn.execute("SELECT id FROM processes")}
    for r in conn.execute("SELECT process_id, batch_no FROM process_inputs ORDER BY rowid"):
        processes[r["process_id"]]["inputs"].append(r["batch_no"])
    for r in conn.execute("SELECT process_id, batch_no FROM process_outputs ORDER BY rowid"):
        processes[r["process_id"]]["outputs"].append(r["batch_no"])
    shipments = {r["batch_no"]: r["id"]
                 for r in conn.execute("SELECT id, batch_no FROM shipments")}
    inspections = {r["batch_no"]: {"inspection_no": r["inspection_no"],
                                   "conclusion": r["conclusion"],
                                   "version": r["version"]}
                   for r in conn.execute(
                       "SELECT batch_no, inspection_no, conclusion, version FROM inspections")}
    return State(weights=weights, incoming=incoming,
                 processes=processes, shipments=shipments,
                 inspections=inspections)
