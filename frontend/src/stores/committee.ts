import { create } from "zustand";
import {
  api,
  type CommitteeRunItem,
  type CommitteeRunDetail,
  type PaperStatus,
  type PaperEquityRow,
  type SchedulerHealth,
  type McpStatus,
  type JournalDecision,
  type CommitteeRunsParams,
} from "@/lib/api";

interface CommitteeState {
  runs: CommitteeRunItem[];
  paperStatus: PaperStatus | null;
  paperEquity: PaperEquityRow[];
  schedulerHealth: SchedulerHealth | null;
  mcpStatus: McpStatus | null;
  journalDecisions: JournalDecision[];
  runDetail: CommitteeRunDetail | null;
  error: string | null;

  loadDashboard: (params?: CommitteeRunsParams) => Promise<void>;
  loadRuns: (params?: CommitteeRunsParams) => Promise<void>;
  loadRunDetail: (runId: string) => Promise<CommitteeRunDetail | null>;
  reset: () => void;
}

export const useCommitteeStore = create<CommitteeState>((set) => ({
  runs: [],
  paperStatus: null,
  paperEquity: [],
  schedulerHealth: null,
  mcpStatus: null,
  journalDecisions: [],
  runDetail: null,
  error: null,

  loadDashboard: async (params) => {
    try {
      const [runs, paperStatus, paperEquity, schedulerHealth, mcpStatus] = await Promise.all([
        api.getCommitteeRuns(params ?? { limit: 50 }),
        api.getPaperStatus().catch(() => null),
        api.getPaperEquity().catch(() => []),
        api.getSchedulerHealth().catch(() => null),
        api.getMcpStatus().catch(() => null),
      ]);
      set({ runs, paperStatus, paperEquity, schedulerHealth, mcpStatus, error: null });
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "load failed" });
    }
  },

  loadRuns: async (params) => {
    try {
      set({ runs: await api.getCommitteeRuns(params ?? { limit: 50 }), error: null });
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "load failed" });
    }
  },

  loadRunDetail: async (runId) => {
    try {
      const runDetail = await api.getCommitteeRun(runId);
      set({ runDetail, error: null });
      return runDetail;
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "load failed", runDetail: null });
      return null;
    }
  },

  reset: () =>
    set({
      runs: [], paperStatus: null, paperEquity: [], schedulerHealth: null,
      mcpStatus: null, journalDecisions: [], runDetail: null, error: null,
    }),
}));
