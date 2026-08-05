# 真实调用测试项目 WT5 · 生成侧三件套（隔离栈 8095）

## 工作信息（无背景也能开测）

| 项 | 值 |
|---|---|
| 所属任务 | WT5 · 图像 edit/translate + 视频模式矩阵 + 数字人（开发派发单 `dispatch/WT5-gen-providers.md`） |
| 分支 / worktree | `feat/creator-gen-providers`（**基于 feat/creator-tts-voice 拉出**）· `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/.worktrees/creator-gen-providers` |
| 被测代码 | `plugins/apps/qwenpaw-creator/backend/models/`（image/、video_model.py、s2v_model.py）+ ModelConfigModal image/video/s2v 区块；**另含基线自带的 TTS 全套能力（已完成，本次作基线回归，见 5t 组）** |
| 测试实例 | 浏览器 `http://127.0.0.1:8095/` → Apps → QwenPaw Creator |
| 启动/停止 | `cd <worktree> && ./dev-isolated.sh build && ./dev-isolated.sh start`（status / stop / verify） |
| 数据根 | `QWENPAW_WORKING_DIR=~/.qwenpaw-gen`（勿动主实例 `~/.qwenpaw-poc`/8088） |
| 模型凭据 | `~/.qwenpaw-gen/creator-runtime/config/model_config.json`（自主实例同路径复制；解密设 `QWENPAW_KEYRING_ACCOUNT`）；需图像/视频（token-portal）/TTS/S2V 全部已配置 |
| ❗ 费用 | 本套最贵：每条视频/数字人生成前逐条确认；先跑零成本健康检查（A6）再计费 |
| manual pytest | `cd <worktree>/plugins/apps/qwenpaw-creator/backend && pytest -m manual_real tests/manual/test_real_gen_providers.py -v`（可按 5a/5b/5c 分组跑） |
| 背景文档 | 总方案 `MM_PLUGINS_INTEGRATION_PLAN.md` §三 WT5 节 |

> **本 WT 是全套测试中费用最高的**：视频生成按条计费。执行原则：① **真实调用
> 仅限阿里系百炼模型**（happyhorse / wan / qwen-image / qwen-mt-image /
> qwen3-tts / wan2.2-s2v），**seedance2（火山引擎）与 OpenAI 兼容 image 不做
> 真实调用**，只测本地校验拒绝与配置回显；② 每个计费 case 前先跑零成本健康
> 检查端点验证模型名可用；③ 视频统一 5s / 720P 最低档；④ 每次高消费调用
> **事先逐条口头确认**；⑤ 生成结果必须**读图/读帧**判断语义正确。
> 真实 Key：`creator_image_model` / `creator_video_model`（token-portal）/
> `creator_s2v_model` / `creator_tts_model`（均为百炼/DashScope 协议）。

## 全局测试准则（每个 case 强制）
读实际内容验证；UI 只经前端；**分镜图必须以资产图为参考输入，不允许跳步**；
发现 bug 才下钻。

## A. 代码层真实调用测试（后端）
落点：`backend/tests/manual/test_real_gen_providers.py`（`@pytest.mark.manual_real`，
按 5a/5b/5c 分类，逐 case 独立可跑）。

### 5a 图像
| # | case | 输入 | 断言（读图） |
|---|---|---|---|
| A1 | 文生图基线 | 「戴红围巾的橘猫，水彩风」 | 图为橘猫+红围巾+水彩风 |
| A2 | image_edit 合成 | A1 猫图 + 一张埃菲尔铁塔图 +「让这只猫坐在铁塔前的草地上」 | 合成图同时含该猫（特征一致）与铁塔，构图自然 |
| A3 | image_edit 局部修 | A1 图 +「把围巾改成蓝色」 | 仅围巾变蓝，猫其余特征不变 |
| A4 | image_translate | 一张含中文标题的海报图 →「翻译为英文」 | 文字变英文且**排版/背景保留** |
| A5 | 校验（无真实调用） | OpenAI provider + mode=edit | 拒绝并给可读错误（OpenAI 不在百炼验证范围，此 case 仅本地校验） |

### 5b 视频模式矩阵
| # | case | 输入 | 断言（读帧） |
|---|---|---|---|
| A6 | 健康检查 ×4 | happyhorse 基名派生的 -t2v/-i2v/-r2v/-video-edit 四模型名 | 零成本端点均确认可用（否则先修派生规则再继续） |
| A7 | happyhorse t2v | 「海浪拍打礁石，日落，5 秒」 | 内容为海浪礁石日落；时长≈5s |
| A8 | happyhorse i2v | A1 猫图作首帧 +「猫转头看向镜头」 | 首帧与输入图一致，动作发生 |
| A9 | happyhorse video_edit | 一段 10s 素材 +「转为水墨画风格」 | 输出保留原内容结构、风格变化；时长跟随输入（≤15s 截断行为记录） |
| A10 | wan t2v | 同 A7 prompt | 生成成功（协议正确） |
| A11 | 矩阵拒绝（无真实调用） | wan + video_edit；seedance2 + t2v | 校验层拒绝，错误信息提示可用替代 |
| A12 | seedance2 范围声明 | — | **不做真实调用**（非百炼）：仅验证其配置保存回显不受本 WT 影响，及 t2v/i2v/video_edit 均被校验层拒绝；r2v 现状路径不新增验证 |

### 5c 数字人
| # | case | 输入 | 断言 |
|---|---|---|---|
| A13 | detect 通过 | 单人正脸角色图 | passed=true；**未创建执行授权、无计费** |
| A14 | detect 拒绝 | 侧脸/多人图 | passed=false + 可读原因；无授权无计费 |
| A15 | generate | A13 图 + 5s TTS 音频 / 480P | 读帧：口型随音频运动、人物为输入角色；时长≈音频长 |

### 5t · TTS 基线回归（已完成能力，验证未被本 WT 破坏且与新能力正确衔接）
> TTS 来自基线分支 feat/creator-tts-voice（详见 dispatch/WT5「基线自带的 TTS
> 能力」节）；模型 qwen3-tts-flash / qwen3-tts-vc-2026-01-22，按字符计费（便宜）。

| # | case | 输入 | 断言（听音/读数据） |
|---|---|---|---|
| T1 | 基础合成 | 一段 ≤800 字符中文旁白 + 默认音色 | 音频可播、内容与文本一致、无截断；返回 durationSeconds 与实际播放时长一致（WAV header 校正生效，误差 <0.5s） |
| T2 | 超长拒绝 | >800 字符文本 | 拒绝且错误信息提示按句拆分 |
| T3 | 音色设计（voicePrompt） | 角色实体 +「低沉沙哑的中年男声，语速缓慢」 | 复刻成功绑定角色；试听音色与描述相符（人耳判断） |
| T4 | 音色复刻（样本） | 10–20s 已有音频 version 作样本 | 复刻音色与样本听感接近 |
| T5 | 角色音色自动选用 | T3 角色的 characterRef 再调 tts_generation | 自动用复刻音色（与 T1 默认音色听感明显不同） |
| T6 | 既有测试全绿 | `pytest tests/models/test_tts_model.py tests/media/test_audio_mixing.py tests/services/test_tts_specialist_tools.py tests/prompts/test_tts_prompt_guidance.py` | 在本 WT 最终 commit 上全部通过（新代码未破坏 TTS） |
| T7 | TTS→S2V 衔接 | T5 产出的 audio version 作 A15 的 audioAssetRef | 数字人口型跟复刻音色音频同步（与 A15 合并执行） |

## B. 前端真实使用测试（UI，完整数据流）
按准则走全流程：**剧本 → 资产（角色锚点图+场景图）→ 分镜图（以资产图为参考）→
视频**，不跳步：
1. 新建创意项目（2 镜小短剧控成本）→ 生成剧本 → 生成角色/场景资产图（读图确认
   角色一致性）。
2. VISUAL_DEVELOPMENT 流程中用 **edit 模式**修正一张分镜图局部（换天色）→ 读图
   确认仅局部变化、角色未漂移。
3. 视频生成：镜 1 用默认 r2v（资产图+分镜图参考）；镜 2 切 **happyhorse t2v**；
   执行授权弹窗逐条确认费用。
4. 数字人与 TTS 链路（基线能力 + 新能力衔接）：会话中用 voicePrompt 为角色
   设计专属音色（create_character_voice）→ 用该角色 characterRef 生成一段旁白
   （tts_generation，验证自动选用复刻音色）→ `s2v_generation` 出数字人镜头；
   另把该旁白铺上 Timeline，验证 ElementDetail 显示音色/模型/时长、compose 后
   旁白窗口内原片音量自动 ducking。
5. 尝试在 wan 模型下选 video_edit → 预期被拒且提示换 happyhorse。

## C. UI Case 清单
| # | 期望 | 验证方法 |
|---|---|---|
| B1 | 资产→分镜→视频链路完整，分镜确以资产图为参考（角色一致） | 逐图/逐帧对照角色特征 |
| B2 | edit 模式改图仅局部变化 | 前后图对比 |
| B3 | 双引擎切换生效（r2v 与 t2v 产物都正确对应各自分镜语义） | 读帧 |
| B4 | 能力矩阵错误提示准确 | 目视 |
| B5 | 数字人镜头口型同步、音色为复刻音色 | 播放对照 TTS 原音频 |
| B6 | 每个计费操作都出现执行授权且费用预估合理（含 TTS/复刻） | 目视记录 |
| B7 | TTS UI 面完整：ElementDetail 音频详情（音色/模型/时长/文本预览）、AssetsPage 音频类目可预览 | 目视 + 试听 |
| B8 | compose 成片中旁白清晰、旁白期间原片声被压低（ducking 生效）、旁白前后音量恢复 | 听成片对应时段 |

## 通过标准
A1–A15（A5/A11/A12 为本地校验无真实调用）、T1–T7（TTS 基线回归，百炼模型真实
调用）、B1–B8 全过；happyhorse 模型名派生实测结论、TTS 回归结论与各 case 实际
费用记录回填总方案 WT5 节（seedance2 本期不验证无需补格）。
