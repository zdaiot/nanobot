# 07 — 生产级深水区：从 nanobot 到 openclaw 的三个进阶

> 学习笔记 · 面试加分项
>
> nanobot 是简化版，面试官会追问"生产级怎么做"。这篇笔记覆盖三个深水区。

---

## 一、向量记忆检索（RAG）—— 当纯文本记不住的时候

### 1.1 nanobot 的做法：纯文本 MEMORY.md

nanobot 的记忆系统很简单：
- 对话太长 → LLM 做摘要 → 写到 `MEMORY.md`
- 下次对话 → 把 `MEMORY.md` 全文塞进 system prompt

**天花板在哪？** 当 MEMORY.md 超过几千 token，它本身就会占满 context window。而且纯文本没法做"精确查找"——LLM 读到一大段摘要，可能找不到"3天前讨论的那个 API 设计方案"。

### 1.2 openclaw 的做法：完整的向量记忆系统

openclaw 的 `memory/` 模块有 63 个文件，是一个完整的 RAG 管线：

```
用户提问 → Query Expansion(查询扩展) → Embedding(向量化)
  → Hybrid Search(语义+关键词) → Temporal Decay(时间衰减)
  → MMR Re-ranking(多样性排序) → 返回 top-k 结果注入 context
```

### 1.3 关键概念详解

#### Embedding 选型

| Provider | 模型 | 维度 | 特点 |
|----------|------|------|------|
| OpenAI | text-embedding-3-small | 1536 | 通用、便宜 |
| Voyage | voyage-3 | 1024 | 代码场景效果好 |
| Gemini | embedding-001 | 768 | 免费额度大 |
| Mistral | mistral-embed | 1024 | 欧洲数据合规 |
| Ollama | nomic-embed-text | 768 | 本地部署、零成本 |

> **面试追问**："你怎么选 embedding 模型？" → 看场景。代码类选 Voyage，通用对话选 OpenAI，要省钱选 Ollama 本地跑。关键指标是 MTEB 排行榜的得分。

#### 向量数据库选型

| | SQLite-vec | LanceDB |
|---|-----------|---------|
| 部署方式 | 嵌入式（文件级） | 嵌入式（文件级） |
| 适合场景 | 轻量、单用户 | 大规模、需要版本管理 |
| 索引方式 | 暴力搜索 / IVF | DiskANN |

> 两者都是**嵌入式**的，不需要单独部署数据库服务。这是 Agent 场景的关键选择——不想为了记忆功能多跑一个 Redis/Milvus。

#### 为什么 top-k 不够，需要 MMR？

搜索 "Python Web 框架"，top-5 结果可能全是 Flask 相关。你其实更想看到 Flask、Django、FastAPI 各一条。

**MMR（Maximal Marginal Relevance）** 的核心公式：

```
MMR = λ × relevance - (1-λ) × max_similarity_to_selected

λ = 0.7（默认）→ 偏向相关性，但惩罚重复
λ = 1.0 → 退化为普通 top-k
```

openclaw 用 **Jaccard 相似度**（词集合交并比）计算内容相似度：`|A ∩ B| / |A ∪ B|`

#### Query Expansion（查询扩展）

用户说 "之前讨论的那个方案" → 对向量搜索来说太模糊了。openclaw 的 `query-expansion.ts` 做了：

1. **Stop Words 过滤**：去掉无意义词（支持中英日韩等 7 种语言）
2. **关键词提取**：留下有信息量的词
3. **LLM 辅助扩展**（可选）：让 LLM 把模糊查询改写成精确关键词

### 1.4 面试话术

> "nanobot 用纯文本 MEMORY.md 做记忆，适合短期轻量场景。但生产级系统需要 RAG 管线：查询扩展把模糊问题变成精确关键词，Embedding 把文本向量化，Hybrid Search 同时做语义搜索和关键词搜索并融合分数，最后用 MMR 做多样性排序避免结果重复。向量数据库选型上，嵌入式方案比独立服务更适合 Agent 场景。"

---

## 二、安全体系 —— Agent 有手有脚之后怎么管

### 2.1 nanobot 的做法：5 层基础防护

nanobot 有基本的安全措施（06-interview Q8）：白名单、文件沙箱、命令超时、结果截断、子 Agent 无 spawn 权限。

**够用么？** 对个人项目够了。但生产级系统面对的是**恶意用户**和**不可信的外部输入**，需要更完善的安全体系。

### 2.2 openclaw 的完整安全体系（4 层）

#### 第一层：工具风险分级

不是所有工具都一样危险。openclaw 把工具分成三档：

| 低危（默认允许） | 中危（需审批） | 高危（默认禁止） |
|----------------|--------------|----------------|
| read_file, search, list_dir | fs_write, fs_delete, apply_patch | exec, shell, sessions_spawn, gateway |

**关键思想**：同一个工具，在不同**调用场景**下风险不同。本地 CLI 调用 exec 是正常的，但通过 HTTP API 远程调用 exec 就是 RCE 漏洞。

#### 第二层：Prompt Injection 防御（外部内容隔离）

当 Agent 处理外部输入（邮件、Webhook、网页）时，恶意内容可能伪造系统指令。openclaw 的 `external-content.ts` 实现了：

**1) 可疑模式检测**：正则匹配 "ignore all previous instructions"、"you are now a"、伪造 system 标签等。

**2) 安全边界标记（带随机 ID 防伪造）**：

```
<<<EXTERNAL_UNTRUSTED_CONTENT id="a1b2c3d4e5f6g7h8">>>
（不可信内容）
<<<END_EXTERNAL_UNTRUSTED_CONTENT id="a1b2c3d4e5f6g7h8">>>
```

为什么需要**随机 ID**？ 恶意用户可能写一个假的结束标记，提前"关闭"安全边界。随机 ID 让伪造变得不可能。

**3) Unicode 同形字清洗**：攻击者可能用全角 `＜＜＜` 或 CJK 角括号绕过检测。openclaw 把 66 种角括号变体统一成 ASCII 再检测。

#### 第三层：Skill 安全扫描

openclaw 的 `skill-scanner.ts` 在加载每个 Skill 时做代码审计：
- 🔴 critical：Shell 命令执行、动态代码（eval）、挖矿特征、环境变量收割
- 🟡 warn：文件读取+网络发送组合（数据外泄嫌疑）、大段 base64（混淆代码）

> **设计思想**：Skill 是可扩展的，任何人都可以写。所以**加载前必须扫描**，就像应用商店审核 App。

#### 第四层：调用场景感知

| 场景 | exec 工具 | sessions_spawn |
|------|----------|---------------|
| 本地 CLI | ✅ 允许 | ✅ 允许 |
| ACP（自动化） | ⚠️ 需确认 | ⚠️ 需确认 |
| HTTP Gateway | ❌ 禁止 | ❌ 禁止 |

### 2.3 面试话术

> "Agent 安全我分四层思考：工具风险分级、外部内容隔离（带随机 ID 的安全边界+Unicode 清洗）、Skill 安全扫描（静态代码审计）、调用场景感知。核心理念是**架构兜底，不依赖 prompt 层的弱防护**。"

---

## 三、Token 精细化管控 —— 每个 Token 都是钱

### 3.1 nanobot 的做法：简单近似

nanobot 的 token 估算：`estimated_tokens = len(text) / 4`（字符数除以4）。对英文大致成立，对中文（1 个字 ≈ 1-2 token）偏差很大。

触发和目标也很简单：超过 `context_window_tokens` 就压缩到一半。

### 3.2 openclaw 的 Bootstrap Budget 精确管控

当用户的 Bootstrap 文件（AGENTS.md / SOUL.md 等）太大时，nanobot 直接全部塞进去。openclaw 实现了**精确的预算分配和截断机制**（`bootstrap-budget.ts`，349 行）：

```
两个限制维度：
├── per-file limit（单文件上限）：每个 Bootstrap 文件最多占多少字符
└── total limit（总量上限）：所有 Bootstrap 文件加起来最多占多少字符

工作流程：
1. 读取所有 Bootstrap 文件，统计 rawChars
2. 尝试注入 → 统计 injectedChars
3. injectedChars < rawChars → 被截断了
4. 分析原因：单文件超限 or 总量超限
5. 生成警告 + 用签名去重（同样的截断只警告一次）
```

Safety Margin = 1.2（20% 缓冲），因为估算 token 数不精确，乘以 1.2 确保偏少时不会真正超标。

### 3.3 多步摘要合并

消息量太大时，一次摘要会超出 LLM 上下文窗口。openclaw 的解法：

```
Step 1: 100 条消息按 token 比例分成 2 块
        ├── Chunk A（1-50）→ LLM 摘要 A
        └── Chunk B（51-100）→ LLM 摘要 B

Step 2: 摘要 A + 摘要 B → LLM 合并成最终摘要
```

合并时的保留优先级：
- ✅ 正在进行的任务及当前状态
- ✅ 批量操作进度（如 "5/17 项已完成"）
- ✅ 已做出的决策及理由、TODO 和开放问题
- ✅ 近期上下文优先于旧历史

### 3.4 Identifier 保护策略

压缩摘要时的隐蔽坑：LLM 可能"简化"标识符。

```
原文：UUID a1b2c3d4-e5f6-7890-abcd-ef1234567890
摘要：一个 UUID（开头是 a1b2...） ← 信息丢失！
```

openclaw 的三种策略：
- **strict（默认）**：保留所有不透明标识符原样（UUID、hash、URL、文件名）
- **custom**：用户自定义保护规则
- **off**：关闭保护（不推荐）

### 3.5 面试话术

> "Token 管控我关注三个层面：Bootstrap 预算控制（per-file + total limit + 20% Safety Margin）、多步分块摘要（先分块各自摘要再合并，优先保留任务状态和近期上下文）、Identifier 保护（摘要时原样保留 UUID/URL 等不透明标识符）。"

---

## 总结：三张表速查

### 向量记忆 RAG 管线

```
Query Expansion → Embedding → Hybrid Search → Temporal Decay → MMR → top-k
  (查询扩展)     (向量化)    (语义+关键词)    (时间衰减)     (多样性)  (结果)
```

### 安全体系四层模型

```
工具风险分级 → 外部内容隔离 → Skill 安全扫描 → 调用场景感知
  (低/中/高)   (边界标记+清洗)  (静态代码审计)   (CLI/API/HTTP)
```

### Token 管控三板斧

```
Bootstrap Budget → 多步分块摘要 → Identifier 保护
 (预算+截断+警告)   (分块→合并)    (标识符原样保留)
```

---

> **如何在面试中使用这篇笔记**：当面试官问到 nanobot 的简化版实现时，先讲 nanobot 的做法（简单明了），然后**主动**补充"但在生产环境中还需要..."，展示你对完整系统的理解。这比被动等面试官追问要强得多。

**返回 ←** [06-interview.md](./06-interview.md) 面试高频问题速查
