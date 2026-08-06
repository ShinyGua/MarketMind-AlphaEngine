<p align="center">
  <h1 align="center">MarketMind-AlphaEngine</h1>
  <p align="center">
    <strong>多智能体股票研究系统与投行风格报告引擎</strong>
  </p>
  <p align="center">
    自动化日报与周报分析 · 多分析师辩论 · 情景 DCF + 可比公司估值 · BUY/HOLD/SELL 决策 · JPM 风格 PDF 报告
  </p>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/version-dev%200.1-orange.svg" alt="Version">
  <img src="https://img.shields.io/badge/platform-Claude%20Code-purple.svg" alt="Platform">
  <img src="https://img.shields.io/badge/python-3.10%2B-green.svg" alt="Python">
</p>

<p align="center">
  <a href="README.md"><strong>English</strong></a>
</p>

---

> **重要声明**
>
> 本项目是一个**实验性研究项目**，使用 AI 智能体生成投行风格的股票研究报告。系统输出的报告、分析结论以及 BUY/HOLD/SELL 建议仅供**学习与研究用途**，**不构成任何投资建议、理财指导或证券买卖建议**。
>
> **请勿依据本系统的输出做出投资决策。** 在进行任何投资操作前，请务必咨询合格的金融顾问，并自行完成充分的尽职调查。项目作者不对因使用本系统输出而造成的任何财务损失承担责任。

---

## MarketMind-AlphaEngine 是什么？

MarketMind-AlphaEngine 是一个原生构建在 [Claude Code](https://code.claude.com) 之上的全自动股票研究流水线。它把自己当作一个**自治型研究组织**来运行：自动采集市场数据、执行量化分析、组织多分析师结构化辩论、撰写机构级研究报告，并基于证据链输出投资决策与理由。

### 它和普通 Agent 项目有什么不同？

- **多智能体辩论**：3-6 位分析师分别独立分析同一只股票，再通过多轮评审团展开辩论——每一轮每位分析师都给出带置信度自评的方向性立场，主持者汇总该轮结果，循环持续到观点收敛——从而形成更平衡的投资观点，而不是单一 Agent 的总结
- **量化估值引擎**：公式驱动的 DCF（CAPM WACC、Gordon 永续价值、乐观/基准/悲观三档情景、WACC×永续增长率敏感性矩阵），加上带分位数基准的可比公司估值，产出内在价值区间和**安全边际**。CAPM 无风险利率取自宏观层的**实时美债 10 年期收益率**（记录来源溯源，并有配置兜底值）。混合公允价值的置信度**由 DCF/可比公司各分项的置信度推导**（低置信度的 DCF 无法把混合结果抬成「高」），并作为**按置信度加权的参考**输入 BUY/HOLD/SELL 决策——提供支撑、但绝不单独决定方向
- **宏观环境层（仅作背景，绝非触发器）**：确定性采集器拉取 CPI、联邦基金利率、美债收益率曲线、广义美元指数、高收益债利差与 VIX（FRED 优先，无 key 时自动退化到 yfinance 代理），并将环境分类——利率趋势、曲线斜率、通胀趋势、政策取向、VIX 分位、信用利差状态——写入共享的 `macro_regime.json`（每个字段带数据质量标记）。重大宏观观察会生成可引用的证据卡。该环境层输入 DCF 无风险利率、分析师叙事框架与对冲叠加——但绝不机械地翻转任何投票
- **盘中时点模块（仅用于择时）**：1小时/4小时 RSI/MACD 与摆动高低点只用于为最终观点框定分批进出场价格区间；若任何评审成员把盘中动能写成投票理由，评分器会发出警告——在研究的时间尺度上，盘中信号是噪音而非依据
- **投行风格 PDF**：通过确定性的 Markdown → HTML/CSS → PDF 流水线（WeasyPrint）生成 JPM 风格研究报告——首页评级框、内嵌标注图表、带样式表格，以及中英双语排版
- **讨论评审团（立场 → 收敛 → 退出）**：投资观点由多轮评审团锻造——每一轮每位分析师角色各提交一份结构化视角（立场 bullish/neutral/bearish + 置信度自评 + 对其他角色的质询），确定性评分器（`eval/graders/discussion_convergence_grader.py`）度量收敛程度，循环持续到观点一致或触及硬性轮次上限。紧凑的 `*_view.json` 取代全量 N×N 长文互评，使 token 成本在分析师人数增加时保持平稳
- **决策评审团（投票 → 收敛 → 退出）**：最终决策由多轮评审团产生——每位分析师角色各投一票（BUY/HOLD/SELL，并附带置信度自评与对冲叠加），确定性评分器度量收敛程度，循环持续到评审团达成一致或触及硬性轮次上限。随后主席据「按置信度加权的倾向」写出最终结论，并保留少数派异议
- **日期归档历史**：每次运行都按 `{YYYY-MM-DD}/` 目录保留结果，因此可以对同一家公司进行连续日度分析而不丢失历史研究记录
- **智能交易日逻辑**：自动识别正确的数据截面，处理盘前、周末和节假日等情况
- **MCP 服务器架构**：3 个 Model Context Protocol 服务器（market-data、mm-workspace、memory）为 Agent 提供结构化的数据、文件和持久记忆工具访问
- **长期记忆系统**：情景记忆、语义记忆和过程记忆三层架构，使系统能跨运行回顾历史分析、学习到的模式和优化后的流程
- **自动化评测流水线**：代码评分器对每次运行进行多维度打分——其中包含一个**咨询性的决策风险门**，对过度自信进行提示（基于弱收敛、保留异议、低置信度估值或证据稀薄计算出一个不修改决策的置信度上限）——并配合运行日志和聚合指标持续追踪报告质量
- **免费数据源优先**：完全支持免费 API（yfinance、NewsAPI 免费版、SEC EDGAR、FRED），没有 API key 时可自动退化到 WebSearch

---

## 快速开始

### 前置依赖

- 已安装 [Claude Code](https://code.claude.com) CLI —— 或 [Codex CLI](https://developers.openai.com/codex)（见[用 Codex CLI 运行](#用-codex-cli-运行claude-code-之外的另一种方式)）
- 已安装 [Ralph Loop plugin](https://github.com/anthropics/claude-code)（推荐，用于长时间连续执行）
- Python 3.10 及以上
- 生成 PDF 需要 WeasyPrint 的原生依赖（Pango、cairo、GDK-PixBuf）。macOS：`brew install pango gdk-pixbuf libffi`；Debian/Ubuntu：`apt-get install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0`。中文报告需安装 CJK 字体（如 `fonts-wqy-microhei` 或 Noto Sans CJK）。（`weasyprint` 本身由 `setup.sh` 安装。）

### 安装与启动

```bash
# 1. 克隆仓库
git clone git@github.com:ShinyGua/MarketMind-AlphaEngine.git
cd MarketMind-AlphaEngine

# 2. 创建 Python 环境，并固定仓库根目录
source setup.sh
export MM_ROOT="$(pwd)"

# 3. （可选）配置 API key
cp config.example.yaml config.yaml
# 编辑 config.yaml 中的数据源配置
# API 密钥通常通过环境变量提供

# 4. （可选）配置 API key —— 推荐放在仓库根目录的 .env 文件里。
#    market-data MCP 服务器会在启动时自动加载，无需 export，且跨会话保留。
cp .env.example .env
# 编辑 .env，填入 NEWSAPI_KEY / FRED_API_KEY
# （shell export 仍然有效且优先级更高；不设置也可以运行，系统会自动回退到 WebSearch）

# 5. 启动 Claude Code 并加载插件（在仓库根目录下运行）
claude --plugin-dir "$MM_ROOT/plugin" --dangerously-skip-permissions
```

### NewsAPI 申请与填写位置

如果你希望新闻采集质量高于默认的 WebSearch 回退方式，建议先申请 NewsAPI key：

1. 打开 [newsapi.org/register](https://newsapi.org/register) 注册账号
2. 完成邮箱验证后，在 NewsAPI 控制台或官方示例中复制你的 API key
3. 把它填入 `.env` 的 `NEWSAPI_KEY`（或在 shell 里 export）

本项目读取的键名定义在 [`config.example.yaml`](config.example.yaml) 这里：

```yaml
data_sources:
  news:
    api_key_env: NEWSAPI_KEY
```

推荐把密钥放在仓库根目录的 `.env` 文件里（复制 `.env.example`）。market-data MCP
服务器会在启动时自动加载 `.env`，因此无需手动 export：

```bash
# .env
NEWSAPI_KEY=your_newsapi_key
FRED_API_KEY=your_fred_key
```

`NEWSAPI_API_KEY` 也可作为 `NEWSAPI_KEY` 的别名。shell 里的 `export NEWSAPI_KEY=...`
依然有效，且优先级高于 `.env`。

> **注意：** MCP 服务器只在启动时读取一次 `.env`，所以改完 key 后需要重启
> Claude Code / Codex 才会生效。

### 用 Codex CLI 运行（Claude Code 之外的另一种方式）

MarketMind 也可在 [Codex CLI](https://developers.openai.com/codex) 下运行。`mm-*` 技能通过 `.agents/skills/`（指向 `.claude/skills/` 的符号链接）作为 **Codex 原生技能** 暴露，`AGENTS.md` 是始终加载的控制入口。

```bash
# 1. 注册 MCP 服务器：把 .agents/references/codex-config.toml 里的三个
#    [mcp_servers.*] 表合并进 ~/.codex/config.toml
# 2.（可选）把 API key 放进 .env（自动加载），然后在仓库根目录启动 Codex
#    （MCP 服务器的 command/args 路径是相对仓库根目录的）。也可改用 shell export：
export NEWSAPI_KEY="your_newsapi_key"   # 可选；未设置时回退到网络搜索
codex
```

在 Codex 中用 `/skills` 查看技能。只有 **`mm-init`** 和 **`mm-orchestrator`** 是可隐式匹配的入口（例如*"为 NVDA 初始化工作区"*、*"对 workspaces/NVDA 运行 MarketMind 流水线"*）。内部各阶段技能为显式调用（`$mm-…`）——它们运行在被编排的流水线中，不可单独运行。完整的 Codex 配置见 `AGENTS.md` 与 `.agents/`。

**推荐 —— 用无头驱动脚本以「对齐 Claude 的深度」运行整条流水线**。它把每个阶段作为独立的 `codex exec` 运行（每个阶段一个全新上下文，等价于 Claude 的 `context: fork`），从而让产出与 Claude 一致，而不会被压缩成残缺片段：

```bash
.venv/bin/python3 scripts/run_codex_pipeline.py workspaces/NVDA
.venv/bin/python3 scripts/run_codex_pipeline.py workspaces/NVDA --dry-run   # 仅打印执行计划
```

确定性阶段（估值、图表/PDF、去重、评分器）作为已提交的 Python 直接运行；LLM 阶段（数据台、分析师、撰写器……）作为并行的 `codex exec` 运行；深度闸会重做任何过薄的 memo/报告。

> **拉取实时数据需要放开网络出站。** Codex 的 `workspace-write` 沙箱默认禁止出站网络，
> 因此采集台会静默回退到 WebSearch/缓存（诊断里会看到 `dns_failed`）。无头驱动会为每个
> 阶段加上 `-c sandbox_workspace_write.network_access=true`；交互式 `codex` 则靠
> `.agents/references/codex-config.toml` 里的 `[sandbox_workspace_write] network_access = true`
> 实现同样效果。collect 阶段会先运行 `scripts/check_data_sources.py`，把 key 是否存在（绝不
> 打印取值）、DNS、以及每个数据源的状态（`auth_ok` / `dns_failed` / `auth_failed` /
> `rate_limited` / `no_key`）写入 `raw/{date}/diagnostics/data_sources.json` —— 当某次运行
> 回退过多时可据此排查。

### 使用方法

```text
/mm:init                          # 创建新的公司工作区（交互式）
/mm:run workspaces/NVDA           # 运行完整的 15 阶段研究流水线
/mm:status                        # 查看所有工作区状态
```

`/mm:init` 的流程如下：

1. 询问你要分析的公司（股票代码或公司名称）
2. 自动进行网页检索校验，例如：`NVIDIA Corporation (NVDA) - NASDAQ`
3. 由你确认或纠正识别结果
4. 创建工作区并自动启动完整流水线

---

## 流水线架构

```text
/mm:run workspaces/{TICKER}
```

```text
+== MarketMind /mm:run ================================================+
|
|  已有公司 workspace
|       |
|       v
|  解析配置并校验当前状态
|       |
|       v
|  初始化工作区上下文
|       |
|       v
|  Collect（4 个采集器并行）
|       |-- Market desk: 宏观新闻、指数、宏观资产
|       |-- Company desk: 公司新闻、监管披露、催化事件
|       |-- Sector desk: 行业新闻、可比公司价格数据
|       |-- Web research: 带来源溯源的网络/NASDAQ 新闻
|       |
|       v
|  Normalize：整理证据卡片与时间序列表
|       |
|       v
|  Quant 快照
|       | RSI、MACD、量价与筹码结构、相对强弱
|       v
|  Valuation 估值（情景 DCF + 可比公司）
|       | 内在价值区间、安全边际、估值判断
|       v
|  Discussion 辩论循环
|       |-- 分析师独立 memo（并行）
|       |-- 评审团多轮：分析师视角 -> 主席汇总 -> 收敛
|       |-- 观点综合与 thesis map
|       |
|       v
|  生成机构风格研究报告草稿
|       |
|       v
|  Review 循环
|       | 通过 -> 继续
|       | 不通过 -> 定向修订 -> 重写草稿（最多循环次数由 config 控制）
|       v
|  投资决策（多轮评审团：投票 -> 主席汇总 -> 收敛）
|       | BUY / HOLD / SELL + 风险叠加 + 置信度 + 风险项
|       v
|  导出 markdown + JSON + 图表 + PDF（WeasyPrint）
|       |
|       v
|  用户反馈（唯一暂停等待输入的阶段）
|       | 收集用户对报告的反馈意见
|       v
|  Reflect 反思
|       | 评估运行质量、更新长期记忆、
|       | 记录指标用于持续改进
|       v
|  最终按日期落盘到 workspaces/{TICKER}/final/{YYYY-MM-DD}/
|
+----------------------------------------------------------------------+
```

### 阶段说明

| 阶段 | 发生了什么 | 负责角色 |
|------|------------|----------|
| **Collect** | 先运行确定性宏观层（FRED/代理序列 → 环境分类 → 宏观证据卡），再从 yfinance、NewsAPI、EDGAR 收集宏观、公司与行业数据，并采集带来源溯源的网络/NASDAQ 新闻 | 宏观脚本 + 4 个采集器并行 |
| **Quant** | 用 Python 计算 RSI、MACD、SMA、ATR、相对强弱等指标，外加确定性的筹码结构引擎（量价/成本分布/支撑压力，方向性证据）、同类股分化分类与 1小时/4小时盘中时点模块（仅用于择时） | `mm-quant-analyst` |
| **Valuation** | 基于 yfinance 基本面数据计算情景 DCF（实时 10 年期无风险利率）+ 可比公司 + 安全边际 | `mm-valuation-engine` |
| **Debate** | 分析师独立写 memo，随后通过多轮评审团：分析师视角 → 主席汇总 → 收敛 | 3-6 位分析师 |
| **Draft** | 生成带证据追踪的 JPM 风格叙事研究报告 | `mm-report-writer` |
| **Review** | 进行多维度打分并驱动迭代修订 | `mm-report-reviewer` |
| **Decide** | 多轮评审团——各角色投票并自评置信度，收敛评分器把关循环，再输出最终 BUY/HOLD/SELL 决策（含对冲叠加） | `mm-decision-panelist`、`mm-decision-maker` |
| **Export** | 生成标注 SVG 图表，并通过 Markdown→HTML/CSS→PDF（WeasyPrint）导出 JPM 风格报告 | `mm-pdf-exporter` |
| **User Review** | 暂停收集用户反馈 — 是否认同、修正意见、个人洞察 | 用户（human-in-the-loop） |
| **Reflect** | 通过代码评分器评估运行质量、存储用户反馈和长期记忆 | eval 流水线 + memory |

### 分析师角色（可配置）

| # | 角色 | 关注重点 | 默认启用 |
|---|------|----------|:-------:|
| 1 | `company_analyst` | 基本面、公司事件、关键催化剂 | ✓ |
| 2 | `chips_analyst` | 筹码博弈：量价/资金流向证据、主力视角推演 | ✓ |
| 3 | `risk_analyst` | 空头逻辑、下行风险、失效条件 | ✓ |
| 4 | `market_analyst` | 宏观环境、板块定位、alpha 与 beta | ✓ |
| 5 | `valuation_analyst` | 解读估值引擎的 DCF 区间、可比公司与安全边际 | |
| 6 | `catalyst_analyst` | 事件时点、财报日历、短期催化 | |

如需启用更多分析师，可在 `config.yaml` 中取消对应注释。

---

## 项目结构

```text
MarketMind-AlphaEngine/
├── .claude/
│   ├── agents/                    # 模型档位定义（heavy/standard/light）
│   └── skills/                    # 24 个 agent skill
│       ├── mm-orchestrator/       # 流水线总控（铁律：不能停）
│       ├── mm-company-resolver/   # 股票代码 -> 公司画像 + 同业
│       ├── mm-market-desk/        # 宏观数据采集
│       ├── mm-company-desk/       # 公司新闻、披露文件与基本面采集
│       ├── mm-sector-desk/        # 行业与同业数据采集
│       ├── mm-web-research/       # 带来源溯源的网络/NASDAQ 新闻采集
│       ├── mm-quant-analyst/      # 技术指标计算
│       ├── mm-valuation-engine/   # 情景 DCF + 可比公司 + 安全边际
│       ├── mm-market-analyst/     # 市场环境分析
│       ├── mm-company-analyst/    # 公司基本面分析
│       ├── mm-risk-analyst/       # 风险识别与反方论证
│       ├── mm-valuation-analyst/  # 估值框架与目标价
│       ├── mm-chips-analyst/      # 筹码结构与博弈分析
│       ├── mm-catalyst-analyst/   # 催化剂与事件时间轴
│       ├── mm-discussion-panelist/ # 各角色评审团视角（立场 + 置信度 + 质询）
│       ├── mm-discussion-moderator/ # 评审团主席：逐轮汇总 + 综合判断
│       ├── mm-report-writer/      # 研究报告生成
│       ├── mm-report-reviewer/    # 多维质量打分
│       ├── mm-decision-panelist/  # 单角色决策投票（投票 + 置信度 + 对冲叠加）
│       ├── mm-decision-maker/     # 评审团主席：逐轮计票 + 最终 BUY/HOLD/SELL 决策
│       ├── mm-pdf-exporter/       # 图表生成与 Markdown -> HTML/CSS -> PDF
│       ├── mm-progress-monitor/   # 后台进度监控
│       ├── mm-memory-writer/      # 运行后记忆提取与存储
│       └── mm-init/               # 工作区初始化
├── plugin/
│   ├── .claude-plugin/            # 插件元数据
│   └── commands/                  # 面向用户的命令（/mm:init, /mm:run, /mm:status）
├── .mcp.json                      # Claude Code MCP 服务器注册
├── mcp/                           # MCP 服务器（market-data、mm-workspace、memory）
│   ├── market_data_server.py      # 含 get_fundamentals（DCF/可比公司输入）
│   ├── workspace_server.py
│   ├── memory_server.py
│   └── shared/
│       ├── contracts.py           # 单一事实来源（阶段、路径、命名）
│       ├── schemas.py
│       └── rate_limiter.py
├── valuation/                     # 公式驱动的估值引擎
│   ├── dcf.py                     # WACC、FCFF 预测、永续价值、敏感性矩阵
│   ├── comps.py                   # 可比公司倍数 + 分位数基准
│   ├── run_valuation.py           # 阶段运行器 -> valuation_summary.json
│   └── tests/                     # 单元测试（pytest）
├── memory/                        # 长期记忆存储（情景/语义/过程）
├── eval/                          # 评测流水线（评分器、运行日志、指标聚合）
│   ├── graders/                   # 事实性、证据覆盖、一致性、估值、深度、评审收敛、决策风险、成本评分器
│   ├── release_gate.py            # 确定性的通过/警告/失败裁定
│   ├── stage_timer.py             # 阶段开始/结束时间戳记录
│   ├── finalize_run.py            # 从所有产物汇总运行日志条目
│   └── metrics.py                 # 聚合仪表盘计算
├── logs/
│   └── run_log.jsonl              # 只追加的流水线运行历史（不提交）
├── scripts/                       # 确定性辅助脚本（已提交；临时的 scripts/quant_*.py 被忽略）
│   ├── run_codex_pipeline.py      # Codex 对等驱动（每个阶段一次 codex exec）
│   ├── check_data_sources.py      # 数据源预检诊断
│   └── prune_memory_to_keeplist.py # 将记忆存储裁剪到 ticker 保留清单（默认 dry-run；--apply 先备份）
├── templates/
│   ├── render_pdf.py              # Markdown -> HTML/CSS -> PDF 渲染器（WeasyPrint）
│   ├── report.css                 # JPM 风格样式表（封面、表格、CJK）
│   ├── report.html.j2             # 报告 HTML 模板（封面 + 评级框）
│   └── charts.py                  # 标注图表生成器（matplotlib，SVG）
├── workspaces/                    # 公司工作区（按日期归档）
│   ├── shared/market_context/     # 可复用的宏观数据
│   └── {TICKER}/                  # 单公司工作区
├── tests/                         # 测试脚本
├── config.example.yaml            # 配置模板
├── config.yaml                    # 本地配置（不提交，含 API key）
├── setup.sh                       # 环境初始化脚本
├── CLAUDE.md                      # 系统提示词与流水线文档
├── README.md
└── README_CN.md
```

### 工作区结构（按日期归档）

```text
workspaces/NVDA/
├── profile/                       # 静态公司资料（不按日期）
├── raw/{YYYY-MM-DD}/              # 每个交易日的原始数据（新闻、价格、基本面）
├── normalized/{YYYY-MM-DD}/       # 证据卡片
├── quant/{YYYY-MM-DD}/            # 技术指标结果
├── valuation/{YYYY-MM-DD}/        # DCF + 可比公司 + 安全边际
├── discussion/{YYYY-MM-DD}/       # 分析师 memo 与辩论记录
├── drafts/{YYYY-MM-DD}/           # 报告草稿
├── reviews/{YYYY-MM-DD}/          # 质量评分与修订要求
├── decision/{YYYY-MM-DD}/         # BUY/HOLD/SELL 决策结果
├── final/{YYYY-MM-DD}/            # 最终报告（Markdown + JSON）
├── exports/{YYYY-MM-DD}/pdf/      # JPM 风格 PDF 报告与图表
├── shared_context/{YYYY-MM-DD}.json   # 单次运行的共享上下文包（quant+估值+资料+同业+催化剂）
└── memory/{YYYY-MM-DD}_{role}.json    # 单次运行检索到的记忆上下文（analyst/writer/reviewer）
```

---

## 配置说明

所有运行行为都由 `config.yaml` 控制（从 `config.example.yaml` 复制而来）：

```yaml
# 关键配置片段
run_mode: daily              # daily | weekly
data_sources:
  news:
    provider: newsapi        # 无 API key 时回退到 web_search
    fallback: web_search
discussion:
  analyst_roles:
    - company_analyst
    - risk_analyst
    - market_analyst
    # - valuation_analyst    # 取消注释即可启用
  panel:                     # 多轮讨论评审团（立场 -> 收敛 -> 退出）
    enabled: true            # false -> memo 直接进入综合阶段
    min_rounds: 1
    max_rounds: 3            # 硬性上限
    convergence_threshold: 0.70
valuation:                   # 情景 DCF + 可比公司（阶段 6）
  enabled: true
  equity_risk_premium: 0.05
  default_terminal_growth: 0.025
  projection_years: 5
  scenario_growth_delta: 0.03   # 基准情景上下浮动得到乐观/悲观情景
review:
  min_overall_score: 8.0
  min_factuality: 9.0
```

---

## 数据源

| 数据源 | 是否需要 API Key | 用途 |
|--------|:-----------------:|------|
| yfinance | 否 | 股价、指数、同业、宏观资产以及基本面数据（DCF/可比公司输入） |
| NewsAPI | 可选 | 市场、行业与公司新闻（免费版） |
| SEC EDGAR | 否 | 10-K、10-Q、8-K 披露文件和内幕交易数据 |
| FRED | 可选 | 宏观序列：CPI（整体+核心）、联邦基金利率、美债收益率曲线（2/5/10/30 年）、广义美元指数、高收益债利差、VIX——无 key 时自动退化到 yfinance 代理（^TNX/^FVX/^TYX/^IRX/DX-Y.NYB/^VIX），此时 CPI、联邦基金利率与信用利差会记入 `inputs_missing` |
| NASDAQ | 否 | 美股新闻与报价，经 `api.nasdaq.com`（非官方），失败时回退到 nasdaq.com 页面——由 `mm-web-research` 采集 |
| WebSearch / WebFetch | 否 | 带来源溯源的网络新闻（`mm-web-research`）与核验，适用于任意市场 |

**数据源优先级：** 机构/MCP → NewsAPI → NASDAQ（美股）→ 通用网络搜索。NewsAPI 与 FRED 的 key 从环境变量 `NEWSAPI_KEY` / `FRED_API_KEY` 读取，market-data MCP 服务器会在启动时自动从仓库根目录的 `.env` 文件加载（shell export 优先级更高）。

---

## 发展路线图

**当前版本：dev 0.1**。核心流水线已经可用，JPM 风格 PDF 生成功能已打通。

### 已完成

- [x] **MCP 服务器架构**：3 个 MCP 服务器（market-data、mm-workspace、memory）为 Agent 提供结构化工具访问
- [x] **长期记忆系统**：跨运行的情景记忆、语义记忆和过程记忆三层架构
- [x] **自动化评测流水线**：代码评分器、运行日志和聚合指标，持续追踪报告质量
- [x] **量化估值引擎**：公式驱动的情景 DCF + 可比公司 + 安全边际，配套内部一致性审计评分器，以及**由各分项推导的置信度**（低置信度的 DCF 无法抬高混合结果），作为按置信度加权的参考输入决策
- [x] **机构级 PDF 渲染**：确定性的 Markdown → HTML/CSS → PDF（WeasyPrint）——首页评级框、内嵌标注 SVG 图表、带样式表格、页眉页脚，由统一提交的渲染器生成（无 LaTeX、无逐次运行脚本），并具备优雅降级
- [x] **双语输出**：通过 `language` 配置生成中英文报告与 PDF，CJK 在正文与图表中全链路正确渲染
- [x] **Web 展示层**：浏览器报告查看器（`/mm:dashboard`），提供文档、**幻灯片**（按章节自动拆分、键盘导航 + 导航圆点）与内嵌 PDF 三种模式，均由同一份报告内容驱动
- [x] **Codex CLI 支持**：`mm-*` 技能作为 Codex 原生技能运行（`.agents/skills/` 符号链接 + 每个技能的 `agents/openai.yaml` 调用策略与 MCP 依赖），以 `AGENTS.md` 作为控制入口，并提供可直接粘贴的 `~/.codex/config.toml` MCP 配置
- [x] **Codex 对齐驱动**：`scripts/run_codex_pipeline.py` 把每个 LLM 阶段作为独立的 `codex exec` 运行（每阶段全新上下文 —— 等价于 Claude 的 `context: fork`），确定性阶段用已提交的 Python、数据台/分析师并行执行，使 Codex 产出与 Claude 一致，而不会被压缩成残缺片段
- [x] **确定性质量底线**：深度闸（`eval/graders/depth_grader.py`）会标记过薄的分析师 memo、残缺的报告小节与过少的证据，并接入复审循环（重做）与发布闸，使「准确但过薄」的产出无法静默通过
- [x] **讨论评审团辩论循环**：讨论阶段运行多轮评审团——每位分析师角色提交结构化视角（立场 bullish/neutral/bearish + 置信度自评 + 对其他角色的质询），确定性收敛评分器（`eval/graders/discussion_convergence_grader.py`）以硬性轮次上限把关「继续 vs 退出」，主席综合出收敛后的投资观点并保留异议
- [x] **决策评审团辩论循环**：决策阶段运行多轮评审团——每位分析师角色投票（BUY/HOLD/SELL）并附置信度自评与对冲叠加，确定性收敛评分器（`eval/graders/panel_convergence_grader.py`）以硬性轮次上限把关「继续 vs 退出」，主席据按置信度加权的倾向写出最终结论并保留异议
- [x] **决策风险门**：一个咨询性、不修改决策的置信度上限（`eval/graders/decision_risk_grader.py`），依据可复现的信号——弱评审收敛、保留异议、被当作理由引用的低置信度估值、以及证据稀薄——给最终结论的置信度封顶，把过度自信暴露为发布闸警告，而不改写决策本身

### TODO

- [ ] **高级量化方法**：因子模型、滚动 beta/correlation、事件研究框架
- [ ] **投资组合模式**：多公司协同编排、行业级报告、组合层面风险视角
- [ ] **历史对比分析**：对比最新报告与历史报告，追踪投资论点演变
- [ ] **情绪分析**：社交媒体情绪、期权资金流、机构持仓倾向
- [ ] **实时仪表盘**：支持自动刷新和实时监控
- [x] **中国市场支持 (v1)**：AKShare 筹码/资金流信号（换手率/主力资金/龙虎榜/北向/融资/股东户数/解禁）、筹码结构引擎、故事与博弈层、CN 估值降级为参考
- [ ] **自动调度**：基于 Cron 的每日自动生成
- [ ] **自定义分析师人格**：可配置风险偏好、方向倾向和投资期限视角

---

## 许可证

[MIT License](LICENSE)

---

<p align="center">
  <sub>Built with Claude Code · Powered by Multi-Agent Debate</sub>
</p>
