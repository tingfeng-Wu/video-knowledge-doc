# 参与开发

## 开始任务

阅读[项目总览](PROJECT.md)和[团队任务](TEAM_PLAN.md)，说明希望承担的范围，再从最新main创建功能分支。可以在自己的Fork中开发。

```bash
git switch main
git pull --ff-only
git switch -c feature/your-task
```

## 实现与检查

实现前查看相关模块和测试。开发方法与内部任务拆分由承担者安排；新增接口、依赖或数据格式在交付说明中记录。保持现有CLI、原始结果组和历史版本兼容。

运行与改动相关的功能检查，记录实际结果和未验证内容。完整离线测试命令为`python -m pytest -q`，合成示例为`python tools/demo_offline.py`。中期支线按其任务范围检查，不额外承担安装或稳定性专项。

## 提交成果

将修改推送到功能分支，提供分支地址、基线/最终提交、使用说明、检查结果和已知问题。可以通过Pull Request集中展示改动，由负责人决定是否合并main。

提交代码、说明和可公开的小型素材；共享真实样例包维护在`samples/development/`。日常视频、模型权重、密钥、缓存及大体积outputs保留在本地或约定的数据共享位置。合成测试数据可以随测试提交，真实案例与生成结果分别标识来源。

项目架构见[架构说明](ARCHITECTURE.md)，数据和接口入口见[开发指南](DEVELOPMENT.md)。
