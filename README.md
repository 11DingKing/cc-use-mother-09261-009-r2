# 教材共创贡献归属

多人共创教材的版本与贡献归属服务：保存每版相对上一版的差异与逐行贡献归属，
发布动作固定当时采用的内容，过期版本不会在查询/汇总时重新混入。

## 核心规则

- **差异固化**：每次提交保存完整正文、正文 SHA-256、相对上一版的 opcode 级差异
  （equal/insert/replace/delete，含双方行内容），差异在提交当时写入，不事后重算。
- **逐行归属（blame）**：未改动行沿用上一版归属；新增/替换行归属本次提交人。
  整稿直接覆盖也不会抹掉原作者。每行记录含 `introduced_version`、
  `introduced_at` 与行内容 `line_sha256`。
- **发布固定**：发布对当时整稿做独立快照（body 副本 + SHA-256）。之后的新草稿、
  被废弃版本不进入默认查询与汇总；版本状态标为 `published / superseded / draft`。
- **跨天补录**：`event_time` 是声称的贡献时间（可晚补录），`recorded_time` 是
  服务端入库时间；归属始终挂在引入它的版本上，补录不会让记录错位到更早版本。
- **条件筛选**：可按作者、贡献时间窗口筛选，并可显式固定 `version`；默认作用域
  为当前发布版本。返回结果带固定版本号、正文校验值及逐行校验结果。
- **幂等命令**：`idempotency_key` 重放返回同一版本/发布结果。

## 接口（JSON）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/docs` | 创建文档（含首版正文，可带 `event_time`） |
| POST | `/docs/{id}/commit` | 提交新稿，固化差异与归属 |
| POST | `/docs/{id}/publish` | 发布固定（可指定 `version`） |
| GET | `/docs/{id}/versions/{n}` | 查看某版（含 status、verified） |
| GET | `/docs/{id}/versions/{n}/diff` | 该版相对上一版的差异 |
| GET | `/docs/{id}/published` | 当前发布快照与校验 |
| GET | `/docs/{id}/contributions` | 逐行归属，支持 `actor`、`introduced_from/to`、`version` |
| GET | `/docs/{id}/summary` | 按作者汇总固定版本内仍被采用的贡献 |

仓储默认内存实现，生产用 `SQLiteStore(path)`（versions/diffs/blame/publications
分表持久化，单事务写入）。

## 命令

测试：`python3 -m unittest discover -s tests -v`

编译：`python3 -m compileall -q service_09261_009 tests`
