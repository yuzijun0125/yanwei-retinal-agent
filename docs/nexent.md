# Nexent实际接入

最新产品链路为：专属网页→访客网关→Nexent发布版→MCP→真实OCR/检索工具→请求关联结果。完整配置步骤见根目录《团队部署与维护.md》及README。

`integration/bootstrap.py`是可复现的六工具注册和智能体发布配置；模型ID由管理员选择，MCP/Agent/发布版本ID在当地环境生成，运行状态不随仓库复制。团队完成授权后，访客无需Nexent登录。

六个工具为extract_medical_entities、search_cases、verify_medical_logic、build_evidence_chain、record_feedback、read_medical_record。用户确认OCR文字前不执行该次检索。真实工具结果与当前request_id关联，不复用旧轮结果冒充完成。

管理员端默认本机18880，访客18881；管理接口不向访客透传。敏感授权仅在服务端，仓库没有实际模型Key。直接8765/chat属于本地后端调试，不应当作Nexent链路验收。

本机工程验收报告中的Agent #6/发布版2只是原开发环境的记录，不是其他机器应照抄的配置。
