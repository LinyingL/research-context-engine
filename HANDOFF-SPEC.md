# Research Context Engine v0 — 交接规格

> 本文档自包含：开发 agent 没有任何前置对话上下文，一切以本文档为准。
> Owner：Linying（产品决策人）。产品与代码英文优先，与 Owner 的沟通、报告一律中文。
> 状态：2026-07-21 定稿。竞品扫描存档（仅供人类参考，非开发依据）：https://claude.ai/code/artifact/b52b10db-11c3-46b1-b3af-6d21d4dc282b

---

## 0. 宪法层（凌驾于本文档其余所有内容）

**第一性原理（项目为什么存在）**
北极星查询：**"这个结果哪来的？"**——对一个科研项目，能带着可核验的证据链回答
"Figure 4 来自哪个实验 / 哪次 commit / 哪段代码 / 支撑论文哪一段"。
两条不可违反的约束：
1. **零习惯改变**：用户继续用 GitHub / LaTeX / W&B，本引擎旁路只读，绝不要求用户改变工作方式或手动录入。
2. 每个 backlog 项立项前必须回答："它让北极星查询更准、更快或覆盖更广了吗？"答不上来就不做。

**奥卡姆剃刀（怎么做的裁决规则，冲突时按序引用）**
1. 能用现成库/标准解决的，不自己写。
2. 能用确定性代码（解析、正则、AST）解决的，不用模型。
3. 必须用模型时，能用本地 7B 解决的，不用更大的。
4. 能用单文件（SQLite/JSON）解决的，不引入服务型依赖（不装数据库服务、不上消息队列、不做微服务）。
5. 同类代码出现第三次才允许抽象；新增第三方依赖必须书面说明"为什么标准库/现有依赖不够"。

裁决优先级：第一性原理 > 奥卡姆 > 本文档细节 > agent 自主判断。拿不准时停下来问 Owner，不要猜。

---

## 1. 产品一句话与定位

**Research Context Engine**：架在研究者既有工具（GitHub、repo 内 LaTeX、W&B/MLflow）之上的语义理解层。
自动抽取对象、建立溯源关系、维护项目上下文，通过 MCP 提供给用户已有的 AI 助手（Claude/Cursor 等）。
它不是科研平台，不是笔记工具，不替代任何现有工具。形态：**本地 CLI + 守护进程 + MCP server + 本地只读 Web 视图**。云上没有任何组件。

## 2. 已定架构决策（不得重开讨论）

| 决策 | 内容 | 一句话理由 |
|---|---|---|
| 输入三层 | ①确定性解析层：默认常开，零模型 ②AI 增强层：本地 7B（4bit），可关闭 ③人工层：只做确认/纠错/注释＋状态字段所有权 | 手动为默认已被明确否决（品类死因）；状态类语义归人 |
| 隐私 | v0 数据不出本机；云端 API 仅作 BYOK 开关且**默认关闭** | Owner 拍板：隐私优先于速度 |
| 模型档位 | 抽取/分类/摘要用 7B@4bit（Q4_K_M 或同级 AWQ；禁用 3bit）；无云端大模型时，疑难连边的升级阀=人工确认队列 | 3bit 在 7B 上有质量悬崖，省的显存不值 |
| 存储 | SQLite 单文件（nodes 表 + edges 表 + 确认队列表）；v0 禁用图数据库服务 | 奥卡姆规则 4 |
| 图谱数据模型 | **生产级**：设计评审、迁移机制、测试覆盖；其余代码原型级快速迭代 | schema 是核心资产，改起来伤筋动骨 |
| 每条边的元数据 | 必须带：抽取来源（哪个解析器/模型）、证据指针（文件:行 / run ID / commit SHA）、置信度、确认状态 | 引擎的产品本体是信任；无证据的边不允许存在 |
| 出口 | MCP server 为主界面；本地 Web 视图为辅；每周本地摘要 | 用户已有的 AI 助手就是前端 |
| Contribution | 静默记录贡献者边（git 作者、run owner）；**不做任何评分/展示** | 评分维度与权重由 Owner 本人后续定义，agent 不得擅自设计 |

## 3. v0 范围

**做：**
- 摄入：本地 git 仓库（代码、LaTeX 源码、.bib 文件、图片文件）＋ W&B **或** MLflow（二选一先做，另一个紧随；用只读 API/本地目录）
- 确定性骨干图（见 §5 连接键）
- 7B 语义增强（见 §6）
- MCP server：溯源问答工具（查询走图谱，答案必附证据链；图上没有的链，明说没有，禁止编造）
- CLI：`init` / `ingest` / `status` / `query`（CLI 命名避开 `ros`，与机器人 ROS 冲突；暂用 `rce`，Owner 可改）
- 本地 Web 视图：时间轴、图谱浏览、低置信边确认队列、状态字段编辑
- 每周摘要（本地 markdown：新对象、过期图表、待确认边）

**不做（v0 明确排除）：**
- Overleaf / Zotero / OpenReview 集成（v0.5 再议）
- 云服务、账号系统、多人协作、任何网络上传
- 论文编辑器（永不）
- Contribution 评分与展示
- Meeting / Task / 湿实验对象
- 硬验收门槛（见 §8 的替代机制）

## 4. 对象模型最小集（ontology v0）

节点类型（8 个，不得擅自增加）：
`Project` `Experiment`（=一次 run）`Commit` `Figure` `Section` `Claim`（正文中的量化断言，如"87.3%"）`Reference`（.bib 条目）`Contributor`

边类型（按抽取层分组）：

确定性层（零模型）：
- `Commit --implements--> Experiment`（run 记录的 git SHA）
- `Experiment --produces--> Figure`（run artifact / 输出文件名）
- `Commit --generates--> Figure`（代码静态分析 savefig/输出路径；src=生成代码所在 commit，具体文件:行放 evidence。2026-07-22 勘误：原文误写 Code，8 类节点中无 Code，实现按 Commit 落地）
- `Section --includes--> Figure`（\includegraphics 文件名）
- `Section --cites--> Reference`（\cite key ↔ .bib）
- `* --authored_by--> Contributor`（git 作者 / run owner）

7B 语义层（全部带置信度）：
- `Claim --backed_by--> Experiment`（正文数字 ↔ run 指标）
- `Figure --supports--> Section`（图表与论述的语义关联）
- `Experiment --summarized_as--> …`（对象摘要，挂在节点属性上）

人工层：任何边的确认/否决/手动补充；`Experiment.status` 等状态字段仅人可写。

## 5. 连接键清单（确定性层的全部依据）

1. `\includegraphics{path}` 文件名 ↔ repo 内图片路径
2. `\cite{key}` ↔ .bib 条目（含 natbib/biblatex 常见变体：\citep/\citet/\citealp/\parencite/\textcite/\autocite；bib key 匹配大小写不敏感。2026-07-22 补充，理由：真实论文多用 \citep/\citet，只匹配 \cite 会让 cites 边在真实数据上几乎不触发）
3. W&B/MLflow run 记录的 git commit SHA ↔ repo commit
4. run 的 artifact/输出文件名 ↔ repo 内文件
5. 代码中 `savefig(...)` / 输出路径字面量 ↔ 图片文件（AST/正则，含常见变量拼接的保守处理：拼不出来就放弃，不猜）
6. `\label` / `\ref` 内部引用
7. 正文数字 ↔ run 指标：**候选由代码生成**（正则＋数值容差），7B 只在候选中裁决

## 6. 7B 管线硬性要求

- 推理服务选当前最简单可用方案（如 Ollama 或 llama.cpp server），模型建议 Qwen 系 7B 级 instruct，Q4_K_M
- **约束解码强制**（GBNF / guided JSON）：格式合法性由采样器保证，不靠模型自觉
- **验证器兜底**：每条模型产出的边先机器核验（文件/commit/run 是否真实存在），核验失败直接丢弃并记日志
- 低置信（阈值先取保守值，可配置）→ 入人工确认队列，不入正式图
- 所有任务切成小上下文微任务（分类/裁决/短摘要）；禁止把长文档整段喂 7B
- 模型服务未运行时：引擎退化为纯确定性层照常工作，语义任务标"待处理"

## 7. 三阶段计划（每阶段独立可交付，顺序不得颠倒）

**Phase A — 骨干（先于一切模型工作）**
git+LaTeX+.bib+W&B/MLflow 摄入 → 确定性图入 SQLite → MCP server + CLI query 可答溯源问题。
完成定义：对 1 个公开项目，在 Claude/Cursor 里通过 MCP 问"Figure X 哪来的"，得到带证据链的正确回答。
（注意：Phase A 结束时产品已有演示价值——这是奥卡姆的直接体现，骨干不依赖任何模型。）

**Phase B — 语义层**
7B 服务＋约束解码＋验证器＋置信度；实现 §4 语义层三类边；确认队列落表。
完成定义：语义边带置信度出现在图中；验证器拒绝率有日志可查。

**Phase C — 人机闭环与测评**
Web 视图（时间轴/图谱/确认队列/状态编辑）＋每周摘要＋测评机制（§8）。
完成定义：每周错误报告脚本可一键运行；人工纠错持久化并覆盖模型判断。

## 8. 测评机制（代替硬门槛）

- Phase A 期间由 agent 按以下标准挑选并报 Owner 确认 **3–5 个公开复现项目**做测试床：repo 含 LaTeX 论文源码；有公开 W&B project 或 repo 内 MLflow 记录；图表由脚本生成；提交历史真实非玩具
- 对其中至少 1 个项目，人工标注 **50–100 条金标准边**（agent 起草、Owner 抽查），存为版本化 YAML
- 每周跑一次对照：输出错误案例清单＋准确率趋势。**不设通过/不通过的硬数字**（Owner 明确决策）；趋势恶化即停下修根因
- 任何指标阈值、权重类数字：agent 不得自行设定为"标准"，一律报 Owner 拍板

## 9. 工程纪律

- 遵守 Owner 全局 guardrails（no-diff-no-done、no-evidence-no-success、size-budget、understand-before-modify、verify-after-each-file、revert-before-retry）
- 技术栈默认 Python；依赖最小化（§0 奥卡姆规则 5）
- 面向 Owner 的一切报告用中文产品语言，技术细节折叠
- 图谱 schema 变更 = 高风险改动：先出设计说明再动手

## 10. 留给 Owner 的开放项（agent 不要自行决定）

- 产品对外名称与 CLI 最终命名（现用 ResearchOS / `rce` 代号）
- 7B 具体选型的最终确认（agent 给出 2–3 个候选与实测对比后由 Owner 选）
- v0.5 优先级：Overleaf git 同步 vs Zotero vs OpenReview
- Contribution 评分的维度与权重（Owner 亲自定义，时间未定）
