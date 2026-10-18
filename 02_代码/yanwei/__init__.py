"""眼维 · 决策智能体 Demo（零依赖纯 Python 实现）。

模块地图：
  miniyaml  配置解析（零依赖 YAML 子集）
  config    配置装载 —— thresholds.yaml 是决策阈值的唯一真源
  texttoken 中文分词与术语归一
  bm25      BM25 稀疏检索（零训练基线）
  dataio    读 silver/gold 数据，构建内存索引
  imageqc   FundusPreprocess：眼底图像质量评估（可判读/需降级/需重拍）
  pnglite   纯 Python PNG 读写（生成演示图 + 读图，零依赖）
  trace     编排轨迹记录与回放
  mcp_tools 5 个 MCP 工具实现
  pipeline  轻编排器：状态机 + 三道保险 + 决策闸门
  demo_cli  命令行跑通全链路
  server    本地 HTTP 服务 + 单页主界面
"""

__version__ = "1.0.0"
