# 真实调用测试项目 WT4 · 自我审阅模块（隔离栈 8094）

## 工作信息（无背景也能开测）

| 项 | 值 |
|---|---|
| 所属任务 | WT4 · 成片自我审阅模块（开发派发单 `dispatch/WT4-self-review.md`） |
| 分支 / worktree | `feat/creator-self-review` · `/Users/linxuanrui/Documents/projects/Project-Creator/QwenPaw/.worktrees/creator-self-review` |
| 被测代码 | `plugins/apps/qwenpaw-creator/backend/services/render_review/`；总开关 env `CREATOR_SELF_REVIEW_ENABLED`（默认关，无任何前端配置入口，需在启动环境中设为 true） |
| 测试实例 | 浏览器 `http://127.0.0.1:8094/` → Apps → QwenPaw Creator |
| 启动/停止 | `cd <worktree> && CREATOR_SELF_REVIEW_ENABLED=true ./dev-isolated.sh start`（先 build；B4 需去掉该 env 重启对照） |
| 数据根 | `QWENPAW_WORKING_DIR=~/.qwenpaw-review`（勿动主实例 `~/.qwenpaw-poc`/8088） |
| 模型凭据 | `~/.qwenpaw-review/creator-runtime/config/model_config.json`（自主实例同路径复制；解密设 `QWENPAW_KEYRING_ACCOUNT`）；需 VLM + TTS + 图像/视频模型已配置（跑完整 compose 链路） |
| 审阅报告位置 | `~/.qwenpaw-review/creator-runtime/…/runtime/render-review/{video_id}/round-N.json`（本期无前端面板，看文件） |
| manual pytest | `cd <worktree>/plugins/apps/qwenpaw-creator/backend && pytest -m manual_real tests/render_review/test_eval_set.py -v` |
| 背景文档 | 总方案 `MM_PLUGINS_INTEGRATION_PLAN.md` §三 WT4 节 |

> 真实模型调用：VLM 多图评审（`creator_vlm_model`，**须配置为阿里系百炼 VLM**，
> 如 qwen-vl 系列——真实验证范围仅限百炼模型）。评测集每例一次多图调用 +
> 完整链路每轮一次；全套约 20–30 次 VLM 调用。运行前设
> `CREATOR_SELF_REVIEW_ENABLED=true`。完整链路中的配音为 qwen3-tts（百炼，
> 属真实验证范围），E4 配音维度的判定依赖 TTS 旁白轨存在。

## 全局测试准则（每个 case 强制）
质量验证看实际内容（证据帧要人工打开对照）；UI 层只经前端操作；发现 bug 才下钻。

## 评测集准备（一次性，`backend/tests/fixtures/render_review/`）
| 编号 | 素材 | 制作方式 | 期望六维结论（写入 expected.json） |
|---|---|---|---|
| E0 | 正常成片 ×2 | 取历史项目合格成片 | 全维 pass，verdict=pass |
| E1 | 黑帧片 | ffmpeg 在中段插入 2s 黑场 | 工程正确性 fail（evidence 落在黑场时间窗） |
| E2 | 字幕溢出片 | 手改字幕文本超长导致溢出/压框 | 字幕 fail |
| E3 | 音画错位片 | ffmpeg 音轨整体偏移 +2s | 配音 fail |
| E4 | 配音缺失片 | 剥掉 TTS 旁白轨 | 配音 fail（静音段检出） |
| E5 | 节奏拖沓片 | 单镜头拉长至 3 倍时长 | 节奏 fail 或 major 建议（允许 minor 容差） |

## A. 代码层真实调用测试（后端）
落点：`backend/tests/render_review/test_eval_set.py`（`@pytest.mark.manual_real`）。

| # | case | 断言 |
|---|---|---|
| A1 | 评测集全量真实 VLM 跑 | E1–E5 缺陷**零漏报**；E0 误报 ≤1 项/例；每个 fail finding 的 `evidence_timestamp_ms` 处抽帧人工确认确实是该缺陷 |
| A2 | 报告 schema | 全部输出可被 RenderReviewReport 校验；报告落 `runtime/render-review/` |
| A3 | 迭代终止 | 对 E2 连续跑 3 轮（不修复）| 第 3 轮后停止，verdict 仍 revise，不阻塞 |
| A4 | prompt 回归基线 | 记录本轮通过率快照 | 后续任何 prompt 修改必须重跑全集且不劣化 |

## B. 前端真实使用测试（UI）
1. 开启开关重启隔离栈。
2. 完整跑一条「创意生成」短剧（≤3 镜控成本，含 TTS 配音）至 compose。
3. 查看审阅报告（runtime 目录 / trace，本期无前端面板）：核对结论与成片实际质量。
4. 人为制造缺陷复跑：在时间轴删除旁白音频后重新 compose → 观察 revise 反馈进入
   剪辑专家回合、给出修订建议。

## C. UI Case 清单
| # | 操作 | 期望 | 验证方法 |
|---|---|---|---|
| B1 | 正常片 compose | 审阅 verdict=pass，无无效迭代 | 读报告 + 抽帧对照 |
| B2 | 缺陷片 compose | verdict=revise，配音维度 fail，AI_EDITING_DIRECTOR 收到结构化反馈并产出修订动作 | 会话内可见修订回合；对照报告 suggestion 是否可执行 |
| B3 | 3 轮上限 | 反复不修复时最多 3 轮后交付不阻塞 | 计数回合 |
| B4 | 开关关闭 | 关闭后同项目 compose 无任何审阅行为、耗时无明显增加 | 对照 trace 无 render_review 事件 |
| B5 | 素材剪辑链路 | 「素材剪辑」项目同样触发审阅 | 同 B1 标准 |

## 通过标准
A1–A4、B1–B5 全过；评测集通过率数据（漏报/误报统计）与最终 prompt 版本一并回填
总方案 WT4 节。
