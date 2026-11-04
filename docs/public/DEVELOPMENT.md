# 开发指南

## 阅读顺序

新成员先读[项目总览](PROJECT.md)和[当前任务](MIDTERM_BRANCH_PLAN.md)，运行README中的合成示例，再按任务查看相关模块和测试。当前中期支线主要涉及已有结果预览、基础编辑和再导出。

源码头部保留了一些原阶段裁决文档名称，它们用于追溯开发历史。常用接口和数据结构可通过本指南、合成示例及公开测试了解，接手当前任务无需读取私有仓库。

## 代码入口

| 需要做的事 | 代码入口 | 对应测试 |
|---|---|---|
| 查看CLI命令及参数 | `video_doc/cli.py` | `tests/test_run_m2_cli.py` |
| 理解已有完整处理流程 | `video_doc/m2_pipeline.py`中的`run_task_m2` | `tests/test_m2_pipeline.py`、`test_m2_pipeline_e2e.py` |
| 读取已发布的正文 | `document_composer_v2.load_published_document_content` | `tests/test_document_composer_v2.py` |
| 生成现有机器正文的导出包 | `document_exporter_v2.export_document_v2` | `tests/test_document_exporter_v2.py` |
| 渲染Markdown或Word | `document_exporter_v2.render_export_markdown`、`render_export_docx` | 同上 |
| 了解模型和调用身份 | `video_doc/model_roles.py`、`config/model-roles.json` | `tests/test_model_roles.py` |

表中的模块均位于`video_doc/`。当前没有现成Web服务或通用编辑服务，这些属于支线待实现内容。

## 先获得完整的本地开发数据

在仓库根目录执行：

```bash
python tools/demo_offline.py
```

命令创建新的合成工作目录，并打印导出包路径，例如：

```text
data/tmp/offline-demo/run-<随机标识>/outputs/SYNTH01/m2/documents/formal-results/export-<id>
```

这个目录包含完整的合成任务、上游组、正文和导出包，可用于预览/编辑功能开发。每次运行保留旧示例，路径中的随机标识会改变。示例使用仓库测试fixture和固定transport，运行前按README安装开发依赖。

只想先看JSON形态时，可打开[合成正文示例](../examples/synthetic-document-content.json)。该静态示例不附带上游组和图片，完整开发数据由上述命令生成。

## 任务与产物目录

```text
<output_root>/<task_id>/
  task.json
  asr/                         带时间戳转写
  m2/
    frames/                    抽帧结果与图片
    ocr/                       OCR结果
    timelines/                 统一时间轴
    boundaries/                章节计划
    chapter-frames/            章节配图选择
    content/
      current.json
      formal-results/<content-id>/
        document-content.json
        raw-responses.json
        execution-report.json
        manifest.json
    documents/
      current.json
      formal-results/<export-id>/
        result.json
        result.md
        result.docx
        images/
        manifest.json
```

`current.json`包含`result_id`、相对`group`和manifest哈希。组内manifest记录文件哈希、输入来源及版本。读取机器正文优先使用公开loader，它会连同相关上游身份进行复验；按文件时间排序不能替代current引用。

导出包自带展示图片。正式正文的帧引用则需通过源帧集索引定位，两个位置的路径用途不同。失败审计通常位于`m2/failures/`，原始机器结果保留为不可变组。

## 正文数据结构

当前正文为`StructuredDocumentContent` 3.0，主要字段如下：

| 字段 | 含义 |
|---|---|
| `task_id`、`content_id` | 任务和内容身份 |
| `title`、`overview`、`overall_conclusion` | 标题、概述块和结论块 |
| `chapters` | 章节列表，含`index`、`chapter_id`、`title`、`start_ms`、`end_ms` |
| `chapters[].blocks` | 正文块，`text`为正文，来源通过chapter/unit/event ID记录 |
| `chapters[].frames` | 配图投影，含`frame_id`、`decoded_ms`、`caption_text`及排序信息 |
| `source_*` | 时间轴、章节计划、选帧、源帧集的身份与哈希 |
| `generation`、`content_versions`、`coverage` | 生成设置、版本和覆盖信息 |

时间字段`*_ms`为毫秒。内容ID由规范JSON计算，原机器组还会重放原始响应做一致性检查。编辑后文本不再等同于原模型响应，因此编辑服务应保存自己的版本和来源引用，沿用机器正式组的校验及ID不适合表示修改结果。

编辑版本的具体结构由实现者设计并说明，至少应能确定来源任务/机器内容、所选正文与配图、编辑版本身份和保存信息。既有机器Schema和current继续保留。

## 常用接口

下面的`output_root`是合成示例实际创建的`outputs`目录，不一定是仓库根目录的`outputs`。

```python
from pathlib import Path
from video_doc.document_composer_v2 import load_published_document_content
from video_doc.document_exporter_v2 import export_document_v2

output_root = Path("data/tmp/offline-demo/run-<实际标识>/outputs")
content = load_published_document_content(output_root, "SYNTH01")
result = export_document_v2(output_root, "SYNTH01")
print(result.group_path)
```

`load_published_document_content(output_root, task_id)`返回已验证的正文dict；无正文current时可返回None，输入不完整或损坏时抛出对应异常。`export_document_v2(output_root, task_id)`返回导出结果对象，包含`group_path`、`result_id`、`content`、`manifest`、`cache_hit`及图片计数等。

底层渲染函数：

```python
render_export_markdown(content, embedded)  # 返回bytes；embedded为成功嵌入的frame_id集合
render_export_docx(content, embedded)      # 返回(docx_bytes, failed_frame_ids)
# DOCX的embedded为 {frame_id: image_bytes}
```

这些函数负责渲染，不负责编辑版本登记或正式组发布。实现编辑功能时，保存、来源记录和所选版本导出需由新增服务串接。选择复用哪些接口由承担者根据设计确定。

## 功能检查

中期支线重点检查：已有结果读取、图文展示、编辑保存后重新打开、机器结果保留、选定版本导出和删图一致性。可复用上述测试的合成fixture，补充覆盖新增行为的检查。

```bash
python -m pytest tests/test_document_composer_v2.py tests/test_document_exporter_v2.py -q
```

完整离线测试命令仍为`python -m pytest -q`。部分真实案例检查在没有私有产物时会skip，Windows符号链接用例也可能skip，具体原因由pytest输出。中期任务不额外承担安装、跨机器或稳定性专项。

## 合并与交接

从公开main建立独立功能分支，提交运行说明、接口/版本设计、检查结果和素材索引。负责人评估后决定合并范围。提交格式和流程见[贡献指南](../../CONTRIBUTING.md)。
