import { useEffect, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Activity, ServerCog, Wallet, Plug } from "lucide-react";
import { useCommitteeStore } from "@/stores/committee";
import { EquityChart } from "@/components/charts/EquityChart";
import type { EquityPoint, PaperEquityRow, SchedulerSupervisor } from "@/lib/api";
import { RunsTable } from "@/pages/committee/RunsTable";

const COMMITTEE_POLL_MS = 45_000;
// Heartbeat file is touched on every supervisor tick; anything older than this
// reads as stopped even if the last recorded row was ok (§ corrections: no
// `alive` field on the wire, so liveness is derived, never fabricated).
const HEARTBEAT_STALE_AFTER_S = 180;

function toEquityPoints(rows: PaperEquityRow[]): EquityPoint[] {
  let peak = -Infinity;
  return rows.map((row) => {
    const equity = Number(row.equity);
    peak = Math.max(peak, equity);
    const drawdown = peak > 0 ? equity / peak - 1 : 0;
    return { time: String(row.ts), equity, drawdown };
  });
}

function isSupervisorAlive(supervisor: SchedulerSupervisor | null | undefined): boolean {
  if (!supervisor) return false;
  const lastRow = supervisor.last_row;
  const rowOk = !!lastRow && lastRow.ok === true;
  const mtime = supervisor.heartbeat_mtime;
  const recent = typeof mtime === "number" && Date.now() / 1000 - mtime < HEARTBEAT_STALE_AFTER_S;
  return rowOk && recent;
}

function lastRowTimestamp(supervisor: SchedulerSupervisor | null | undefined): string | null {
  const ts = supervisor?.last_row?.ts;
  return typeof ts === "string" ? ts : null;
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return Number.isFinite(d.getTime()) ? d.toLocaleString() : iso;
}

export function Committee() {
  const { t } = useTranslation();
  const { runs, paperStatus, paperEquity, schedulerHealth, mcpStatus, error, loadDashboard } =
    useCommitteeStore();

  useEffect(() => {
    loadDashboard({ limit: 50 });
    const id = window.setInterval(() => loadDashboard({ limit: 50 }), COMMITTEE_POLL_MS);
    return () => window.clearInterval(id);
  }, [loadDashboard]);

  const equityPoints = useMemo(() => toEquityPoints(paperEquity), [paperEquity]);
  const supervisorAlive = isSupervisorAlive(schedulerHealth?.supervisor);
  const supervisorLastTs = lastRowTimestamp(schedulerHealth?.supervisor);

  return (
    <div className="min-h-screen p-6 lg:p-8">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6">
        <header className="border-b pb-4">
          <h1 className="text-3xl font-bold tracking-tight">{t("committee.title")}</h1>
          <p className="mt-2 text-sm text-muted-foreground">{t("committee.subtitle")}</p>
        </header>

        {error ? (
          <section className="rounded-md border border-amber-500/30 bg-amber-500/5 p-4">
            <p className="text-sm font-medium text-amber-700 dark:text-amber-300">{t("committee.loadError")}</p>
            <p className="mt-1 text-xs text-muted-foreground">{error}</p>
            <p className="mt-1 text-xs text-muted-foreground">{t("committee.loadErrorHint")}</p>
          </section>
        ) : null}

        <div className="grid gap-4 lg:grid-cols-3">
          <section className="rounded-md border p-4">
            <div className="mb-3 flex items-center gap-2 text-sm font-medium"><Wallet className="h-4 w-4 text-muted-foreground" />{t("committee.paperAccount")}</div>
            {paperStatus ? (
              <dl className="space-y-1 text-sm">
                <div className="flex justify-between"><dt className="text-muted-foreground">{t("committee.equity")}</dt><dd className="tabular-nums font-medium">{paperStatus.equity.toLocaleString()}</dd></div>
                <div className="flex justify-between"><dt className="text-muted-foreground">{t("committee.cash")}</dt><dd className="tabular-nums">{paperStatus.cash.toLocaleString()}</dd></div>
                <div className="flex justify-between"><dt className="text-muted-foreground">{t("committee.positionsValue")}</dt><dd className="tabular-nums">{paperStatus.positions_value.toLocaleString()}</dd></div>
              </dl>
            ) : <p className="text-sm text-muted-foreground">-</p>}
          </section>

          <section className="rounded-md border p-4">
            <div className="mb-3 flex items-center gap-2 text-sm font-medium"><ServerCog className="h-4 w-4 text-muted-foreground" />{t("committee.scheduler")}</div>
            {schedulerHealth && schedulerHealth.jobs.length > 0 ? (
              <ul className="space-y-1.5 text-sm">
                {schedulerHealth.jobs.map((job) => (
                  <li key={job.id} className="flex items-center justify-between gap-2">
                    <span className="font-mono text-xs">{job.id}</span>
                    <span className="text-xs text-muted-foreground">{job.status || "-"}</span>
                  </li>
                ))}
              </ul>
            ) : <p className="text-sm text-muted-foreground">{t("committee.noJobs")}</p>}
            {schedulerHealth?.supervisor ? (
              <p className="mt-3 flex items-center justify-between gap-2 border-t pt-2 text-xs text-muted-foreground">
                <span>{t("committee.supervisor")}: <span>{supervisorAlive ? t("committee.alive") : t("committee.stopped")}</span></span>
                {supervisorLastTs ? <span>{t("committee.lastFired")}: {fmtTime(supervisorLastTs)}</span> : null}
              </p>
            ) : null}
          </section>

          <section className="rounded-md border p-4">
            <div className="mb-3 flex items-center gap-2 text-sm font-medium"><Plug className="h-4 w-4 text-muted-foreground" />{t("committee.mcp")}</div>
            {mcpStatus ? (
              <dl className="space-y-1 text-sm">
                <div className="flex justify-between"><dt className="text-muted-foreground">{t("committee.mcpEnabled")}</dt><dd>{mcpStatus.committee_tools_enabled ? "✓" : t("committee.mcpDisabled")}</dd></div>
                <div className="flex justify-between"><dt className="text-muted-foreground">{t("committee.mcpTrigger")}</dt><dd>{mcpStatus.trigger_enabled ? `${mcpStatus.triggers_used_today}/${mcpStatus.trigger_budget}` : t("committee.mcpDisabled")}</dd></div>
                <div className="flex justify-between"><dt className="text-muted-foreground">{t("committee.mcpStdio")}</dt><dd className="font-mono text-xs">{mcpStatus.stdio_command}</dd></div>
                {mcpStatus.http_mount ? <div className="flex justify-between"><dt className="text-muted-foreground">{t("committee.mcpHttp")}</dt><dd className="font-mono text-xs">{mcpStatus.http_mount}</dd></div> : null}
                {mcpStatus.committee_tools_enabled ? <p className="pt-2 text-xs text-muted-foreground">{t("committee.mcpConnectHint")}</p> : null}
              </dl>
            ) : <p className="text-sm text-muted-foreground">-</p>}
          </section>
        </div>

        <section className="rounded-md border p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium"><Activity className="h-4 w-4 text-muted-foreground" />{t("committee.equityCurve")}</div>
          {equityPoints.length > 0 ? <EquityChart data={equityPoints} height={260} /> : <p className="text-sm text-muted-foreground">{t("committee.noEquity")}</p>}
        </section>

        <section>
          <h2 className="mb-3 text-lg font-semibold">{t("committee.runs")}</h2>
          <RunsTable runs={runs} />
        </section>
      </div>
    </div>
  );
}
