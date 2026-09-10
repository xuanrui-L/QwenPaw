# -*- coding: utf-8 -*-
"""Agent inputs for the entire work interface. Deliberately no example HTML."""

import hashlib
import json

from services.project_files.models import narrative_timeline_ids

PRESENTATION_TARGET = "__presentation__"
PRESENTATION_SYSTEM_PROMPT = """你负责从零设计这部互动作品的全部页面，输出完整 HTML 文档。
作品的首页、播放页、剧情地图、结局页的布局、排版、色彩、装饰、文案与 CSS 动画都由你创作。
根据故事和用户要求独立设计；不套用固定页面模板，不默认霓虹、暗色、卡片、居中大标题或呼吸动画。
只使用内联 CSS 与 HTML/SVG，无脚本、事件属性、外部资源、链接、图片、url()、@import、CSS 注释或转义。
支持 html/head/body/title/meta/style/div/span/p/h1-h6/button，
section/main/article/header/footer/nav/aside/figure/figcaption，
strong/b/em/i/small/br/hr/ul/ol/li/dl/dt/dd/code/pre/time/video，
以及基本 SVG 图形。完整闭合所有非空标签。
宿主只绑定以下协议，不提供任何页面视觉或兜底样式。请自行设计响应式布局和视频/抉择层容器。
四个互不嵌套的区域 data-screen="title|play|map|ending" 各一个，宿主切换可见性。
title 内必须有 button data-action="start" 和 "resume"。
play 内恰好一个 video data-player-video（无 src/autoplay/loop/poster，可加 controls），
一个 data-slot="interaction" 容器；容器覆盖视频区域且为空，抉择时宿主挂载另一个生成的动效。
play 内必须有 button data-action="toggle_play"、"map" 和 "replay"（重新开始）；播放器不会绘制导航按钮。
map 内必须有 button data-action="map_back"。
为输入的每个真实节点手写一个 data-node-ref="节点ID" 区域，
并可配 button data-action="jump" data-node-ref="节点ID"。
地图宿主仅显示已访问节点；未访问节点将隐藏，切勿在区域外泄露未访问分支。用 data-visited 属性设计已访问样式。
ending 内必须有 button data-action="replay" 和 "title"。
其他可选按钮动作：map、title、reset；所有动作均使用 button，无 href，不写跳转地址。
开始、重新开始、剧情地图属于必备能力，不能因剧本或风格省略。其他交互根据剧本需要设计，不虚构功能。
输入 screens 是逐页的设计意图，controls 是该页按钮的文案和外观要求，不是视觉模板。
必须实现 screens 中声明的全部 controls；非空 label 逐字用作按钮的可见文案（可包 span，不添加额外文字）。
没有配置的页面或必备按钮仍需你独立设计。不要自行隐藏 data-screen，宿主负责页面显隐；不要使用 active 类模拟页面切换。
用 CSS 媒体查询保证窄屏可操作，使用 prefers-reduced-motion 尊重减少动态效果设置；预留交互层和页面导航各自空间，不能挡住导航按钮。
文本节点可用 data-bind，值选择 project.title、project.synopsis、
node.title、node.synopsis、progress.visited 或 progress.endings。
宿主只更新该节点文本，不要放在含按钮的容器上。
首页和结局页需要使用对应 title/synopsis 绑定，以支持修改故事后准确呈现。各页面自行创作文案与视觉。
只输出完整文档，不要解释或 Markdown。"""


def presentation_inputs(project, creation=None):
    return {
        "interface_contract": 2,
        "project": {
            "title": project.name,
            "description": project.description,
            "brief": project.strategy.creative_brief,
            "aspect_ratio": project.settings.aspect_ratio,
            "visual": {
                "style": project.visual.style,
                "visual_bible": project.visual.visual_bible,
            },
        },
        "design_prompt": (
            creation or project.interactive_presentation
        ).design_prompt,
        "screens": {
            key: value.model_dump(mode="json")
            for key, value in (
                creation or project.interactive_presentation
            ).screens.items()
        },
        "nodes": [
            {
                "id": tid,
                "title": project.timelines.items[tid].title,
                "synopsis": project.timelines.items[tid].synopsis,
            }
            for tid in narrative_timeline_ids(project)
        ],
        "edges": [
            edge.model_dump(mode="json") for edge in project.narrative_edges
        ],
    }


def presentation_fingerprint(project, creation=None):
    raw = json.dumps(
        presentation_inputs(project, creation),
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def presentation_is_current(project):
    motion = project.interactive_presentation.motion
    return bool(
        motion
        and (motion.html or motion.html_file_id)
        and f"input_fingerprint={presentation_fingerprint(project)}"
        in motion.design_notes,
    )


def presentation_prompt(project, creation):
    return json.dumps(
        presentation_inputs(project, creation),
        ensure_ascii=False,
        indent=2,
    )
