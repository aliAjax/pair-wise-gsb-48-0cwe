# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性和公司行动调整和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。指令`data`支持可选`counterparty`（清算对手），用于净额批次分组。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

### 净额批次

月末按清算对手、币种和交收日把已复核（`approved`）指令轧差。公司行动未应用等异常成员保留原单、不参与净额并在`flags`中标出；只要有异常成员，整批停在`pending`（待处理），交易员修正后经`update_batch`重算或显式`submit_batch`提交；核对齐全的批次直接进入`reviewing`（待复核）。结算专员复核后一次完成全部成员指令（同一事务交收，进入批次审计）；成员随后被`reverse`冲正时，批次自动回到`reviewing`。批次状态：`pending`/`reviewing`/`completed`。

- `GET /api/batches`：批次列表，可带`state`和`limit`参数。
- `GET /api/batches/{id}`：批次详情（含净数量、净金额、净额方向和每个成员的标记）。
- `GET /api/batches/{id}/audit`：批次审计时间线。
- `POST /api/batches`：交易员创建批次，请求体为`{"reference":"NB-...","data":{"counterparty":"CCP1","currency":"CNY","settlement_day":2,"member_ids":[1,2]}}`。
- `POST /api/batches/{id}/actions/update_batch`：待处理批次重选成员并重算，请求体为`{"expected_version":1,"data":{...同创建...}}`。
- `POST /api/batches/{id}/actions/submit_batch`：交易员提交复核，请求体为`{"expected_version":1}`。
- `POST /api/batches/{id}/actions/return_batch`：结算专员退回待处理，请求体为`{"expected_version":1,"data":{"reason":"..."}}`。
- `POST /api/batches/{id}/actions/complete_batch`：结算专员一次完成全部成员，请求体为`{"expected_version":2}`。

成员在未完成批次中时不能单独`settle`/`fail`，也不能重复选入其他未完成批次；所有写操作带乐观并发`expected_version`校验。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
