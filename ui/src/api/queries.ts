import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, isActive, unwrap, type Accepted, type ClusterStatus } from "./client";

const TRANSITIONAL: ClusterStatus[] = ["provisioning", "recovering", "deleting", "degraded"];
const POLL_MS = 2000;

export const keys = {
  clusters: ["clusters"] as const,
  cluster: (id: string) => ["clusters", id] as const,
  clusterBackups: (id: string) => ["clusters", id, "backups"] as const,
  backups: ["backups"] as const,
  operations: (clusterId?: string) => ["operations", clusterId ?? "all"] as const,
  operation: (id: string) => ["operation", id] as const,
  audit: (f: AuditFilter) => ["audit", f] as const,
};

export function useClusters() {
  return useQuery({
    queryKey: keys.clusters,
    queryFn: async () => unwrap(await api.GET("/api/v1/clusters")),
    refetchInterval: (q) =>
      q.state.data?.some((c) => TRANSITIONAL.includes(c.status)) ? POLL_MS : 10_000,
  });
}

export function useCluster(id: string) {
  return useQuery({
    queryKey: keys.cluster(id),
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/clusters/{cluster_id}", { params: { path: { cluster_id: id } } })),
    refetchInterval: (q) =>
      q.state.data && TRANSITIONAL.includes(q.state.data.status) ? POLL_MS : 5000,
  });
}

export function useClusterBackups(id: string) {
  return useQuery({
    queryKey: keys.clusterBackups(id),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/clusters/{cluster_id}/backups", {
          params: { path: { cluster_id: id } },
        }),
      ),
    refetchInterval: (q) => (q.state.data?.some((b) => b.status === "running") ? POLL_MS : 10_000),
  });
}

export function useBackups() {
  return useQuery({
    queryKey: keys.backups,
    queryFn: async () => unwrap(await api.GET("/api/v1/backups")),
    refetchInterval: (q) => (q.state.data?.some((b) => b.status === "running") ? POLL_MS : 10_000),
  });
}

export function useOperations(clusterId?: string) {
  return useQuery({
    queryKey: keys.operations(clusterId),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/operations", {
          params: { query: { cluster_id: clusterId ?? null, limit: 100 } },
        }),
      ),
    // Poll every 2 s while any Operation is active.
    refetchInterval: (q) => (q.state.data?.some((o) => isActive(o.status)) ? POLL_MS : 10_000),
  });
}

export function useOperation(id: string) {
  return useQuery({
    queryKey: keys.operation(id),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/operations/{operation_id}", {
          params: { path: { operation_id: id } },
        }),
      ),
    refetchInterval: (q) => (q.state.data && !isActive(q.state.data.status) ? false : POLL_MS),
  });
}

export interface AuditFilter {
  cluster_id: string;
  actor: string;
  action: string;
  limit: number;
}

export function useAuditEvents(f: AuditFilter) {
  return useQuery({
    queryKey: keys.audit(f),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/audit-events", {
          params: {
            query: {
              cluster_id: f.cluster_id || null,
              actor: f.actor || null,
              action: f.action || null,
              limit: f.limit,
            },
          },
        }),
      ),
    refetchInterval: 10_000,
  });
}

/** Wraps a mutating call: refreshes everything Cluster/Operation-related afterwards. */
export function useOperationMutation<V>(fn: (v: V) => Promise<Accepted>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["clusters"] });
      void qc.invalidateQueries({ queryKey: ["operations"] });
      void qc.invalidateQueries({ queryKey: ["backups"] });
    },
  });
}
