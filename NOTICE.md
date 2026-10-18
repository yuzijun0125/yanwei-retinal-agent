# 第三方来源与许可

本项目代码目前未另行授予开源许可；公开仓库用于展示、审阅与技术交流。第三方模型、代码与数据仍适用各自条款。

## 预训练模型

- FLAIR 代码：<https://github.com/jusiro/FLAIR>，使用提交 d6652d53389ff49e5f73efaccf4246e9de88d1a3；模型来源 jusiro2/FLAIR，模型卡标记 Apache-2.0。
- FLAIR 论文：Silva-Rodríguez 等，*A Foundation Language-Image Model of the Retina (FLAIR): encoding expert knowledge in text supervision*，Medical Image Analysis 99 (2025), 103357。
- ClinicalBERT：emilyalsentzer/Bio_ClinicalBERT，模型卡标记 MIT；论文 *Publicly Available Clinical BERT Embeddings*，arXiv:1904.03323。

模型权重和依赖运行时不随 Git 仓库分发。项目集成这些预训练组件进行检索，没有宣称自主预训练基础模型。

## 数据与截图

IDRiD 来源：Porwal 等，*Indian Diabetic Retinopathy Image Dataset (IDRiD)*，Data 2018, 3(3), 25，DOI:10.3390/data3030025。现有项目来源记录标记 CC-BY-4.0，请遵守官方发布条款并保留引用。

随仓库提供的是公开数据派生的检索表、规则标签、合成文本及演示查询。派生标签为 PENDING，合成配对不是临床报告；截图中的 IDRiD 图片用于展示已有工程结果。原图、掩膜及参考论文全文未在本仓库重新打包。

## 工具与平台

Nexent：<https://atomgit.com/ModelEngine/nexent>。项目通过 MCP 接入平台；Nexent 的代码、商标和权利归其相应权利人。Python、Pillow、PyTorch 及其他依赖按各自原有许可使用。
