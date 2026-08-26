export interface VideoTemplateSummary {
  templateId: string;
  name: string;
  description: string;
  contentType: string;
  scenario: string;
  colorGrade: string;
  defaultTransitionKind: string;
  previewDescription: string;
  iconEmoji: string;
  captionBlueprints: string[];
  energy: "low" | "mid" | "high";
  density: "low" | "mid" | "high";
  decoration: "low" | "mid" | "high";
}

export interface VideoTemplateListResponse {
  items: VideoTemplateSummary[];
}
