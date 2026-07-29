# QwenPaw Creator Agent 管理的本地资产库与特效体系设计提案

> 状态：设计提案（适配当前 `dev/creator` 文件原生架构）
>
> 日期：2026-07-21
>
> 适用实现：`plugins/apps/qwenpaw-creator`
>
> 关联设计：Creator Web Grounding、单一 `project.json` 存储、Timeline Element 架构
>
> 目标：为 Creator 建立 Global、User、Project 三层本地资产体系。Global 是随仓库或程序发布的只读内置资产；Agent 在对话和后台更新中主动维护 User Library 与 Project Kit，并在程序升级后自动重建 Global 索引、验证新版本和提出迁移建议。

---

## 1. 提案摘要

Creator 的素材能力分成三个语义不同的领域：

1. **Context Grounding**：获取事实依据、真实人物身份、地点、产品、节目舞台和视觉风格参考。
2. **Asset Retrieval**：查找可以直接进入生成、剪辑、合成或音频流程的生产资产。
3. **Asset Stewardship**：持续管理资产的身份、版本、元数据、许可证、质量、兼容性、替代关系和生命周期。

三个领域可以由 Creator Agent 在同一次用户目标中协调，但必须返回和持久化为不同语义：

- `sources`：带来源的文本事实。
- `visual_sources`：身份、风格、地点、产品或文化视觉参考。
- `asset_sources`：可直接用于生产的 Project、Global 或 User 资产。
- `asset_change_proposals`：超出当前 Agent 授权边界、需要审批的资产变更。

资产产品结构采用三个层级：

| 产品层级 | 内部权限作用域 | 主要职责 |
|---|---|---|
| Global Catalog | `global` | 随 Creator 仓库、插件或程序版本发布的只读内置资产和 Effect Bundle |
| My Library | `user` | 当前本地用户长期保留的上传、生成、收藏和常用资产 |
| Project Kit | `project` | 当前 Project 已选、已固定版本、可直接生产的精简资产集合 |

产品层级和内部作用域保持一致，不引入当前 Host 无法证明或执行的 Organization scope。

本提案的默认运营方式不是人工维护后台表格，而是 **Agent 管理、策略约束、例外审批**：

- 用户在对话中提出创作目标，Creator Agent 自动检索 Project Kit、已有 Project Asset、My Library 和 Global Catalog。
- Agent 在权限和许可证允许时自动选择、物化并固定精确版本。
- 用户上传或生成新资产后，Agent 自动补充标签、描述、预览、技术元数据、去重和用途建议。
- Agent 根据上传、生成、定时任务、使用反馈和健康检查持续维护 User Catalog。
- Creator 程序升级后，Agent 自动发现新的 Global manifest、重建派生索引和 Preview，并为已弃用版本提出 Project 迁移建议；运行时不能改写或发布 Global payload。
- Agent 可以自动发布满足预授权策略的 User 资产更新；不满足策略的变更进入 Proposal/Review，而不是要求用户参与所有日常操作。
- Agent 不能给自己扩大权限、伪造许可证、绕过发布策略、静默撤销历史可用资产或执行 Effect Bundle 中的任意代码。

执行架构采用 **Asset Manager 领域服务 + Agent-facing Tool Surface + 可选 Specialist**：

- Asset Manager 是确定性的领域服务和工具边界，不是必须经过一次额外模型调用的 Agent。
- Creator Agent 以及已有的 Visual Development、R2V、AI Editing 等 Specialist 可以在各自权限内直接调用资产检索、检查、物化、登记和使用记录工具。
- Asset Librarian 是可选 Specialist，只处理需要多步判断的跨 scope 检索、批量整理、去重、替代和生命周期规划；普通检索或物化不委派给它。
- Local Asset Steward 处理上传、生成、定时健康检查和 User Library 批量维护，同样只能通过 Asset Manager 工具执行变更。

核心原则：

1. Provider、所有权、可见性、权限和物理存储是独立概念。
2. Project Kit 保存精确版本选择和生产用途，不复制整个上层 Catalog。
3. 上层候选只有被 Project 真正选中时才物化到 Project Asset Index。
4. Project 一旦使用资产，必须固定不可变版本，不能依赖“最新版本”。
5. Agent 默认承担资产维护工作，但所有写操作受确定性 Policy Guard 和显式 Authority Envelope 约束。
6. 特效可以是文件，也可以是声明式、可验证、可预览、可编译为 Timeline Element 的复合资产包。
7. 本地资产不是事实证据；音效、转场和模板不能写入 `facts` 或伪装成 Web Grounding 来源。
8. `project.json` 内部保存由 Pydantic 图校验的精确版本 ID；`asset://...@...` 和 `artifact://...@...` 是 API、Tool 和审计边界的 Workspace Ref。
9. 正式生产只消费当前 Project Asset Index 中已存在的精确版本，不消费 Library Ref、裸路径或不稳定 URL。
10. 用户不需要手工完成常规分类、升级、去重或元数据维护；人工操作是可选的高级控制面和例外处理面。
11. Agent 直接调用最小、类型化的资产工具；只有任务本身需要独立上下文、多步推理或长时间自治时才委派 Asset Librarian。
12. Global 表示“随当前程序安装、对本地 Creator Project 全局可见”，不表示云端服务、跨安装共享或运行时可写 Catalog。

---

## 2. 当前仓库基础与约束

本设计以当前 `plugins/apps/qwenpaw-creator` 为实现基础，不再沿用旧的 Section/Unit/Composition 模型。

### 2.1 已有基础

当前 Creator 已具备：

- 单一 `project.json` 作为 Project 领域权威。
- `IndexedFile`、`SourceAssetVersion`、`ArtifactSlot` 和 `ArtifactVersion` 组成的 Project Asset Index。
- Project 内 `assets/` 的不可变文件发布、checksum 校验、安全读取和 orphan 扫描边界。
- `asset://<logicalAssetId>@<versionId>` 和 `artifact://<slotId>@<versionId>` Workspace Ref。
- Project Commit Boundary、generation/ETag CAS、Review 和 crash recovery。
- Timeline Element 作为唯一持久化时间/图层实体，支持 `r2v`、`edit`、`overlay`、`transition` 和 `audio` 创作类型。
- Web Grounding triage、Provider 搜索、视觉候选下载、VLM 复核和已接受视觉参考的 Project 物化。
- 文件、URL、文本和批量素材入库 API。
- Project 级执行授权和 Specialist Tool 权限边界。

这些能力直接支撑本提案的以下部分：

- Project 本地物化。
- 精确版本固定。
- 幂等导入。
- Agent 使用受约束工具修改 Project。
- 资产使用位置的 Timeline/Visual/Artifact 图校验。

### 2.2 必须尊重的当前架构约束

1. `project.json.assets` 是文件和版本索引，不是 Project Kit。
2. `ProjectSource` 表示需要作为 Project 输入和素材理解对象的 Source；SFX、模板或 LUT 不应仅为进入 Project Kit 就自动成为 `ProjectSource`。
3. Project 内部引用使用精确 version ID；Workspace Ref 在 Tool/API 层解析后必须再次对 Project 图进行校验。
4. 不新增 Section、Unit、Track、独立 Content 或顺序 Composition。
5. Effect Bundle 必须编译为现有或明确新增的 Timeline Element primitive，不能恢复嵌套的旧 Composition 结构。
6. Runtime Task、Agent Run、检索 trace、审批和后台同步状态不进入 `project.json`。
7. 当前 Project schema 是 v2；新增 Project Kit 属于正式领域模型变化，应升级为 v3 并注册 v2 → v3 确定性迁移。

### 2.3 当前需要修正的边界

当前 `ground_prompt_context` 的 Function 描述宣称只读，但 Creator Runtime 会把已接受的 Web Visual 自动物化并提交到 Project。实现本提案前必须让边界真实可解释：

- 或把 Tool 描述改为“检索可能触发已接受视觉参考的幂等 Project 物化”；
- 或把视觉 promotion 拆为独立的、显式可审计写工具。

本提案推荐后者，并进一步把本地资产检索与物化拆开。

当前本地媒体执行器还不能完整执行资产体系中规划的效果：

- 显式非零 Transition 会被拒绝。
- Audio Element 尚未进入最终时间线混音。
- Overlay 执行只覆盖有限类型和组合。

因此 Effect Bundle 必须晚于基础 Timeline renderer 能力建设。

---

## 3. 目标与非目标

### 3.1 目标

- 为 Project 提供统一、可搜索、可解释的本地生产资产检索入口。
- 支持 Global、User、Project 三个内部权限作用域。
- 在产品上形成 Global Catalog、My Library、Project Kit 三层结构。
- 让 Agent 在普通对话中自动发现、复用、选择并固定资产，用户无需手工浏览才能继续创作。
- 让 Agent 在上传、生成、Provider 更新、定时维护和使用反馈中自动维护 Catalog。
- 同一用户目标可以同时获得 Web 依据和本地生产资产，但结果保持语义隔离。
- 让 Project 固定精确资产版本，保证历史任务可复现。
- 支持简单文件资产和声明式 Effect Bundle。
- 支持许可证、权限、来源、审核、兼容性、安全策略和替代关系。
- 复用 Project `IndexedFile`、`SourceAssetVersion`、`ArtifactVersion` 和内容完整性边界。
- 只把实际选中的上层资产物化到 Project，避免检索即导入造成资产膨胀。
- 让 Agent 自动修复缺少的预览、标签、技术元数据和可重建搜索索引。
- 对超出预授权范围的变更生成最小、可解释的审批请求。

### 3.2 非目标

- 不把本地素材库变成第二套事实数据库。
- 不允许 Catalog Asset 绕过 Project Asset Index 直接进入生产任务。
- 不允许 Agent 把任意本地绝对路径当作资产引用。
- 不允许 Agent 自行提升自己的 scope、预算、许可证或发布权限。
- 不允许特效包携带未经授权的任意 Shell、Python、JavaScript 或远程执行入口。
- 不在第一阶段建设完整采购、版权结算或企业 DAM 平台。
- 不要求 Global、User 和 Project 保存三份相同二进制。
- 不让 Project 自动跟随上层资产最新版本。
- 不把临时下载、搜索缓存、Agent 对话或 Runtime Task 登记为正式资产。
- 不要求用户日常手工给每个资产打标签、选择目录或逐版本确认。
- 不设计 Organization Library、Organization Principal、Membership、Role 或企业多租户 ACL；这些概念在当前 Creator/QwenPaw 中不存在。
- 不建设远程 Global Catalog 服务，也不允许运行时 Agent 修改随程序发布的 Global payload 或 authoritative manifest。

---

## 4. 概念边界

### 4.1 Provider

Provider 表示“如何发现或同步候选”，例如：

- `tavily`
- `dashscope_web_search`
- `dashscope_web_search_image`
- `project_assets`
- `local_catalog`
- `builtin_catalog`
- 未来的企业 DAM、NAS 或对象存储 Provider

Provider 不决定资产所有权，也不决定最终存储位置。

### 4.2 Ownership Scope

Ownership Scope 表示谁拥有逻辑资产身份和发布权限：

- `global`
- `user:local`
- `project:{projectId}`

`global` 是安装包拥有的只读 scope，只能由仓库/程序发布流程产生新版本。`user:local` 表示当前 `CREATOR_DATA_ROOT` 所属的隐式本地用户，不是由请求参数选择的多租户身份。Agent 只能在 Authority Envelope 允许的 scope 中创建或修改资产。使用来自某个 scope 的资产不自动授予修改该 scope 的权限。

### 4.3 Visibility、Entitlement 与 Authority

- **Visibility**：谁能发现候选。
- **Entitlement**：谁能 Preview、Import、Render、Publish 或 Redistribute。
- **Authority**：本次 Agent Run 被允许执行哪些变更。

可见不等于可用于所有输出；拥有 Import 权限不等于拥有 Publish 或 Revocation 权限。

### 4.4 Storage

Storage 表示字节实际保存在哪里：

- 共享内容寻址 Blob Store。
- 企业对象存储。
- 本地只读资产包。
- Project 自己的 `assets/`。
- 可重建缓存。

Storage 不能向无权 Principal 泄漏其他 scope 是否拥有相同内容。跨 scope 去重只能是服务内部优化。

### 4.5 Selection

Selection 表示 Project 已决定使用哪个精确版本，以及用途、优先级、审批状态和许可证快照。Selection 属于 Project，不改变上层资产所有权。

### 4.6 Stewardship

Stewardship 表示 Agent 对资产生命周期执行的持续维护：

- 发现和同步更新。
- 内容与元数据校验。
- 自动描述、Tag、Embedding、Preview 和技术探测。
- 去重和重复身份合并建议。
- 版本发布、弃用、替代和撤回建议。
- 许可证、兼容性和安全状态复核。
- 基于 Project 使用结果的质量反馈。
- 缺失预览、损坏索引和过期 Provider 状态的修复。

Stewardship 是独立于一次 Retrieval 的长生命周期能力。

---

## 5. 三层资产结构

### 5.1 Global Catalog

Global Catalog 保存随 Creator 仓库、插件或程序版本发布的通用基础库存：

- 通用音效、环境声和音乐 Stinger。
- 转场、叠加层、粒子、光效和胶片质感。
- LUT、调色预设和音频处理预设。
- 标题、字幕、Lower Third、片头和片尾模板。
- 图标、纹理、背景、贴纸、遮罩和 3D 资源。
- 平台验证过的 Effect Bundle。

Global payload、authoritative manifest、许可证声明、版本和 replacement 关系由构建/发布流程生成，运行时只读。程序启动或升级后，Asset Manager：

- 校验 manifest、checksum、许可证声明和兼容性。
- 为当前安装重建可删除的 Search Index、Embedding、Preview 和技术探测缓存。
- 识别新增、弃用和 replacement，并向仍固定旧版本的 Project 提出迁移建议。
- 在 Global 文件损坏或 manifest 不一致时禁用候选并报告安装完整性问题，不尝试让 Agent 修补发布内容。

Agent 可以维护 Global 的本地派生索引和观察结果，但不能修改随程序发布的原始文件、authoritative metadata 或版本关系。若用户修改 Global 资产，必须 fork 为新的 Project 或 User 资产。

### 5.2 My Library（User Library）

保存：

- 用户上传的私有素材。
- 收藏和常用素材。
- 用户生成并选择保留的资产。
- 用户自己的草稿和派生版本。
- 用户偏好、别名和替代建议。

Agent 可以在对话中自动完成：

- 对上传内容分类、去重和补全元数据。
- 在用户明确表达“以后都用这个”“保存到我的素材库”等意图时发布到 User Library。
- 在预授权偏好允许时，把高复用、已验证的 Project Asset 提议或自动提升到 User Library。
- 记录用户拒绝、收藏、替换和常用选择，调整个人排名。

默认不能仅因一次临时上传就永久提升到 User Library；需要明确用户意图或已配置的自动保留策略。

第一阶段 My Library 是单机本地用户库，不宣称支持多个用户之间的安全隔离。个人偏好不能绕过 Global 禁用、许可证或安全策略。

### 5.3 Project Kit

Project Kit 是当前制作已选择的精简集合，不等同于 `project.json.assets`。

- `assets`：Project 可验证读取的文件与版本索引。
- `asset_kit`：Project 对精确版本的选择、角色、优先级、状态和许可证快照。
- `visual` / `timelines`：这些版本在创作与生产中的实际消费者。

Project Kit 主要承担：

- 固定精确版本。
- 表达资产在本项目中的角色和用途。
- 记录 `approved`、`candidate`、`blocked`、`deprecated` 状态。
- 为 Creator Agent 提供高优先级、低噪声候选池。
- 保证视觉、声音、品牌和人物连续性。
- 支持自动替换建议和使用位置查询。

### 5.4 Project 关系

| 关系 | 含义 | 所有权 |
|---|---|---|
| `linked` | 从 Global/My Library 选择并物化精确版本 | 上层拥有逻辑资产；Project 拥有固定导入记录 |
| `forked` | 基于上层版本进行 Project 专属修改 | Project 拥有新的派生逻辑资产 |
| `generated` | 在 Project 内新生成或制作 | Project 拥有逻辑资产 |

进入正式生产的所有引用最终必须解析为 Project Asset Index 中的精确 Source 或 Artifact Version。

### 5.5 Asset Kind 分类

`asset_kind` 使用可扩展的点分层级；`media_kind` 继续兼容当前 Project 的 `image`、`video`、`audio`、`document`、`text` 和 `other`。

建议第一阶段支持：

- `reference.identity`、`reference.style`、`reference.location`、`reference.product`
- `image.background`、`image.texture`、`image.mask`
- `video.stock`、`video.broll`、`video.overlay`、`video.loop`
- `audio.sfx`、`audio.ambience`、`audio.music`、`audio.stinger`、`audio.voice`
- `brand.logo`、`brand.font`、`brand.color-system`、`brand.watermark`
- `graphic.icon`、`graphic.sticker`、`graphic.lower-third`、`graphic.title-card`
- `effect.transition`、`effect.overlay`、`effect.lut`、`effect.motion-preset`
- `template.subtitle`、`template.intro`、`template.outro`、`effect.bundle`
- `generation.prompt-preset`、`generation.negative-prompt`、`generation.reference`
- `generation.adapter`、`generation.control-map`、`generation.camera-preset`
- `spatial.model`、`spatial.material`、`spatial.hdri`、`spatial.rig`
- `derived.thumbnail`、`derived.proxy`、`derived.transcript`、`derived.subtitle`
- `derived.beat-map`、`derived.depth`、`derived.tracking`、`derived.embedding`

当前 `SourceAssetVersion.media_kind` 没有 `model` 或 `font`；此类 payload 在 Project 中使用 `other`，由 `asset_kind`、media type、manifest schema 和安全策略表达专用语义。不能为了增加 Catalog 分类而把任意新字符串写进现有 Project `media_kind`。

以下内容不进入正式 Catalog：

- API Key、Credential 和 Secret。
- 临时下载、可重建 Cache 和未选中的搜索候选。
- Agent 对话、Prompt Transcript 和 Runtime Task 状态。
- 裸绝对路径或无法验证的 `file://`。
- 没有明确来源、授权或用途的远程 URL。
- Project strategy、Timeline Element 或 R2V Shot 等领域决策本身。

一个对象只有在具备稳定身份、不可变版本、有效 payload 或 manifest、可搜索元数据、明确来源和定义明确的消费者时，才成为正式资产。

---

## 6. Agentic 运营模型

### 6.1 角色划分

#### Asset Manager 服务与 Agent-facing Tool Surface

Asset Manager 是资产领域的确定性执行边界，不是 LLM，也不是一个拥有隐式无限权限的“万能 Agent”。它负责：

- 统一 Project Asset Index、Project Kit、User Library 和 Global Catalog 的访问协议，并显式保证 Global 只读、User/Project 按 Authority 可写。
- 执行 exact version、checksum、幂等、CAS、许可证、Entitlement、Authority 和审计校验。
- 协调 `project_assets`、`local_catalog` 和 `builtin_catalog` Provider。
- 暴露按能力拆分的 Agent-facing 工具，而不是把检索、写入、发布和撤回藏在一个不可审计的通用操作中。
- 为对话 Run、Specialist Run、后台 Stewardship Job 和确定性事件处理器提供同一套规则。

`Asset Manager` 可以作为服务和 Tool Surface 的产品名称，但实现上应保留多个类型化工具。Creator 不需要为了普通资产操作先启动另一个 Agent。

#### Creator Agent

- 理解用户目标和当前 Project。
- 判断是否需要事实 Grounding、本地 Asset Retrieval 或两者。
- 优先复用 Project Kit 和 Project 现有版本。
- 直接调用 Asset Manager 的检索、检查、物化、登记和使用记录工具。
- 仅在跨 scope、多候选、多步骤整理等复杂任务中委派 Asset Librarian。
- 向用户汇报重要选择和需要审批的例外。

#### 当前 Creator Specialist

本设计沿用当前仓库的受约束 Specialist 模型，不建立第二套通用多 Agent 框架：

| 当前角色 | Asset Manager 工具用途 |
|---|---|
| `source_intelligence_agent` | 第一阶段保持现有最小权限；上传后的登记和派生 metadata 由 Runtime/Creator 协调 |
| `visual_development_agent` | 检索视觉资产、物化参考、登记生成结果、记录选用关系 |
| `r2v_generation_director` | 检索和物化 storyboard/生成参考、登记生成的视频版本 |
| `ai_editing_director` | 检索和物化音频、Overlay、Transition 等剪辑资产，记录 Timeline 使用位置 |

所有 Specialist 只获得完成其角色所需的工具和 admitted target scope。它们不能通过 Tool 参数扩大 Principal、scope 或动作权限。

#### 可选 Asset Librarian Specialist

- 把创作需求转换为 Typed Retrieval Job。
- 在单次工具调用不足时，执行跨 Project、User 和 Global scope 的多步检索与比较。
- 解释复杂的排名、兼容性、许可证、重复身份和替代关系。
- 为批量整理、去重、Collection 重组或生命周期变更形成受约束计划。
- 通过与 Creator 和其他 Specialist 相同的 Asset Manager 工具执行允许的动作，不拥有专属绕过通道。
- 不直接绕过 Project Commit Boundary 写文件。
- 不作为普通 `retrieve_assets`、`materialize_library_assets` 或 metadata 更新的必经代理。

满足以下任一条件时才建议委派 Asset Librarian：

- 需要多轮检索、候选比较和约束收敛。
- 需要跨多个资产或 scope 做去重、替代、Collection 或升级规划。
- 维护任务需要独立上下文、独立预算、可暂停状态或可审查的 Specialist Run。

单次类型化工具调用可以完成的任务不应委派，以避免额外延迟、成本和责任边界模糊。

#### Local Asset Steward

- 由上传、生成、定时任务、User Catalog 健康事件或程序升级触发。
- 批量校验 User 版本、更新非权威 metadata、生成 Preview、修复索引和管理 User 生命周期。
- 对 Global 只校验安装完整性并重建可删除的本地派生数据，不能发布或改写 Global 版本。
- 只能在 Run Authority 允许的 User scope 中执行持久化资产变更。
- 对高风险或策略不明确的变更生成 Proposal。

#### Deterministic Policy Guard

Policy Guard 不是 LLM：

- 解析可信 Principal 和 Authority Envelope。
- 校验 Visibility、Entitlement、license、region、channel 和 scope。
- 限制预算、候选数、文件大小、Provider、自动发布数量和变更范围。
- 阻止 scope escalation、任意代码、路径逃逸和不安全 payload。
- 决定动作是 `auto_apply`、`require_review` 还是 `deny`。

Agent 负责判断和编排，Policy Guard 负责不可绕过的授权。

### 6.2 对话触发

典型对话不要求用户手工管理 Catalog。

#### 创作时自动选用

用户说：“做一个冠军揭晓视频，结尾要观众欢呼和金色彩带。”

1. Creator Agent 读取 Project 和 Project Kit。
2. Creator Agent 或正在执行创作子任务的 Specialist 直接调用 `retrieve_assets`，产生 `audio.sfx`、`visual.overlay`、`effect.bundle` 等 Typed Retrieval Job。
3. Asset Manager 依次检索 Project Kit、Project existing、User、Global；只有复杂的多步候选整理才委派 Asset Librarian。
4. Policy Guard 过滤无权限、许可证不兼容和引擎不兼容候选。
5. 调用方通过 Asset Manager Tool 自动物化允许使用的精确版本，并写入 Project Kit。
6. Agent 把资产编译或绑定到 Timeline Element。
7. 用户看到创作结果和简短选择说明，而不是被要求逐项管理素材。

#### 上传时自动整理

用户上传 Logo、音乐或参考视频：

1. 先进入当前 Project Asset Index。
2. Runtime 触发 Asset Manager ingestion/metadata 流程；Agent 在需要语义判断时补充描述、Tag 和用途建议，确定性模块生成技术 metadata、Preview 和 checksum。
3. Agent 检查 Project/User 中是否已有同内容或同逻辑资产。
4. 临时使用默认只留在 Project。
5. 用户表达“以后都用这个”或策略允许自动保留时，Agent 创建 User Library 版本。
6. User 资产不能在运行时提升为 Global；若要随程序分发，必须进入仓库/插件发布流程。

#### 反馈驱动维护

用户说：“这个片头以后不要再用，换成新版。”

Agent 应区分：

- 仅当前 Project 替换。
- 记录 User Preference。
- 更新 User Preference 或 User Library Replacement。
- 如果请求涉及 Global 资产，则只更新当前 Project/User 偏好或 fork；不能撤销或改写 Global 版本。

Agent 不得把模糊反馈直接解释为 Global Revocation。

### 6.3 后台更新触发

资产维护 Job 可以由以下事件触发：

- `creator_package_updated`
- `scheduled_catalog_health_check`
- `asset_uploaded`
- `asset_generated`
- `project_asset_selected`
- `project_asset_rejected`
- `render_failed`
- `license_status_changed`
- `engine_version_changed`
- `catalog_integrity_issue`

后台 Job 应增量运行，只处理发生变化或健康状态过期的资产。

### 6.4 默认自动化与审批边界

| 动作 | 默认策略 |
|---|---|
| 搜索、Preview、技术探测、生成 Tag/Embedding | 自动 |
| 复用 Project Kit 或 Project existing exact version | 自动 |
| 从允许的 Global/User 版本物化到 Project | 自动，受 Entitlement 和 Project Authority 约束 |
| 修复可重建 Preview、Embedding、搜索索引 | 自动 |
| 程序升级后校验 Global manifest、重建索引/Preview | 自动；不修改 Global authority |
| 为资产补充非权威描述和别名 | 自动，但保留 provenance |
| 合并逻辑资产、改变许可证、扩大 Visibility | 审批 |
| Project → User Library 发布 | 明确用户意图或预授权自动保留策略 |
| 运行时 Global publish、replacement、blocked、revoked | 禁止；只能通过仓库/程序发布流程变更 |
| 付费采购、未知许可证、模型权重、Adapter、可执行内容 | 必须审批或拒绝 |
| 删除仍被 Project 固定的版本 | 禁止；只能改变新使用策略 |

### 6.5 Authority Envelope

每个 Agent Stewardship Run 必须带不可由模型修改的 Authority Envelope：

```json
{
  "principalId": "local-user",
  "trustedPrincipalSource": "creator_runtime_local_profile",
  "allowedScopes": ["project:project-123", "user:local"],
  "allowedActions": [
    "discover",
    "preview",
    "import",
    "update_metadata",
    "publish_user_version"
  ],
  "maxImportedBytes": 536870912,
  "maxChanges": 20,
  "expiresAt": "2026-07-21T12:00:00Z"
}
```

Tool 参数不能提供或覆盖 `principalId`、`allowedScopes` 或权限列表。

这里的 `local-user` 是 Runtime 根据当前 `CREATOR_DATA_ROOT` 注入的本地 profile identity，不来自 HTTP query/header，也不表示已经实现多用户认证。

---

## 7. 数据模型

### 7.1 LibraryAsset

跨版本稳定的逻辑身份：

```json
{
  "library_asset_id": "library-asset-crowd-cheer",
  "owner_scope": "global",
  "asset_kind": "audio.sfx",
  "media_kind": "audio",
  "name": "Arena Crowd Victory Cheer",
  "description": "大型室内场馆的胜利欢呼",
  "tags": ["crowd", "victory", "arena", "finale"],
  "status": "active",
  "current_version_id": "library-version-004",
  "created_by": "catalog-curator",
  "updated_at": "2026-07-21T00:00:00Z"
}
```

`current_version_id` 只用于浏览和默认推荐，不能进入 Project 正式生产引用。

### 7.2 LibraryAssetVersion

不可变版本：

```json
{
  "library_version_id": "library-version-004",
  "library_asset_id": "library-asset-crowd-cheer",
  "manifest_sha256": "...",
  "payloads": [{
    "role": "primary",
    "blob_sha256": "...",
    "media_type": "audio/wav",
    "size_bytes": 4182032
  }],
  "technical_metadata": {
    "duration_seconds": 4.2,
    "sample_rate": 48000,
    "channels": 2
  },
  "license_ref": "license-platform-cleared-v2",
  "compatibility": {},
  "validation_report_ref": "catalog-report-123",
  "created_at": "2026-07-21T00:00:00Z"
}
```

### 7.3 ScopeMembership

资产在某个 scope 中的可见性、排序和策略覆盖：

```json
{
  "scope": "user:local",
  "library_asset_id": "library-asset-crowd-cheer",
  "visibility": "visible",
  "priority": 60,
  "collection_ids": ["collection-sports"],
  "replacement_asset_id": null,
  "blocked": false,
  "policy_generation": 12
}
```

### 7.4 Entitlement

允许的操作包括：

- `discover`
- `preview`
- `import`
- `generate`
- `render`
- `publish`
- `redistribute`
- `maintain_metadata`
- `publish_version`
- `deprecate`
- `revoke`

Entitlement 可以包含地区、时间、渠道、分辨率、Project 类型或用途限制。

### 7.5 Project schema v3：Asset Kit

建议在 Project 根增加：

```text
Project
├── ...
├── asset_kit
│   └── pins: EntityCollection[ProjectAssetPin]
└── assets: AssetIndex
```

内部使用精确、可校验 ID，不在 `project.json` 持久化解析后的文件路径：

```json
{
  "pin_id": "pin-finale-cheer",
  "version_ref": {
    "kind": "source",
    "version_id": "asset-version-123"
  },
  "origin": {
    "scope": "global",
    "library_asset_id": "library-asset-crowd-cheer",
    "library_version_id": "library-version-004"
  },
  "relation": "linked",
  "role": "finale_audience_reaction",
  "status": "approved",
  "priority": 90,
  "usage_target_refs": ["timeline:timeline:main"],
  "license_snapshot": {
    "license_ref": "license-platform-cleared-v2",
    "captured_at": "2026-07-21T00:00:00Z"
  },
  "payload_version_ids_by_role": {
    "primary": "asset-version-123"
  },
  "resolved_dependency_pin_ids": [],
  "added_by": "asset-librarian",
  "added_at": "2026-07-21T00:00:00Z"
}
```

`version_ref.kind` 支持 `source` 和 `artifact`：

- 从 Library 导入的新资产通常物化为 `SourceAssetVersion`。
- Project 已生成的 `ArtifactVersion` 可以被 Project Kit 固定和复用。
- Artifact 发布到 User Library 后，在其他 Project 中重新物化为 Source Asset，不共享原 Project 的 ArtifactSlot 身份。若某个 Artifact 经人工代码审查和构建流程纳入后续 Creator 版本，则它以新的 Global manifest/version 身份发布，不复用 Runtime 的 ArtifactSlot 身份。

### 7.6 多 Payload 映射

一个 Library Version 可以包含 manifest、primary、preview、overlay、sound 等多个 payload，而当前一个 `SourceAssetVersion` 只对应一个正式文件。

物化结果必须显式记录：

- 每个生产 payload 对应的 Project SourceAssetVersion ID。
- Manifest 对应的 IndexedFile/SourceAssetVersion。
- 解析后的 dependency pin。
- 原 Library Version 和每个 payload checksum。

不能把多文件 Bundle 模糊压成一个无法校验内部依赖的 Project version。

### 7.7 EffectBundleManifest

```json
{
  "schema": "creator.effect-bundle",
  "schema_version": 1,
  "capability": "championship_winner_reveal",
  "engine": {
    "name": "creator-timeline-compositor",
    "version_range": ">=1.0 <2.0"
  },
  "parameters": {
    "duration": {"type": "number", "minimum": 0.5, "maximum": 5, "default": 2.5},
    "intensity": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.8},
    "primary_color": {"type": "color", "default": "#FFD700"}
  },
  "dependencies": [{
    "role": "overlay",
    "library_version_id": "library-version-confetti"
  }, {
    "role": "sound",
    "library_version_id": "library-version-cheer"
  }],
  "preview_version_id": "library-version-preview",
  "fallback_capability": "simple_title_reveal"
}
```

Manifest 只能引用受信 schema 和 allowlisted engine capability。进入 Project 后，Library dependencies 必须解析为 Project Kit pins 和 Project version IDs。

---

## 8. 引用与物化规则

### 8.1 引用层次

Catalog 发现阶段：

```text
library://library-asset-crowd-cheer@library-version-004
```

Project Tool/API 边界：

```text
asset://asset-crowd-cheer@asset-version-123
artifact://element%3Ar2v-1%3Avideo@artifact-version-456
```

`project.json` 内部：

```json
{"kind": "source", "version_id": "asset-version-123"}
```

Library Ref 不能直接写入 R2V、Edit、Audio、Overlay、Transition 或最终渲染 Task。

### 8.2 选择后物化

当 Agent 或用户选中 Catalog Candidate：

1. Policy Guard 校验 Principal、Authority、Import Entitlement 和目标用途。
2. 校验 Library Manifest、许可证状态、兼容性和所有 payload checksum。
3. 为每个生产 payload 计算稳定 Project file/version ID。
4. 使用 Project `AssetFileStore` stage、校验并发布到 `assets/`。
5. 创建或复用 `IndexedFile` 和 `SourceAssetVersion`。
6. 创建或更新 ProjectAssetPin，保存 origin、role、license snapshot 和 payload 映射。
7. 通过 Project Commit Boundary 一次提交 Asset Index 和 Asset Kit 领域变化。
8. 记录 Library → Project provenance、Agent Run 和 materialization trace。

文件发布与 `project.json` 更新不能依赖跨文件系统原子事务。正确的 crash-safe 顺序是：

```text
private staging
    → immutable file publish
    → Project Commit Boundary
    → success record
```

Commit 失败可能留下未索引的不可变文件，但不能留下引用不存在文件的 Project Index。未索引文件进入 durable orphan observation 和延迟 GC/repair 流程。

### 8.3 稳定 ID 与幂等

同一个 Library Version 重复导入同一个 Project 必须幂等。稳定身份至少包含：

```text
project_id + origin_library_version_id + payload_role + checksum
```

派生或修改资产必须生成新的 Project 逻辑资产和版本，不能覆盖 linked 版本。

### 8.4 ProjectSource 规则

- 需要素材理解、转录或作为主要创作输入的图片、视频、音频可以附加为 `ProjectSource`。
- SFX、LUT、Transition、Overlay、字体或 Effect Bundle 仅进入 Asset Index 和 Project Kit，不自动成为 `ProjectSource`。
- 是否创建 ProjectSource 是物化策略的一部分，不由媒体类型单独决定。

---

## 9. Retrieval Provider 与 Asset Manager Tool 设计

### 9.1 Provider Capability

```json
{
  "name": "local_catalog",
  "capabilities": [
    "visual_reference",
    "production_asset",
    "effect_bundle",
    "audio_search"
  ],
  "media_kinds": ["image", "video", "audio", "document", "model", "other"]
}
```

建议 capability：

- `factual_text`
- `visual_reference`
- `production_asset`
- `audio_search`
- `effect_bundle`
- `project_asset`

### 9.2 Typed Retrieval Job

```json
{
  "query": "dramatic audience cheer after winner announcement",
  "media_kinds": ["audio"],
  "asset_kinds": ["audio.sfx"],
  "usage": "sound_effect",
  "project_id": "project-123",
  "duration_hint_seconds": 4,
  "constraints": {
    "license": "publishable",
    "explicit_content": false
  },
  "providers": ["project_assets", "local_catalog", "builtin_catalog"]
}
```

视觉身份 Job 与音效 Job 不能共享同一套验证规则。

### 9.3 Provider

#### `project_assets`

- 检索 Project Kit、SourceAssetVersion 和 ArtifactVersion。
- 返回现有 exact workspace ref 和 version kind。
- 不下载、不复制、不 promotion。
- Project Kit 候选优先于普通 Project Asset。

#### `local_catalog`

- 只检索 Runtime 管理的 User Catalog。
- 权限过滤必须发生在候选返回之前。
- 返回 Library Ref 和 Preview，不立即修改 Project。
- 只有明确选择后才调用 materialization。

#### `builtin_catalog`

本地第一阶段不实现远程 trusted feed。Global 使用 `builtin_catalog` Provider：

- 读取随当前 Creator 版本发布的只读 manifest 和 payload。
- 在候选返回前验证 manifest schema、checksum 和当前安装兼容性。
- 程序升级时以新 manifest 作为新的 Global authority；Runtime 只重建派生数据。

### 9.4 排名

排名信号包括：

- Query 语义、Tag 和结构化字段匹配。
- Project Kit、Project existing、User、Global scope boost。
- 用途、媒体类型、时长、比例、分辨率、BPM 和透明通道匹配。
- 许可证、渠道、地区和当前用途可用性。
- Engine、Provider 和模型兼容性。
- 历史选用率、用户收藏、明确拒绝和人工替换。
- 质量、安全和品牌审核结果。
- 与当前 Project 已选资产的连续性。

无权限候选必须在排名前过滤，避免通过标题、数量或时序泄漏私有资产。

### 9.5 结果契约

```json
{
  "asset_sources": [{
    "provider": "local_catalog",
    "library_asset_id": "library-asset-crowd-cheer",
    "library_version_ref": "library://library-asset-crowd-cheer@library-version-004",
    "origin_scope": "global",
    "asset_kind": "audio.sfx",
    "media_kind": "audio",
    "usage": "sound_effect",
    "title": "Arena Crowd Victory Cheer",
    "duration_seconds": 4.2,
    "license_status": "allowed",
    "compatibility_status": "compatible",
    "match_score": 0.93,
    "preview_ref": "preview://library-version-preview",
    "materialization_status": "not_materialized"
  }],
  "asset_search_trace": [],
  "asset_issues": []
}
```

### 9.6 Asset Manager Agent-facing Tool 边界

不把本地资产循环硬编码进 `services.web_grounding`。建议增加 `services/asset_manager/` 领域服务，并由 `services/retrieval/` 协调 Provider。Asset Manager 通过多个边界清晰的 Agent-facing 工具暴露能力；这些工具可按角色加入 Creator 或 Specialist 的 tool manifest。

`Asset Manager` 是这组工具的逻辑名称，不建议实现为一个接受任意 `operation` 和任意 payload 的单一万能 Tool。读、Project 写、Catalog 写和高风险生命周期动作应分别授权、记录和测试。

#### `ground_prompt_context`（Asset Manager 之外）

- 事实和视觉依据。
- 不返回本地 SFX、模板或 Effect Bundle。
- 如果保留 Web Visual auto-promotion，Tool 描述和 trace 必须明确写副作用；推荐拆出 promotion。

#### `retrieve_assets`

- 只读。
- 搜索 Project 和允许的 Catalog scope。
- 返回 exact Project candidates 或 Library candidates。
- 不修改 Project 或 Catalog。

#### `inspect_asset`

- 只读。
- 接受 exact Project Version Ref 或 Library Version Ref。
- 返回可见 metadata、Preview、许可证、兼容性、provenance 和当前 Project 使用位置。
- 不泄漏调用 Principal 无权发现的 scope 或相同 checksum 所属关系。

#### `materialize_library_assets`

- 写操作。
- 只接受刚检索到且仍有效的 exact Library Version Ref。
- 必须携带 Runtime 注入的 Authority、idempotency key 和目标 Project generation/ETag。
- 创建 Project versions 和 Project Kit pins。

#### `register_asset_version`

- 写操作。
- 登记上传、生成、派生或 Provider 同步得到的不可变版本；原始字节写入仍由 Runtime 文件服务负责。
- 根据调用 Run 的 Authority 只能写入允许的 Project/User scope；Global registration 必须来自安装时加载的内置 manifest，不能通过 Agent Tool 调用。
- 低风险 Project/User 版本可自动登记；Project → User publish 由 Policy Guard 决定自动应用、审批或拒绝。

#### `update_asset_metadata`

- 写操作。
- 更新描述、Tag、别名、Preview/Embedding 状态和用途建议，不覆盖技术探测、许可证或来源的权威事实。
- 必须记录字段级 provenance；重复调用保持幂等。

#### `record_asset_usage`

- 写操作。
- 记录 exact version 在 Project Kit、Timeline Element、R2V 或 Render 中的使用、拒绝和失败信号。
- 不直接改变 Library 版本；质量或替代建议由后台维护 Job 消费。

Creator Agent 和现有 Specialist 的建议权限矩阵：

| 调用方 | 默认可用工具 |
|---|---|
| Creator Agent | `retrieve_assets`、`inspect_asset`、Project-scoped `materialize_library_assets`、`register_asset_version`、`update_asset_metadata`、`record_asset_usage` |
| Visual Development | `retrieve_assets`、`inspect_asset`、目标 Project 内物化/登记/metadata/usage |
| R2V Generation Director | `retrieve_assets`、`inspect_asset`、目标 Element 所需物化、生成结果登记和 usage |
| AI Editing Director | `retrieve_assets`、`inspect_asset`、目标 Timeline 所需物化和 usage |
| Source Intelligence | 第一阶段不新增 Catalog 写权限；由 Creator/Runtime 处理登记，避免扩大现有最小权限 |
| Asset Librarian（可选） | 按委派任务获得上述工具的受限子集；Global 永远只读 |

User Catalog Stewardship 使用单独、Role-scoped 工具：

- `inspect_library_asset`
- `validate_library_version`
- `repair_asset_metadata`
- `publish_user_library_version`
- `update_scope_membership`
- `set_asset_replacement`
- `deprecate_library_version`
- `propose_asset_revocation`

不存在运行时 Global 写 Tool。普通 Creator Agent 不获得 User Catalog 的批量生命周期维护工具。

所有写工具必须调用同一个 Asset Manager 服务和 Policy Guard。对话 Agent、Specialist 或 Local Asset Steward 都不能直接写 User Catalog、Blob Store 或 Project 文件来绕过该边界；Global 安装内容没有 Runtime 写入口。

---

## 10. Local Asset Steward Job 与变更协议

### 10.1 Stewardship Job

```json
{
  "job_id": "asset-job-123",
  "trigger": "creator_package_updated",
  "scope": "user:local",
  "intent": "reindex_builtin_and_check_user_replacements",
  "authority_ref": "authority-456",
  "provider": "builtin_catalog",
  "changed_library_asset_ids": ["library-asset-crowd-cheer"],
  "budgets": {
    "max_candidates": 100,
    "max_changes": 20,
    "max_download_bytes": 2147483648
  },
  "status": "queued"
}
```

状态：

- `queued`
- `running`
- `waiting_for_review`
- `succeeded`
- `partial`
- `failed`
- `cancelled`

### 10.2 Change Proposal

当 Policy Guard 不允许自动应用时，Agent 提交最小 Proposal：

```json
{
  "proposal_id": "asset-proposal-123",
  "scope": "project:project-123",
  "action": "set_replacement",
  "target_ref": "library://old-logo@library-version-2",
  "replacement_ref": "library://new-logo@library-version-1",
  "reason": "installed builtin manifest marks old asset deprecated",
  "impact": {
    "future_selection_count": 12,
    "pinned_projects_unchanged": true
  },
  "evidence_refs": ["builtin-manifest-version-456", "validation-report-789"],
  "status": "pending"
}
```

Proposal 不复制整个对话，只保留决定所需事实、影响、证据和精确目标。

### 10.3 自动更新规则

自动发布 User Library 新版本必须同时满足：

- Provider 在 scope 的 allowlist 中。
- Update 有签名或其他受信身份。
- Payload、manifest 和 dependency checksum 完整。
- License 未改变或变化在预授权兼容范围内。
- 安全扫描和技术验证通过。
- Engine/model compatibility 已知。
- 没有扩大 Visibility、Redistribute 或商业使用权限。
- 变更数和影响范围未超过 Authority budget。

否则进入 Proposal，不把失败伪装成“已自动更新”。Global 新版本不经过此协议，只随经过校验的 Creator 仓库/程序版本发布。

---

## 11. 存储布局

Global authority 随 Creator 程序发布，User/Project 状态保存在现有 Creator Runtime Root：

```text
CREATOR_INSTALL_ROOT/
└── builtin-assets/
    ├── catalog.json
    └── payloads/

CREATOR_DATA_ROOT/
├── .library/
│   ├── blobs/
│   │   └── sha256/{prefix}/{sha256}
│   ├── catalogs/
│   │   └── user/
│   ├── derived/
│   │   └── builtin-index/
│   ├── manifests/
│   │   └── {libraryVersionId}.json
│   ├── previews/
│   ├── indices/
│   ├── runtime/
│   │   ├── jobs/
│   │   ├── proposals/
│   │   ├── locks/
│   │   └── traces/
│   ├── quarantine/
│   └── cache/
├── {projectId}/
│   ├── project.json
│   ├── assets/
│   └── runtime/
├── config/
└── runtime-tools/
```

Global 安装目录只读；`.library` 只保存 User authority 和可重建派生数据。选择 `.library` 的原因：

- 当前 Project ID 必须以字母或数字开头，不会与 `.library` 冲突。
- 保持所有 Creator 数据在 PawApp 的 `CREATOR_DATA_ROOT` 内。
- ProjectStore 会忽略没有 `project.json` 的内部目录。
- 不新增必须配置的绝对路径即可支持个人本地运行。

可以增加 `CREATOR_LIBRARY_ROOT` 显式覆盖，但默认值为 `CREATOR_DATA_ROOT/.library`，且必须是绝对路径。

逻辑要求：

- Blob 不可变并按 checksum 校验。
- Catalog 和 Manifest 更新使用独立的原子 JSON Record、CrossProcess lock 和 recovery boundary。
- Preview 与正式 payload 分离；Preview 权限不自动等于原文件权限。
- Cache 和 Index 可删除、可重建，不是事实源。
- Quarantine 内容不能被 Retrieval 或 Project materialization 消费。
- `derived/builtin-index` 可以删除重建，不能覆盖 `builtin-assets/catalog.json` 的权威字段。
- Project 选中资产后，生产 payload 通过 `AssetFileStore` 正式物化到 Project `assets/`。
- 第一阶段优先 copy 或安全 reflink；不依赖 hardlink，避免共享 inode 被修改造成隔离问题。

Provider 和 Catalog Service Contract 不绑定文件实现，以便未来接入企业 DAM 或远程对象存储。

---

## 12. 许可证、权限与安全

### 12.1 许可证

每个可生产资产记录：

- 权利来源和许可证类型。
- 地区和有效时间。
- 可发布渠道。
- 是否允许修改、衍生和再分发。
- 是否要求署名。
- 商业、广告、政治或敏感内容限制。

Project 导入时保存许可证快照。Agent 不能根据文件名、Provider 名或自然语言猜测许可证。

许可证变化必须区分：

- 禁止新使用。
- 历史 Project 可继续使用。
- 历史使用需要撤回。
- 需要人工法律/合规判断。

### 12.2 本地用户边界

当前 Creator API 没有完整的可信 User Principal 或多租户 ACL Contract；QwenPaw 的 PawApp `user_id` 当前可来自请求 query/header，Creator 也没有把它接入资产权限模型。因此：

- Phase 1/2/3 只使用绑定当前 `CREATOR_DATA_ROOT` 的单机隐式 `user:local` scope。
- 请求 Body、query、header 或 Agent Tool 参数都不能选择 Library owner scope。
- 当前 My Library 是本地产品分层，不是安全的多用户数据隔离边界。
- 如果未来 Host 提供不可伪造的用户身份，再单独设计多用户迁移；本提案不预留 Organization scope、Membership 或 Role contract。

### 12.3 文件安全

- 拒绝绝对路径、路径穿越、符号链接逃逸和特殊文件。
- 读取时校验 checksum、size 和 media type。
- 对图片、视频、音频、压缩包、字体和模型分别设置大小与解码限制。
- 对复杂文件执行离线安全扫描或受限解析。
- 防止 decompression bomb、polyglot、恶意字体和模型 pickle。
- Effect Bundle 禁止任意代码，只允许声明式参数和 allowlisted capability。

### 12.4 Agent 安全

- Agent 不直接访问 Blob 路径；只使用 Tool 和 Ref。
- Agent 不决定自己是否有权限，Policy Guard 决定。
- Catalog 写 Tool 必须 Role-scoped，并验证 target scope 与 Authority Envelope。
- Agent 生成的描述、Tag 和质量判断不是许可证或所有权证据。
- 自动维护 Run 必须有预算、超时、候选上限和可取消状态。
- Provider 失败、模型不确定或扫描不可用时 fail closed 或进入 Proposal。

### 12.5 模型与 Adapter

LoRA、Adapter、模型权重和第三方插件属于高风险资产：

- 声明目标 Provider、基础模型和版本。
- 通过格式、大小、恶意 payload 和许可证检查。
- 使用独立 Entitlement 和 Engine allowlist。
- 不允许任何运行时对话或后台 Agent 提升、发布或改写 Global；Global 只由 Creator 构建/发布流程更新。
- 不兼容时返回诊断，不能降级到未知模型。

---

## 13. 生命周期与自动维护

### 13.1 Library Version 状态机

以下状态机适用于 Runtime 管理的 User Library Version：

```text
discovered
    → staged
    → validating
    → active
    → deprecated
    → blocked / revoked

validating → quarantined / rejected
```

Agent 可以推进 User 版本状态，但每个迁移都由确定性 Guard 校验。Global 版本的状态、弃用和 replacement 来自随程序发布的 manifest，Runtime 只消费和验证。

### 13.2 User Library 发布

Library Version 进入 `active` 前必须满足：

- Payload checksum 完整。
- 元数据和 Preview 可用，或明确声明 Preview 不适用。
- License 状态明确。
- 安全扫描完成。
- Engine/媒体检查通过。
- 发布者 Authority 覆盖 `user:local` scope。

Global 发布不属于 Runtime 生命周期；它必须经过仓库审查、资源打包、manifest 生成和 Creator 程序发布。

### 13.3 升级

- 新版本不修改旧版本。
- Agent 可以自动发现、验证和发布符合策略的 User 新版本。
- Global 新版本只在安装新的 Creator 程序版本后出现；Runtime 自动校验和索引，但不发布。
- Catalog 默认推荐 current version。
- Project 继续固定旧版本。
- Agent 可以在对话或后台提供 Project upgrade proposal，但不得静默改写已固定版本。

### 13.4 弃用、替代与撤回

- `deprecated`：历史 Project 可继续使用；新选择降低排名。
- `blocked`：User scope 禁止新使用；Global 完整性失败可在 Runtime 派生状态中临时阻止使用，但不改写内置 manifest。
- `replacement`：Agent 优先建议兼容替代版本。
- `revoked`：根据许可证或安全事件阻止读取、渲染或发布；必须记录影响与替代诊断。

User 自动撤回是高影响动作。除非预先定义了明确安全事件策略，否则 Agent 只生成 Proposal。Global 撤回需要新的 Creator 发布；当前 Runtime 只能因本地完整性或安全失败拒绝使用。

### 13.5 自动修复

Agent 可自动修复：

- 缺失或过期 Preview。
- 缺失技术元数据。
- 可重建 Embedding 和搜索索引。
- 已知 Provider 字段规范化。
- 无损 Tag/别名补充。
- orphan staging、失败 Job 和不完整 validation record 的恢复。

不可自动“修复”：

- 未知许可证。
- 所有权冲突。
- 不确定的逻辑资产合并。
- 已发布 payload 内容本身。

---

## 14. Timeline 与 Effect Bundle 集成

### 14.1 基础资产到 Element 的映射

| Asset 类型 | 当前 Timeline 表达 |
|---|---|
| Stock/B-roll/Project video | `EditCreation` + `SourceVersionRenderSource` |
| R2V reference | R2V Creation reference version IDs |
| SFX/Music | `AudioCreation` + exact Source version |
| Text/procedural overlay | `OverlayCreation` |
| Media overlay | Overlay Element + exact Project version |
| Transition | `TransitionCreation` 引用两个 endpoint Element |
| Generated output | `ArtifactVersion` / Element output |

### 14.2 Bundle 编译

Effect Bundle 不是新的嵌套 Composition。它由受信 compiler 转换为：

- 一个或多个 Overlay Element。
- 一个或多个 Audio Element。
- 必要的 Transition Element。
- 精确的 Project version references。
- 确定性参数和 provenance。

Compiler 输入：

- 已物化并校验的 Bundle Manifest。
- 当前 Timeline 和 target Element refs。
- 经 schema 校验的参数。
- Engine capability/version。

Compiler 输出必须先通过 Project Pydantic 图校验，再经 Commit Boundary 写入。

### 14.3 实现前置

在 Phase 4 之前必须完成：

- Audio Element 混音、gain、pan 和时间范围执行。
- Transition Element 的至少 cut、crossfade 和 duration 执行。
- Overlay 对 R2V/Edit render source 的统一合成。
- 多 Overlay、Audio 和 Transition 的冲突诊断。
- Engine capability/version health probe。
- Timeline render 的回归和 golden tests。

没有这些能力时只能发布“可检索、不可执行”的 Effect Bundle Preview，不得标记 `compatibility_status=compatible`。

---

## 15. UI 与用户体验

### 15.1 三层浏览

当前 Assets 页面扩展为：

- **Project Kit**：当前制作已选和已批准资产。
- **My Library**：当前本地用户长期保留的资产。
- **Global Catalog**：随当前 Creator 程序版本安装的只读内置库存。

### 15.2 Agent-first 体验

主要入口仍是对话：

- “给结尾加掌声。”
- “以后都用这个 Logo。”
- “把品牌片头更新成最新版。”
- “不要再用这个模板。”

Agent 自动执行允许的检索和维护，并返回简短结果：

- 使用了什么。
- 来自哪个 scope。
- 是否固定到 Project。
- 是否有许可证或兼容性限制。
- 是否产生需要审批的 Proposal。

用户不需要先打开 Catalog 手工操作。

### 15.3 Catalog 面板

面板用于可见性、检查和高级控制，而不是日常工作的必经路径。每个候选展示：

- Preview。
- 名称、类型、时长、尺寸或格式。
- 来源 scope 和 Provider。
- 许可证、兼容性和安全状态。
- 匹配原因和用途。
- 是否已在 Project Kit。
- 精确版本和弃用状态。
- 最近一次 Agent 验证和维护摘要。

### 15.4 Agent Activity 与 Review

提供：

- 最近自动导入、升级、修复、弃用和替代记录。
- 等待审批的 Proposal。
- 可撤销的 Project Kit 选择。
- Catalog 变更影响范围。
- “为什么 Agent 选择这个资产”的解释。

对可恢复的 Project 选择提供撤销；对已发布 Catalog immutable version 不提供内容覆盖式撤销，只能发布新版本或改变生命周期状态。

---

## 16. 可观测性与审计

每次 Retrieval 或 Stewardship Run 至少记录：

- 原始用户目标或后台 trigger ref。
- 结构化 Retrieval/Stewardship Job。
- Trusted Principal、Authority Envelope ref 和 scope。
- Provider、capability 和版本。
- 候选数、过滤数、去重数和最终选择数。
- 权限、许可证、格式和兼容性过滤原因。
- 排名信号摘要。
- Agent 的选择/变更理由和 confidence。
- Tool Calls、idempotency keys、Project base/result generation。
- Library Version → Project Version provenance。
- 自动应用或转为 Proposal 的 Policy Guard 决策。

不向用户可见 trace 暴露无权候选的名称、ID 或数量。

结果状态：

- `success`
- `partial`
- `no_candidates`
- `permission_denied`
- `license_blocked`
- `incompatible`
- `materialization_failed`
- `waiting_for_review`
- `degraded`

---

## 17. 分阶段实现

### Phase 0：边界修正与 schema v3

- 明确 `ground_prompt_context` 的真实副作用，推荐拆分 visual promotion。
- 定义 `ExactProjectVersionRef`、`ProjectAssetPin` 和 `ProjectAssetKit`。
- 升级 Project schema v3，注册 v2 → v3 migration。
- 更新 Pydantic schema prompt、TypeScript contract、fixtures 和 tests。
- 定义 Retrieval Job、Asset Candidate、Stewardship Job、Authority Envelope 和 Proposal contract。
- 定义 Asset Manager 领域服务边界、Agent-facing Tool 权限矩阵和 current Specialist 接入方式。

验收重点：旧 Project 可确定性迁移；Project Kit 与 Asset Index 权威不混淆。

### Phase 1：Agentic Project Asset Retrieval

- 实现 `project_assets` Provider，同时检索 Source 和 Artifact Version。
- 实现 Project-scoped Asset Manager 服务适配层。
- 新增 `retrieve_assets`、`inspect_asset`、`register_asset_version`、`update_asset_metadata` 和 `record_asset_usage` Tool；写能力按角色和 target scope 裁剪。
- 把工具直接加入 Creator、Visual Development、R2V 和 AI Editing 的相应 tool manifest；Source Intelligence 保持现有最小权限。
- Agent 在对话中自动复用 Project Kit/Project existing。
- Project 内 generated/linked 资产可自动加入 Kit，不产生重复版本。
- UI 增加 Project Kit 和 Agent 选择说明。

验收重点：用户只说创作目标即可复用已有资产，无需手工管理。

### Phase 2：随程序发布的 Global Catalog

- 在 Creator 安装包中增加只读 `builtin-assets/catalog.json` 和 payload。
- 支持图片、视频、音频和简单效果文件。
- 实现 `builtin_catalog` Provider 和显式 materialization Tool。
- 让同一 Asset Manager Tool Surface 支持受权限约束的 Global Catalog 读取和 Project 物化。
- 启动和程序升级时验证 manifest/checksum，并重建 Runtime 下的 Preview、Embedding 和 Search Index。
- Global 版本、许可证和 replacement 只通过仓库/程序发布流程更新；Runtime Agent 无写权限。
- 建立 Library Ref → Project Version 的稳定映射。

验收重点：Global Asset 随程序离线可用、不被检索即导入、不能被 Runtime 修改；升级后新 manifest 自动生效且不改变既有 Project pin。

### Phase 3：本地 User Library

- 单机模式先实现隐式 User Library、收藏、偏好和 Project → User 自动保留策略。
- Owner scope 固定为 `user:local`，不接受请求或 Tool 参数覆盖。
- 增加 Collection、blocked、replacement 和许可证约束。
- 支持 Project → User 自动保留；User 资产进入 Global 必须走仓库/程序发布流程，不由 Runtime 提交跨 scope Proposal。

验收重点：Agent 自动整理个人资产；用户无需手工维护；本地 User 数据不会被误当作 Global 发布。

### Phase 4：Timeline Renderer 与 Effect Bundle

- 先实现 Audio、Transition、Overlay 基础执行。
- 定义 Effect Bundle Schema 和 allowlisted compiler capability。
- 支持依赖物化、参数校验、Preview、fallback 和 Element 编译。
- 接入执行授权、Engine health 和 render diagnostics。

验收重点：Bundle 确定性编译为 Timeline Elements，不运行任意代码，缺依赖时可诊断。

### Phase 5：语义检索与持续自治

- 增加多模态 Embedding 和结构化排名。
- 根据 Project strategy、Timeline Elements、R2V Shots、情绪、时长和品牌连续性推荐资产。
- 使用历史选择和拒绝信号，但不跨 scope 泄漏行为数据。
- 增加定时健康、增量 Provider sync、自动 metadata repair 和 replacement proposal。
- 根据策略自动构建 Project Kit candidate，并在允许时自动批准低风险选择。
- 根据真实检索复杂度和运行遥测决定是否启用可选 Asset Librarian；只用于多步检索、批量整理和生命周期规划，不替代直接 Tool 调用。

---

## 18. 验收标准

### 18.1 Agentic 用户体验

- 用户通过自然语言请求资产效果，无需先手工浏览或分类。
- Creator 和相关 Specialist 自动调用 Asset Manager 工具，优先复用 Project Kit。
- 普通检索、物化和登记不需要额外 Specialist Run；复杂多步整理才委派可选 Asset Librarian。
- 允许的 Global/User 资产可自动物化并固定到 Project。
- 用户上传或生成资产后，Agent 自动补齐描述、Tag、Preview 和技术元数据。
- Creator 程序升级后，Global manifest 可被自动验证和重新索引。
- 只有许可证、权限、高影响替换或安全异常需要用户/管理员介入。
- Agent 的重要选择和自动维护动作可解释、可追踪。

### 18.2 功能

- 同一用户目标可以获得 Web `visual_sources` 和本地 `asset_sources`，但 Tool/结果语义清晰。
- 本地音效不进入 `facts` 或 `visual_sources`。
- Project Kit、Project existing、User、Global 按策略检索。
- 无权限或许可证不允许的候选不会出现在结果中。
- Catalog Search 不修改 Project。
- 选择后生成精确 Project Source/Artifact Version 和 ProjectAssetPin。
- 重复选择同一 Library Version 幂等。
- 上层版本升级不改变既有 Project。
- 多 payload Bundle 有完整 Project mapping 和 resolved dependencies。
- Project export（实现后）包含所有已用 payload、精确版本、manifest、provenance 和许可证快照。

### 18.3 Agent 权限与安全

- Tool 参数不能伪造 Principal 或扩展 Authority。
- Runtime Agent 不能把 Project/User Asset 发布到 Global，也不能修改 Global authority。
- Agent 不能伪造许可证、所有权或扫描结果。
- 不接受裸绝对路径和未验证 `file://`。
- Effect Bundle 不执行任意代码。
- 所有物化文件经过 checksum、size 和媒体类型校验。
- 跨 scope Retrieval、Preview 和 hash 不泄漏私有资产。
- Import、Render、Publish 分别执行权限和许可证检查。
- 高影响自动更新超过 budget 时转为 Proposal。

### 18.4 可复现性与恢复

- Project 永远固定精确版本。
- Catalog 删除或 Provider 暂时不可用不破坏合法的已物化 Project。
- Materialization 失败不会产生引用缺失文件的 Project Index。
- 中断的 Catalog Job、staging 和 Proposal 可恢复或明确失败。
- Agent retry 使用稳定 idempotency key，不重复创建版本或 pins。

### 18.5 可观测性

- Retrieval trace 可以解释为何检索、过滤和选择。
- Stewardship trace 可以解释为何更新、发布、弃用或申请审批。
- Library Version 与 Project Version provenance 可追踪。
- 部分 Provider 失败时返回 `partial` 或 `degraded`，不伪装完整成功。
- 用户可看到与自己相关的 Agent Activity，但看不到无权 scope 的候选信息。

---

## 19. 最终建议

采用以下产品与执行边界：

```text
Repo / program release       Uploads / generations / usage feedback
          │                                  │
          ▼                                  ▼
Read-only Global Catalog             Local Asset Steward
          │                                  │
          └──────────────┬───────────────────┘
                         ▼
        Creator Agent + current Specialists
        (+ optional Asset Librarian for complex curation)
                         │
                         ▼
             Agent-facing Asset Manager Tools
                         │
              Deterministic Policy Guard
                         │
                  Asset Manager Service
                         │
                ┌────────┴────────┐
                ▼                 ▼
          My Library         Project Kit
                                  │
                                  ▼
                     Exact Project Asset Versions
                                  │
                                  ▼
                    Timeline / R2V / Edit / Render
```

Global Catalog 提供随程序离线可用的标准库存，My Library 保存 Runtime 管理的长期个人资产，Project Kit 解决当前制作的确定性和连续性。

用户不应成为 Catalog 的日常维护者。Creator Agent、当前 Specialist 和 Local Asset Steward 应在每次对话、上传和生成中通过 Asset Manager Tools 主动完成 User/Project 常规工作；程序升级时自动验证和重新索引 Global。可选 Asset Librarian 只承接真正需要独立多步推理的整理任务。确定性 Policy Guard 限制所有调用方的权限、预算和风险。Global 内容的新增或变更属于仓库/程序发布工作，不属于 Runtime Agent 权限。

实现上应先建设 Project Kit、Asset Manager 服务边界、Agentic Project Retrieval 和清晰的只读/写 Tool Surface，再把这些工具接入现有 Creator/Specialist，之后加入 Global/User Catalog。不要把 Asset Librarian 设为资产访问的必经层，也不要为当前不存在的 Organization 概念预建 scope、ACL 或目录结构。Effect Bundle 必须建立在真实可用的 Audio、Transition 和 Overlay renderer 之上。
