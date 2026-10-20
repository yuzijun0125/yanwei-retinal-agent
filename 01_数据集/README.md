# 01_数据集 · 数据底座（只读）

> ❌ **不要手工改这里的 CSV。**
> 数据要改，回数据集仓库（`D:\WorkBuddyFiles\眼维数据集\`）改，改完重新导出到这里。
> 手工改单个 CSV 会让三层契约（Bronze/Silver/Gold）失效。

---

## 三层契约（来自数据字典 v2.0）

| 层 | 特性 | 谁消费 |
|---|---|---|
| Bronze（原始件） | 只追加，永不修改，SHA256 可验 | 只有数据构建脚本 |
| **Silver** | 清洗对齐后的病例级资产，按主键 upsert | 本 Demo 直接消费 |
| **Gold** | 评测与决策专用，全量重建，带冻结版本号 | 本 Demo 直接消费 |

**铁律**：Gold 绝不直接读 Bronze；评测集一旦冻结（`frozen_at` 非空），该分区禁止任何写入。

---

## 这里有什么

### silver/（病例层，6 张非空表）

| 表 | 行数 | Demo 怎么用 |
|---|---:|---|
| `silver_case.csv` | 81 | 提供眼别、疾病标签 |
| `silver_image.csv` | 81 | 提供模态、图像质量标记、原始路径 |
| `silver_region.csv` | 282 | **检索单元**（病灶区域） |
| `silver_lesion.csv` | 282 | 提供病灶程度（severity） |
| `silver_text.csv` | 691 | 合成描述文本 |
| `silver_pair_synthetic.csv` | 363 | 合成图文对（Demo 不用，仅训练用） |

### gold/（评测层）

| 表 | 行数 | Demo 怎么用 |
|---|---:|---|
| `gold_retrieval_query.csv` | 363 | 检索任务：282 条 T2I（文本→区域）+ 81 条 I2T |
| `gold_relevance.csv` | 1090 | 相关性真值：每条查询恰有 EXACT / SIMILAR / DISSIMILAR 各一条 |

### ⚠️ 空表（如实标注，不伪造）

| 表 | 行数 | 说明 |
|---|---:|---|
| `silver_grading.csv` | 0 | 无权威 ICDR 分级真值 → 转诊规则只能先用占位条款 |
| `silver_pair_real.csv` | 0 | 无真实图文配对 → 评测只能用合成配对 |

---

## 用数据前必须知道的三个坑

### 坑 1：`gold` 全部是 `DEMO_ONLY`

迁移报告原文：**"gold_retrieval_query 全部 metric_validity=DEMO_ONLY：合成查询不得用于正式指标"**。

👉 含义：Demo 跑出来的 HR@1 / MRR **只能证明"链路能跑通"，不能对外当项目指标**。
👉 依据：数据字典 §5 的 `MT-01` / `MT-02` 规则，这是**防指标循环**的硬门禁。

### 坑 2：原始图像不在本地

`silver_image.raw_path` 指向 `01_数据集本体/图像/IDRiD/原始图像/...`，**该目录在 D 盘和 E 盘都不存在**。

👉 含义：图像质检链路暂时只能用**合成的演示图**跑通。真图到位后零改动切换。

### 坑 3：相关性判定全是 `AUTO_RULE` / `DRAFT`

1090 条相关性判定全部由规则生成（`judgment_source=AUTO_RULE`），复核状态 `DRAFT`。

👉 含义：这是"规则造出来的答案"，不是医生判的。**每一处使用都要标注 DEMO_ONLY。**

---

## 相关文档

- `数据字典_v2.0.md` —— 数据契约，每个字段的类型、可空性、值域、强制质量规则。**改代码前必读**。
- `医学标注规范_v2.0.md` —— 标注口径。
- `_quality/` —— 迁移报告与契约校验报告。
