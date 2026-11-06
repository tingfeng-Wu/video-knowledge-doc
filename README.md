# 知识型视频结构化文档生成系统

将本地教学、演讲和科普等知识型视频转换为带章节、正文、配图与时间位置的JSON、Markdown和Word讲义。

## 功能

- 提取音频，生成带时间戳的语音转写。
- 抽取视频帧、检测页面变化并识别画面文字。
- 按统一时间轴组织文本与画面信息。
- 使用文本LLM划分章节、生成正文并选择章节配图。
- 导出JSON、Markdown和内嵌图片的Word文档。
- 保存来源引用和生成记录，使用缓存继续处理已有任务。

## 快速开始

使用Python 3.12，在项目根目录执行。以下为PowerShell示例：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m video_doc.cli --help
```

也可以使用Conda环境。真实视频处理需要FFmpeg和FFprobe；通过`--ffmpeg-dir`可指定FFmpeg目录。ASR和OCR模型会在首次使用时下载。

### 启动本机工作区

安装项目依赖后，在仓库根目录启动完整界面：

```powershell
python -m video_doc.web_server --open
```

默认访问地址为`http://127.0.0.1:8765/`，任务文件保存在`outputs/`。界面支持视频上传或本机路径导入、任务创建与阶段进度、运行取消与重试、图文预览、章节编辑、删图、独立编辑版本保存，以及机器版/编辑版的JSON、Markdown和Word下载。

Web工作区默认使用无需API Key的匿名线上模型，并在界面上显式勾选模型调用授权；匿名服务按IP限流，每次调用至少间隔约30秒，视频文本会发送到公共模型服务。命令行仍兼容原有百炼配置。本机已有缓存会优先复用，接口和存储设计见[Web工作区说明](docs/public/WEB_APP.md)。

### 运行合成示例

```powershell
python tools/demo_offline.py
```

示例使用合成时间轴、图片和固定模型响应，无需API Key或真实视频。运行后会打印导出包路径，完整任务位于：

```text
data/tmp/offline-demo/run-<标识>/outputs/SYNTH01/
```

数据结构可参考[合成JSON示例](docs/examples/README.md)。

### 处理本地视频

复制`.env.example`为`.env`，填写`VIDEO_DOC_LLM_API_KEY`。分章与正文模型角色由`config/model-roles.json`管理，默认连接阿里云百炼。

```powershell
Copy-Item .env.example .env
# 填写Key，并替换为自己的视频路径
python -m video_doc.cli run-m2 MY01 --source-video "D:\videos\lesson.mp4" --ocr auto --bounded-content --dry-run
python -m video_doc.cli run-m2 MY01 --source-video "D:\videos\lesson.mp4" --ocr auto --bounded-content --allow-llm
# 继续处理已有任务，复用缓存
python -m video_doc.cli run-m2 MY01 --ocr auto --bounded-content
```

`--dry-run`预览执行计划，`--allow-llm`启用本次付费模型调用。`--bounded-content`按章分批生成正文，正文供应商请求上限为12次，每批最多尝试2次。更多参数见命令帮助和[开发指南](docs/public/DEVELOPMENT.md)。

## 真实开发样例

仓库提供教学、科普和演讲三类真实视频，以及对应的已有处理结果：

```powershell
python tools/unpack_dev_samples.py
```

样例包总计约208 MB，默认解包到`data/tmp/development-samples/`。中文课程和科普样例附带图文讲义与三格式导出，演讲样例附带ASR和帧处理结果。可用`--case COURSE01`单独解包，详细目录和读取方法见[真实开发样例](samples/development/README.md)。

## 生成结果

导出文件保存在`outputs/<task_id>/m2/documents/formal-results/<export-id>/`：

```text
result.json     结构化内容与来源信息
result.md       Markdown讲义
result.docx     Word讲义
images/         章节配图
manifest.json   文件与版本记录
```

`current.json`指向任务当前使用的结果组。

## 项目结构

| 目录 | 内容 |
|---|---|
| `video_doc/` | 视频处理、内容生成、导出与CLI |
| `tests/` | 离线测试和合成fixture |
| `config/` | 模型角色配置 |
| `tools/` | 示例和开发工具 |
| `docs/` | 项目文档与数据示例 |
| `video_doc/webui/` | 无需构建的本机Web界面 |
| `samples/development/` | 随仓库分发的真实开发样例包 |
| `data/tmp/`、`outputs/` | 本地运行数据与产物 |

## 文档与贡献

- [项目总览](docs/public/PROJECT.md)
- [架构说明](docs/public/ARCHITECTURE.md)
- [开发指南](docs/public/DEVELOPMENT.md)
- [团队任务与执行计划](docs/public/TEAM_PLAN.md)
- [贡献指南](CONTRIBUTING.md)

在功能分支或个人Fork中提交改进。完整离线测试命令为`python -m pytest -q`，相关模块的检查入口见开发指南。
