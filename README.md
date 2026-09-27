# 教材共创贡献归属

纯 Python 服务端项目，提供教材多人共创场景下的版本差异、贡献归属、发布封存与校验能力。

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q service_09261_009 tests`

## 解决的问题

编辑在新稿中直接覆盖旧内容时，系统仍能回答：某段内容是谁贡献、基于哪一版修改、发布时采用的到底是什么。

## 核心保证

1. **差异与归属**：每次修订必须声明父版本（`base_version`，默认最新版），存储 difflib 行级 opcodes、作者、内容哈希、差异哈希；版本校验和把父版校验和绑定进哈希链。
2. **发布固定**：发布向 `publications` 表独立封存当时内容副本、版本号与校验和；之后的修订不影响任何已发布版次，可按版次回溯。
3. **过期版本不混入**：贡献查询/汇总先锚定一个版本（显式 `anchor` > 最新发布版 > 最新草稿），只沿 `parent_no` 祖先链取记录。基于过期版本分叉出的稿件，即使作者/时间条件匹配也不会混入。
4. **跨天补录正确**：`version_no` 只按提交顺序递增，与业务时间 `event_time` 解耦；补录的旧时间稿件得到更大的版本号，时间筛选只在锚定祖先集内收窄。
5. **可校验**：`GET /docs/{id}/verify` 不信任任何已存哈希，从版本内容重新生成 diff 并复算全部哈希链；发布封存内容也与对应版本逐条比对。可检测内容篡改、归属篡改、发布后改动。
6. **幂等**：创建/修订/发布均支持 `idempotency_key`，重试返回同一记录。

## API（经 `api.dispatch(flow, method, path, body, query)` 调用）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/docs` | 创建教材（body: id, actor, content, event_time?, idempotency_key?） |
| POST | `/docs/{id}/revisions` | 提交修订（actor, content, base_version?, event_time?, idempotency_key?） |
| POST | `/docs/{id}/publications` | 发布封存（actor, version_no? 默认最新, event_time?, idempotency_key?） |
| GET | `/docs/{id}` | 版本历史（不含全文，含校验信息与增删行统计） |
| GET | `/docs/{id}/versions/diff?v=N` | 第 N 版相对父版的行级差异 |
| GET | `/docs/{id}/published?edition=K` | 读取发布内容（edition 省略取最新发布） |
| GET | `/docs/{id}/contributions` | 贡献记录（anchor?, actor?, since?, until?） |
| GET | `/docs/{id}/summary` | 按作者汇总增删行数（anchor?, since?, until?） |
| GET | `/docs/{id}/blame` | 逐行标注引入版本与作者（anchor?） |
| GET | `/docs/{id}/verify` | 重新复算全部哈希链与封存一致性 |

每条贡献记录都带 `version_no`、`parent_no`、`content_hash`、`diff_hash`、`checksum`，响应同时回显 `anchor_version` 与 `anchor_source`（publication/version/latest）。

## 模块

- `textbook.py`：纯函数——diff opcodes、规范化哈希、版本/发布校验和、逐行溯源。
- `store.py`：insert-only SQLite 仓储（versions / publications / idempotency_keys）与独立校验。
- `workflow.py`：修订、发布、祖先链锚定查询与汇总。
- `api.py`：JSON 边界，`ValueError` 映射为 400。
