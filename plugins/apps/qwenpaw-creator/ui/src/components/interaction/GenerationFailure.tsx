export default function GenerationFailure({ error }: { error: string }) {
  if (!error) return null;
  return (
    <div
      role="alert"
      className="mb-3 rounded-lg border border-red-400/40 bg-red-500/10 p-3 text-sm"
    >
      <strong>生成失败</strong>
      <p className="mt-1 whitespace-pre-wrap break-words">{error}</p>
      <p className="mt-2 text-xs">请根据错误原因处理后重试。已有设计会保留。</p>
    </div>
  );
}
