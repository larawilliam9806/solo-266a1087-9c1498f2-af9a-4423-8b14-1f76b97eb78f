"""API 端到端测试：登记、冲突回滚、循环拒绝、上下界与分配单核对、
质检结论录入/改判（版号并发控制）、发货全链合格闸及其与改判的并发。"""
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.main import app
    with TestClient(app) as c:
        yield c


def register(client, docs, expect=201):
    resp = client.post("/api/documents", json={"documents": docs})
    assert resp.status_code == expect, resp.text
    return resp.json() if expect == 201 else resp


def incoming(batch, weight):
    return {"type": "incoming", "batch": batch, "weight": weight}


def process(inputs, outputs):
    return {"type": "process",
            "inputs": [{"batch": b, "weight": w} for b, w in inputs],
            "outputs": [{"batch": b, "weight": w} for b, w in outputs]}


def shipment(batch, weight):
    return {"type": "shipment", "batch": batch, "weight": weight}


def inspect(client, batch, conclusion="pass", expected_version=0,
            inspection_no="QC-001", expect=200):
    resp = client.put(f"/api/batches/{batch}/inspection",
                      json={"inspection_no": inspection_no,
                            "conclusion": conclusion,
                            "expected_version": expected_version})
    assert resp.status_code == expect, resp.text
    return resp.json() if expect == 200 else resp


def pass_all(client, batches):
    for b in batches:
        inspect(client, b, "pass")


def trace(client, batch, expect=200):
    resp = client.get(f"/api/trace/{batch}")
    assert resp.status_code == expect, resp.text
    return resp.json() if expect == 200 else resp


def by_batch(shipments):
    return {s["batch"]: s for s in shipments}


def check_witness(witness, weights, defective, defective_weight, target, extreme):
    """分配单必须：总量出自不合格批、不超各批称重、逐工序守恒、目标批等于极值。"""
    bg = witness["batch_grams"]
    assert bg[defective] == defective_weight
    for b, g in bg.items():
        assert 0 <= g <= weights[b], (b, g)
    assert bg.get(target, 0) == extreme
    for p in witness["processes"]:
        assert sum(p["input_grams"].values()) == sum(p["output_grams"].values())
        for b, g in list(p["input_grams"].items()) + list(p["output_grams"].items()):
            assert 0 <= g <= weights[b], (b, g)


# 混炼分装 + 回炉场景：
#   A(1000,不合格) + X(500) --P1--> B(900) + C(600)
#   C(600) --P2--> S1(400) + R(200)
#   R(200) + Y(300) --P3--> S2(500)     （R 回炉）
#   发货: B, S1, S2
MAIN_DOCS = [
    incoming("A", 1000), incoming("X", 500), incoming("Y", 300),
    process([("A", 1000), ("X", 500)], [("B", 900), ("C", 600)]),
    process([("C", 600)], [("S1", 400), ("R", 200)]),
    process([("R", 200), ("Y", 300)], [("S2", 500)]),
]
MAIN_SHIPMENTS = [shipment("B", 900), shipment("S1", 400), shipment("S2", 500)]
MAIN_WEIGHTS = {"A": 1000, "X": 500, "Y": 300, "B": 900, "C": 600,
                "S1": 400, "R": 200, "S2": 500}


def register_main(client):
    """登记主场景：单据落账 -> 全链判合格 -> 发货。"""
    ids = register(client, MAIN_DOCS)
    pass_all(client, MAIN_WEIGHTS)
    register(client, MAIN_SHIPMENTS)
    return ids


def test_trace_bounds_and_witnesses(client):
    register_main(client)
    body = trace(client, "A")
    assert body["defective_weight"] == 1000
    got = by_batch(body["shipments"])
    assert set(got) == {"B", "S1", "S2"}
    assert (got["B"]["min_grams"], got["B"]["max_grams"]) == (400, 900)
    assert (got["S1"]["min_grams"], got["S1"]["max_grams"]) == (0, 400)
    assert (got["S2"]["min_grams"], got["S2"]["max_grams"]) == (0, 200)
    for batch, entry in got.items():
        assert entry["status"] == "ok"
        check_witness(entry["min_witness"], MAIN_WEIGHTS, "A", 1000,
                      batch, entry["min_grams"])
        check_witness(entry["max_witness"], MAIN_WEIGHTS, "A", 1000,
                      batch, entry["max_grams"])


def test_trace_other_incoming(client):
    register_main(client)
    got = by_batch(trace(client, "X")["shipments"])
    assert (got["B"]["min_grams"], got["B"]["max_grams"]) == (0, 500)
    assert (got["S1"]["min_grams"], got["S1"]["max_grams"]) == (0, 400)
    assert (got["S2"]["min_grams"], got["S2"]["max_grams"]) == (0, 200)
    # Y 只经 P3 流向 S2，且 S2 是其唯一出口：300 克必然全部落在 S2
    got_y = by_batch(trace(client, "Y")["shipments"])
    assert (got_y["B"]["min_grams"], got_y["B"]["max_grams"]) == (0, 0)
    assert (got_y["S1"]["min_grams"], got_y["S1"]["max_grams"]) == (0, 0)
    assert (got_y["S2"]["min_grams"], got_y["S2"]["max_grams"]) == (300, 300)


def test_late_source_blocks_then_resolves(client):
    register(client, [
        incoming("D", 100), incoming("E", 70),
        process([("D", 100)], [("M", 100)]),
        process([("M", 100), ("Y", 50)], [("S", 150)]),  # Y 来源缺单
    ])
    pass_all(client, ["D", "E", "M", "S"])
    # Y 来源缺单、无法录入结论（按待检），阻断 S 的发货
    resp = register(client, [shipment("S", 150)], expect=409)
    assert resp.json()["detail"]["blocking_batches"] == ["Y"]
    # 链完整的 E 照常发货
    register(client, [shipment("E", 70)])
    # 补齐来源单并判合格后，S 即可发货
    register(client, [incoming("Y", 50)])
    inspect(client, "Y", "pass")
    register(client, [shipment("S", 150)])
    got = by_batch(trace(client, "D")["shipments"])
    assert (got["S"]["min_grams"], got["S"]["max_grams"]) == (100, 100)
    check_witness(got["S"]["min_witness"],
                  {"D": 100, "M": 100, "Y": 50, "S": 150, "E": 70},
                  "D", 100, "S", 100)


def test_trace_pending_for_externally_planted_shipment(client, monkeypatch, tmp_path):
    # 质检闸保证 API 登记的发货全链合格；若账簿由 API 之外写入（迁移/补录），
    # 已发批上游缺单时追溯仍返回待补批号
    register(client, [incoming("D", 10)])
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.execute("INSERT INTO batches(batch_no, weight) VALUES ('Z', 5)")
    conn.execute("INSERT INTO shipments(batch_no) VALUES ('Z')")
    conn.commit()
    conn.close()
    got = by_batch(trace(client, "D")["shipments"])
    assert got["Z"] == {"batch": "Z", "status": "pending", "missing_batches": ["Z"]}


def test_weight_conflict_rolls_back(client):
    register(client, [incoming("A", 100)])
    resp = register(client, [process([("A", 150)], [("B", 150)])], expect=409)
    assert "重量冲突" in resp.json()["detail"]
    batch = client.get("/api/batches/A").json()
    assert batch["weight"] == 100
    assert batch["destination"] == {"type": "none"}
    assert client.get("/api/batches/B").status_code == 404


def test_cycle_rejected(client):
    resp = register(client, [
        process([("A", 100)], [("B", 100)]),
        process([("B", 100)], [("C", 100)]),
        process([("C", 100)], [("A", 100)]),  # A<-P3, 形成 A->P1->B->P2->C->P3->A
    ], expect=409)
    assert "循环" in resp.json()["detail"]
    assert client.get("/api/batches/B").status_code == 404


def test_unbalanced_process_rejected(client):
    resp = register(client, [process([("A", 100)], [("B", 90)])], expect=422)
    assert "不等于" in str(resp.json())


def test_process_structure_rejected(client):
    assert register(client, [process([("A", 50), ("A", 50)], [("B", 100)])],
                    expect=422).status_code == 422  # 投入重复
    assert register(client, [process([("A", 100)], [("A", 100)])],
                    expect=422).status_code == 422  # 投入产出重叠
    assert register(client, [{"type": "incoming", "batch": "A", "weight": 0}],
                    expect=422).status_code == 422  # 重量非正整数
    assert register(client, [{"type": "incoming", "batch": "A", "weight": 1.5}],
                    expect=422).status_code == 422


def test_duplicate_source_and_destination(client):
    register(client, [incoming("A", 100), process([("A", 100)], [("B", 100)])])
    pass_all(client, ["A", "B"])
    register(client, [shipment("B", 100)])
    assert register(client, [incoming("A", 100)], expect=409).status_code == 409
    assert register(client, [process([("X", 1)], [("B", 1)])],
                    expect=409).status_code == 409   # B 已有来源
    assert register(client, [process([("B", 100)], [("C", 100)])],
                    expect=409).status_code == 409   # B 已有去向
    assert register(client, [shipment("B", 100)], expect=409).status_code == 409


def test_atomicity_across_documents(client):
    register(client, [incoming("K", 5)])
    inspect(client, "K")
    register(client, [shipment("K", 5)])
    resp = register(client, [incoming("N", 10), shipment("K", 5)], expect=409)
    assert "已有去向" in resp.json()["detail"]
    assert client.get("/api/batches/N").status_code == 404  # 整次不生效


def test_same_request_late_source_then_shipment(client):
    # 同一请求内：工序先用料、进料后补；判合格后即可发货
    result = register(client, [
        process([("A", 100)], [("B", 60), ("C", 40)]),
        incoming("A", 100),
    ])
    assert result["batches_created"] == ["A", "B", "C"]
    pass_all(client, ["A", "B"])
    register(client, [shipment("B", 60)])
    got = by_batch(trace(client, "A")["shipments"])
    assert (got["B"]["min_grams"], got["B"]["max_grams"]) == (60, 60)


def test_trace_query_errors(client):
    register(client, [process([("A", 100)], [("B", 100)])])
    assert trace(client, "ZZ", expect=404).status_code == 404
    assert trace(client, "B", expect=400).status_code == 400  # B 不是进料批


def test_direct_shipment_of_incoming(client):
    register(client, [incoming("A", 100)])
    inspect(client, "A")
    register(client, [shipment("A", 100)])
    got = by_batch(trace(client, "A")["shipments"])
    assert (got["A"]["min_grams"], got["A"]["max_grams"]) == (100, 100)


def test_batch_and_process_endpoints(client):
    ids = register_main(client)
    b = client.get("/api/batches/C").json()
    assert b["weight"] == 600
    assert b["source"] == {"type": "process", "process_id": ids["process_ids"][0]}
    assert b["destination"] == {"type": "process", "process_id": ids["process_ids"][1]}
    assert b["inspection"] == {"inspection_no": "QC-001",
                               "conclusion": "pass", "version": 1}
    assert b["unqualified_batches"] == []
    p = client.get(f"/api/processes/{ids['process_ids'][0]}").json()
    assert {x["batch_no"] for x in p["inputs"]} == {"A", "X"}
    assert {x["batch_no"] for x in p["outputs"]} == {"B", "C"}
    assert sum(x["weight"] for x in p["inputs"]) == sum(x["weight"] for x in p["outputs"])
    ships = client.get("/api/shipments").json()["shipments"]
    assert {s["batch_no"] for s in ships} == {"B", "S1", "S2"}


# ---------------- 质检结论：录入、版号、未合格清单 ----------------

def test_inspection_defaults_to_pending(client):
    register(client, [incoming("A", 100)])
    b = client.get("/api/batches/A").json()
    assert b["inspection"] == {"inspection_no": None,
                               "conclusion": "pending", "version": 0}
    assert b["unqualified_batches"] == ["A"]  # 未录入按待检，本批即未合格


def test_inspection_record_and_rejudge_with_version(client):
    register(client, [incoming("A", 100), process([("A", 100)], [("B", 100)])])
    rec = inspect(client, "B", "fail", inspection_no="QC-1")
    assert rec == {"batch_no": "B", "inspection_no": "QC-1",
                   "conclusion": "fail", "version": 1}
    # 改判须带所见版号：版号已变即 409，且原账不变
    resp = inspect(client, "B", "pass", expected_version=0, expect=409)
    assert resp.json()["detail"]["current_version"] == 1
    b = client.get("/api/batches/B").json()
    assert b["inspection"] == {"inspection_no": "QC-1",
                               "conclusion": "fail", "version": 1}
    # 带上所见版号即可改判，版号 +1
    rec = inspect(client, "B", "pass", expected_version=1, inspection_no="QC-2")
    assert rec["conclusion"] == "pass" and rec["version"] == 2
    b = client.get("/api/batches/B").json()
    assert b["inspection"] == {"inspection_no": "QC-2",
                               "conclusion": "pass", "version": 2}


def test_inspection_target_must_be_registered_source(client):
    register(client, [process([("M", 100)], [("S", 100)])])  # M 来源缺单
    assert inspect(client, "ZZ", expect=404).status_code == 404   # 批号不存在
    assert inspect(client, "M", expect=400).status_code == 400  # 非进料/工序产出
    inspect(client, "S")  # 工序产出批可录入


def test_inspection_request_validation(client):
    register(client, [incoming("A", 100)])
    for body in ({"inspection_no": "Q", "conclusion": "ok", "expected_version": 0},
                 {"inspection_no": "", "conclusion": "pass", "expected_version": 0},
                 {"inspection_no": "Q", "conclusion": "pass", "expected_version": -1}):
        resp = client.put("/api/batches/A/inspection", json=body)
        assert resp.status_code == 422, resp.text
    assert client.get("/api/batches/A").json()["inspection"]["version"] == 0


def test_unqualified_batches_listing(client):
    register(client, [incoming("A", 100),
                      process([("A", 100)], [("B", 100)]),
                      process([("B", 100)], [("S", 100)])])
    inspect(client, "A", "fail")
    inspect(client, "S", "pass")  # B 未录入（待检）
    assert client.get("/api/batches/S").json()["unqualified_batches"] == ["A", "B"]
    assert client.get("/api/batches/B").json()["unqualified_batches"] == ["A", "B"]
    assert client.get("/api/batches/A").json()["unqualified_batches"] == ["A"]


# ---------------- 发货质检闸 ----------------

def test_shipment_blocked_until_whole_chain_passes(client):
    register(client, [incoming("A", 100), process([("A", 100)], [("B", 100)])])
    # 未录入任何结论：全部待检，阻断发货
    resp = register(client, [shipment("B", 100)], expect=409)
    detail = resp.json()["detail"]
    assert detail["blocking_batches"] == ["A", "B"]
    assert detail["shipments"] == {"B": ["A", "B"]}
    assert client.get("/api/shipments").json()["shipments"] == []  # 整笔不生效
    # 部分合格仍阻断
    inspect(client, "B", "pass")
    resp = register(client, [shipment("B", 100)], expect=409)
    assert resp.json()["detail"]["blocking_batches"] == ["A"]
    # 全链合格才入账
    inspect(client, "A", "pass")
    register(client, [shipment("B", 100)])
    ships = client.get("/api/shipments").json()["shipments"]
    assert [s["batch_no"] for s in ships] == ["B"]


def test_shipment_blocked_by_fail_and_explicit_pending(client):
    register(client, [incoming("A", 100), incoming("P", 50),
                      process([("A", 100), ("P", 50)], [("S", 150)])])
    pass_all(client, ["A", "P", "S"])
    inspect(client, "A", "fail", expected_version=1)  # 上游改判不合格
    resp = register(client, [shipment("S", 150)], expect=409)
    assert resp.json()["detail"]["blocking_batches"] == ["A"]
    # 改回合格；显式录入"待检"同样阻断
    inspect(client, "A", "pass", expected_version=2)
    inspect(client, "P", "pending", expected_version=1)
    resp = register(client, [shipment("S", 150)], expect=409)
    assert resp.json()["detail"]["blocking_batches"] == ["P"]
    inspect(client, "P", "pass", expected_version=2)
    register(client, [shipment("S", 150)])


def test_shipment_block_rolls_back_whole_request(client):
    register(client, [incoming("A", 100), incoming("B", 50),
                      process([("A", 100)], [("S1", 100)])])
    pass_all(client, ["A", "S1", "B"])
    inspect(client, "B", "fail", expected_version=1)
    resp = register(client, [shipment("S1", 100), shipment("B", 50)], expect=409)
    detail = resp.json()["detail"]
    assert detail["blocking_batches"] == ["B"]
    assert detail["shipments"] == {"B": ["B"]}  # 按发货批分别列出阻断批号
    # 整笔不生效：链全合格的 S1 也未入账
    assert client.get("/api/shipments").json()["shipments"] == []
    register(client, [shipment("S1", 100)])


def test_committed_rejudge_participates_in_shipment_check(client):
    # 先入账的改判必须参与发货校验
    register(client, [incoming("A", 100), process([("A", 100)], [("S", 100)])])
    pass_all(client, ["A", "S"])
    inspect(client, "A", "fail", expected_version=1)  # 改判先入账
    resp = register(client, [shipment("S", 100)], expect=409)
    assert resp.json()["detail"]["blocking_batches"] == ["A"]


def test_concurrent_rejudge_first_blocks_shipment(client):
    # 改判与发货并发：改判先入账时，发货必须看到并被阻断
    register(client, [incoming("A", 100), process([("A", 100)], [("S", 100)])])
    pass_all(client, ["A", "S"])
    outcome = {}

    def rejudge():
        outcome["rejudge"] = inspect(client, "A", "fail", expected_version=1)

    def ship():
        time.sleep(0.2)  # 让改判先入账
        resp = client.post("/api/documents",
                           json={"documents": [shipment("S", 100)]})
        outcome["ship"] = resp.status_code

    threads = [threading.Thread(target=ship), threading.Thread(target=rejudge)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcome["rejudge"]["conclusion"] == "fail"
    assert outcome["ship"] == 409
    assert client.get("/api/shipments").json()["shipments"] == []


def test_concurrent_shipment_first_then_rejudge(client):
    # 发货先入账时改判不影响已入账的发货，但改判本身仍生效
    register(client, [incoming("A", 100), process([("A", 100)], [("S", 100)])])
    pass_all(client, ["A", "S"])
    outcome = {}

    def ship():
        resp = client.post("/api/documents",
                           json={"documents": [shipment("S", 100)]})
        outcome["ship"] = resp.status_code

    def rejudge():
        time.sleep(0.2)  # 让发货先入账
        outcome["rejudge"] = inspect(client, "A", "fail", expected_version=1)

    threads = [threading.Thread(target=rejudge), threading.Thread(target=ship)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcome["ship"] == 201
    assert outcome["rejudge"]["conclusion"] == "fail"
    assert client.get("/api/batches/A").json()["inspection"]["conclusion"] == "fail"
    ships = client.get("/api/shipments").json()["shipments"]
    assert [s["batch_no"] for s in ships] == ["S"]


def test_concurrent_rejudges_same_version_exactly_one_wins(client):
    # 两个改判带同一所见版号并发：恰好一个入账，另一个因版号已变被拒
    register(client, [incoming("A", 100)])
    inspect(client, "A", "pass")  # 版号 1
    outcomes = []

    def rejudge(conclusion):
        resp = client.put("/api/batches/A/inspection",
                          json={"inspection_no": "QC-X", "conclusion": conclusion,
                                "expected_version": 1})
        outcomes.append(resp.status_code)

    threads = [threading.Thread(target=rejudge, args=(c,))
               for c in ("pass", "fail")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == [200, 409]
    assert client.get("/api/batches/A").json()["inspection"]["version"] == 2
