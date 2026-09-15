export function formatControlPrompt(label: string, designPrompt: string) {
  return `按钮文案：${label}\n\n外观与动效：${designPrompt}`;
}

export function parseControlPrompt(value: string) {
  const match = value
    .trim()
    .match(
      /^按钮文案[ \t]*[：:][ \t]*([\s\S]*?)\r?\n[ \t]*外观与动效[ \t]*[：:][ \t]*([\s\S]*)$/,
    );
  if (!match) {
    throw new Error(
      "请保留“按钮文案：”和“外观与动效：”两行标题，在后面填写内容。",
    );
  }
  return {
    label: match[1].trim(),
    design_prompt: match[2].trim(),
  };
}
