# awam-knowledge HTTP 服务（AFM 集成）：内容检索 + 账号引用取用

> 状态：定稿（2026-11-18，与 AFM ADR 0016、knowledge-desensitization ADR-002 配套）。
> 产出代码：`code/http-server.js`。本文是该服务的对外契约与红线。

## 1. 目标与边界

AFM（a-file-manager）的搜索面板需要两个能力：

1. **内容搜文件**：对脱敏知识副本做 BM25 内容检索，命中笔记在面板中打开；
2. **密钥展示复制**：命中块中的「见保险库：… [vault:…]」/ [REDACTED:…] 引用可展示（掩码 + 短显）与复制，**明文不进 AI/MCP 上下文、不落日志**。

本服务 = 常驻 HTTP 进程，**代理层**：检索复用 `bm25-index.js`（BM25 标题×4、增量缓存），明文取用代理到 knowledge-desensitization 的 `agent_secret.py` 人机通道（ADR-002）。本服务自身**不读 map.json、不碰 Bitwarden**。

不做的事：不提供写入（`propose_write` 等仍只在 MCP server）；不索引真源（沿用 `index-root.js` 禁止默认真源约定）；不向任何 AI/Agent 通道暴露明文。

## 2. 配置（全部环境变量，零依赖）

| 变量 | 默认 | 说明 |
|---|---|---|
| `KM_SERVICE_HOST` | `127.0.0.1` | 监听地址；仅回环，不改不监听 0.0.0.0 之外的提醒 |
| `KM_SERVICE_PORT` | `18790` | 监听端口（awam-file 网关 18787，错开） |
| `KM_SERVICE_TOKEN` | （必填） | AFM → 服务的鉴权 token；**未设置时启动即退出（码 2）**，fail-fast |
| 索引根 | 沿用 `index-root.js` | `AWAM_INDEX_ROOT` → `KM_DATA_ROOT`/`KM_SOURCE_ID` → `settings.yaml`；命中 `KM_RAW_ROOTS` 且未 `--allow-raw-source` 直接拒绝 |
| `AWAM_INDEX_CACHE` 等 | 同 mcp-server | 增量缓存、切块、标题权重环境变量原样透传给 `bm25-index.js` |
| `KM_SECRET_PY` | `python` | 人机通道 Python 解释器 |
| `KM_SECRET_SCRIPT` | （必填，启用 vault 时） | `agent_secret.py` 绝对路径（knowledge-desensitization 仓） |
| `KM_SECRET_ID` | （必填，启用 vault 时） | 源 id（`--id` 传给人机通道）；也可用 `KM_SECRET_DATA_ROOT` 直指秘密材料目录 |
| `KM_SECRET_AGENT_TOKEN` | （必填，启用 vault 时） | 服务自身在脱敏体系的 caller token（需 `enroll --human-channel` 签发）；以 `AGENT_SECRET_TOKEN` 注入子进程环境 |

vault 三项任一缺失 → `/vault/*` 返回 `503 vault_not_configured`，检索功能不受影响。

## 3. 端点（全部 JSON；除 `/health` 外都要求 `Authorization: Bearer <KM_SERVICE_TOKEN>`，常量时间比较）

### `GET /health`
`{ok, service:"awam-knowledge-http", index:{configured, root, docs, chunks}, vault:{configured}}`。无敏感字段，不鉴权，供 AFM 探活。

### `POST /search` `{query, topk?, snippetWidth?}`
- `query` 空白 → `{hits:[]}`；`topk` 夹取 1..50（默认 12）。
- 复用 `ensureIndex()`（同 mcp-server 的懒加载 + 增量缓存）；根未配置 → `503 index_root_not_configured`。
- 命中 = `bm.search()` 结果（path/line/lines/title/score/snippet），额外做**引用提取**：对每个命中块原文扫描
  - `见保险库：<名> [vault:<label>:<hash8>]` → `{kind:"vault", key:"[REDACTED:<label>:<hash8>]", display:"<名>"}`
  - `[REDACTED:<label>:<hash8>]` 裸占位 → `{kind:"redacted", key:原文, display:"<label>"}`
  - 跨块去重（同 key 只保留 display 最长者）。
- 响应：`{hits:[{path,title,line,lines,score,snippet,refs:[…]}], mode:"bm25-title4", total}`。**响应只含脱敏语料内容，天然零明文。**

### `POST /vault/status`
代理 `agent_secret.py status --json` → `{window:{active, expires_at, scope, by} | null}`。

### `POST /vault/reveal` `{entry}`
`entry` = map.json 条目名（`[REDACTED:<label>:<hash8>]`）。代理 `agent_secret.py resolve --name <entry> --json`：
- 成功 → `{name,label,sensitivity,masked,value}`（**这是全服务唯一出现明文的响应体，禁止任何日志/缓存落它**）；
- 未解锁 → `423 unlock_required`；未注册/非 human_channel → `403`；策略拒绝 → `403 denied`（带原因，不带值）。

### `POST /vault/unlock` `{ttl?, confirm?}` / `POST /vault/lock`
代理 `unlock --yes --ttl <ttl>` / `lock`。`confirm !== true` → `400`。AFM 侧必须由用户点击确认才发。

## 4. 红线（服务实现必须遵守）

1. **明文只出现在 `/vault/reveal` 的响应体与子进程 stdout→内存**：不写日志（含错误日志）、不进缓存、不进任何 `console.*`。
2. 审计全部由人机通道侧落盘（`secret-access.jsonl`，无值）；服务侧只记无 body 的访问日志（路径 + 状态码）。
3. token 比较常量时间；所有响应 `Cache-Control: no-store`。
4. 索引根拒绝真源（沿用 `index-root.js`），服务不提供绕过参数（`--allow-raw-source` 只在 CLI 启动参数，不进 HTTP 面）。
5. vault 子进程超时（10s）即杀；Python 不存在/脚本报错 → `503 vault_backend_error`。

## 5. 与 MCP server 的关系

`mcp-server.js`（9 工具，面向 AI）保持不变；本服务面向**本机 UI**（AFM）。两者共享 `bm25-index.js` 与索引根约定，但进程、端口、鉴权独立。明文能力**只在**本服务，且只经人机通道——MCP 面向 AI 的通道永远无读值出口（脱敏体系 ADR-001 红线）。
