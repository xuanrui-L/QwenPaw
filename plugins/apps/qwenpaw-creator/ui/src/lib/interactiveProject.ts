export const INTERACTIVE_CONTENT_TYPE = "interactive_branching_drama";

export function isInteractiveContentType(value?: string | null): boolean {
  return /^(?:(?:interactive|branching)(?:_(?:branching_)?(?:video|film|story|drama|short_drama))?|(?:交互|互动)式?(?:视频|短剧|影游))$/i.test(
    (value ?? "").trim().replace(/[\s-]+/g, "_"),
  );
}

/** Bootstrap presentation from an explicit request, before the first Agent
 * patch. Only read the opening brief and type declarations, not story dialogue. */
export function interactiveContentTypeFromBrief(brief: string): string | null {
  const text = brief.trim();
  const declarations = text
    .split(/\r?\n/)
    .filter((line) =>
      /^(?:类型|作品类型|项目类型|type|format)\s*[:：]/i.test(
        line.replace(/[#*`]/g, "").trim(),
      ),
    );
  const intent = [text.split(/\r?\n\s*\r?\n/)[0], ...declarations].join("\n");
  const formats =
    /(?:交互|互动)式?(?:视频|短剧|影游)|多结局(?:分支剧|短剧)|\b(?:interactive|branching)\s+(?:video|film|movie|story|drama)\b/gi;
  for (const match of intent.matchAll(formats)) {
    const prefix =
      intent
        .slice(0, match.index)
        .split(/[，。！？；\n.!?;]/)
        .pop() ?? "";
    if (
      !/(?:不要|不需要|无需|禁止|不是|不做|不用|\bnot\b|\bwithout\b)[^，。！？；\n]{0,12}$|(?:非|\bnon[-\s])$/i.test(
        prefix,
      )
    ) {
      return INTERACTIVE_CONTENT_TYPE;
    }
  }
  return null;
}
