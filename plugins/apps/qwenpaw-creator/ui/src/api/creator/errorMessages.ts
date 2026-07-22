import { CreatorHttpError } from "./client";

export function getApiErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof TypeError) {
    return "网络连接失败，请检查网络";
  }
  if (error instanceof CreatorHttpError) {
    switch (error.status) {
      case 401:
        return "登录已过期，请刷新页面";
      case 403:
        return "权限不足，无法执行此操作";
      case 404:
        return "请求的资源不存在";
      case 409:
        return "操作冲突，请刷新页面后重试";
      case 422:
        return error.message || "请求参数有误，请检查输入";
      case 503:
        return "服务暂时不可用，请稍后重试";
      default:
        return error.message || fallback;
    }
  }
  return error instanceof Error ? error.message : fallback;
}
