# 数据与模型准备

## 文字演示

安装 Python 3.12 和根目录 `requirements-demo.txt`，运行 `python 02_代码/run_demo.py --port 8765`。HTTP、BM25 和规则编排使用标准库；Pillow 用于图像读取和部分回归测试。

派生检索表已随仓库提供；没有原图时只能演示文字、规则及来源信息，网页不会生成缺失的医学图片。

## 图文环境

请使用独立虚拟环境。`02_代码/requirements-multimodal.lock.txt` 记录现有环境的精确版本，其中 FLAIR 代码固定到 Git 提交，Torch 使用 CPU 构建。

```bash
python -m venv .venv-multimodal
# Windows
.venv-multimodal\Scripts\Activate.ps1
python -m pip install --extra-index-url https://download.pytorch.org/whl/cpu -r 02_代码/requirements-multimodal.lock.txt
python 02_代码/tools/download_flair.py
```

准备脚本固定模型修订：

| 模型 | 来源 | 修订 |
|---|---|---|
| FLAIR | jusiro2/FLAIR | 5f6bdd0a068353dc41a896ba3abdd7c0f6d35938 |
| ClinicalBERT | emilyalsentzer/Bio_ClinicalBERT | d5892b39a4adaed74b92212a44081509db72f87b |

安装和权重准备需要联网；依赖可获得性、下载速度与平台支持需按实际环境检查。本仓库没有声称已在所有平台进行全新联网安装验收。

## 原图和索引

从 IDRiD 官方发布渠道按其条款获取原图。论文出处：Porwal 等，*Indian Diabetic Retinopathy Image Dataset (IDRiD)*，Data 2018, 3(3), 25，DOI:10.3390/data3030025。

当前检索表对应原始分割子集的 81 张原图，路径结构为：

```text
01_数据集/图像/IDRiD/原始图像/
  Training set/IDRiD_01.jpg ...
  Testing set/IDRiD_55.jpg ...
```

以 `01_数据集/silver/silver_image.csv` 中的文件名、划分和 SHA256 为准。旧表路径前缀 `01_数据集本体` 会由代码映射到当前数据目录；不要任意把其他图片重命名成这些病例。

```bash
python 02_代码/tools/build_multimodal_index.py
python 02_代码/run_demo.py --port 8765
```

索引输出到 `04_产物/multimodal`。检查 `/api/multimodal`：需确认 `ready=true`、`model_loaded=true`、索引图片数符合准备范围。若有失败文件，先核对索引 manifest，不把部分成功当作全部准备完成。

## 本地数据

上传文件、对话、反馈、证据与轨迹保存在 `04_产物`，不进入 Git。公开示例来源为 IDRiD；仓库不含私人患者信息、生产账号或 API Key。使用自己的图片前请自行完成授权与去标识处理。
