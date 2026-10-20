# 团队协作

完整分工见[三人团队与AI协作方案](docs/眼维_三人团队分工与AI协作执行方案_20261020.md)，技术路线见[研究与算法方案](docs/眼维_研究定位与技术算法优化实施方案_20261020.md)。文档中的后续算法和图谱属于计划，不是已实现功能。

## 获取项目

```bash
git clone https://github.com/yuzijun0125/yanwei-retinal-agent.git
cd yanwei-retinal-agent
```

组长在GitHub仓库Settings → Collaborators中邀请两位组员。源码公开可读不自动授予写权限；本轮没有代发邀请，也不需要向队友分享账号密码。

## 分支与交接

每个任务从更新后的main创建分支，分别负责数据评测、算法智能体、前端交付。示例：

```bash
git switch main
git pull --ff-only
git switch -c codex/retrieval-baseline
```

数据使用`codex/data-eval-*`，前端使用`codex/frontend-*`；任务完成后提交PR，由另一人核对范围、结果和文档，再合并。不要force-push共享main。已经克隆旧版本的组员先提交或暂存自己的修改，再更新，不要直接丢弃本地工作。

## 资源与首次配置

本仓库提供最新代码、派生检索表、规则、中文OCR服务和Docker配置，不包含约GB级模型、原图、索引及个人运行状态。见[数据与模型准备](docs/setup.md)。要快速复用现有资源，可向组长取得完整部署包，只复制指定资源目录；不要复制组长的.env、账号或授权文件。

团队部署一次供用户使用时，Nexent与模型服务由团队管理员配置。每位开发者在自己机器运行完整环境时，仍需各自的模型授权或团队批准的服务配置。服务端密钥不能提交Git。

## 必要检查

使用Python 3.11/3.12隔离环境，安装根目录requirements-demo.txt：

```bash
python -m pip install -r requirements-demo.txt
cd 02_代码
python -m unittest discover -s tests -p "test_*.py"
python tests/run_golden.py
cd ../integration
python -m unittest test_integration test_public test_hosting
```

上述检查覆盖工程契约，部分使用fixture上游，不代表真实模型、完整Docker部署或医学准确性通过。涉及图文/Nexent修改时，再提供真实调用证据；没有依赖则标记未验证。

PR说明至少写：具体问题、改动、执行命令与实际结果、未验证项。数据或模型变化必须记录版本和评测口径。暂不承诺正式开源许可；仓库作者代码和第三方条款见NOTICE.md。
