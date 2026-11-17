# 日切放行

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账

## 裁决边界册（裁决与放行分入口）

- 裁决只认甲/乙两路，当天在策略条选定后写入 `boundary_register`，**当天不得更换**，两路不得混壬裁：
  - **甲路**：`POST /api/slices/{id}/evaluate` 只更新完整度字段，放行态与水位一律不动；
  - **乙路**：`POST /api/slices/{id}/preview` 只写完整度预看快照，`POST /api/slices/{id}/confirm` 确认提交时才落正式完整度，仍不得改放行态。
- 禁止裁决成功顺带标已放行；禁止用放行接口冒充裁决（放行请求夹带完整度等裁决字段一律拦下）。
- 偏离裁决边界册（未选定口径、跨路落笔、越界字段、当天换口径）→ 409 拦下，并在裁决台账记「裁决口径偏了」。
- 同一候审切片两名值班同时落笔须串行：原子 `候审→已裁决` 翻转，后手 409 记「裁决争用」，禁静默写成成功。
- 可核对面：策略条（边界册）、候审队列、裁决列表（`adjudications` 台账）、切片侧栏（单切片全留痕）、放行台账（`releases` 流水）。

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 日切放行 |
| 目录 | `04-slicegate` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10626 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

迟到切片、重切、跨分区依赖、部分完整冻结、消费水位回拨

## 避开

别做成作业拓扑编排换皮；别做成纯水位大盘

## 互斥切口候选

- 迟到切片整批拒收 vs 进待补齐冻结区
- 重切失败整笔回滚 vs 保留已完整分区
- 放行与水位同步闸 vs 异步放行后纠偏

## 启动（一键）

`powershell
cd d:\document\cccodex\workspace\slicegate\slicegate-bugfix-01
docker compose up -d --build
`

浏览器：http://127.0.0.1:10626/  
健康检查：GET http://127.0.0.1:10626/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10626
python -m pytest -q
`
