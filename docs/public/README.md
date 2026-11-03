# 知识型视频结构化文档生成系统

将本地知识型视频转换成带章节、正文、时间索引和代表帧的 JSON、Markdown、Word 文档。

当前为命令行原型。已实现 ASR、抽帧、页面变化检测、可选 OCR、多模态时间轴、LLM 分章与文档导出；Web界面为下一交付重点，规则与PPO作为独立探索；这些能力目前尚未实现。长视频与部分模型输出仍会触发严格校验失败，失败会保留记录并停止，不生成伪成功文档。

**团队成员先读[团队执行计划与任务入口](https://github.com/tingfeng-Wu/video-knowledge-doc/blob/main/docs/public/TEAM_PLAN.md)**：包含技术路线、0—30阶段、真实进度、中期前优先事项和可认领任务，无需先了解此前的开发讨论。2026-11-03已调整为2027年1月软件交付，详见[当前交付方案](https://github.com/tingfeng-Wu/video-knowledge-doc/blob/main/docs/public/DELIVERY_PLAN.md)；有限VLM增强尚未接入。

当前DEV01、COURSE01、LONG01已有真实图文文档并通过0 API缓存复跑；EXPLAINER01仍因重复引用校验失败而停止，M2尚未全部验收。

## Fork 后开始

在 GitHub 点击 Fork，将自己的仓库克隆到本地，然后进入仓库根目录。以下命令使用 Python 3.12，支持 PowerShell；其他终端调整虚拟环境激活命令即可。

项目地址：[tingfeng-Wu/video-knowledge-doc](https://github.com/tingfeng-Wu/video-knowledge-doc)。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m video_doc.cli --help
```

也可以使用 Conda 创建 Python 3.12 环境。运行视频处理需要另行安装 FFmpeg，并确认 `ffmpeg` 和 `ffprobe` 可用；可以通过 `--ffmpeg-dir` 指定其目录。首次 ASR 会下载 Whisper small 权重，OCR 首次使用也可能下载模型，需预留网络和磁盘空间。已有本地模型缓存时可离线使用本地处理模块。

## 无 API 的安装检查

```powershell
python -m video_doc.cli check-env
python -m pytest tests/test_bounded_content.py tests/test_boundary_evaluation.py -q
python tools/demo_offline.py
```

Windows建议克隆到较短路径，例如`D:\code\kvdoc`。完整测试产生多层临时目录；若遇到路径长度相关的文件写入错误，使用短临时目录运行：`python -m pytest -q --basetemp "$env:TEMP\kvdoc-tests"`。该目录专供pytest使用，重复运行会清理其中内容。

离线演示使用合成时间轴、合成图像和固定模型响应，输出到 `data/tmp/offline-demo/`。它验证代码到文档的链路，不代表真实视频识别或生成质量。自动测试不要求真实视频或 API Key。

## 处理自己的视频

在仓库根目录复制 `.env.example` 为 `.env`，填写自己的 `VIDEO_DOC_LLM_API_KEY`。M2 的模型和连接由 `config/model-roles.json` 管理，当前使用阿里云百炼；`.env` 的 M1 mock 开关不控制 M2。原始音视频与图片在本地处理；LLM 接收章节标题、ASR 文本和已生成正文。仅处理有权使用的视频。

```powershell
Copy-Item .env.example .env
# 编辑 .env 后先预览。将路径换成自己的实际视频路径。
python -m video_doc.cli run-m2 MY01 --source-video "D:\videos\lesson.mp4" --ocr auto --bounded-content --dry-run
python -m video_doc.cli run-m2 MY01 --source-video "D:\videos\lesson.mp4" --ocr auto --bounded-content --allow-llm
# 再次运行时省略 --allow-llm；已发布正文按上游身份复验后复用。
python -m video_doc.cli run-m2 MY01 --ocr auto --bounded-content
```

FFmpeg 未在 PATH 时，为命令加 `--ffmpeg-dir "D:\tools\ffmpeg\bin"`。任务ID使用字母、数字、连字符或下划线，源视频变更时使用新任务ID。

`--allow-llm` 允许当前任务的真实付费请求。分章最多2次供应商请求；`--bounded-content` 正文按章节每6个连续单元分批，整次正文最多12次供应商请求，每个分批最多尝试2次。超过计划上限提前停止；达到请求上限立即停止。成功分批有独立缓存，失败原因存入任务目录。重复引用、遗漏和跨章引用仍会失败。未添加该参数时保留历史三调用路径。

`--dry-run` 不写入结果、不调用 API。新任务尚无时间轴时，后续输入量会显示不可测；它不保证随后生成成功。旧路径的运行计划对配置版本漂移存在已知限制；已发布正文的复用以执行时的来源复验为准。

文档位于 `outputs/MY01/m2/documents/formal-results/<export-id>/`，包含 `result.docx`、`result.md`、`result.json` 和图片。`current.json` 指向当前结果组。检查正文与视频一致后再用于汇报；不要手工修改正式组。

## 目录与协作

| 目录 | 用途 |
|---|---|
| `video_doc/` | 核心处理模块及 CLI |
| `tests/` | 离线测试和合成 fixture |
| `config/` | 模型角色配置，不放密钥 |
| `tools/` | 离线演示和公开目录导出 |
| `docs/public/` | 团队协作和当前能力说明 |
| `data/tmp/`、`outputs/` | 本地产物，不提交 |

参阅 [贡献说明](https://github.com/tingfeng-Wu/video-knowledge-doc/blob/main/CONTRIBUTING.md)与[模块架构](https://github.com/tingfeng-Wu/video-knowledge-doc/blob/main/docs/public/ARCHITECTURE.md)。代码库不附带原始视频、模型权重、个人材料或私人开发历史，成员需要准备自己的输入和 API Key。当前未附开源许可证；公开发布前由项目负责人确认代码与第三方材料的分发许可。
