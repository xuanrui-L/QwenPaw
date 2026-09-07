import { describe, expect, it } from "vitest";
import type {
  CreatorEvent,
  CreatorMessage,
  ProjectDocument,
} from "@/contracts/creator";
import {
  actionAwareConversationContent,
  actionEnvelopeFromStreamText,
  conversationContent,
  creatorActionEnvelope,
  deduplicateReviewFeedbackMessages,
  isReviewFeedbackMessage,
  isUserAuthorityMessage,
  publicAssistantText,
  shouldRenderConversationMessage,
  toolCallPresentations,
} from "@/lib/creatorMessagePresentation";
import { taskErrorMessage, taskProgressPercent } from "@/lib/taskPresentation";
import i18n from "@/i18n";
import {
  creatorToolLabel,
  getToolRunningLabel,
} from "@/lib/creatorPresentation";

function creatorMessage(overrides: Partial<CreatorMessage>): CreatorMessage {
  return {
    messageId: "message-1",
    messageSeq: 1,
    role: "user",
    content: [{ type: "text", text: "消息" }],
    source: "user",
    metadata: {},
    createdAt: "now",
    ...overrides,
  };
}

function creatorEvent(overrides: Partial<CreatorEvent>): CreatorEvent {
  return {
    eventId: "event-1",
    seq: 1,
    type: "agent.tool_started",
    projectId: "p1",
    creatorSessionId: "s1",
    at: "now",
    data: {},
    ...overrides,
  };
}

function text(value: string): CreatorMessage["content"] {
  return [{ type: "text", text: value }];
}

const tev = (seq: number, type: string, data: Record<string, unknown>) =>
  creatorEvent({ eventId: `event-${seq}`, seq, type, data });

const actionMeta = (tool: string, args: Record<string, unknown>) => ({
  actionId: "action-1",
  parsedAction: { action: "tool_call", tool, arguments: args },
});

describe("Creator conversation presentation", () => {
  const versionProject = {
    assets: {
      artifact_versions_by_id: {
        "artifact-version-final-known": { name: "找钥匙 · 成片" },
      },
      source_versions_by_id: {
        "source-version-known": { name: "走廊参考素材" },
      },
    },
  } as unknown as ProjectDocument;

  it("replaces only complete known artifact/source inline code tokens in assistant answers", () => {
    const body =
      "成片文件：`artifact-version-final-known`\n参考素材：``source-version-known``";
    const expected = "成片文件：找钥匙 · 成片\n参考素材：走廊参考素材";
    expect(
      publicAssistantText(body, { project: versionProject, streaming: false }),
    ).toBe(expected);
    expect(
      conversationContent(
        creatorMessage({ role: "assistant", content: text(body) }),
        versionProject,
      ),
    ).toEqual(text(expected));
    expect(
      conversationContent(
        creatorMessage({ role: "user", content: text(body) }),
        versionProject,
      ),
    ).toEqual(text(body));
  });

  it.each([
    "`artifact-version-unknown`",
    "artifact-version-final-known",
    "`artifact-version-final-known_extra`",
    "`artifact-version-final-known + source-version-known`",
    "` artifact-version-final-known `",
    "`https://example.com/artifact-version-final-known`",
    "https://example.com/`artifact-version-final-known`",
    "[下载](https://example.com/`artifact-version-final-known`)",
    "\\`artifact-version-final-known\\`",
    "```text\n`artifact-version-final-known`\n```",
    "~~~text\n`artifact-version-final-known`\n~~~",
    "````text\n```\n`artifact-version-final-known`\n````",
    "> ```text\n> `artifact-version-final-known`\n> ```",
    "- ```text\n  `artifact-version-final-known`\n  ```",
    "示例：\n\n    `artifact-version-final-known`",
  ])(
    "preserves unknown versions, code expressions and protected Markdown: %s",
    (body) => {
      expect(
        publicAssistantText(body, {
          project: versionProject,
          streaming: false,
        }),
      ).toBe(body);
    },
  );

  it("does not resolve a version from another or unloaded project's snapshot", () => {
    const body = "成片：`artifact-version-final-known`";
    expect(publicAssistantText(body, { streaming: false })).toBe(body);
    expect(
      publicAssistantText(body, {
        project: { assets: {} } as ProjectDocument,
        streaming: false,
      }),
    ).toBe(body);
  });

  it.each([
    [
      {
        status: "BLOCKED",
        items: [{ status: "BLOCKED", reason: "WAITING_REVIEW" }],
      },
      "waiting_review",
    ],
    [
      {
        status: "BLOCKED",
        items: [{ status: "BLOCKED", reason: "EXECUTION_NOT_COMPLETED" }],
      },
      "unconfirmed",
    ],
    [
      {
        status: "BLOCKED",
        items: [{ status: "BLOCKED", reason: "INPUTS_NOT_READY" }],
      },
      "not_started",
    ],
    [
      { status: "BLOCKED", items: [{ status: "BLOCKED", reason: "RUNNING" }] },
      "unconfirmed",
    ],
    [
      {
        status: "BLOCKED",
        items: [
          { status: "BLOCKED", reason: "WAITING_REVIEW", taskId: "real-task" },
        ],
      },
      "unconfirmed",
    ],
    [
      {
        status: "PARTIAL",
        items: [
          { status: "SUCCEEDED", taskId: "real-task" },
          { status: "BLOCKED", reason: "WAITING_REVIEW" },
        ],
      },
      "incomplete",
    ],
    [
      {
        status: "COMPLETED",
        items: [{ status: "SUCCEEDED", taskId: "real-task" }],
      },
      undefined,
    ],
  ])(
    "keeps a finished production request distinct from media completion: %j",
    (result, outcome) => {
      const message = creatorMessage({
        source: "runtime_action_result",
        content: text(JSON.stringify(result)),
        metadata: {
          actionId: "production-request",
          tool: "request_workgraph_execution",
        },
      });
      const [call] = toolCallPresentations([message], []);
      expect(call.status).toBe("succeeded");
      expect(call.executing).toBe(false);
      expect(call.productionOutcome).toBe(outcome);
      const [ordinary] = toolCallPresentations(
        [
          {
            ...message,
            metadata: { ...message.metadata, tool: "read_project" },
          },
        ],
        [],
      );
      expect(ordinary.productionOutcome).toBeUndefined();
    },
  );

  it("settles reused authorization ids across calls and stops a superseded main run", () => {
    const events = [
      tev(1815, "agent.tool_started", {
        toolCallId: "old",
        runId: "old-run",
        tool: "request_workgraph_execution",
      }),
      tev(1819, "execution.authorization_required", {
        toolCallId: "old",
        authorizationId: "scene",
      }),
      tev(1820, "execution.authorization_required", {
        toolCallId: "old",
        authorizationId: "key",
      }),
      tev(1822, "agent.run.cancelled", { runId: "old-run", superseded: true }),
      tev(1881, "agent.tool_started", {
        toolCallId: "new",
        runId: "new-run",
        tool: "request_workgraph_execution",
      }),
      tev(1882, "execution.authorization_required", {
        toolCallId: "new",
        authorizationId: "scene",
      }),
      tev(1883, "execution.authorization_required", {
        toolCallId: "new",
        authorizationId: "key",
      }),
      tev(1884, "execution.authorization_decided", {
        toolCallId: "new",
        authorizationId: "scene",
        status: "APPROVED",
      }),
    ];
    let calls = toolCallPresentations([], [...events].reverse());
    expect(calls[0]).toMatchObject({
      status: "cancelled",
      superseded: true,
      executing: false,
    });
    expect(calls[0].waitingAuthorization).toBeUndefined();
    expect(calls[1].waitingAuthorization).toBe(true);
    calls = toolCallPresentations(
      [],
      [
        ...events,
        tev(1885, "execution.authorization_decided", {
          toolCallId: "new",
          authorizationId: "key",
          status: "APPROVED",
        }),
      ],
    );
    expect(calls.every((call) => !call.waitingAuthorization)).toBe(true);
    // Even when the run-terminal event is outside a recovered page, the
    // durable decision clears the old call's reference to the same approval.
    expect(
      toolCallPresentations(
        [],
        events
          .filter((e) => e.seq !== 1822)
          .concat(
            tev(1885, "execution.authorization_decided", {
              authorizationId: "key",
            }),
          ),
      )[0].waitingAuthorization,
    ).toBeUndefined();
  });

  it("keeps a parallel production request waiting until every actual authorization is decided", () => {
    const events = [
      tev(1, "agent.tool_started", {
        toolCallId: "production",
        tool: "request_workgraph_execution",
      }),
      tev(2, "execution.authorization_required", {
        toolCallId: "production",
        authorizationId: "image-a",
      }),
      tev(3, "execution.authorization_required", {
        toolCallId: "production",
        authorizationId: "image-b",
      }),
      tev(4, "execution.authorization_decided", {
        toolCallId: "production",
        authorizationId: "image-a",
        status: "APPROVED",
      }),
    ];
    expect(toolCallPresentations([], events)[0].waitingAuthorization).toBe(
      true,
    );
    const decided = tev(5, "execution.authorization_decided", {
      toolCallId: "production",
      authorizationId: "image-b",
      status: "REJECTED",
    });
    expect(
      toolCallPresentations([], [decided, ...events, events[1]])[0]
        .waitingAuthorization,
    ).toBeUndefined();
    expect(
      toolCallPresentations(
        [],
        [
          ...events,
          tev(6, "agent.tool_completed", { toolCallId: "production" }),
        ],
      )[0].waitingAuthorization,
    ).toBeUndefined();
  });

  it("keeps the real drama content while hiding schema narration and resolving exact object ids", () => {
    const project = {
      visual: {
        entities: { items: { "char:woman": { name: "找钥匙的女子" } } },
      },
      timelines: {
        items: {
          main: {
            elements_by_id: { shot1_search: { label: "焦急翻包找钥匙" } },
          },
        },
      },
    } as unknown as ProjectDocument;
    const body =
      "Now let me write the Variant prompts.\n\n| char:woman | 灰色外套 |\n| **shot1_search** | 有意静默 |\n\n已在 narrative 和 min_dialogue_ratio=0 中标注。\n\n台词：原来在这儿。";
    expect(publicAssistantText(body, { project, streaming: false })).toBe(
      "| 找钥匙的女子 | 灰色外套 |\n| **焦急翻包找钥匙** | 有意静默 |\n\n台词：原来在这儿。",
    );
    expect(
      publicAssistantText("Now I need to fix the remaining prompts."),
    ).toBe("");
    const authored =
      "台词：Now let me tell you about shot1_search.\n《shot1_search》\n“char:woman”\n[资料](https://example.com/shot1_search)\nshot1_search_extra";
    expect(publicAssistantText(authored, { project, streaming: false })).toBe(
      authored,
    );
  });

  it("humanizes structure terms and exact segment references observed in real model output", () => {
    const project = {
      timelines: {
        items: {
          "timeline:main": {
            title: "色彩练习",
            elements_by_id: {
              "seg:0-3": { label: "色彩练习 · 前半段" },
              "seg:3-6": { label: "色彩练习 · 后半段" },
            },
          },
        },
      },
    } as unknown as ProjectDocument;
    const body =
      "前半段（0–3s）：`seg:0-3`，截取素材0–3秒。\n两个 Edit Element 首尾衔接，Timeline 已更新，WorkGraph 会更新进展。";
    expect(publicAssistantText(body, { project, streaming: false })).toBe(
      "前半段（0–3s）：色彩练习 · 前半段，截取素材0–3秒。\n两个剪辑片段首尾衔接，视频时间线已更新，制作进度会更新进展。",
    );
    expect(
      publicAssistantText(
        "分析完成后你会收到【系统自动消息 · Runtime 通知】，届时我会读取分析结果并回复你。",
      ),
    ).toBe("分析完成后你会收到进展更新，届时我会读取分析结果并回复你。");
  });

  it("does not rewrite authored dialogue, titles, ordinary words or code examples", () => {
    const script =
      '片名：《The Fifth Element》\n台词：Timeline is the name of my album.\n字幕：WorkGraph\n“Timeline” 是片名。\n```json\n{"title":"Timeline"}\n```\n[文档](https://example.com/Timeline)';
    expect(publicAssistantText(script, { streaming: false })).toBe(script);
  });

  it("hides the observed derived-state explanation and incorrect clearing advice only from assistant narration", () => {
    const observed =
      "镜头2 的 `prompt_sync` 指纹与当前提示词内容不匹配，导致调度器阻塞。清除过期同步记录即可恢复提交。";
    expect(publicAssistantText(observed, { streaming: false })).toBe("");
    expect(
      conversationContent(
        creatorMessage({ role: "assistant", content: text(observed) }),
      ),
    ).toEqual([]);
    expect(
      conversationContent(
        creatorMessage({ role: "user", content: text(observed) }),
      ),
    ).toEqual(text(observed));
    const prose = `请核对镜头内容与两份提示词。\n\n${observed}\n\n清除过期同步记录即可恢复提交。`;
    expect(publicAssistantText(prose, { streaming: false })).toBe(
      "请核对镜头内容与两份提示词。",
    );
    const final = creatorMessage({
      role: "assistant",
      content: [],
      metadata: {
        toolCall: { name: "final", arguments: { message: observed } },
      },
    });
    expect(actionAwareConversationContent(final)).toEqual([]);
  });

  it.each([
    "plan_fingerprint",
    "storyboard_prompt_fingerprint",
    "video_prompt_fingerprint",
  ])("omits ordinary narration of the exact derived field %s", (field) =>
    expect(
      publicAssistantText(`需要重置 ${field} 才能继续制作。`, {
        streaming: false,
      }),
    ).toBe(""),
  );

  it.each([
    "片名：《prompt_sync》\n台词：删除 plan_fingerprint。\n字幕：storyboard_prompt_fingerprint\n旁白：video_prompt_fingerprint",
    "《prompt_sync》是片名，“plan_fingerprint”是角色说出的暗号。",
    '```json\n{"prompt_sync":{"plan_fingerprint":"example"}}\n\n{"storyboard_prompt_fingerprint":"example","video_prompt_fingerprint":"example"}\n```',
    "~~~text\nprompt_sync\n\nplan_fingerprint\n~~~",
    "> ```text\n> prompt_sync\n> ```",
    "示例：\n\n    prompt_sync = { plan_fingerprint: 'example' }",
    "[示例文档](https://example.com/prompt_sync/plan_fingerprint)",
    "prompt_sync_extra 与 custom_plan_fingerprint 是用户作品中的普通文字。",
  ])("preserves authored literals and code sample boundaries: %s", (body) => {
    expect(publicAssistantText(body, { streaming: false })).toBe(body);
  });

  it.each([
    '```json\n{"action":"tool_call","tool":"read_project","arguments":{"path":"private.json"}}\n```',
    '{"action":"tool_call","tool":"read_project","arguments":{"path":"private.json"}}',
    '{"arguments":{"path":"private.json"},"tool":"read_project","action":"tool_call"}',
    '<tool_call><function=read_project><parameter=arguments>{"path":"private.json"}</parameter></function></tool_call>',
  ])(
    "never flashes partial runtime syntax at any stream boundary",
    (payload) => {
      const narration = "我先查看视频方案。";
      for (let index = 1; index <= payload.length; index += 1) {
        expect(
          publicAssistantText(`${narration}\n${payload.slice(0, index)}`),
          `boundary ${index}`,
        ).toBe(narration);
      }
    },
  );

  it("preserves public prose, complete ordinary JSON, and final messages", () => {
    const ordinary =
      '建议采用蓝色。\n```json\n{"color":"blue","duration":3}\n```';
    expect(publicAssistantText(ordinary, { streaming: false })).toBe(ordinary);
    expect(
      publicAssistantText(`${ordinary}\n\n确认后就按这个方向制作。`, {
        streaming: false,
      }),
    ).toBe(`${ordinary}\n\n确认后就按这个方向制作。`);
    expect(
      publicAssistantText('```json\n{"action":"walk","duration":3}\n```', {
        streaming: false,
      }),
    ).toBe('```json\n{"action":"walk","duration":3}\n```');
    expect(
      publicAssistantText(
        '{"color":"blue","caption":"{早晨}"}\n\n采用这个配色。',
        { streaming: false },
      ),
    ).toBe('{"color":"blue","caption":"{早晨}"}\n\n采用这个配色。');
    expect(
      publicAssistantText(
        '视频方案已更新。\n```json\n{"action":"final","message":"请查看第一幕。"}\n```',
      ),
    ).toBe("视频方案已更新。\n\n请查看第一幕。");
    expect(publicAssistantText("## 第一幕\n\n清晨，主角打开窗户。")).toBe(
      "## 第一幕\n\n清晨，主角打开窗户。",
    );
  });

  it("never turns thinking or an internal data structure into public prose", () => {
    const thinking = "<think>需要先读取内部数据</think>已整理好方案。";
    for (let index = 1; index < thinking.indexOf("已整理"); index += 1)
      expect(publicAssistantText(thinking.slice(0, index))).toBe("");
    expect(publicAssistantText(thinking)).toBe("已整理好方案。");
    expect(
      publicAssistantText(
        '已更新视频方案。\n\n{"elements_by_id":{"private-id":{}}}',
      ),
    ).toBe("已更新视频方案。");
    expect(
      publicAssistantText(
        '已更新视频方案。\n\n[RUNTIME_ACTION_RESULT]\n\n{"ok":true}',
      ),
    ).toBe("已更新视频方案。");
  });

  it("keeps native final text safe even when its envelope is supplied by metadata", () => {
    const message = creatorMessage({
      role: "assistant",
      content: [],
      metadata: {
        toolCall: {
          name: "final",
          arguments: { message: "<think>hidden</think>已完成。" },
        },
      },
    });
    expect(actionAwareConversationContent(message)).toEqual(text("已完成。"));
  });

  it("does not treat completed argument streaming as execution, or reopen a completed call", () => {
    const progress = tev(1, "agent.tool_progress", {
      toolCallId: "call-1",
      tool: "read_project",
      complete: true,
      receivedBytes: 100,
    });
    const started = tev(2, "agent.tool_started", {
      toolCallId: "call-1",
      tool: "read_project",
    });
    const lateProgress = { ...progress, eventId: "late", seq: 3 };
    const completed = tev(4, "agent.tool_completed", {
      toolCallId: "call-1",
      tool: "read_project",
    });
    expect(toolCallPresentations([], [progress])[0]).toMatchObject({
      status: "started",
      executing: false,
    });
    expect(
      toolCallPresentations([], [progress, started, lateProgress])[0],
    ).toMatchObject({ status: "started", executing: true });
    expect(
      toolCallPresentations(
        [],
        [progress, started, completed, { ...lateProgress, seq: 5 }],
      )[0],
    ).toMatchObject({ status: "succeeded", executing: false });
    expect(
      toolCallPresentations([], [completed, { ...started, seq: 6 }])[0],
    ).toMatchObject({ status: "succeeded", executing: false });
  });

  it("keeps actual user authority sources and rejects Runtime control rows as user bubbles", () => {
    const userSources = [
      "initial_goal",
      "agent_dock",
      "frontend_action",
      "frontend_manual_edit",
      "user",
      "user_continuation",
      "review_rejection_feedback",
    ];
    const controlSources = [
      "runtime_action_result",
      "runtime_work_update",
      "specialist_result",
      "completion_context",
      "completion_rejected",
    ];
    for (const source of [...userSources, ...controlSources]) {
      const message = creatorMessage({ source });
      const isUser = userSources.includes(source);
      expect(isUserAuthorityMessage(message)).toBe(isUser);
      expect(shouldRenderConversationMessage(message)).toBe(isUser);
    }
  });

  it("renders one review feedback message per durable decision", () => {
    const first = creatorMessage({
      messageId: "feedback-first",
      messageSeq: 4,
      source: "review_rejection_feedback",
      metadata: {
        decisionId: "decision-1",
        rejectionFeedback: {
          action: "UNDO_AND_REGENERATE",
          feedbackNote: "人物状态不对，请保持身份一致",
        },
      },
    });
    const replay = creatorMessage({
      ...first,
      messageId: "feedback-replay",
      messageSeq: 8,
    });
    const unrelated = creatorMessage({ messageId: "ordinary", messageSeq: 9 });

    expect(isReviewFeedbackMessage(first)).toBe(true);
    expect(shouldRenderConversationMessage(first)).toBe(true);
    expect(
      deduplicateReviewFeedbackMessages([replay, unrelated, first]),
    ).toEqual([unrelated, first]);
  });

  it("never renders reserved control markers or legacy file Runtime tool rows", () => {
    const hidden: Array<Partial<CreatorMessage>> = [
      ...(
        [
          ["runtime_action_result", "[CREATOR_ACTION_REJECTED]\n\n必须持久化"],
          ["user", "[RUNTIME_EVENT: CREATOR_WAITING]\n\ninternal"],
          ["user", "[RUNTIME_ACTION_BLOCKED]\n\ninternal"],
          ["user", "USER_HARD_STOP"],
        ] as const
      ).map(([source, body]) => ({ source, content: text(body) })),
      {
        role: "tool" as const,
        source: "file_agent_runtime",
        content: text("读取完成"),
        metadata: { toolCallId: "call-read", toolName: "read_project" },
      },
    ];
    for (const overrides of hidden) {
      expect(shouldRenderConversationMessage(creatorMessage(overrides))).toBe(
        false,
      );
    }
    expect(
      shouldRenderConversationMessage(
        creatorMessage({ role: "tool", source: "another_runtime" }),
      ),
    ).toBe(true);
  });

  it.each([
    "[RUNTIME_EVENT: CREATOR_WAITING]\n\nCreator 已显式等待异步 Run",
    '我会等待当前任务完成。\n[RUNTIME_ACTION_RESULT]\n\n{"ok":true}',
    "我会先处理可恢复问题。\n[RUNTIME_ACTION_BLOCKED]\n\ninternal",
    "USER_HARD_STOP",
    "正在重新确认已有任务。\n这里连续出现了 `[CREATOR_OUTPUT_REJECTED]`，说明内部协议被拒绝。",
  ])("keeps runtime feedback out of public assistant prose: %s", (body) => {
    const message = creatorMessage({
      role: "assistant",
      source: "creator_agent_stream",
      content: text(body),
    });
    expect(conversationContent(message)).toEqual([]);
  });

  it("recognizes a tool action before its SSE JSON is complete and moves syntax into the action card", () => {
    const message = creatorMessage({
      role: "assistant",
      source: "creator_agent_stream",
      content: text(
        '我先读取计划。\n```json\n{"action":"tool_call","tool":"read_project_file","arguments":{"path":"plan',
      ),
      metadata: { streaming: true },
    });

    const envelope = creatorActionEnvelope(message);
    expect(envelope).toMatchObject({
      action: "tool_call",
      tool: "read_project_file",
      complete: false,
      narration: "我先读取计划。",
    });
    expect(envelope?.rawPayload).toContain('"path":"plan');
    expect(actionAwareConversationContent(message, envelope)).toEqual(
      text("我先读取计划。"),
    );
  });

  it("parses streamed function-call parameters before and after completion", () => {
    expect(
      actionEnvelopeFromStreamText(
        '<function=read_project_file><parameter=arguments>{"path":"story/',
      ),
    ).toMatchObject({
      action: "tool_call",
      tool: "read_project_file",
      complete: false,
    });
    expect(
      actionEnvelopeFromStreamText(
        '<function=read_project_file><parameter=arguments>{"path":"story/outline.md"}</parameter></function></tool_call>',
      ),
    ).toMatchObject({
      action: "tool_call",
      tool: "read_project_file",
      complete: true,
      payload: { arguments: { path: "story/outline.md" } },
    });
  });

  it("merges assistant arguments, Runtime result metadata/text and tool events by actionId", () => {
    const messages = [
      creatorMessage({
        messageId: "assistant-1",
        messageSeq: 2,
        role: "assistant",
        source: "creator_agent",
        metadata: actionMeta("read_project_file", { path: "plan.json" }),
      }),
      creatorMessage({
        messageId: "result-1",
        messageSeq: 3,
        source: "runtime_action_result",
        content: text('[RUNTIME_ACTION_RESULT]\n\n{"head":"h2","ok":true}'),
        metadata: { actionId: "action-1", tool: "read_project_file" },
      }),
    ];
    const events = [
      tev(1, "agent.tool_started", { actionId: "action-1" }),
      tev(2, "agent.tool_completed", { actionId: "action-1" }),
    ];
    expect(toolCallPresentations(messages, events)).toEqual([
      {
        actionId: "action-1",
        anchorMessageId: "assistant-1",
        order: 2,
        status: "succeeded",
        executing: false,
        tool: "read_project_file",
        arguments: { path: "plan.json" },
        result: { head: "h2", ok: true },
        error: undefined,
      },
    ]);
  });

  it("surfaces failures from durable result text, event errorType and file-native lifecycle", () => {
    const failed = [
      creatorMessage({
        messageId: "assistant-1",
        role: "assistant",
        metadata: actionMeta("write_file", { path: "x" }),
      }),
      creatorMessage({
        messageId: "result-1",
        messageSeq: 2,
        source: "runtime_action_result",
        content: text("[RUNTIME_ACTION_ERROR]\n\n权限不足"),
        metadata: { actionId: "action-1", tool: "write_file", failed: true },
      }),
    ];
    expect(toolCallPresentations(failed, [])).toMatchObject([
      { status: "failed", tool: "write_file", error: "权限不足" },
    ]);

    const delegate = [
      tev(1, "agent.tool_started", {
        actionId: "d1",
        tool: "delegate_to_agent",
      }),
      tev(2, "agent.tool_completed", {
        actionId: "d1",
        tool: "delegate_to_agent",
        failed: true,
        errorType: "ProjectionInputError",
      }),
    ];
    expect(toolCallPresentations([], delegate)).toMatchObject([
      { actionId: "d1", status: "failed", error: "ProjectionInputError" },
    ]);

    const fileNative = [
      tev(1, "agent.tool.started", {
        toolCallId: "c1",
        toolName: "read_project",
      }),
      tev(2, "agent.tool.failed", {
        toolCallId: "c1",
        toolName: "read_project",
        messageId: "tool-msg-1",
      }),
    ];
    expect(toolCallPresentations([], fileNative)).toMatchObject([
      {
        actionId: "c1",
        anchorMessageId: "tool-msg-1",
        status: "failed",
        tool: "read_project",
      },
    ]);
  });

  it("projects native AgentScope control tool metadata without parsing assistant text", () => {
    const message = creatorMessage({
      role: "assistant",
      source: "creator_agent",
      content: [],
      metadata: {
        actionId: "call-final",
        toolCall: {
          id: "call-final",
          name: "final",
          arguments: { message: "原生工具回复", awaitUserInput: false },
        },
      },
    });
    const envelope = creatorActionEnvelope(message);
    expect(envelope).toMatchObject({
      action: "final",
      syntax: "native",
      payload: { message: "原生工具回复", awaitUserInput: false },
    });
    expect(actionAwareConversationContent(message, envelope)).toEqual(
      text("原生工具回复"),
    );
  });

  it("projects canonical tool arguments, aggregated progress and removes rejected calls", () => {
    const started = [
      tev(1, "agent.tool_progress", {
        messageId: "assistant-native",
        toolCallId: "call-read",
        tool: "read_file",
        receivedBytes: 25_257,
        providerChunkCount: 2_140,
        complete: true,
      }),
      tev(2, "agent.tool_started", {
        messageId: "assistant-native",
        toolCallId: "call-read",
        tool: "read_file",
        arguments: { file_path: "story/outline.md" },
      }),
    ];
    expect(toolCallPresentations([], started)).toMatchObject([
      {
        actionId: "call-read",
        anchorMessageId: "assistant-native",
        status: "started",
        tool: "read_file",
        arguments: { file_path: "story/outline.md" },
        receivedBytes: 25_257,
        providerChunkCount: 2_140,
        argumentStreamComplete: true,
      },
    ]);
    const rejected = [
      ...started,
      tev(3, "assistant.output_rejected", {
        rejectedAssistantMessageId: "assistant-native",
      }),
    ];
    expect(toolCallPresentations([], rejected)).toEqual([]);
  });
});

describe("durable Task presentation", () => {
  it("converts progress into bounded percentages and surfaces task errors", () => {
    expect(taskProgressPercent(0)).toBe(0);
    expect(taskProgressPercent(0.42)).toBe(42);
    expect(taskProgressPercent(1)).toBe(100);
    expect(taskProgressPercent(null)).toBeNull();

    const perItem = {
      kind: "ASSET_INGEST_FAILED",
      items: [{ name: "large.mp4", error: "远程素材下载连接超时" }],
    };
    expect(taskErrorMessage(perItem, "素材处理失败（FAILED）")).toBe(
      "远程素材下载连接超时",
    );
    expect(
      taskErrorMessage({ message: "provider rejected input" }, "任务失败"),
    ).toBe("provider rejected input");
    expect(taskErrorMessage(null, "任务失败")).toBe("任务失败");
  });
});

describe("creatorToolLabel", () => {
  it("labels specialist tools and never falls back to the processing status label", () => {
    for (const [tool, label] of [
      ["tts_generation", "合成语音"],
      ["s2v_generation", "生成口型视频"],
      ["create_character_voice", "创建角色音色"],
      ["read_document", "读取文档"],
      ["query_source_memory", "查询素材记忆"],
      ["design_motion_overlays", "设计动态字幕"],
    ]) {
      expect(creatorToolLabel(tool)).toBe(label);
    }
    expect(getToolRunningLabel("tts_generation")).toBe("语音合成中…");
    expect(getToolRunningLabel("design_motion_overlays")).toBe(
      "动态字幕设计中…",
    );
    // The action title appends 处理中/完成 after the label, so the
    // fallback must stay neutral to avoid "处理中处理中" / "处理中完成".
    const fallback = creatorToolLabel("some_future_tool");
    expect(fallback).not.toBe(i18n.t("presentation.processing"));
    expect(fallback).not.toContain("处理中");
  });
});

it("hides specialist outcome markers while keeping the public result", () => {
  expect(
    publicAssistantText("[SUCCESS] 素材分析已完成。", { streaming: false }),
  ).toBe("素材分析已完成。");
});

it("projects the backend review continuation template into a public decision", () => {
  const result = publicAssistantText(
    "当前产物已生成，后续步骤尚未开始。请先完成审阅；审阅通过后主线需重新委派同一目标以继续。\n\n无需另行发送消息。",
    { streaming: false },
  );
  expect(result).toContain("结果已准备好，请在下方审阅。");
  expect(result).not.toContain("重新委派");
  expect(result).toContain("无需另行发送消息");
});
