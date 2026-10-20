# 数据、模型与索引准备

## 源码与完整部署包

Git仓库只包含最新可迭代源码、派生表、配置、部署脚本、工程样例和说明。大体积权重、原图、索引和个人运行状态被排除，因此克隆后不能直接宣称完整图文服务就绪。

最快方式：由组长通过团队允许的文件渠道提供 `眼维_统一托管版_20261020.zip`，完整解压，再将以下三个目录复制到本仓库相同位置：

| 从完整包复制 | 用途 |
|---|---|
| `02_代码/models/` | 固定模型权重及manifest |
| `01_数据集/图像/` | 对应派生表的IDRiD图像与必要标注 |
| `04_产物/multimodal/` | 离线图像向量及manifest |

不要从旧运行环境复制 `.env`、`本机首次登录信息.txt`、`hosting.secret.json`、agent.json、访客数据库、上传文件或浏览器登录状态。不要用完整包的旧代码覆盖更新后的Git代码。资源不匹配时重建索引，不能静默混用。

## 从原始来源准备

从IDRiD官方渠道按其条款获取原图，核对`01_数据集/silver/silver_image.csv`中的文件与hash。原图路径示例：

```text
01_数据集/图像/IDRiD/原始图像/Training set/IDRiD_01.jpg
01_数据集/图像/IDRiD/原始图像/Testing set/IDRiD_55.jpg
```

模型来源与固定修订见[model-versions.json](model-versions.json)：FLAIR `5f6bdd0a068353dc41a896ba3abdd7c0f6d35938`，ClinicalBERT `d5892b39a4adaed74b92212a44081509db72f87b`。准备脚本需要huggingface-hub，下载需要联网。

```bash
python 02_代码/tools/download_flair.py
```

完整运行优先使用Dockerfile.retrieval与deploy/requirements-linux.txt的Linux CPU环境（PyTorch 2.6.0 CPU），FLAIR源码已随integration/vendor提供并保留许可。`requirements-multimodal.lock.txt`是原开发机的历史环境快照，不作为全平台安装保证。

没有预建索引时，在准备好依赖、权重和图像的检索环境运行：

```bash
python 02_代码/tools/build_multimodal_index.py
```

也可以在完整环境启动后，使用其Docker Compose相同项目与env参数，在yanwei-retrieval容器中执行 `/app/02_代码/tools/build_multimodal_index.py`；索引写入共享04_产物目录。检查manifest的图片数、failures、模型版本和实际图文查询是否used=true。不要仅凭HTTP200或ready字段认定模型已执行。

## 轻量工程验证

Python 3.11/3.12安装根目录requirements-demo.txt即可运行文字基线和不依赖真实模型的工程检查。缺原图不显示候选影像，缺模型不假装图文成功。

## 平台与授权

回到根目录运行START.cmd或sh start.sh，由本机管理员完成Nexent账号和模型服务首次设置。团队共用站点时仅团队配置；各自本地开发则需独立授权配置。配置和运行数据不进入Git。
