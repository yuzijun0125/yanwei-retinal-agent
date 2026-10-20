# 02_代码 · 代码工作区

> 2026-10-18：新增真实图文检索。请使用 `.venv-multimodal/Scripts/python.exe` 或项目根目录 `启动眼维.cmd` 启动；原有纯文字路径仍可降级运行。模型准备使用 `tools/download_flair.py`，原图离线建库使用 `tools/build_multimodal_index.py`。详细验收与边界见项目根目录《图文检索接入与验收记录_20261018.md》。下面的“零依赖”说明针对最初的文字基线，不适用于图文模型环境。

> ✅ **这里是唯一需要改代码的地方。**
> 状态（2026-10-15）：**第一版已跑通** —— 入口 / 工具 / 编排器 / 界面 / 评测 / Nexent 接入全部就位。

---

## 三分钟跑起来

```bash
cd 02_代码
python run_demo.py        # 起服务 + 自动开浏览器（Python 3.10+，零第三方依赖）
```

其他入口：

```bash
python demo_cli.py                 # 命令行版：四个决策动作一次跑完
python demo_cli.py "你的描述"       # 跑自己的描述
python demo_cli.py --replay <id>   # 回放一条留痕
python mcp_server.py               # MCP 服务端（给 Nexent 接入用，走 stdio）
```

---

## 现状速览

| 部分 | 状态 | 说明 |
|---|---|---|
| `config/` 三张表 | ✅ | `thresholds.yaml` 是决策阈值唯一真源（当前 v1.1.0） |
| `yanwei/` 内核 9 模块 | ✅ | 配置 / 分词 / 检索 / 数据 / 质检 / PNG / 轨迹 / 评测 |
| `yanwei/` 工具与编排 | ✅ | `mcp_tools.py`（5 工具）+ `pipeline.py`（状态机+保险）+ `server.py` |
| 入口脚本 | ✅ | `run_demo.py` / `demo_cli.py` / `mcp_server.py` |
| `web/index.html` | ✅ | 单页五面板，零 CDN、可离线 |
| `tests/` 回归与冒烟 | ✅ | 4 条 golden trace + 工具层冒烟 + HTTP 冒烟 |
| `tools/` 数据工具 | ✅ | 体检、修复、溯源、渲染、统计、**坏图测试床**、**评测**、**发布打包** |

---

## 文件地图

```
02_代码/
├── README.md
├── run_demo.py              ★ 唯一入口：起服务 + 打开浏览器
├── demo_cli.py              命令行版（无浏览器也能跑完一次完整决策）
├── mcp_server.py            MCP 服务端入口（stdio）→ 给 Nexent 指这个路径
│
├── config/                  ⚙️ 改行为，先来这里，不先改代码
│   ├── thresholds.yaml      ★ 决策阈值唯一真源（充分性 0.8/0.5、权重、三道保险）
│   ├── rules_medical.yaml   五维规则 + 术语表 + 冲突策略 + 转诊规则（草案，未临床复核）
│   └── model_dispatch.yaml  模型分派表（换模型只改这张表）
│
├── yanwei/                  内核包（零依赖）
│   ├── __init__.py          包入口
│   ├── miniyaml.py          零依赖 YAML 解析
│   ├── config.py            配置装载（阈值唯一真源在这里被读）
│   ├── texttoken.py         中文分词 + 术语归一
│   ├── bm25.py              BM25 稀疏检索（零训练基线）
│   ├── dataio.py            读 silver/gold 数据、建索引
│   ├── pnglite.py           纯 Python PNG 读写 + 合成演示图 + 退化算子
│   ├── imageqc.py           FundusPreprocess 图像质检（三档 + 遮挡/过曝判据）
│   ├── trace.py             编排轨迹记录与回放
│   ├── mcp_tools.py         ★ 五个工具的实现（契约见 03_文档/接口契约.md）
│   ├── pipeline.py          ★ 轻编排器：9 段状态机 + 三道保险
│   ├── mcp_stdio.py         MCP 服务端（stdio 传输）
│   ├── server.py            零依赖 HTTP 服务（REST + MCP over HTTP + 静态页）
│   └── evaluate.py          双口径评测 + 信息论天花板
│
├── web/
│   └── index.html           单页主界面（一页五面板，离线可用）
│
├── tools/                   🔧 工具脚本（零依赖，直接跑）
│   ├── dataset_health.py            数据体检：8 维度，改数据前后各跑一次
│   ├── fix_dataset.py               数据修复：默认 dry-run，--apply 才写入
│   ├── add_provenance.py            溯源锚定重构（默认 dry-run）
│   ├── render_provenance_registry.py 渲染《字段来源分级表》+ 漏登核对
│   ├── import_official.py           官方 CSV 导入器（含 --self-test）
│   ├── provenance_stats.py          字段来源等级统计
│   ├── make_bad_images.py           ★ 坏图测试床：生成 + 自检
│   ├── make_release.py              ★ 发布打包器：把工程打成可分发的 zip
│   └── evaluate.py                  ★ 评测入口（双口径）
│
└── tests/                   回归与冒烟
    ├── run_golden.py        回归跑分器：任何改动先跑它（退出码 0 = 全过）
    ├── smoke_tools.py       工具层边界冒烟：9 条刁钻输入（否定/不确定/空/无关…）
    ├── smoke_http.py        HTTP 冒烟：REST + MCP-over-HTTP + 错误兜底 + 幂等
    ├── golden_traces/golden.json   4 条固定输入 → 期望动作
    └── fixtures/bad_images/        坏图测试床生成物（PNG + manifest）
```

---

## 常用命令速查

```bash
# 回归门禁（改动后必跑）
python tests/run_golden.py            # 应「全部通过 ✓（4/4）」
python tests/smoke_tools.py           # 工具层边界，应「9/9」
python tests/smoke_http.py            # HTTP 端到端，应「18/18」

# 演示
python run_demo.py                    # 界面
python demo_cli.py                    # 命令行，四个动作

# 评测
python tools/evaluate.py              # 双口径 + 天花板

# 测试床
python tools/make_bad_images.py       # 生成并自检 7 张退化图

# 数据工具
python tools/dataset_health.py        # 体检
python tools/fix_dataset.py           # 修复预览（dry-run）
```

---

## 改东西的优先级（别搞反）

```
想改"什么时候拒答"      → 改 config/thresholds.yaml          （不改代码）
想改"什么算冲突"        → 改 config/rules_medical.yaml       （不改代码）
想换检索模型            → 改 config/model_dispatch.yaml      （改一处实现）
想改"防跑飞上限"        → 改 config/thresholds.yaml 的 guards 段
想加一个新判定维度      → 才需要动 yanwei/ 里的代码
```

**能改配置解决的，不要改代码。** 这是这个工程能维护下去的前提。

---

## 环境要求

- **Python 3.10 及以上**
- **运行零第三方依赖**（纯标准库）

> **关于读图**：项目原图是 **JPG**（IDRiD，4288×2848）。纯 Python 解不了 JPG，
> 所以 `imageqc` 的 JPG 分支需要**可选**安装 Pillow（`pip install Pillow`）；
> 未装时该分支自动跳过并如实返回 `UNASSESSED`（不假装成功）。
> **PNG 全功能、零依赖**，合成演示图与坏图测试床都走 PNG，跑通链路不需要装任何东西。

---

## 给队友的话

1. **先看文档再动手**：接口在 `03_文档/接口契约.md`，改之前先确认没破坏接口；
2. **改完必须跑回归**：`python tests/run_golden.py`，跑不过就是改坏了；
3. **别直接改 `main`**，开自己的分支，见 `03_文档/眼维_团队协作与分支操作卡_v1.md`。
