import { useEffect, useMemo, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowLeft, Radio } from "lucide-react";
import { useCommitteeStore } from "@/stores/committee";
import { useSSE, type SSEStatus } from "@/hooks/useSSE";
import { api, type CommitteeSeat, type CommitteeJournal, type DecisionPnl, type CommitteeDecision } from "@/lib/api";
import { cn } from "@/lib/utils";
import { SeatSection } from "@/pages/committee/SeatSection";

const PHASE_ORDER = ["analysts", "debate", "research_manager", "trader", "risk", "portfolio_manager"] as const;
const PHASE_LABEL_KEY: Record<string, string> = {
  analysts: "committee.phaseAnalysts",
  debate: "committee.phaseDebate",
  research_manager: "committee.phaseResearchManager",
  trader: "committee.phaseTrader",
  risk: "committee.phaseRisk",
  portfolio_manager: "committee.phasePortfolioManager",
};
const RUNNING_POLL_MS = 30_000;
// `/swarm/runs/:id/events` (agent/src/api/swarm_routes.py) streams each swarm
// event's own `type` as the SSE event name directly (e.g. `event: task_completed`)
// — unlike the chat-session stream, there is no "swarm.event" envelope here.
// Any of these means a seat's report.md may now exist -> refetch the authoritative
// detail endpoint (SSE payloads never carry artifact markdown; never fabricate).
const REFETCH_EVENT_TYPES = [
  "task_completed", "worker_completed",
  "task_failed", "worker_failed", "worker_timeout", "worker_incomplete",
  "run_completed", "run_error",
] as const;

type SSEHandlers = Record<string, (data: Record<string, unknown>) => void>;

export function CommitteeRunDetail() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { runDetail, error, loadRunDetail } = useCommitteeStore();
  const [loading, setLoading] = useState(true);
  const [sseStatus, setSseStatus] = useState<SSEStatus>("disconnected");
  const { connect, disconnect, onStatusChange } = useSSE();

  const status = String((runDetail?.run as { status?: string } | undefined)?.status ?? "");
  const isRunning = status === "running";

  // Initial + param-change load.
  useEffect(() => {
    if (!runId) return;
    setLoading(true);
    loadRunDetail(runId).finally(() => setLoading(false));
  }, [runId, loadRunDetail]);

  // Live-follow: reuse the swarm run's own SSE stream (no new emitters). On
  // completion-type events, refetch the detail endpoint — SSE payloads carry
  // no artifact markdown, so we never render anything the store didn't fetch.
  // A 30s poll covers the gap when the stream is quiet, reconnecting, or the
  // event type isn't one we recognize.
  useEffect(() => {
    if (!runId || !isRunning) return;
    let active = true;
    const refetch = () => { if (active) loadRunDetail(runId); };
    const handlers: SSEHandlers = { done: refetch };
    for (const type of REFETCH_EVENT_TYPES) handlers[type] = refetch;
    onStatusChange(setSseStatus);
    connect(api.swarmSseUrl(runId), handlers);
    const poll = window.setInterval(refetch, RUNNING_POLL_MS);
    return () => {
      active = false;
      window.clearInterval(poll);
      disconnect();
      setSseStatus("disconnected");
    };
  }, [runId, isRunning, connect, disconnect, onStatusChange, loadRunDetail]);

  const grouped = useMemo(() => groupSeats(runDetail?.seats ?? []), [runDetail]);

  if (loading) return <div className="p-8 text-sm text-muted-foreground">…</div>;
  if (!runDetail) {
    return (
      <div className="p-8 space-y-2">
        <p className="font-medium text-danger">{t("committee.runNotFound")}</p>
        {error ? <p className="text-sm text-muted-foreground">{error}</p> : null}
        <button onClick={() => navigate("/committee")} className="inline-flex items-center gap-1.5 text-sm text-primary hover:underline">
          <ArrowLeft className="h-3.5 w-3.5" />{t("committee.backToCommittee")}
        </button>
      </div>
    );
  }

  return (
    <div className="min-h-screen p-6 lg:p-8">
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-6">
        <header className="flex items-center gap-3 border-b pb-4">
          <button onClick={() => navigate("/committee")} className="rounded-md p-1 text-muted-foreground hover:bg-muted" title={t("committee.backToCommittee")}>
            <ArrowLeft className="h-4 w-4" />
          </button>
          <h1 className="font-mono text-sm font-medium">{runId}</h1>
          <span className="text-xs text-muted-foreground">{status}</span>
          {isRunning ? (
            <span className={cn("inline-flex items-center gap-1 text-xs", sseStatus === "connected" ? "text-success" : "text-muted-foreground")}>
              <Radio className={cn("h-3.5 w-3.5", sseStatus === "connected" && "animate-pulse")} />
              {sseStatus === "connected" ? t("committee.liveFollowing") : t("committee.liveFallbackPolling")}
            </span>
          ) : null}
        </header>

        <section className="space-y-4">
          <h2 className="text-lg font-semibold">{t("committee.discussion")}</h2>
          {PHASE_ORDER.map((phase) => {
            const seats = grouped.get(phase);
            if (!seats || seats.length === 0) return null;
            return (
              <div key={phase} className="space-y-2">
                <h3 className="text-sm font-medium text-muted-foreground">{t(PHASE_LABEL_KEY[phase] as any)}</h3>
                {phase === "debate"
                  ? renderDebateByRound(seats).map(([round, roundSeats]) => (
                      <div key={round} className="space-y-2 border-s-2 border-muted ps-3">
                        <div className="text-xs font-medium text-muted-foreground">{t("committee.round", { n: round })}</div>
                        {roundSeats.map((s) => <SeatSection key={s.agent_id + s.phase + (s.round ?? "")} seat={s} />)}
                      </div>
                    ))
                  : seats.map((s) => <SeatSection key={s.agent_id + s.phase} seat={s} />)}
              </div>
            );
          })}
          {grouped.get("__other__")?.length ? (
            <div className="space-y-2">
              <h3 className="text-sm font-medium text-muted-foreground">{t("committee.phaseOther")}</h3>
              {grouped.get("__other__")!.map((s) => <SeatSection key={s.agent_id + s.phase} seat={s} />)}
            </div>
          ) : null}
        </section>

        <DecisionCard decision={runDetail.decision} />
        <JournalCard journal={runDetail.journal} />
        <PnlCard pnl={runDetail.pnl} />
      </div>
    </div>
  );
}

function groupSeats(seats: CommitteeSeat[]): Map<string, CommitteeSeat[]> {
  const map = new Map<string, CommitteeSeat[]>();
  for (const seat of seats) {
    const key = (PHASE_ORDER as readonly string[]).includes(seat.phase) ? seat.phase : "__other__";
    const list = map.get(key) ?? [];
    list.push(seat);
    map.set(key, list);
  }
  return map;
}

function renderDebateByRound(seats: CommitteeSeat[]): Array<[number, CommitteeSeat[]]> {
  const byRound = new Map<number, CommitteeSeat[]>();
  for (const seat of seats) {
    const r = seat.round ?? 1;
    const list = byRound.get(r) ?? [];
    list.push(seat);
    byRound.set(r, list);
  }
  return [...byRound.entries()].sort((a, b) => a[0] - b[0]);
}

function Row({ label, value }: { label: string; value: string }) {
  return <div className="flex justify-between gap-4"><span className="text-muted-foreground">{label}</span><span className="tabular-nums">{value}</span></div>;
}

function DecisionCard({ decision }: { decision: CommitteeDecision | null }) {
  const { t } = useTranslation();
  const meta = decision as { missing?: boolean; error?: string } | null;
  if (!decision || meta?.missing || meta?.error) {
    return (
      <section className="rounded-md border p-4 text-sm text-muted-foreground">
        <span>{t("committee.noDecision")}</span>
        {meta?.error ? <span className="ms-1 text-danger">{meta.error}</span> : null}
      </section>
    );
  }
  return (
    <section className="rounded-md border p-4">
      <h2 className="mb-3 text-lg font-semibold">{t("committee.decision")}</h2>
      <div className="space-y-1 text-sm">
        {decision.rating != null ? <Row label={t("committee.colRating")} value={String(decision.rating)} /> : null}
        {decision.price_target != null ? <Row label={t("committee.priceTarget")} value={String(decision.price_target)} /> : null}
        {decision.stop_loss != null ? <Row label={t("committee.stopLoss")} value={String(decision.stop_loss)} /> : null}
        {decision.take_profit != null ? <Row label={t("committee.takeProfit")} value={String(decision.take_profit)} /> : null}
        {decision.position_size_pct != null ? <Row label={t("committee.positionSize")} value={`${decision.position_size_pct}%`} /> : null}
        {decision.time_horizon != null ? <Row label={t("committee.timeHorizon")} value={String(decision.time_horizon)} /> : null}
      </div>
    </section>
  );
}

function JournalCard({ journal }: { journal: CommitteeJournal | null }) {
  const { t } = useTranslation();
  if (!journal) return <section className="rounded-md border p-4 text-sm text-muted-foreground">{t("committee.noJournal")}</section>;
  const horizons = Object.entries(journal.horizons ?? {});
  return (
    <section className="rounded-md border p-4">
      <h2 className="mb-3 text-lg font-semibold">{t("committee.journal")}</h2>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead><tr className="border-b text-start text-muted-foreground">
            <th className="p-2 text-start">{t("committee.horizon")}</th>
            <th className="p-2 text-end">{t("committee.rawReturn")}</th>
            <th className="p-2 text-end">{t("committee.alpha")}</th>
            <th className="p-2 text-end">{t("committee.directionCorrect")}</th>
          </tr></thead>
          <tbody>
            {horizons.map(([h, v]) => {
              const resolved = !!v.resolved_at;
              return (
                <tr key={h} className="border-b last:border-0">
                  <td className="p-2 font-mono text-xs">{h}</td>
                  <td className="p-2 text-end tabular-nums">{resolved && v.raw_return != null ? `${(v.raw_return * 100).toFixed(2)}%` : t("committee.pending")}</td>
                  <td className="p-2 text-end tabular-nums">{resolved && v.alpha != null ? `${(v.alpha * 100).toFixed(2)}%` : t("committee.pending")}</td>
                  <td className="p-2 text-end">{resolved && v.direction_correct != null ? (v.direction_correct ? t("committee.correct") : t("committee.incorrect")) : t("committee.pending")}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="mt-3 border-t pt-2 text-sm">
        <span className="text-muted-foreground">{t("committee.reflection")}: </span>
        {journal.reflection ? <span>{journal.reflection}</span> : <span className="text-muted-foreground">{t("committee.notReflected")}</span>}
      </div>
    </section>
  );
}

function PnlCard({ pnl }: { pnl: DecisionPnl | null }) {
  const { t } = useTranslation();
  if (!pnl) return <section className="rounded-md border p-4 text-sm text-muted-foreground">{t("committee.noPnl")}</section>;
  return (
    <section className="rounded-md border p-4">
      <h2 className="mb-3 text-lg font-semibold">{t("committee.pnl")}</h2>
      {pnl.executed ? (
        <div className="space-y-1 text-sm">
          <Row label={t("committee.realizedPnl")} value={pnl.realized_pnl != null ? pnl.realized_pnl.toFixed(2) : "-"} />
          {pnl.unrealized_pnl != null ? <Row label={t("committee.unrealizedPnl")} value={pnl.unrealized_pnl.toFixed(2)} /> : null}
          {pnl.fees_paid != null ? <Row label={t("committee.feesPaid")} value={pnl.fees_paid.toFixed(2)} /> : null}
          {pnl.summary ? <p className="mt-2 text-xs text-muted-foreground">{pnl.summary}</p> : null}
        </div>
      ) : <p className="text-sm text-muted-foreground">{t("committee.notExecuted")}</p>}
    </section>
  );
}
