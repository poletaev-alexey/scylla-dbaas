/**
 * Typed client for the Control Plane public API (/api/v1).
 * Types are generated from the API's OpenAPI document: `npm run gen:api`.
 */
import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./schema";

type S = components["schemas"];
export type Cluster = S["ClusterOut"];
export type ClusterStatus = S["ClusterStatus"];
export type Node = S["NodeOut"];
export type NodeStatus = S["NodeStatus"];
export type Operation = S["OperationOut"];
export type OperationDetail = S["OperationDetail"];
export type OperationStatus = S["OperationStatus"];
export type OperationType = S["OperationType"];
export type Backup = S["BackupOut"];
export type BackupStatus = S["BackupStatus"];
export type AuditEvent = S["AuditEventOut"];
export type AuditResult = S["AuditResult"];
export type ConnectionInfo = S["ConnectionInfo"];
export type Accepted = S["Accepted"];
export type BackupSchedule = S["BackupScheduleOut"];

export const API_URL: string = (import.meta.env.VITE_API_URL as string | undefined) ?? "";

const KEY_STORAGE = "dbaas.apiKey";

export function getApiKey(): string | null {
  try {
    return localStorage.getItem(KEY_STORAGE);
  } catch {
    return null;
  }
}

export function setApiKey(key: string | null): void {
  try {
    if (key) localStorage.setItem(KEY_STORAGE, key);
    else localStorage.removeItem(KEY_STORAGE);
  } catch {
    /* storage unavailable: key lives only for this page load */
  }
  window.dispatchEvent(new Event("dbaas:apikey"));
}

/** Error raised for every non-2xx response, carrying what the UI needs to explain it. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** For 409: the Operation currently blocking this Cluster, when the API names it. */
    readonly activeOperationId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function detailText(body: unknown): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = body.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      // FastAPI validation errors: [{loc: [...], msg: "..."}]
      return d
        .map((e: unknown) => {
          const item = e as { loc?: unknown[]; msg?: string };
          const field = item.loc?.slice(1).join(".");
          return field ? `${field}: ${item.msg ?? ""}` : (item.msg ?? "");
        })
        .join("; ");
    }
  }
  return "";
}

function activeOperation(body: unknown, message: string): string | null {
  if (body && typeof body === "object" && "active_operation_id" in body) {
    const id = body.active_operation_id;
    if (typeof id === "string") return id;
  }
  // The API currently reports the blocker only inside `detail`:
  // "Operation <id> (<type>) is already active on this Cluster".
  const m = /Operation (\S+) \(/.exec(message);
  return m?.[1] ?? null;
}

const auth: Middleware = {
  onRequest({ request }) {
    const key = getApiKey();
    if (key) request.headers.set("X-API-Key", key);
    return request;
  },
  onResponse({ response }) {
    if (response.status === 401) setApiKey(null);
    return response;
  },
};

export const api = createClient<paths>({ baseUrl: API_URL });
api.use(auth);

/** Turn an openapi-fetch result into data or a thrown ApiError. */
export function unwrap<T>(res: { data?: T; error?: unknown; response: Response }): T {
  if (res.response.ok && res.data !== undefined) return res.data;
  const message = detailText(res.error) || `${res.response.status} ${res.response.statusText}`;
  const blocker = res.response.status === 409 ? activeOperation(res.error, message) : null;
  throw new ApiError(res.response.status, message, blocker);
}

export const isActive = (s: OperationStatus): boolean => s === "pending" || s === "running";
