# 真实开发样例

仓库随附教学、科普和演讲三类视频及已有处理结果，可用于界面开发、数据格式对照和PPT案例整理。

| 样例 | 场景 | 视频时长 | 压缩包大小 | 已有结果 |
|---|---|---|---|---|
| COURSE01 | 中文课程 | 17分56秒 | 约43 MB | ASR、抽帧、页面变化、OCR、时间轴、分章、图文正文、三格式导出、独立审校版 |
| EXPLAINER01 | 科普解说 | 16分32秒 | 约93 MB | ASR、抽帧、页面变化、OCR、时间轴、分章、图文正文、三格式导出、独立审校版 |
| TALK01 | 演讲 | 24分09秒 | 约72 MB | 视频、ASR参考文本、抽帧和页面变化；用于输入与预处理开发 |

视频和内容来自项目现有开发样例。三类场景都属于项目范围，课程中的图表、板书和课件适合展示图文整合能力；实际处理难度还与音频质量、专业术语及画面结构有关。

## 解包

在仓库根目录执行：

```bash
python tools/unpack_dev_samples.py
```

只使用一个案例时：

```bash
python tools/unpack_dev_samples.py --case COURSE01
```

默认解包到`data/tmp/development-samples/`，命令会输出每条样例的视频路径、`output_root`和已有结果ID。可用`--destination`指定其他目录。重复运行会检查并复用已有文件。

## 目录说明

```text
data/tmp/development-samples/COURSE01/
  source/COURSE01.mp4
  outputs/COURSE01/
    task.json
    m2/
      frames/、ocr/、timelines/、boundaries/、chapter-frames/
      content/                   机器正文与原始响应
      documents/                 JSON、Markdown、Word和图片
      editorial-revisions/       独立审校版及编辑记录
  reference/
    asr-transcript.json          参考转写
    video-metadata.json          视频信息
  sample.json                   样例索引
  manifest.json                 文件哈希
```

TALK01只包含表中列出的预处理结果。各包的`sample.json`记录实际可用阶段与结果版本。

ASR参考文本只规范化了原机器上的音频路径，转写内容保留原样。M2当前结果组及其manifest按原字节打包，解包时仅将本地`task.json`的视频路径指向新位置。音频文件、模型权重、LLM缓存、无关历史组及运行日志不在样例包中。

## 读取已有讲义

```python
from pathlib import Path
from video_doc.document_composer_v2 import load_published_document_content
from video_doc.document_exporter_v2 import export_document_v2

root = Path("data/tmp/development-samples/COURSE01/outputs")
content = load_published_document_content(root, "COURSE01")
print(content["title"])
export = export_document_v2(root, "COURSE01")
print(export.group_path)
```

COURSE01和EXPLAINER01可直接读取正文、打开配图并使用已有导出包，无需模型请求。独立审校版位于各任务的`m2/editorial-revisions/`，机器原始结果和编辑成果分别保留。

需要从视频重新处理时，使用新任务ID和解包命令输出的视频路径；已有样例主要提供读取、编辑、导出和素材整理的起点，并非全部上游运行缓存的镜像。真实模型调用沿用项目的范围和预算约定。

## 校验与维护

`index.json`列出包的SHA-256、大小及结果ID，旁边的`.sha256`文件可独立校验下载。解包工具会检查压缩包及各成员文件，已有文件不一致时保留原文件并提示使用新的目标目录。

更新样例时可使用`tools/build_dev_sample_pack.py`从已有项目任务生成新包。日常开发产生的数据放在`data/tmp/`或`outputs/`，共享样例包集中维护在本目录。
