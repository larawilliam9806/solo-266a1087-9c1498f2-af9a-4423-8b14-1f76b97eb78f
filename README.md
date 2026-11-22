# 再生塑料发货追溯 API

再生塑料混炼后分装、部分分装料回炉的生产场景下，用 **FastAPI + SQLite** 实现的
发货追溯服务：登记进料批、工序投入/产出批与整批发货；为批次录入质检结论
（待检/合格/不合格），**发货前核对本批及全部上游批次的质检结论**；质检时输入
不合格进料批号，查出各已发批可能含该进料的**最少/最多克数**，两个极值各附一份
符合整条链称重记录的克数分配，供逐道核对。

## 启动

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，交互文档见 http://localhost:8000/docs
```

SQLite 数据文件落在命名卷 `trace-data`（容器内 `/data/app.db`，可用 `DB_PATH` 改）。
本地开发：`pip install -r requirements-dev.txt && uvicorn app.main:app --reload`，
测试：`python -m pytest tests/`。

## 概念与规则

- **批号**是追溯的基本单位，重量一律为**正整数克**。
- 每个批号**至多一个来源**（进料登记，或某道工序的产出）和**至多一个去向**
  （投入某道工序，或整批发货）。投料按整批记账。
- **来源可以晚交**：工序单、发货单可以先登记，批号的来源单（进料或上游工序）后补。
- 三类单据一次可提交多张，**整次原子生效**：
  - 同一批号在各单上的重量必须一致；
  - 每道工序投入总重必须等于产出总重；
  - 批号/重量冲突、来源或去向重复、形成循环、保存失败——任一发生，整次登记不生效。
- **质检结论**：可为**已登记的进料批或工序产出批**录入检验号和结论
  （`pending` 待检 / `pass` 合格 / `fail` 不合格）；**未录入结论的批次按待检处理**。
  每批结论带**版号**：未录入为 0，首次录入为 1，每次改判 +1。
- **改判须带所见版号**：与当前版号不一致即拒绝（409），不产生任何写入。
- **发货质检闸**：登记发货时，**本批和全部上游批次均合格（pass）才可入账**，
  否则返回阻断发货的批号，整笔登记不生效。待检（含从未录入）与不合格一样阻断。
- **并发与失败**：改判与发货登记各自在单个 `BEGIN IMMEDIATE` 事务内完成，
  写事务互斥——**先入账的改判必然参与发货校验**；任一写入失败即回滚，**保留原账**。

## 接口

### `POST /api/documents` — 登记（原子）

```json
{
  "documents": [
    {"type": "incoming", "batch": "A", "weight": 1000},
    {"type": "incoming", "batch": "X", "weight": 500},
    {"type": "process",
     "inputs":  [{"batch": "A", "weight": 1000}, {"batch": "X", "weight": 500}],
     "outputs": [{"batch": "B", "weight": 900},  {"batch": "C", "weight": 600}]},
    {"type": "process",
     "inputs":  [{"batch": "C", "weight": 600}],
     "outputs": [{"batch": "S1", "weight": 400}, {"batch": "R", "weight": 200}]},
    {"type": "shipment", "batch": "B", "weight": 900},
    {"type": "shipment", "batch": "S1", "weight": 400}
  ]
}
```

- `201`：全部落库，返回新建的工序/发货 id 等。
- `409`：批号、重量冲突，来源/去向重复，或形成循环（整次不生效）。
- `409`（发货被质检阻断）：`detail` 为结构体，列出阻断发货的批号——

  ```json
  {"detail": {"message": "发货被阻断，本批或上游存在未合格批次：A、B",
              "blocking_batches": ["A", "B"],
              "shipments": {"B": ["A", "B"]}}}
  ```

  `blocking_batches` 为全部阻断批号的并集，`shipments` 按发货批分别列出。
  被阻断时**整笔登记不生效**（同请求内其他单据也不落账）。
- `422`：单据结构非法（重量非正整数、工序投入≠产出、批号重复等，整次不生效）。

### `PUT /api/batches/{batch_no}/inspection` — 录入/改判质检结论

```json
{"inspection_no": "QC-2026-001", "conclusion": "pass", "expected_version": 0}
```

- `expected_version` 为调用方在批次详情中看到的版号（**未录入的批为 0**）。
- `200`：落账成功，返回最新 `conclusion` 与 `version`（旧版号 +1）。
- `409`：版号已变，`detail.current_version` 给出当前版号，需重新获取后再改判。
- `404`：批号不存在；`400`：批号不是已登记的进料批或工序产出批；
  `422`：结论非法或检验号为空。
- 写入失败时保留原账（回滚）。

### `GET /api/trace/{batch_no}` — 质检查询

输入**不合格进料批号**，返回**每个已发批**可能含该进料的克数下界与上界：

```json
{
  "defective_batch": "A",
  "defective_weight": 1000,
  "shipments": [
    {"batch": "B", "status": "ok", "min_grams": 400, "max_grams": 900,
     "min_witness": {"batch_grams": {"A": 1000, "B": 400, "C": 600, "S1": 400, "R": 200},
                     "processes": [{"process_id": 1,
                                    "input_grams":  {"A": 1000, "X": 0},
                                    "output_grams": {"B": 400, "C": 600}},
                                   {"process_id": 2,
                                    "input_grams":  {"C": 600},
                                    "output_grams": {"S1": 400, "R": 200}}]},
     "max_witness": {...}},
    {"batch": "S2", "status": "pending", "missing_batches": ["Y"]}
  ]
}
```

- **上下界**：在符合全部称重记录（批重上限、工序投入=产出守恒、该进料批全部克数
  进入链路）的前提下，该已发批含该进料的最少/最多克数。用最小费用流在
  "批号容量 = 称重克数"的守恒网络上求得，结果为整数克。
- **克数分配（witness）**：每个极值附一份达到该极值的分配——`batch_grams` 给出
  链路内每个批号含该料的克数，`processes` 按工序列出投入/产出克数，可逐道核对：
  每道工序投入克数之和 = 产出克数之和，且每个批号不超过其称重。
- **待补批号（pending）**：已发批的上游有缺单（批号既非进料也非任何工序产出）时，
  该批不给出上下界，返回待补批号。发货质检闸保证经 API 登记的发货全链合格、
  来源齐全，故此状态只会出现在账簿由 API 之外写入（迁移/补录）的情形。
- `404`：批号不存在；`400`：批号不是进料批。

### 辅助查询

- `GET /api/batches` / `GET /api/batches/{batch_no}`：批号列表 / 单个批号的重量、
  来源与去向（`missing` 表示来源缺单），以及：
  - `inspection`：本批质检结论与版号（`inspection_no` 未录入时为 `null`）；
  - `unqualified_batches`：**本批和上游未合格（待检/不合格）的批号**，升序。
- `GET /api/processes/{id}`：工序的投入/产出批及称重。
- `GET /api/shipments`：已发批列表。
- `GET /api/health`：健康检查。

## 典型流程

```bash
# 1. 登记进料、混炼分装、回炉（可一次提交，也可分次；来源可晚交）
curl -X POST localhost:8000/api/documents -H 'Content-Type: application/json' \
  -d '{"documents":[...]}'

# 2. 为进料批与工序产出批录入质检结论（expected_version 取批次详情中的版号）
curl -X PUT localhost:8000/api/batches/A/inspection -H 'Content-Type: application/json' \
  -d '{"inspection_no":"QC-001","conclusion":"pass","expected_version":0}'

# 3. 查看批次详情：本批结论、版号、本批和上游未合格批号
curl localhost:8000/api/batches/S1

# 4. 全链合格后登记发货；若有未合格批次，返回 409 与阻断批号，整笔不生效
curl -X POST localhost:8000/api/documents -H 'Content-Type: application/json' \
  -d '{"documents":[{"type":"shipment","batch":"S1","weight":400}]}'

# 5. 质检发现进料批 A 不合格，查各已发批的可能含量
curl localhost:8000/api/trace/A
```

## 目录结构

```
app/
  main.py      路由
  models.py    单据与质检请求的结构校验（正整数克、工序平衡、版号非负等）
  registry.py  登记：一致性/唯一性/循环校验 + 发货质检闸 + 单事务落库
  quality.py   质检结论录入/改判（乐观版号控制，单事务）
  trace.py     追溯：影响面展开、缺单检查、上下界与分配单
  flow.py      最小费用流（SPFA 增广）
  db.py        SQLite 连接与建表
  state.py     账簿状态载入（含质检结论、上游闭包、未合格清单）
tests/test_api.py
Dockerfile / docker-compose.yml
```
