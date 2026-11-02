"""阶段8稳定性实验共用的简洁分章政策。"""

from __future__ import annotations


STABILITY_POLICY_VERSION = "concise-top-level-chapters-1.0"
STABILITY_MIN_CHAPTER_SECONDS = 60.0


def render_stability_policy(duration_seconds: float) -> str:
    """为两套实验方案生成语义一致的软约束文本。"""

    if duration_seconds <= 20 * 60:
        chapter_reference = "3—5章"
    elif duration_seconds <= 30 * 60:
        chapter_reference = "4—6章"
    else:
        chapter_reference = "按一级主题数量决定，并优先保持章节完整"
    return (
        f"共用简洁分章政策版本：{STABILITY_POLICY_VERSION}。"
        "只划分贯穿一段内容的一级主题；同一主题下的例子、案例、解释、步骤、数据、"
        "重复表述和总结必须保持在同一章。仅有表达方式、说话节奏或过渡句变化时不得"
        "新建章节。相邻内容服务于同一个主要目的时应合并；无法确定是否拆分时优先少分。"
        "标题应简短并概括整章，不得使用章内单个例子作为标题。"
        f"本视频章节数量只软参考{chapter_reference}，不是必须达到的数量；内容高度连续时"
        "允许更少，只有明确出现新的一级主题时才允许更多。"
    )
