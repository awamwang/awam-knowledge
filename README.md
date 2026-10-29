# awam-knowledge

本地 Markdown / Logseq 知识库的**召回、导入、评测工具**（零依赖 Node 脚本）。本仓保存可运行代码、设计定稿与公开结论；**调研过程与真实评测集在仓外**。许可：[MIT](LICENSE)。

- 遗留问题与未完成项：[`docs/OPEN-ISSUES.md`](docs/OPEN-ISSUES.md)
- 调研存放约定：[`docs/RESEARCH.md`](docs/RESEARCH.md)（私有副本：同级 `awam-knowledge.private/`）
- 主方案：`design/AI知识系统-设计方案.md`；交接说明：`progress/进度与交接说明.md`

## 目录

```
code/       可复用脚本（BM25 索引、评测、导入、MCP server）
design/     设计方案与原型
docs/       遗留问题、QA、调研存放约定
progress/   进度与交接说明
```

本机可另有未跟踪的 `research/`；正式私有归档在 `../awam-knowledge.private/research/`。

## 索引根约定（强制）

**禁止默认真源。** 脚本的默认可索引输入是脱敏工作副本，全部经 `code/index-root.js` 校验：

- `KM_DATA_ROOT` + `KM_SOURCE_ID`（默认 `logseq-main`）→ 索引根 `<data_root>/<id>`
- 两者未设时回退读 `~/.knowledge-manager-skills/settings.yaml` 的 `data_root` / `source_id`（`KM_SETTINGS` 可改路径；支持 `~` 与 `%USERPROFILE%`）
- 真源路径由 `KM_RAW_ROOTS` 声明（分号分隔）；命中且未传 `--allow-raw-source` 时直接退出（码 2）
- 全部未配置时同样退出（码 2），不做路径猜测
- 显式 `--root` 始终优先

跨仓数据流：真源 → 脱敏仓 sanitize → `build_index` → 本仓 BM25。

```bash
export KM_DATA_ROOT=<data_root>
export KM_SOURCE_ID=logseq-main
```

## 旁路 Index

若 ROOT 下存在 `index.json`（由脱敏仓 `scripts/build_index.py` 生成）：

- 用于文档清单与**标题**（优先 `index.title`，回退文件名）
- 切块仍读 `.md`；不要求 frontmatter
- 无 `index.json` 时保持旧行为

## 默认召回

- **默认**：BM25 + **标题权重 ×4**，20 行切块，CJK bigram + ASCII 词
- **hybrid 非默认**：`--mode hybrid` 需显式指定 embedding 端点（`--embed-endpoint` 或 `EMBED_ENDPOINT`），未配置时输出 `unavailable` 并退出码 2，不产数字
- 评测**未证明 hybrid 强于** 标题×4 的 BM25（原始数字在仓外 `awam-knowledge.private/research/`）
- 评测方向已收口（2026-10-29）：**以标题×4 的 BM25 为主，向量仅作跨语言兜底**，不再新增评测轮次

```bash
node code/smoke-index.js --queries "知识库|Logseq"
# 仓库内 queries-t18.json 为脱敏示例；真实 26 条用仓外私有集
node code/eval-recall.js --queries-json ../awam-knowledge.private/queries-t18.json --topn 50 --md-out out.md > out.json
```

## MCP server

`code/mcp-server.js` 是手写 JSON-RPC 2.0 的 stdio server（9 个工具）：

- `search`：本地 BM25（标题×4）；索引根取 `AWAM_INDEX_ROOT` → `KM_DATA_ROOT/<KM_SOURCE_ID>` → `settings.yaml`；带增量缓存（按 size/mtime 复用 chunk）
- `propose_write` / `commit_write` / `undo_write`：两阶段写入闸（HMAC 一次性 token + 基线 hash + TTL）；提案与已用 token 持久化，重启后仍拒重放；提交与撤销均原子写；目标路径限定在写入根内
- `start_import` / `get_job_status` / `cancel_job` / `resume_job`：真实导入 job（遍历 md + 附件 → 写 manifest → 导入根等于索引根时自动刷新索引），支持 `notifications/progress` 与取消；运行中每隔 ≤1s/50 个文件把已完成条目写成检查点 `<state>/imports/<job_id>.partial.json`，中断/取消/崩溃后 `resume_job` 起新 job 复用未变文件（size+mtime），完成时清理检查点
- `get_audit`：读取写入闸审计（提案 / 提交 / 拒绝及原因 / 撤销，带客户端名与版本），`audit.jsonl` 只追加，状态日志压实不影响它

运行时配置：

| 变量 | 默认 | 作用 |
|---|---|---|
| `AWAM_STATE_DIR` | `.spike-runtime/` | job / token / 撤销 / 提案 / manifest / 导入检查点 / 审计 / 索引缓存 |
| `AWAM_WRITE_ROOT` | 同状态目录 | `commit_write` 允许写入的根，越界拒绝 |
| `AWAM_SECRET` / `AWAM_SECRET_CMD` | 自动生成 `secret.key`（POSIX 0600；Windows 上 chmod 不生效） | token 签名密钥；`AWAM_SECRET_CMD` 可接 `bw get password …`（已用真实 Bitwarden 验证） |
| `AWAM_INDEX_CACHE` | `<state>/index-cache.json` | 索引缓存路径 |

状态库为快照 + 追加日志；同一状态目录只允许一个写者，第二个进程只读并返回 `state_locked`。

密钥来源集中在 `code/secret-provider.js`（`AWAM_SECRET` → `AWAM_SECRET_CMD` → `secret.key`，长度不足 16 一律报错），
签发端 `companion.js` 与校验端 `mcp-server.js` 共用同一解析。命令继承进程环境，因此 Bitwarden 的 `BW_SESSION` 直接透传：

```bash
export BW_SESSION=$(bw unlock --raw)          # 解锁后会话留在本 shell
export AWAM_SECRET_CMD='bw get password <vault-item>'
node code/secret-broker-check.js              # 结论：source / length / 两侧同源 / 签验往返
```

## 自检

```bash
npm run selftest   # 9 个脚本 / 220 项：eval 19、infra 38、secret 18、mcp 60、import 31、
                   # scan-links 16、prepare 9、query-set 6、spike 23
```

自检只用临时语料与临时状态目录，不读私有数据；所有产物写完即删。

## 脚本一览

| 脚本 | 用途 |
|---|---|
| `bm25-index.js` | 共享检索核心（分词、切块、BM25、搜索、增量缓存） |
| `index-root.js` | 索引根解析与校验（环境变量 / settings.yaml / 拒绝真源） |
| `fs-util.js` / `store.js` | 原子写与目录锁；job / token / undo 的持久化状态库；独立追加式审计 `audit.jsonl` |
| `eval-recall.js` | 召回评测（bm25 / m3 / hybrid，含 `--selftest`） |
| `smoke-index.js` | 索引冒烟检索 |
| `import-md-dir.js` | 带附件 Markdown 目录导入（深度感知链接解析、密钥扫描、断点复用、`--resume`、NFC/win32 大小写归一、Logseq 块引用对账） |
| `mcp-server.js` / `mcp-selftest.js` | MCP server 与端到端自检（含跨重启、取消、崩溃后续跑与单写者锁） |
| `companion.js` | 一次性 token 签发模拟（与 server 同源取密钥） |
| `secret-provider.js` | 密钥三级来源解析（环境变量 / 外部命令 / 本地 0600 文件） |
| `secret-broker-check.js` | 运维自检：确认密钥来源可用且签发端与校验端同源（不打印密钥） |
| `secret-broker-selftest.js` | 密钥来源自检（优先级 / 命令成功失败 / 会话透传 / 文件复用） |
| `infra-selftest.js` | 配置回退 / 增量索引 / 状态库 / 审计日志四组自检 |
| `ui-render-check.js` | 原型真渲染与可访问性检查（headless Chrome）：三视口（1280×900 / 390×844 / 200% 缩放）溢出、WCAG AA 对比度、Tab 焦点、语义、密钥明文扫描、状态切换、Tab+Enter 纯键盘写入与撤销；需本机 Chrome/Edge，不进 `npm run selftest` |
| `mine-expansion.js` | 语料无监督扩展词典（产物不入库） |
| `prepare-test-corpus.js` / `scan-links.js` / `query-set-check.js` | 夹具准备 / 链接普查 / query 集核验（各带 `--selftest`） |
