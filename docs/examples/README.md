# 合成数据示例

[synthetic-document-content.json](synthetic-document-content.json)是`tools/demo_offline.py`生成的StructuredDocumentContent 3.0正文快照，任务ID为SYNTH01，包含两章、正文块、来源引用和配图投影。

它使用合成时间轴、合成图片和固定模型响应，可用于阅读数据结构。图片和完整上游组不随这个JSON快照附带。需要开发或运行完整读取/导出功能时，在仓库根目录执行：

```bash
python tools/demo_offline.py
```

完整任务和导出包位于新创建的`data/tmp/offline-demo/run-<标识>/outputs/`。命令不调用真实API，不需要真实视频或模型权重。相关读取方式见[开发指南](../public/DEVELOPMENT.md)。

真实视频及已有产物见[真实开发样例](../../samples/development/README.md)。合成示例用于轻量功能检查。
