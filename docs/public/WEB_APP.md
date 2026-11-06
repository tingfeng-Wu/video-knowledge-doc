# 本机Web工作区

本工作区把现有视频处理链、任务状态、文档预览编辑和导出下载放在同一套本机界面中。服务默认只监听`127.0.0.1`，面向单用户本机使用。

## 启动

```powershell
python -m pip install -e ".[dev]"
python -m video_doc.web_server --open
```

访问`http://127.0.0.1:8765/`。可通过`--host`、`--port`和`--output-root`调整监听地址、端口及任务根目录。

真实视频处理需要FFmpeg/FFprobe。ASR与OCR模型在首次使用时可能下载；文本模型调用必须通过界面显式授权，已有阶段缓存会优先复用。

Web界面默认使用`config/model-roles-public.json`中的匿名公共模型，不需要百炼API Key。该服务best-effort、按IP限流，每个请求至少间隔约30秒，并会把待处理文本发送给公共模型服务。正式或敏感内容应切换到受控商业API。命令行默认仍使用`config/model-roles.json`，不影响原有百炼接入。

匿名模型输出长度有限，Web会自动把长章节按最多2个连续单元分批，并按项目原有来源覆盖和引用规则校验。

可设置`VIDEO_DOC_ASR_MODEL_PATH`指向本地Faster-Whisper模型目录，避免运行时访问模型仓库。Web服务也会自动复用
`data/tmp/models/faster-whisper-small/`中的本地模型。

## 用户流程

1. 在工作台上传视频或填写本机路径，创建任务。
2. 启动生成后查看当前阶段、累计进度、缓存状态和失败原因。
3. 失败任务可直接重试；模型审批失败时可显式授权后重试。
4. 完成后阅读章节、正文、配图和时间位置，并切换机器原稿或编辑版本。
5. 修改标题、概述、章节标题、正文块及配图保留状态，每次保存创建独立版本。
6. 选择任一版本下载JSON、Markdown或Word。

任务支持取消正在运行的生成进程、归档和恢复。归档不删除任务、缓存或文档。

## 存储边界

```text
<output_root>/
  _web/tasks/<task-id>.json              Web任务状态
  _uploads/<task-id>/<video-file>        通过页面上传的视频
  <task-id>/
    task.json                            现有任务清单
    m2/                                  现有机器结果组，Web不修改
    edits/
      current.json                       最新编辑版本指针
      <edit-id>/
        content.json                     独立编辑内容
        manifest.json                    来源、内容哈希与版本信息
    web-exports/<edit-id>/               编辑版独立导出
      result.json
      result.md
      result.docx
      images/
      manifest.json
```

机器正式组、机器导出的`current.json`和CLI行为保持不变。编辑版本引用来源机器正文的`content_id`，但不会复用机器内容ID作为编辑版身份。

## API概览

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/overview` | 工作台统计、最近任务和本机环境 |
| `GET` | `/api/tasks` | 任务筛选与搜索 |
| `POST` | `/api/tasks` | 以本机路径创建任务 |
| `POST` | `/api/tasks/upload` | 以请求体上传视频并创建任务 |
| `POST` | `/api/tasks/{id}/run` | 启动或重试生成 |
| `POST` | `/api/tasks/{id}/cancel` | 终止当前生成进程 |
| `GET` | `/api/tasks/{id}/document` | 读取机器或编辑版本文档 |
| `POST` | `/api/tasks/{id}/edits` | 保存新的独立编辑版本 |
| `POST` | `/api/tasks/{id}/exports` | 生成所选版本导出包 |
| `GET` | `/api/tasks/{id}/download/{version}/{format}` | 下载JSON、Markdown或Word |

生成通过当前Python环境启动`video_doc.cli run-m2`子进程，因此与CLI保持相同的错误码、缓存和阶段语义。网页关闭不会取消已经启动的生成进程；重新打开任务会从持久化状态继续展示。

## 当前限制

- 面向单用户本机流程，不包含账号、权限、协作和公网部署。
- 文本编辑覆盖标题、概述、结论、章节标题、段落、列表和对比表，不提供复杂富文本操作。
- 取消会终止子进程，但进程启动前或已完成的短暂状态仍以最后一次持久化状态为准。
- 本机已有但不由Web创建的任务会自动发现；首次打开时会生成Web任务状态，不重写已有机器结果。
