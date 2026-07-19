import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import type { CommitteeRunItem } from "@/lib/api";

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return Number.isFinite(d.getTime()) ? d.toLocaleString() : iso;
}
function fmtPnl(item: CommitteeRunItem): string {
  const v = item.pnl_summary?.realized_pnl;
  if (typeof v !== "number") return "-";
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}`;
}
function ratingTone(rating?: string | null): string {
  const r = (rating || "").toLowerCase();
  if (r.includes("buy") || r.includes("long")) return "bg-success/10 text-success";
  if (r.includes("sell") || r.includes("short")) return "bg-danger/10 text-danger";
  return "bg-muted text-muted-foreground";
}

export function RunsTable({ runs }: { runs: CommitteeRunItem[] }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  if (runs.length === 0) {
    return <p className="rounded-md border border-dashed p-8 text-center text-sm text-muted-foreground">{t("committee.noRuns")}</p>;
  }
  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b text-start text-muted-foreground">
            <th className="p-2 text-start">{t("committee.colTime")}</th>
            <th className="p-2 text-start">{t("committee.colSymbol")}</th>
            <th className="p-2 text-start">{t("committee.colRating")}</th>
            <th className="p-2 text-start">{t("committee.colStatus")}</th>
            <th className="p-2 text-end">{t("committee.colWallClock")}</th>
            <th className="p-2 text-end">{t("committee.colTokens")}</th>
            <th className="p-2 text-end">{t("committee.colPnl")}</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr
              key={run.run_id}
              onClick={() => navigate(`/committee/runs/${encodeURIComponent(run.run_id)}`)}
              className="cursor-pointer border-b last:border-0 hover:bg-muted/30"
            >
              <td className="p-2 font-mono text-xs">{fmtTime(run.created_at)}</td>
              <td className="p-2">{run.target}</td>
              <td className="p-2">
                {run.rating ? <span className={cn("rounded px-2 py-0.5 text-xs font-medium", ratingTone(run.rating))}>{run.rating}</span> : "-"}
              </td>
              <td className="p-2 text-muted-foreground">{run.status}</td>
              <td className="p-2 text-end tabular-nums">{typeof run.wall_clock_s === "number" ? `${run.wall_clock_s.toFixed(1)}s` : "-"}</td>
              <td className="p-2 text-end tabular-nums text-muted-foreground">
                {(run.input_tokens ?? 0) + (run.output_tokens ?? 0) || "-"}
              </td>
              <td className="p-2 text-end tabular-nums">
                {run.decision_id ? <span className="text-primary hover:underline">{fmtPnl(run)}</span> : fmtPnl(run)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
