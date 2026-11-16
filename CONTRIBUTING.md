# 团队协作

完整分工见[三人团队与AI协作方案](docs/眼维_三人团队分工与AI协作执行方案_20261020.md)，技术路线见[研究与算法方案](docs/眼维_研究定位与技术算法优化实施方案_20261020.md)。文档中的后续算法和图谱属于计划，不是已实现功能。

## 获取项目

```bash
git clone https://github.com/yuzijun0125/yanwei-retinal-agent.git
cd yanwei-retinal-agent
```

组长可以在 [Collaborators 设置](https://github.com/yuzijun0125/yanwei-retinal-agent/settings/access) 添加组员的 GitHub 用户名。组员接受邀请后，可向团队仓库推送任务分支。尚未获得写权限的组员也可以先 Fork 仓库，再从自己的 Fork 向团队仓库的 `main` 提交 PR。

## 每次改动怎么提交

1. 提交或暂存已有的本地改动，再从最新 `main` 建立任务分支。下面以修改医生工作台为例。

   ```bash
   git switch main
   git pull --ff-only
   git switch -c frontend/review-workspace
   ```

2. 改完后运行下方的相关检查。用 `git status --short` 查看变更，挑选本次任务的文件提交。

   ```bash
   git add -- 02_代码/web/workspace.js
   git commit -m "Improve doctor review workspace"
   git push -u origin frontend/review-workspace
   ```

3. 在 GitHub 打开 PR，目标选团队仓库的 `main`。填写改动和验证结果，请另一位组员审阅，待检查通过后合并。使用 Fork 时，`origin` 是自己的 Fork，发 PR 时选择团队仓库作为目标；后续可从 Fork 页面使用 **Sync fork** 同步更新。

一个任务对应一个 PR。三位组员可以分别负责数据与评测、算法与智能体、前端与部署。修改共用接口或数据字段时，在 PR 里写清使用该接口的组员需要怎么调整。

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
python -m unittest test_integration test_public test_hosting test_clinical_review
```

上述检查覆盖工程契约，部分使用fixture上游，不代表真实模型、完整Docker部署或医学准确性通过。涉及图文/Nexent修改时，再提供真实调用证据；没有依赖则标记未验证。

涉及图谱时，在 `research/knowledge-graph/` 运行 `python build_graph.py --source inputs` 和 `python -m unittest test_graph`。PR 模板会提示填写具体问题、改动、验证结果和未验证项。数据或模型变化必须记录版本和评测口径。暂不承诺正式开源许可；仓库作者代码和第三方条款见NOTICE.md。
