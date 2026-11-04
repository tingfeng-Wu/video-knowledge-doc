# 知识型视频结构化文档生成系统

将本地教学视频和知识讲解整理成带章节、正文、配图与时间位置的JSON、Markdown和Word讲义。

当前命令行主链路已实现。项目正在准备中期检查：接下来由协作者在独立分支完成已有结果预览、基础编辑和PPT成果素材，负责人评估后决定是否合并。

## 新成员从这里开始

| 想了解什么 | 阅读入口 |
|---|---|
| 项目目标、当前能力和整体阶段 | [项目总览](PROJECT.md) |
| 当前可以接的任务及交付要求 | [中期支线计划](MIDTERM_BRANCH_PLAN.md)、[团队任务入口](TEAM_PLAN.md) |
| 处理链和代码模块 | [架构说明](ARCHITECTURE.md) |
| 数据格式、接口、合成数据和测试 | [开发指南](DEVELOPMENT.md) |
| 中期后的产品路线 | [交付计划](DELIVERY_PLAN.md) |
| 分支和成果提交 | [贡献指南](../../CONTRIBUTING.md) |

仓库包含当前实现、测试、配置模板和合成示例，新成员可独立开始功能开发。真实视频、模型权重和内部产物由负责人另行协调。

## 当前功能

- 视频信息检查、任务创建、音频提取和本地ASR转写。
- 抽帧、页面变化检测、可选OCR和统一时间轴。
- 文本LLM分章、正文生成、章节配图与三格式导出。
- 来源引用、版本化结果组、缓存、请求预算和失败记录。

A0—A2收口已完成，已有四条真实视频案例。Web和应用内编辑尚未实现，属于当前中期支线；VLM和RL保留后续任务。正文仍可能继承ASR错词，编辑成果与机器原始结果分别保存。最近改动见[收口记录](CLOSEOUT_20261104.md)。

## 本地运行

使用Python 3.12，在项目根目录执行。以下为PowerShell示例：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m video_doc.cli --help
```

Conda环境也可以。真实视频处理需要FFmpeg和FFprobe；首次ASR/OCR使用可能下载模型。命令可通过`--ffmpeg-dir`指定FFmpeg目录。

## 无API开发示例

```powershell
python tools/demo_offline.py
python -m pytest tests/test_document_composer_v2.py tests/test_document_exporter_v2.py -q
```

演示使用合成时间轴、合成图片和固定模型响应，不需要API Key、真实视频或模型权重。它会打印新建导出包的位置，完整任务位于：

```text
data/tmp/offline-demo/run-<标识>/outputs/SYNTH01/
```

该目录可用于开发结果页与编辑功能。也可先查看仓库中的[合成JSON示例](../examples/README.md)。示例内容仅用于开发，真实案例由负责人协调。

## 处理自己的视频

复制`.env.example`为`.env`并填写`VIDEO_DOC_LLM_API_KEY`。M2模型角色与连接使用`config/model-roles.json`，目前为阿里云百炼；`.env`中的M1 mock设置不控制M2。

```powershell
Copy-Item .env.example .env
# 填写自己的Key，将下列路径替换为实际视频路径
python -m video_doc.cli run-m2 MY01 --source-video "D:\videos\lesson.mp4" --ocr auto --bounded-content --dry-run
python -m video_doc.cli run-m2 MY01 --source-video "D:\videos\lesson.mp4" --ocr auto --bounded-content --allow-llm
# 复用已有结果时无需再次授权模型调用
python -m video_doc.cli run-m2 MY01 --ocr auto --bounded-content
```

`--allow-llm`允许本次真实付费请求。分章最多2次供应商请求；`--bounded-content`逐章按最多6个单元分批，整次正文最多12次供应商请求，每批最多2次尝试。团队任务的调用范围和预算执行前另行确认。

`--dry-run`预览计划，不写结果、不调用API；新任务尚无时间轴时后续输入量显示不可测。原始音视频和图片本地处理，当前文本LLM接收ASR、章节及正文相关文本。

输出位于`outputs/MY01/m2/documents/formal-results/<export-id>/`，包括`result.json`、`result.md`、`result.docx`和`images/`。`current.json`指向当前版本，编辑功能应另存独立版本。

## 开发与协作

在个人Fork或功能分支中开发，完成后提交分支、运行说明、检查结果和成果素材，由负责人决定是否合并main。代码入口与相关测试见[开发指南](DEVELOPMENT.md)。

完整离线测试命令为`python -m pytest -q`。Windows建议使用较短目录；需要时通过`--basetemp`指定专供pytest的短临时目录。部分用例需要真实产物或符号链接权限，缺少条件时会输出skip原因。当前中期任务不额外安排安装或稳定性专项。

主要目录：`video_doc/`为实现，`tests/`为测试，`config/`为配置，`tools/`为开发工具，`docs/`为项目说明与示例。`data/tmp/`和`outputs/`保存本地数据，不提交Git。
