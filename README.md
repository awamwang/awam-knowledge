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
- **bm25-rerank 非默认**：`--mode bm25-rerank` 需 `--rerank-endpoint` 或 `RERANK_ENDPOINT`（BM25 召回 top-N → 精排）；未配置同样 `unavailable` + 退出码 2
- 评测**未证明 hybrid 强于** 标题×4 的 BM25（原始数字在仓外 `awam-knowledge.private/research/`）；**rerank 对照尚未跑数**
- 评测方向曾收口（2026-10-29）：以标题×4 的 BM25 为主；**rerank 为重新开测的一条刀**，须先定后跑、对照同 `title-weight` 的 BM25

```bash
node code/smoke-index.js --queries "知识库|Logseq"
# 仓库内 queries-t18.json 为脱敏示例；真实 26 条用仓外私有集
node code/eval-recall.js --queries-json ../awam-knowledge.private/queries-t18.json --topn 50 --md-out out.md > out.json
# M4 · BM25 + Rerank（公平对照：同 title-weight 的 bm25）
node code/eval-recall.js --mode bm25 --title-weight 4 --queries-json ../awam-knowledge.private/queries-t18.json --topn 50 --md-out out-bm25.md > out-bm25.json
node code/eval-recall.js --mode bm25-rerank --title-weight 4 --rerank-endpoint "$RERANK_ENDPOINT" --rerank-model jina-reranker-v2-base-multilingual --queries-json ../awam-knowledge.private/queries-t18.json --topn 50 --md-out out-rerank.md > out-rerank.json
```

## MCP server

**Python 主干**：`src/awam_knowledge/mcp_server.py`（console script `awam-knowledge-mcp`，或
`python -m awam_knowledge.mcp_server`）与 `code/mcp-server.js` 协议/行为逐字等价（手写
JSON-RPC 2.0，零依赖；等价性由 `tests/mcp_selftest.py` 60 项断言钉死）。以下为共同契约：

`mcp-server` 是手写 JSON-RPC 2.0 的 stdio server（9 个工具）：

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

## HTTP 服务（AFM 集成）

`code/http-server.js` 是面向**本机 UI**（AFM 搜索面板 + 知识页）的常驻 HTTP 服务（默认 `127.0.0.1:18790`，契约见
`design/afm-http-service-design.md`）：`GET /health` 探活 + `POST /search`（BM25 内容检索，命中块做
「见保险库 / [REDACTED:]」引用提取；`dedup:"file"` 时按文件去重出单行，响应附 `root` 索引根）+
`POST /doc`（读取索引根下文档全文供知识页渲染：服务端强制路径边界拒 `..` 穿越、2MiB 上限）+
`POST /vault/status|reveal|unlock|lock`（代理脱敏体系
`agent_secret.py` 人机通道，ADR-002）。除 `/health` 外全部 `Bearer KM_SERVICE_TOKEN` 鉴权（常量时间比较），
`KM_SERVICE_TOKEN` 未设置启动即退出（码 2）。明文只出现在 `/vault/reveal` 响应体（`Cache-Control: no-store`），
不写日志、不进缓存；访问日志只有「方法+路径+状态码」。与 MCP server 进程/端口/鉴权独立，共享 `bm25-index.js`
与索引根约定；`npm run http` 启动，`node code/http-server.js --selftest` 自检（28 项）。

密钥来源集中在 `code/secret-provider.js`（`AWAM_SECRET` → `AWAM_SECRET_CMD` → `secret.key`，长度不足 16 一律报错），
签发端 `companion.js` 与校验端 `mcp-server.js` 共用同一解析。命令继承进程环境，因此 Bitwarden 的 `BW_SESSION` 直接透传：

```bash
export BW_SESSION=$(bw unlock --raw)          # 解锁后会话留在本 shell
export AWAM_SECRET_CMD='bw get password <vault-item>'
node code/secret-broker-check.js              # 结论：source / length / 两侧同源 / 签验往返
```

# M3 · 知识脱敏管道（全量融合，Python 零依赖）

M3 把 knowledge-desensitization 仓的脱敏管道（向导 + 四步）全量融合进本包，stdlib 之外零依赖
（YAML 用内置 `mini_yaml` 子集实现，load/dump 与 PyYAML 在线上配置上逐字节等价）。
脱敏仓 `G:\Knowledge\knowledge-desensitization` 保留不动，两仓共用同一份秘密材料。

## 模块（`src/awam_knowledge/`）

| 模块 | 用途 |
|---|---|
| `mini_yaml.py` | 零依赖 YAML 子集（load/dump；`--selftest` 40 项） |
| `km_paths.py` | 路径解析：用户配置根 / 数据根 / 工作副本 / 秘密材料目录（默认秘密根锚 `<KM_CONFIG_HOME>/.km`） |
| `km_config.py` | 两级配置（settings.yaml + sources/<id>.yaml；`--selftest` 26 项） |
| `sanitize_lib.py` | 脱敏内核：稳定 token、四层召回、路径排除、白名单指纹、配置分层（`--selftest` 42 项） |
| `km_sync.py` | ① 真源 → 工作副本增量同步（源只读；删除软移） |
| `km_sanitize.py` | ② 就地脱敏（稳定 token、幂等、可逆；原文只进 vault/） |
| `km_materials.py` | ③ 还原材料落盘「单独备份目录」（salt/map/literals → 私密离线） |
| `km_build_index.py` | ④ 旁路 Index（index.json 等；默认不改写 .md） |
| `wizard.py` | 配置向导：`--source <真源目录>` 逐步建立配置与目录 |
| `km_pipeline.py` | 全流程编排（进程内调用各步 main(argv)，不再 subprocess） |
| `entry.py` | 统一入口：选择 + 分发（新增源 → wizard；处理源 → km_pipeline） |

内置基线配置随包分发（`config/rules.json` 等），配置分层：内置模板 → 全局
`<KM_CONFIG_HOME>/config/` → 每源 `<秘密材料根>/<id>/config/`（后者覆盖前者，改全局即生效）。

## 用法

```bash
python -m awam_knowledge.entry --list                          # 列出已保存的源
python -m awam_knowledge.entry --source <真源> --data-root <根>  # 新增/配置一个源（→ wizard）
python -m awam_knowledge.entry --id <源id> --apply             # 全流程（→ km_pipeline）
python -m awam_knowledge.km_pipeline --id <源id> --apply       # sync→sanitize→materials→build_index
```

端到端沙箱自检（临时 KM_CONFIG_HOME，不碰真实配置/真源/秘密材料）：

```bash
PYTHONPATH=src python tests/km_check.py   # 37 项：wizard→sync→sanitize→materials→build_index→pipeline 全链
```

安全口径不变：真源只读；秘密材料（salt/map/literals）永不进 Git、永不上云；
工作副本（已脱敏）才可入向量库 / 网盘 / 私有 Git。

## 自检

```bash
npm run selftest   # 10 个脚本：eval 23、infra 38、secret 18、mcp 60、import 31、
                   # scan-links 16、prepare 9、query-set 6、spike 23、http-server 17
```

Python 主干自检（与上表同口径）：

```bash
PYTHONPATH=src python tests/parity/check_parity.py   # bm25 内核 Node↔Python 等价
PYTHONPATH=src python tests/storage_check.py         # fs_util/store/index_root
PYTHONPATH=src python tests/http_selftest.py         # http_service 28 项（AFM 契约）
PYTHONPATH=src python -m awam_knowledge.import_md_dir --selftest   # 导入 31 项
PYTHONPATH=src python -m awam_knowledge.eval_recall --selftest     # 评测核心
PYTHONPATH=src python tests/mcp_selftest.py          # MCP 60 项（stdio 真实进程）
PYTHONPATH=src python tests/parity_recall.py         # 全语料召回回归 Node↔Python 对账
PYTHONPATH=src python -m awam_knowledge.mini_yaml --selftest    # YAML 子集 40 项
PYTHONPATH=src python -m awam_knowledge.km_config --selftest    # 两级配置 26 项
PYTHONPATH=src python -m awam_knowledge.sanitize_lib --selftest # 脱敏内核 42 项
PYTHONPATH=src python tests/km_check.py             # M3 管道端到端 37 项（沙箱）
```

自检只用临时语料与临时状态目录，不读私有数据；所有产物写完即删。

## Python 模块（主干，`src/awam_knowledge/`）

| 模块 | 用途 |
|---|---|
| `bm25.py` | 共享检索核心（分词、切块、BM25、搜索、增量缓存 `build_corpus_cached`） |
| `fs_util.py` / `store.py` / `index_root.py` | 原子写与目录锁；job / token / undo 状态库；独立审计；索引根解析 |
| `http_service.py` | AFM 集成常驻 HTTP 服务（`/health /search /doc /vault/*`，selftest 28 项） |
| `import_md_dir.py` | Markdown 目录导入（selftest 31 项；`python -m awam_knowledge.import_md_dir`） |
| `eval_recall.py` | 召回评测 bm25 模式核心（联网模式仍用 `code/eval-recall.js`；`--selftest`） |
| `mcp_server.py` | MCP stdio server 9 工具（零依赖手写 JSON-RPC，行为与 js 版等价） |
| `secret_provider.py` | 密钥三级来源解析（与 `code/secret-provider.js` 同源语义） |
| M3 管道模块 | `mini_yaml / km_paths / km_config / sanitize_lib / km_sync / km_sanitize / km_materials / km_build_index / wizard / km_pipeline / entry`（见上文 M3 节） |

## 脚本一览（Node 原版）

| 脚本 | 用途 |
|---|---|
| `bm25-index.js` | 共享检索核心（分词、切块、BM25、搜索、增量缓存） |
| `index-root.js` | 索引根解析与校验（环境变量 / settings.yaml / 拒绝真源） |
| `fs-util.js` / `store.js` | 原子写与目录锁；job / token / undo 的持久化状态库；独立追加式审计 `audit.jsonl` |
| `eval-recall.js` | 召回评测（bm25 / m3 / hybrid / bm25-rerank，含 `--selftest` 23 项） |
| `smoke-index.js` | 索引冒烟检索 |
| `import-md-dir.js` | 带附件 Markdown 目录导入（深度感知链接解析、密钥扫描、断点复用、`--resume`、NFC/win32 大小写归一、Logseq 块引用对账） |
| `mcp-server.js` / `mcp-selftest.js` | MCP server 与端到端自检（含跨重启、取消、崩溃后续跑与单写者锁） |
| `http-server.js` | 面向本机 UI（AFM）的常驻 HTTP 服务：内容检索 + vault 引用提取 + 人机通道代理（`--selftest` 17 项） |
| `companion.js` | 一次性 token 签发模拟（与 server 同源取密钥） |
| `secret-provider.js` | 密钥三级来源解析（环境变量 / 外部命令 / 本地 0600 文件） |
| `secret-broker-check.js` | 运维自检：确认密钥来源可用且签发端与校验端同源（不打印密钥） |
| `secret-broker-selftest.js` | 密钥来源自检（优先级 / 命令成功失败 / 会话透传 / 文件复用） |
| `infra-selftest.js` | 配置回退 / 增量索引 / 状态库 / 审计日志四组自检 |
| `ui-render-check.js` | 原型真渲染与可访问性检查（headless Chrome）：三视口（1280×900 / 390×844 / 200% 缩放）溢出、WCAG AA 对比度、Tab 焦点、语义、密钥明文扫描、状态切换、Tab+Enter 纯键盘写入与撤销；需本机 Chrome/Edge，不进 `npm run selftest` |
| `mine-expansion.js` | 语料无监督扩展词典（产物不入库） |
| `prepare-test-corpus.js` / `scan-links.js` / `query-set-check.js` | 夹具准备 / 链接普查 / query 集核验（各带 `--selftest`） |
