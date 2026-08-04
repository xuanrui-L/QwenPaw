export type PostIngestAction = "NONE" | "ATTACH_SOURCE";

/** Document facts produced by the backend document reader. */
export interface DocumentMetadata {
  format: string;
  pageCount: number;
}

export interface AssetIngestAccepted {
  assetId: string;
  taskId: string;
  status:
    | "QUEUED"
    | "RUNNING"
    | "SUCCEEDED"
    | "FAILED"
    | "CANCELLED"
    | "QUARANTINED";
  progress?: number | null;
  assetVersionId?: string | null;
  result?: Record<string, unknown> | null;
  error?: Record<string, unknown> | null;
}

export interface AssetImportAccepted {
  importId: string;
  taskId: string;
  eventSeq: number;
}

export interface AssetImportItem {
  name: string;
  assetVersionId: string;
  checksum: string;
}

export interface AssetImportFailure {
  name: string;
  error: string;
}

export interface AssetImportView {
  importId: string;
  taskId: string;
  status: string;
  progress?: number | null;
  items: AssetImportItem[];
  failures: AssetImportFailure[];
  error?: Record<string, unknown> | null;
}
