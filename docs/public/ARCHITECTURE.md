# 开发入口与模块边界

## 当前处理链

`cli.py`解析参数，`m2_pipeline.py`编排以下模块：

1. `video_probe.py`、`task_workspace.py`：检查源视频身份并初始化任务。
2. `audio_extract.py`、`asr_transcribe.py`、`asr_timeline.py`：本地音频、ASR与M1文本时间轴。
3. `frame_sampler.py`、`page_change_detector.py`、`ocr_extractor.py`：抽帧、页面变化候选与可选OCR。
4. `unified_timeline.py`、`timeline_builder_v2.py`：只读组合来源，生成Timeline 2.0。
5. `boundary_planner_v2.py`：LLM生成ChapterPlan 2.0。
6. `chapter_frames.py`：按章选择代表帧并生成确定性说明。
7. `document_composer_v2.py`：仅以章内ASR文本生成正文，发布StructuredDocumentContent 3.0。
8. `document_exporter_v2.py`：确定性导出JSON、Markdown和内嵌图片的Word。

模型角色由`model_roles.py`校验，连接与生成参数来自`config/model-roles.json`；网络传输由`llm_adapter.py`提供。测试通过注入transport避免真实付费请求。

## 产物与缓存

M1输出保留在任务原目录；M2域位于`outputs/<task_id>/m2/`。每个域使用不可变结果组、哈希manifest及最后切换的`current.json`，失败不覆盖成功组。缓存与来源、模型角色、提示词、参数和策略版本绑定。读取结果应使用各模块公开loader，并检查来源身份，不按目录日期猜测最新组。

`--bounded-content`逐章按最多6个连续单元生成正文，再生成全文字段，最多12次供应商请求。原始响应及调用计数进入结果组并可重放验证。既有已发布正文可在验证当前上游后固定复用，保留其原生成版本。

## 下一阶段接口

`boundary_evaluation.py`已实现边界匹配与评价，真实人工标注数据尚未提供。规则分章和PPO需要共享候选边界与特征、保持训练/验证/测试视频隔离，并输出统一章节计划供后续内容模块消费。当前不要假定PPO、候选生成器或Web服务已经存在。

修改模块前先阅读对应测试，优先添加能复现实际故障的用例。普通缺陷修复保持Schema和M1行为兼容；涉及新Schema、算法、模型配置或预算时先在任务中说明设计及迁移，再实施。


## 2026-11-03交付范围调整（待实现部分）

稳定ASR链路复用；新增章内少量图片+ASR直接输入现成VLM的可选模式，约一周主动投入止损，不训练VLM、不单独搭视觉解析流水线、不同时改分章。先设计引用约束兼容修复，再实施最小Web；预览/下载必须，基础编辑按余量保存独立人工版本。规则和PPO独立探索，不阻塞软件。新增模式和交互均尚未实现，API/图片外发仍需另批。详见[交付方案](DELIVERY_PLAN.md)。
