# AI 介入的知识系统：本地可控 + MCP（问答记录）

- 日期: 2026-10-03
- 来源: Grok Bot 群聊讨论
- 偏好结论: **本地可控、自己接 MCP**（非全托管封闭产品）

---

## 需求（用户）

AI 介入的知识系统，不再用界面化的管理工具，替代普通文档，以及例如 Notion、Logseq 这类知识管理工具。

- **关于安全**：将密码、密钥之类的使用隐藏方式或者单独工具进行管理。
- **关于召回**：要比原始的管理软件的关键字匹配更强。
- **密码 / 密钥**：可以快速反填（autofill）。

问题：有没有这类的 AI Agent，或者是技能，或者产品？

---

## 回答 1：整体格局

有，但很少「一个产品打满」以上三点；更常见是拆成两层。

### 知识 / 召回（对话优先，弱化 Notion 式界面）

- **产品向**：Mem、Reflect（仍有界面，但以 AI 召回为主）
- **Agent 记忆层**：Memobase、Hjarni、Knowz、Cerefox、Lore 等——面向 MCP / 多助手，语义 + 混合检索，比关键字强；笔记给 Agent 读，而不是人点点点
- **自建**：Obsidian / 本地库 + 向量检索 / GraphRAG，外面挂一个聊天 Agent

### 安全 / 密钥（不要进知识库）

密码和 API Key 应放 **1Password、Bitwarden** 等；Agent 只调「填充 / 取用」接口，正文里不存明文。

知识库只记「哪有账号、怎么用」，不记秘密本身。

### 技能形态

本质是「**记忆 / RAG MCP + 密码管理器连接器**」两个 skill 拼起来，而不是单一万能 Agent。

追问选项：全托管产品，还是本地可控、自己接 MCP？

---

## 用户选择

**本地可控、自己接 MCP。**

---

## 回答 2：本地栈建议

按本地栈拼，别买封闭第二脑。

### 知识层

- Markdown / Obsidian 或纯文件夹当唯一真相
- 上面挂本地 MCP：Knowz 本地库、Cerefox，或自建（SQLite / LanceDB + 混合检索：全文 + 向量）
- Agent 只通过 MCP 的 `search` / `add` / `update` 读写，人几乎不再开 Notion 式界面

### 密钥层

- 本机 1Password 或 Bitwarden；另接密码管理器 MCP / CLI
- 知识库只存「账号名、用在哪」，绝不存明文
- 需要登录时走填充，不把 secret 写进笔记

### 召回

- 入库时切块 + 嵌入
- 查询用 hybrid（BM25 + 向量）
- 重要实体可再加简单图谱
- 比 Logseq 关键字强一截

### 起步最小集

1. 一个笔记目录  
2. 一个知识 MCP  
3. Bitwarden / 1Password  

Cursor / Claude / 助手侧都能挂同一套。

---

## 相关参考（讨论中提到的方向）

| 名称 | 角色 |
|------|------|
| Mem / Reflect | AI 笔记产品（仍有 UI） |
| Memobase | 跨助手可移植记忆层（MCP） |
| Hjarni | AI-native second brain，MCP 知识库 |
| Knowz | 可本地的知识层，多客户端 / MCP |
| Cerefox | 用户自有、Agent 优先的共享记忆（向量检索） |
| Lore | 跨 Agent 记忆 SDK / MCP，含脱敏等 |
| 1Password / Bitwarden | 密钥与反填，与知识库分离 |

---

## 非目标（本讨论未承诺）

- 未在本次落地安装或配置具体 MCP
- 未把真实密码写入本技能或 docs
