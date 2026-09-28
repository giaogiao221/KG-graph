# 含能材料知识抽取流水线（v105 + v106 重构版）

面向**含能材料 / 火炸药 / 固体推进剂**领域，把 Markdown 格式的专业书籍自动抽取成
**59 列固定 Schema 的结构化事实表**（TSV），可直接用于构建知识图谱（Neo4j 等图数据库导入）。

本工程是从 `KGchouqu_clean` 当前运行版本重构而来的干净版：去除了备份目录、弃用代码、
独立训练栈（`training_gplinker` / `third_party/PaddleNLP`）等历史包袱，只保留可运行的抽取主线。

---

## 一、工作原理

整套流水线分两个阶段，规则与 LLM 互补：

```
Markdown 书籍
     │
     ▼
┌─────────────────────────────────────────────┐
│  v105  本地规则抽取（不调 LLM，保精度基线）      │
│  文档画像 → 表格流(网格/表头树/语义/条件)        │
│        → 文本流(切块/主体锚定/关系/工艺步骤)     │
│        → 双门控导出 schema59                   │
└─────────────────────────────────────────────┘
     │  产出：高精度事实 + 候选事实 + 被拒事实
     ▼
┌─────────────────────────────────────────────┐
│  v106  LLM 抽取（OpenAI 兼容接口，补召回）       │
│  ① 透传 v105 高精度事实                        │
│  ② LLM 审核 v105 候选事实（candidate_review）   │
│  ③ 对规则漏掉的段落直接抽取（direct_extraction） │
│  ④ 工艺流程图抽取与编译（process_graph）         │
│  → 合并去重 → schema59 校验                   │
└─────────────────────────────────────────────┘
     │
     ▼
59 列事实表（graph_import_ready.tsv …）
```

- **v105**：确定性规则引擎，由 `src/config/` 下的本体/策略配置驱动，零 LLM 调用，可离线跑。
- **v106**：调用 LLM（OpenAI 兼容协议，默认 DeepSeek / 阿里云 qwen3-max 均可），
  在 v105 基础上提升召回；带请求缓存，中断可续跑。

## 二、目录结构

```
demo/
├── start.py                  # 统一启动器：单元测试 → v105 → v106
├── setup_env.cmd             # 一键创建 .venv 并安装依赖
├── requirements.txt          # 精简依赖清单
├── requirements-lock.txt     # 精确锁定清单（pip freeze）
├── config.env.example        # v106 LLM 配置模板（复制为 config.env 填密钥）
├── config.env                # 实际 LLM 配置（已被 .gitignore 忽略，勿提交）
├── books/                    # 输入：Markdown 书籍目录（需由使用者放入，包内不含书籍）
├── src/
│   ├── book_engine/          # 抽取引擎（v105 规则 + v106 LLM 流水线）
│   │   ├── cli.py            #   v105 命令行入口
│   │   ├── pipeline/         #   v105 编排器（orchestrator：表格流+文本流→schema59）
│   │   ├── llm_v106/         #   v106 LLM 抽取（client/prompts/pipeline/process_graph/schema59）
│   │   ├── document/         #   Markdown 加载、标题重建、文本切块
│   │   ├── tables/           #   表格网格重建、表头树、语义规划、条件抽取
│   │   ├── text/             #   文本事实、叙述/泛化关系、工艺步骤抽取
│   │   ├── routing/          #   主体锚定、主体登记、类型调和
│   │   ├── gates/ quality/   #   门控与质量校验（精度守门）
│   │   ├── ontology/ normalize/  # 属性对齐本体、单位/数值归一化
│   │   └── export/           #   schema59 导出与各类审计报告
│   └── config/               # 本体/策略配置（yaml/tsv/json，59列契约）
├── scripts/                  # v106 运行与离线校验/修复脚本
│   ├── run_v106_qwen3_max.py             # v106 抽取入口
│   ├── check_semantic_precision_schema59_v2_phase95.py
│   ├── validate_v106_english_compat.py
│   ├── check_v106_qwen3_max_outputs.py
│   └── repair_*.py                       # 历史数据修复工具
├── tests/unit/               # 单元测试（pytest，自包含，313 项）
└── runs/                     # 输出：v105_generalized/ 与 v106_qwen3_max/
```

## 三、环境搭建

运行环境为 Python 3.11，依赖见 `requirements.txt`（精简）与
`requirements-lock.txt`（pip freeze 精确锁定，含传递依赖）。

**一键搭建（推荐）**——在 `demo` 目录执行，自动优先用 uv、无 uv 退回系统 python：

```console
setup_env.cmd
```

**手动搭建：**

```console
uv venv --python 3.11 --seed .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-lock.txt
```

无 uv 时：

```console
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
```

> 说明：v105/v106 抽取主线只用 pandas/numpy/bs4/lxml/pytest 等轻量依赖；
> torch/transformers/paddle 训练栈已剥离，不在本环境内。
> LLM 调用基于标准库 HTTP 实现，无需 openai SDK。

## 四、配置 LLM（仅 v106 需要）

v105 完全离线，无需任何配置。v106 需要一个 OpenAI 兼容接口：

1. 复制配置模板：`copy config.env.example config.env`
2. 填入三项（支持旧别名 `BASE_URL`/`API_KEY`/`MODEL`，进程环境变量优先于文件）：

```dotenv
OPENAI_BASE_URL=https://api.deepseek.com/v1      # 或阿里云 dashscope 兼容端点
OPENAI_API_KEY=你的密钥
OPENAI_MODEL=DeepSeek-V4-Flash-0731              # 或 qwen3-max 等
```

## 五、运行

在 `demo` 目录下：

```console
python start.py                  # 完整流程：单元测试 → v105 → v106（需配置 API）
python start.py --skip-v106      # 只跑 v105 本地规则抽取（无需 API）
python start.py --skip-tests     # 跳过启动前单元测试
python start.py --dry-run        # 预演：校验输入并打印命令，不执行、不调 API
python start.py --help           # 全部参数
```

常用组合：

```console
python start.py --skip-v106 --dry-run          # 无 API 时校验环境与缓存
python start.py --books-dir D:\books --v105-root D:\out\v105   # 自定义路径
python start.py --python C:\path\to\python.exe # 指定解释器
python start.py --books-dir D:\books --skip-v106 # 对任意书名的 Markdown 文件夹跑 v105
```

**行为说明：**
- 启动器自动优先使用项目 `.venv` 的 Python，无需手动激活。
- v105 对每本书四项产物齐全且校验通过时**命中缓存自动跳过**；v106 复用底层 LLM 请求缓存。
- 任一阶段失败即停止并保留现场，重新执行同一命令即可**续跑**。
- v106 默认处理 `--books-dir` 中的**全部** Markdown 书籍；仅在显式传入
  `--exclude-title "书名"` 时排除指定书籍。

## 六、旧资源补充约束层（默认启用、可配置）

本增强版将旧 `rawarrange` 资源转换为一个**非封闭、可审计的补充层**，不再把旧 JSON Schema
强行作为新抽取主线的硬约束。配置全部内置在 `src/config/`，无需外部旧工程目录：

| 配置 | 作用 | 默认行为 |
|---|---|---|
| `supplementary_property_aliases.tsv` | 属性别名 → 当前标准属性 | 仅精确命中时规范化 |
| `supplementary_relation_mapping.tsv` | 旧关系码/常见谓词 → 当前关系类型 | 仅精确命中时规范化 |
| `supplementary_subject_denylist.txt` | 过泛主语的提示词表 | 命中后转入候选复核，不删除 |
| `supplementary_subject_lexicon.tsv` | 历史实体词表 | 只记录审计命中，不要求实体必须在词表中 |
| `supplementary_constraint_policy.json` | 启停与策略 | 可关闭各个补充功能 |

v105 的审计日志写入每本书的
`step_graph_guard/supplementary_constraints_audit.jsonl`；v106 写入
`step_llm_v106/supplementary_constraints_audit.jsonl`。黑名单命中只会进入
`graph_*_candidate_review_59.tsv`，不会静默丢弃事实。

## 七、产物说明

每本书在 `runs/<版本>/<书名>/` 下生成约 18 个 `step_*` 审计目录，核心产物在
`step_graph_guard/`（均为 59 列锁定 TSV）：

| 文件 | 内容 | 下游用途 |
|---|---|---|
| `graph_import_ready.tsv` | **最终事实表**（v106 合并结果） | 图数据库导入 |
| `graph_import_ready_high_precision.tsv` | 高精度事实 | v106 原样透传 |
| `graph_import_ready_generalized.tsv` | 泛化事实 | — |
| `graph_generalized_candidate_review_59.tsv` | 候选事实 | v106 交 LLM 审核 |
| `graph_rejected_59.tsv` / `graph_generalized_rejected_59.tsv` | 被拒事实 | 人工复核 |
| `graph_process_flow_ready.tsv` | 工艺流程步骤链 | 工艺图谱 |
| `schema59_validation_report.json` | 59 列契约校验（列序哈希/行数） | 质量门禁 |

**59 列 schema**（列序有 SHA-256 契约锁，见 `src/config/legacy_schema59_contract.json`）覆盖：
`fact_id`、`主体名称/主体类型`、`事实类型`、`edge_verb`、`attribute_category/attribute_name`、
`尾实体/取值文本`、`数值/范围下限/范围上限/单位`、`条件文本/structured_condition_json`、
`process_id/step_id/step_index/step_action/…/previous_step_id/next_step_id`（工艺链）、
`component_name/component_amount_*`（配方组分）、`置信度`、`证据文本` 等。

**关系类型**（15 类）：属性、组成、分类、定义、用途、功能、影响、因果、比较、方法、步骤、
位置、连接、条件、命名/别名。

**单元测试：**

```console
.venv\Scripts\python.exe -m pytest tests\unit -q
```

当前版本在打包前会运行完整单元测试；以压缩包内的测试结果为准。

## 八、与原版的差异

- 配置/源码内 `parents[3]` 的 `model/` 层级定位统一改为 `parents[2]`（适配 `demo/src` 结构）。
- 剥离 `book_engine.training` 子包与 `training_gplinker`（独立 torch/transformers 训练栈，与抽取主线无关）。
- 剥离 `third_party/PaddleNLP`、`legacy_v245`、`_deprecated`、各 `_backup_before_*` 备份目录。
- 单元测试经 `tests/unit/conftest.py` 自包含定位 `src`，无需外部 `PYTHONPATH`。
- 新增 `setup_env.cmd`、`requirements.txt` / `requirements-lock.txt`、本 README。
- 新增旧资源的“属性别名、关系归一化、主语审查、词表审计”补充层，且输出逐条 JSONL 审计。
- 移除了 v106 对某一本固定书名的隐式排除；现在默认逐本处理输入目录的全部 Markdown 文件。

## 九、常见问题

- **只跑规则抽取、没有 API key**：用 `python start.py --skip-v106`，完全不联网。
- **v106 想换模型**：改 `config.env` 的 `OPENAI_MODEL`，或命令行加 `--model <名称>`。
- **目录名 v106_qwen3_max 与模型不符**：仅为沿用的默认命名，实际模型以 `config.env` 为准。
- **中途中断**：直接重跑同一命令，v105 命中缓存、v106 复用 LLM 请求缓存，自动续跑。
- **报错退出**：保留 `runs/` 与 `llm_cache_v106/`，修复后重跑即可，不会重复消耗已完成的 LLM 请求。
