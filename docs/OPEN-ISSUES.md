# 遗留问题清单

> 记录时间：2026-10-29。范围：清理与重新初始化之后的 `awam-knowledge`。
> 约定：以下每条都注明**现状证据**（文件/命令）与**未验证部分**；没有实测的不要当成已实现。

## 1. 仓库状态

- 已删除脚手架与一次性探针（`AGENT.md`、`SOUL.md`、`IDENTITY.md`、`USER.md`、`docs/SOP.md`、`docs/features|decisions|discussions`、`diag*.js`、`recon.js`、`wiz-check.js`、`probe-*.js`、`host-probe-*.js`、`embed-stub-server.js`、`worker.js`、`concurrency-test.js` 等）。
- 运行产物（`code/data/`、私有 t21 manifest、两份语料扩展词典）已移出仓库；调研报告与 evidence 在仓外 `awam-knowledge.private/research/`，约定见 [RESEARCH.md](RESEARCH.md)。
- 自检（`npm run selftest`）9 个脚本共 220 项，全部通过：eval 19、infra 38、secret 18、mcp 60、import 31、scan-links 16、prepare-test-corpus 9、query-set 6、spike 23。
- 旧 `.git`、agent / team / memory 运行时目录已移到仓库外的 `awam-knowledge.backup-20261029/`（与仓库同级）。

## 2. 工程缺口

### 已关闭（2026-10-29，第二至七批）

| 原缺口 | 现在 | 证据 |
|---|---|---|
| `start_import` 只模拟进度 | 真实驱动 `import-md-dir.importSteps`：扫描 md + 附件、写 manifest 到 `<state>/imports/<job_id>.json`、`errors[]` 收入 walk errors / 未解析链接数 / 密钥命中数；导入根与索引根相同时自动刷新索引 | [code/mcp-server.js:290](../code/mcp-server.js#L290)（`runImport`）、[code/mcp-server.js:349](../code/mcp-server.js#L349)（`toolStartImport`） |
| job / token / undo 只在进程内 | 快照 + 追加日志（`state.json` / `state.jsonl`），启动回放；崩溃后 `running` job 标记 `interrupted`；job、已用 token、撤销记录跨重启 | [code/store.js](../code/store.js)、[code/mcp-selftest.js](../code/mcp-selftest.js) 跨重启断言 |
| 无原子写、无并发保护 | 临时文件 + fsync + rename；状态目录单写者锁（pid + 过期 + 存活判断），第二个进程只读并返回 `state_locked` | [code/fs-util.js](../code/fs-util.js)、[code/infra-selftest.js](../code/infra-selftest.js) A/B/C 三组 |
| 共享密钥明文 `secret.txt` | 三级来源：`AWAM_SECRET` → `AWAM_SECRET_CMD`（可接 `bw get password …`）→ 状态目录 `secret.key`（首次生成 0600）；`companion.js` 同源读取 | [code/mcp-server.js:48](../code/mcp-server.js#L48)、[code/companion.js](../code/companion.js) |
| 提交路径无边界 | `target_path` 限定在写入根（默认状态目录，`AWAM_WRITE_ROOT` 可改）；越界返回 `path_outside_write_root`；新增 `undo_write`（窗口内回滚、文件被后续改动时拒绝） | [code/mcp-server.js:119](../code/mcp-server.js#L119)、[code/mcp-server.js:197](../code/mcp-server.js#L197) |
| 索引全量重建 | `buildCorpusCached`：按 (size, mtime, title) 复用 chunk，删除文件剔除，切块参数变化整体重建；缓存落 `<state>/index-cache.json` | [code/bm25-index.js:172](../code/bm25-index.js#L172) |
| 配置只读环境变量 | 新增只读 `settings.yaml` 回退：`KM_DATA_ROOT`/`KM_SOURCE_ID` 优先，缺省读 `~/.knowledge-manager-skills/settings.yaml`（`KM_SETTINGS` 可改路径），支持 `~` 与 `%USERPROFILE%` | [code/index-root.js:54](../code/index-root.js#L54)、[code/index-root.js:76](../code/index-root.js#L76) |
| 导入器覆盖不足 | 新增密钥模式扫描（7 类规则，**不记录值**）、零字节附件、同内容附件去重、编码告警、断点复用（`--resume`）、逐文件错误隔离、manifest v2（逐文件 counters/refs/unresolved） | [code/import-md-dir.js:151](../code/import-md-dir.js#L151) |
| 辅助脚本未收口 | `scan-links.js` / `prepare-test-corpus.js` / `query-set-check.js` 全部提供 `--selftest` 并纳入 `npm run selftest`；`query-set-check` 改读 `code/queries-t18.json`，不再硬编码 | [package.json](../package.json)、各脚本 `--selftest` |
| 密钥 broker 端到端（2026-10-29 第三批） | 密钥来源收敛到 `secret-provider.js`（签发端与校验端同源，统一 ≥16 字符）；`AWAM_SECRET_CMD='bw get password awam-mcp'` 经真实 Bitwarden（CLI 2025.12.0）跑通：`secret-broker-check` 返回 `source=cmd / length=64 / same_secret_as_signer=true / roundtrip=true`；MCP server 端到端 `propose_write → commit_write` 提交成功并落盘，即 server 与 companion 各自从 bw 取到同一把密钥。未解锁时命令立即退出（stdin 置 ignore）并给出解锁指引，不会挂到超时 | [code/secret-provider.js](../code/secret-provider.js)、[code/secret-broker-check.js](../code/secret-broker-check.js)、`npm run selftest` secret 18 项 |
| 导入平台差异与双链语义（2026-10-29 第六批） | 路径比对键统一 NFC（macOS 的 NFD 文件名不会再与链接目标错配，manifest 一律存 NFC）；win32 下大小写不敏感比对：`[[Sample]]` 命中 `pages/Sample.md`、`IMG/PHOTO.PNG` 命中 `img/photo.png`，附件不再被误判为孤儿；新增 `links.wiki_pages_total/resolved` 与 `links.block_refs{total,resolved,unresolved,id_defs}`：`((uuid))` 按本批 `id::` 对账，未命中记 `block_ref_missing`；win32 统计 `integrity.long_paths`（>240 字符） | [code/import-md-dir.js:77](../code/import-md-dir.js#L77)（`matchKey`）、[code/import-md-dir.js:194](../code/import-md-dir.js#L194)（块引用待解析）、[code/import-md-dir.js:399](../code/import-md-dir.js#L399)（summary）、[code/import-md-dir.js](../code/import-md-dir.js) selftest 第三批 8 项 |
| 导入无法断点续跑（2026-10-29 第五批） | 运行中每 ≤1s / 50 个已完成文件原子写一次检查点 `<state>/imports/<job_id>.partial.json`（只含已完成 Markdown 的 path/size/mtime/counters 等条目）；`resume_job` 在 interrupted / cancelled / failed 后起一个新 job（`resumed_from` 指向原 job），按 size+mtime 复用未变文件；新 job 完成时删自身检查点并清掉被继承的那份。phase 跃迁也落盘 | [code/import-md-dir.js:155](../code/import-md-dir.js#L155)（`checkpoint` 回调）、[code/mcp-server.js:286](../code/mcp-server.js#L286)（`runImport`）、[code/mcp-server.js:426](../code/mcp-server.js#L426)（`toolResumeJob`）、`mcp-selftest` 取消后续跑 6 项 + 崩溃后续跑 3 项 |
| job 进度数字只在跃迁时落盘（2026-10-29 第七批） | 写检查点的同时把 `done`/`total` 记进状态库，别的进程在 job 运行期间就能读到进度，最多落后一个检查点间隔（≤1s 或 50 个文件）；续跑仍是显式 `resume_job`，启动时**不自动**恢复 | [code/mcp-server.js:299](../code/mcp-server.js#L299)（`checkpoint`）、[code/mcp-selftest.js:120](../code/mcp-selftest.js#L120)（`journalJobProgress`）、mcp 新增 2 项断言 |
| 写入闸无独立审计（2026-10-29 第四批） | 新增 `audit.jsonl`：`propose` / `commit` / `commit_rejected`（带 `unknown_proposal`、`already_committed`、`proposal_expired`、`bad_signature`、`baseline_changed` 等原因）/ `undo` 逐条追加，记录客户端名与版本、提交时的 `committed_hash`；日志与会被压实的 `state.jsonl` 分开，`compact()` 清空状态日志不影响审计；`get_audit` 只读工具支持 `since` / `limit`（夹到 1..2000） | [code/store.js:101](../code/store.js#L101)、[code/mcp-server.js:48](../code/mcp-server.js#L48)（`auditWrite`）、[code/mcp-server.js:345](../code/mcp-server.js#L345)（`toolGetAudit`）、[code/infra-selftest.js](../code/infra-selftest.js) D 组、`mcp-selftest` 7 项审计断言 |

### 仍缺（未实现或未验证）

- **P1 · 跨主机并发**：单写者锁只保证**同一状态目录**单写；共享盘/多主机、客户端排队与重试未验证。T10 的"单例 daemon 丢 0"仍只是 `research/07` 的文字记录，原并发脚本已清理。
- **P2 · 审批仍是协议机制**：独立审计已有（`audit.jsonl` + `get_audit`），但没有宿主侧审批界面，也没有真正的审批人身份 —— 审计只记录客户端名与版本（客户端可自称），审批 token 由 companion 本地签发。`propose_write` 仍是协议层能力，不是产品流程。
- **P2 · 导入语义**：附件只做存在性 + SHA256，不解析图片/文档内容；Logseq 只对账「`[[页面]]` 是否在本批导入内」「`((uuid))` 是否命中本批 `id::` 定义」，不解析页面别名/属性、不判断块所在页面；Windows 长路径只计数不规避（>240 字符仍依赖系统长路径支持）；win32 之外按大小写敏感处理（macOS 默认大小写不敏感，可能出现假未命中，仅影响统计不影响导入）。
- **P2 · 续跑启动策略**：`done`/`total` 已随检查点落盘，但续跑仍是显式调用 `resume_job`，启动时**不自动**恢复中断的 job（避免半自动任务在后端悄悄跑起来）。是否改成启动自动续跑属产品决策，未定。

## 3. 评测方向（2026-10-29 已决定收口）

- 逐条证据与指标表：见仓外 `awam-knowledge.private/research/evidence/`；正文口径见同目录 `09-recall-baseline-report.md`。
- 结论未变：**hybrid 未在同标题权重的词法基准上稳定胜出**（jina 仅 @5/@10/MRR 超、@1/@3 持平；bge 逐条打平）；稳定且模型无关的净增益只有标题×4。**不得写成「已强于关键字」。**
- 决定（用户 2026-10-29）：**就此收口** —— BM25（标题×4）为主，向量仅作跨语言兜底。不扩评测集（真实 26 条在仓外）、不换语义源、不重跑 hybrid；`eval-recall.js` 与仓外 evidence 保留作口径依据，不再新增轮次。
## 4. 产品面未验证项

- UI（2026-10-29 已实测）：`npm run ui-check`（`code/ui-render-check.js`，headless Chrome 直连 CDP）覆盖 1280×900、390×844 与 200% 缩放（640×450 布局视口 + 2× 物理像素）三视口的横向溢出与卡片可见性、52 个可见文本元素的 WCAG 2.1 AA 对比度、10 个 Tab 焦点、landmark/可访问名称/alertdialog/标题层级、正文密钥明文扫描、5 个状态切换、**只用 Tab+Enter 走完「确认写入 → 撤销」**（真键盘事件，不派发 click）；当前 **0 问题**。截图用 `--shots <dir>` 复现，不入库。
  实测查出并修复 4 处：`--accent` 配白字按钮对比度 3.16 → 新增 `--accent-fill:#2A63D4`（5.48）；标题 h1→h3 跳级 → 卡片标题改 `h2`；禁用按钮与可点态外观无差别 → `.button:disabled{opacity:.45}`；演示控制切回「无结果态」后命中卡不恢复。
  **仍未验证**：真实宿主内渲染（Cursor / deepseek harness）、屏幕阅读器实播；脱敏只扫了 DOM 文本（`sk-…`/长令牌/私钥头），宿主侧日志与剪贴板未覆盖。200% 缩放与纯键盘全流程已由上一条覆盖（`zoom200` 用布局视口减半 + 2× DPR 模拟浏览器缩放，不是真按 Ctrl+加号）。
- 宿主：Cursor 的 `notifications/progress` 渲染是**用户人工判定**（UI 不稳，日志无法自证）；harness 不支持 elicitation；审批/渲染行为仍 `[Q]`。以仓外 `research/10-host-support-report.md` 为准。
- 导入：`import-md-dir.js` 已补密钥模式扫描、零字节、同内容去重、编码告警与断点复用；附件仍只校验存在性与 SHA256，**不校验内容**；不解析 Logseq 双链/块引用；孤儿附件只报告不处理。

## 5. 隐私与发布

- 仓库内 `code/queries-t18.json` 仅为脱敏示例；真实 query 集与 `research/evidence/*` 在 `awam-knowledge.private/`，**不进 Git、不对外分发**。约定见 [RESEARCH.md](RESEARCH.md)。
- 已清理出仓：本机绝对路径、内网 IP、Windows 用户目录、团队工作区路径、两份语料扩展词典（含个人账号 / 站点标识）、调研过程全文。
- 仍存在、需人工确认：
  - 仓外 `research/10-host-support-report.md` 保留 `.office-claw` 作为宿主数据目录名（描述性，不含路径）。
  - 备份目录 `awam-knowledge.backup-20261029/` 里有**未脱敏的旧 git 历史与 agent 镜像**。用户 2026-10-29 决定**保留不动**：留在仓库外当离线备份，不提交、不推送、不删除；如需对外分发或释放空间，另作决定。
- 凭据：`EMBED_API_KEY` 仅运行时注入；历史对话中曾明文出现，**建议轮换**。
- 凭据：本机为验证 broker 把 `BW_SESSION` 写进了**用户级环境变量**（注册表 `HKCU\Environment`，明文、跨 shell 长期有效），`AWAM_SECRET_CMD='bw get password awam-mcp'` 依赖它。撤销：`[Environment]::SetEnvironmentVariable('BW_SESSION',$null,'User')`；长期更稳妥的做法是每次会话解锁后只在本进程内传递。
- MCP 状态目录（默认 `.spike-runtime/`，已 ignore）存 `state.json{,l}`、`audit.jsonl`、`proposals.json`、`imports/`、`index-cache.json` 与 `secret.key`（POSIX 0600；Windows 上 chmod 不生效，仅依赖状态目录 ACL）；撤销记录为支持窗口内回滚会保存**上一版文件内容快照**（`UNDO_WINDOW_MS` 内，默认 60s 后不再可用但仍留在日志中），清理策略待定；已用 token 只存 HMAC 串本身，不含密钥。`secret.key` 不得提交，发版前用 `AWAM_SECRET` / `AWAM_SECRET_CMD` 替代。审计记录含写入目标路径与 hash，不含文件内容。

## 6. Git 与远端

- 2026-10-29 再次清空历史并 `git init -b main` 做干净首提：`research/` 与真实 query 集不再入库；`origin` 仍指向 `https://github.com/awamwang/awam-knowledge.git`，**未推送**。
- 用户决定**暂不推送**：本地与 `origin/main` 历史分叉。将来对齐需 force-push，**需单独批准**。

## 7. 变更记录

- 2026-10-29（UI 验证补齐）：`ui-check` 增加 200% 缩放视口（640×450 布局视口 + 2× DPR）与纯键盘流程（真 Tab/Enter 事件走完确认写入 → 撤销），当前 0 问题。过程中确认 `rawKeyDown` 不激活按钮，Enter 必须带 `text` 走 `keyDown`，否则键盘断言会假失败。
- 2026-10-29（第七批）：job 进度数字随检查点落盘（此前只有 phase 跃迁与结束才写状态日志，另一个进程在整段 analyzing 期间都读到旧值）。mcp 自检 58→60：运行中直接读状态日志断言 ≥49、崩溃重启后断言 `done>0`；把落盘那行关掉复跑，两条都失败，确认断言有效。自检 220 项。
- 2026-10-29（决策落地，非代码变更）：召回评测**收口**（BM25 标题×4 为主、向量仅跨语言兜底，不扩集不重跑）；备份目录 `awam-knowledge.backup-20261029/` **保留不动**（仓库外离线备份）；远端**暂不推送**（分叉保留，force-push 仍需单独批准）。代码未改，自检仍 218 项。
- 2026-10-29（第六批）：导入器做平台归一（NFC + win32 大小写比对）并解析 Logseq 块引用（`((uuid))` 对 `id::`）；附件误判孤儿、双链页面假未命中这两类在 Windows 上的缺陷一并修掉。自检 218 项（import 23→31）。
- 2026-10-29（第五批）：导入 job 支持断点续跑 —— 运行中限频写检查点，取消/崩溃后 `resume_job` 起新 job 复用未变文件；phase 跃迁落盘。自检 210 项（mcp 46→58）。
- 2026-10-29（第四批）：新增独立追加式审计 `audit.jsonl` 与只读工具 `get_audit`，记录提案/提交/拒绝原因/撤销、客户端名与版本；状态日志压实不再可能带走审计。自检扩到 198 项（infra 30→38，mcp 38→46）。
- 2026-10-29（第三批）：密钥解析收敛到 `code/secret-provider.js`（签发端与校验端同源，长度校验统一为 ≥16），新增 `secret-broker-selftest.js`（18 项）与运维检查 `secret-broker-check.js`；`npm run selftest` 扩到 9 个脚本 182 项。密钥 broker 端到端已用真实 Bitwarden 跑通（成功路径 + 未解锁降级路径 + MCP 提交），该项关闭。
- 2026-10-29（第二批）：完成工程缺口 — MCP 真实导入 job（进度/取消/manifest/导入后刷新索引）、job·token·undo 持久化（快照+日志+崩溃恢复）、原子写与单写者锁、密钥三级来源（含 `AWAM_SECRET_CMD` 钩子）、写入根约束与 `undo_write`、索引增量缓存、settings.yaml 回退、导入器补齐（密钥扫描/零字节/去重/编码/断点复用/manifest v2）、辅助脚本自检；`npm run selftest` 扩到 8 个脚本 164 项。
- 2026-10-29：清理脚手架/探针/运行产物，抽出 `code/bm25-index.js`，MCP `search` 接真 BM25，配置改为环境变量，补 `npm run selftest`，路径与词典脱敏，证据归档到 `research/evidence/`。
