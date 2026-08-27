import type { VideoTemplateListResponse } from "@/contracts/creator";
import { creatorRequest } from "./client";

export function listVideoTemplates(): Promise<VideoTemplateListResponse> {
  return creatorRequest<VideoTemplateListResponse>("/video-templates");
}
