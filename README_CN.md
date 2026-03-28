<p align="center">
  <h1 align="center">MarketMind-AlphaEngine</h1>
  <p align="center">
    <strong>多智能体股票研究系统与投行风格报告引擎</strong>
  </p>
  <p align="center">
    自动化日报与周报分析 · 多分析师辩论 · BUY/HOLD/SELL 决策 · JPM 风格 PDF 报告
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

- **多智能体辩论**：3-6 位分析师分别独立分析同一只股票，再由主持者识别分歧、指定交叉质询对，通过结构化辩论形成更平衡的投资观点，而不是单一 Agent 的总结
- **投行风格 PDF**：通过 LaTeX 生成 JPM 风格研究报告，包含带注释图表、叙事段落、清晰的信息层次和专业排版
- **选择性辩论**：由主持者定向分配交叉评审对，相比所有分析师两两互评的全量 N×N 辩论，在分析师人数增加时可节省 50-90% 的 token 消耗
- **日期归档历史**：每次运行都按 `{YYYY-MM-DD}/` 目录保留结果，因此可以对同一家公司进行连续日度分析而不丢失历史研究记录
- **智能交易日逻辑**：自动识别正确的数据截面，处理盘前、周末和节假日等情况
- **MCP 服务器架构**：3 个 Model Context Protocol 服务器（market-data、workspace、memory）为 Agent 提供结构化的数据、文件和持久记忆工具访问
- **长期记忆系统**：情景记忆、语义记忆和过程记忆三层架构，使系统能跨运行回顾历史分析、学习到的模式和优化后的流程
- **自动化评测流水线**：代码评分器对每次运行进行多维度打分，配合运行日志和聚合指标持续追踪报告质量
- **免费数据源优先**：完全支持免费 API（yfinance、NewsAPI 免费版、SEC EDGAR、FRED），没有 API key 时可自动退化到 WebSearch

---

## 快速开始

### 前置依赖

- 已安装 [Claude Code](https://code.claude.com) CLI
- 已安装 [Ralph Loop plugin](https://github.com/anthropics/claude-code)（推荐，用于长时间连续执行）
- Python 3.10 及以上
- 用于生成 PDF 的 LaTeX（`xelatex`）。macOS 可执行：`brew install --cask mactex`

### 安装与启动

```bash
# 1. 克隆仓库
git clone git@github.com:ShinyGua/MarketMind-AlphaEngine.git
cd MarketMind-AlphaEngine

# 2. 创建 Python 环境
source setup.sh

# 3. （可选）配置 API key
cp config.example.yaml config.yaml
# 编辑 config.yaml 中的数据源配置
# API 密钥通常通过环境变量提供

# 4. （可选）在启动前导出 API key
export NEWSAPI_API_KEY="your_newsapi_key"
export FRED_API_KEY="your_fred_key"
# 即使不设置也可以运行，系统会自动回退到 WebSearch

# 5. 启动 Claude Code 并加载插件
claude --plugin-dir "$PWD/plugin" --dangerously-skip-permissions
```

### NewsAPI 申请与填写位置

如果你希望新闻采集质量高于默认的 WebSearch 回退方式，建议先申请 NewsAPI key：

1. 打开 [newsapi.org/register](https://newsapi.org/register) 注册账号
2. 完成邮箱验证后，在 NewsAPI 控制台或官方示例中复制你的 API key
3. 在终端里把它设置为环境变量 `NEWSAPI_API_KEY`

本项目读取的键名定义在 [`config.example.yaml`](/Volumes/970SSD/Code/Git/MarketMind-AlphaEngine/config.example.yaml) 这里：

```yaml
data_sources:
  news:
    api_key_env: NEWSAPI_API_KEY
```

也就是说，密钥应填写到你的 shell 环境变量里，而不是直接硬编码进 `config.yaml`。示例：

```bash
export NEWSAPI_API_KEY="your_newsapi_key"
```

### 使用方法

```text
/mm:init                          # 创建新的公司工作区（交互式）
/mm:run workspaces/NVDA           # 运行完整的 14 阶段研究流水线
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
|                                                                      |
|  已有公司 workspace                                                  |
|       |                                                              |
|       v                                                              |
|  解析配置并校验当前状态                                              |
|       |                                                              |
|       v                                                              |
|  初始化工作区上下文                                                  |
|       |                                                              |
|       v                                                              |
|  Collect（3 个 desk 并行）                                           |
|       |-- Market desk: 宏观新闻、指数、宏观资产                      |
|       |-- Company desk: 公司新闻、监管披露、催化事件                 |
|       |-- Sector desk: 行业新闻、可比公司价格数据                    |
|       |                                                              |
|       v                                                              |
|  Normalize：整理证据卡片与时间序列表                                 |
|       |                                                              |
|       v                                                              |
|  Quant 快照                                                          |
|       | RSI、MACD、SMA、ATR、相对强弱                                |
|       v                                                              |
|  Discussion 辩论循环                                                 |
|       |-- 分析师独立 memo（并行）                                    |
|       |-- 主持者选择交叉质询配对                                     |
|       |-- 选择性辩论                                                 |
|       |-- 观点综合与 thesis map                                      |
|       |                                                              |
|       v                                                              |
|  生成机构风格研究报告草稿                                            |
|       |                                                              |
|       v                                                              |
|  Review 循环                                                         |
|       | 通过 ------------------------------+                         |
|       | 不通过 -> 定向修订 -> draft        |                         |
|       v                          （最多循环次数由 config 控制）      |
|  投资决策                                                             |
|       | BUY / HOLD / SELL + 置信度 + 风险项                          |
|       v                                                              |
|  导出 markdown + JSON + 图表 + LaTeX PDF                             |
|       |                                                              |
|       v                                                              |
|  用户反馈（唯一暂停等待输入的阶段）                                  |
|       | 收集用户对报告的反馈意见                                      |
|       v                                                              |
|  Reflect 反思                                                        |
|       | 评估运行质量、更新长期记忆、                                  |
|       | 记录指标用于持续改进                                          |
|       v                                                              |
|  最终按日期落盘到 workspaces/{TICKER}/final/{YYYY-MM-DD}/            |
|                                                                      |
+----------------------------------------------------------------------+
```

### 阶段说明

| 阶段 | 发生了什么 | 负责角色 |
|------|------------|----------|
| **Collect** | 从 yfinance、NewsAPI、EDGAR 收集宏观、公司与行业数据 | 3 个 desk 并行 |
| **Quant** | 用 Python 计算 RSI、MACD、SMA、ATR、相对强弱等指标 | `mm-quant-analyst` |
| **Debate** | 分析师独立写 memo，主持者分配交叉质询对，并组织定向辩论 | 3-6 位分析师 |
| **Draft** | 生成带证据追踪的 JPM 风格叙事研究报告 | `mm-report-writer` |
| **Review** | 进行多维度打分并驱动迭代修订 | `mm-report-reviewer` |
| **Decide** | 输出带置信度、理由和风险项的 BUY/HOLD/SELL 决策 | `mm-decision-maker` |
| **Export** | 生成标注图表并通过 LaTeX 导出 JPM 风格 PDF 报告 | `mm-pdf-exporter` |
| **User Review** | 暂停收集用户反馈 — 是否认同、修正意见、个人洞察 | 用户（human-in-the-loop） |
| **Reflect** | 通过代码评分器评估运行质量、存储用户反馈和长期记忆 | eval 流水线 + memory |

### 分析师角色（可配置）

| # | 角色 | 关注重点 | 默认启用 |
|---|------|----------|:-------:|
| 1 | `company_analyst` | 基本面、公司事件、关键催化剂 | ✓ |
| 2 | `risk_analyst` | 空头逻辑、下行风险、失效条件 | ✓ |
| 3 | `market_analyst` | 宏观环境、板块定位、alpha 与 beta | ✓ |
| 4 | `valuation_analyst` | P/E、DCF、估值框架、目标价 | |
| 5 | `technical_analyst` | 图形形态、动量指标、交易信号 | |
| 6 | `catalyst_analyst` | 事件时点、财报日历、短期催化 | |

如需启用更多分析师，可在 `config.yaml` 中取消对应注释。

---

## 项目结构

```text
MarketMind-AlphaEngine/
├── .claude/
│   ├── agents/                    # 模型档位定义（heavy/standard/light）
│   └── skills/                    # 20 个 agent skill
│       ├── mm-orchestrator/       # 流水线总控（铁律：不能停）
│       ├── mm-company-resolver/   # 股票代码 -> 公司画像 + 同业
│       ├── mm-market-desk/        # 宏观数据采集
│       ├── mm-company-desk/       # 公司新闻与披露文件采集
│       ├── mm-sector-desk/        # 行业与同业数据采集
│       ├── mm-quant-analyst/      # 技术指标计算
│       ├── mm-market-analyst/     # 市场环境分析
│       ├── mm-company-analyst/    # 公司基本面分析
│       ├── mm-risk-analyst/       # 风险识别与反方论证
│       ├── mm-valuation-analyst/  # 估值框架与目标价
│       ├── mm-technical-analyst/  # 图表解读与信号分析
│       ├── mm-catalyst-analyst/   # 催化剂与事件时间轴
│       ├── mm-discussion-moderator/ # 分歧扫描与综合判断
│       ├── mm-report-writer/      # 研究报告生成
│       ├── mm-report-reviewer/    # 多维质量打分
│       ├── mm-decision-maker/     # BUY/HOLD/SELL 决策输出
│       ├── mm-pdf-exporter/       # 图表生成与 LaTeX -> PDF
│       ├── mm-progress-monitor/   # 后台进度监控
│       ├── mm-memory-writer/      # 运行后记忆提取与存储
│       └── mm-init/               # 工作区初始化
├── plugin/
│   ├── .claude-plugin/            # 插件元数据
│   └── commands/                  # 面向用户的命令（/mm:init, /mm:run, /mm:status）
├── .mcp.json                      # Claude Code MCP 服务器注册
├── mcp/                           # MCP 服务器（market-data、workspace、memory）
│   ├── market_data_server.py
│   ├── workspace_server.py
│   ├── memory_server.py
│   └── shared/
│       ├── contracts.py           # 单一事实来源（阶段、路径、命名）
│       ├── schemas.py
│       └── rate_limiter.py
├── memory/                        # 长期记忆存储（情景/语义/过程）
├── eval/                          # 评测流水线（评分器、运行日志、指标聚合）
│   ├── graders/                   # 事实性、证据覆盖、一致性、成本评分器
│   ├── release_gate.py            # 确定性的通过/警告/失败裁定
│   ├── stage_timer.py             # 阶段开始/结束时间戳记录
│   ├── finalize_run.py            # 从所有产物汇总运行日志条目
│   ├── metrics.py                 # 聚合仪表盘计算
│   └── run_log.jsonl              # 只追加的流水线运行历史
├── templates/
│   ├── equity_research.cls        # JPM 风格 LaTeX 文档类
│   └── charts.py                  # 标注图表生成器（matplotlib）
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
├── raw/{YYYY-MM-DD}/              # 每个交易日的原始数据
├── normalized/{YYYY-MM-DD}/       # 证据卡片
├── quant/{YYYY-MM-DD}/            # 技术指标结果
├── discussion/{YYYY-MM-DD}/       # 分析师 memo 与辩论记录
├── drafts/{YYYY-MM-DD}/           # 报告草稿
├── reviews/{YYYY-MM-DD}/          # 质量评分与修订要求
├── decision/{YYYY-MM-DD}/         # BUY/HOLD/SELL 决策结果
├── final/{YYYY-MM-DD}/            # 最终报告（Markdown + JSON）
└── exports/{YYYY-MM-DD}/pdf/      # JPM 风格 PDF 报告与图表
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
  debate_mode: selective     # selective（主持者配对）| full（N×N 全量辩论）
  analyst_roles:
    - company_analyst
    - risk_analyst
    - market_analyst
    # - valuation_analyst    # 取消注释即可启用
review:
  min_overall_score: 8.0
  min_factuality: 9.0
```

---

## 数据源

| 数据源 | 是否需要 API Key | 用途 |
|--------|:-----------------:|------|
| yfinance | 否 | 股价、指数、同业和宏观资产数据 |
| NewsAPI | 可选 | 市场、行业与公司新闻（免费版） |
| SEC EDGAR | 否 | 10-K、10-Q、8-K 披露文件和内幕交易数据 |
| FRED | 可选 | 美国 10 年期国债、美元、VIX 等宏观指标 |
| WebSearch | 否 | 当 API key 不可用时，用于新闻和网页核验回退 |

---

## 发展路线图

**当前版本：dev 0.1**。核心流水线已经可用，JPM 风格 PDF 生成功能已打通。

### 已完成

- [x] **MCP 服务器架构**：3 个 MCP 服务器（market-data、workspace、memory）为 Agent 提供结构化工具访问
- [x] **长期记忆系统**：跨运行的情景记忆、语义记忆和过程记忆三层架构
- [x] **自动化评测流水线**：代码评分器、运行日志和聚合指标，持续追踪报告质量

### TODO

- [ ] **Web 展示层**：与 PDF 内容一致的交互式 HTML 幻灯片（reveal.js 或自定义方案）
- [ ] **高级量化方法**：因子模型、滚动 beta/correlation、事件研究框架
- [ ] **投资组合模式**：多公司协同编排、行业级报告、组合层面风险视角
- [ ] **历史对比分析**：对比最新报告与历史报告，追踪投资论点演变
- [ ] **情绪分析**：社交媒体情绪、期权资金流、机构持仓倾向
- [ ] **实时仪表盘**：支持自动刷新和实时监控
- [ ] **中国市场支持**：接入 Tushare、AKShare 等本地数据源，覆盖 A 股
- [ ] **报告翻译**：通过配置切换中英双语输出
- [ ] **自动调度**：基于 Cron 的每日自动生成
- [ ] **自定义分析师人格**：可配置风险偏好、方向倾向和投资期限视角

---

## 许可证

[MIT License](LICENSE)

---

<p align="center">
  <sub>Built with Claude Code · Powered by Multi-Agent Debate</sub>
</p>
