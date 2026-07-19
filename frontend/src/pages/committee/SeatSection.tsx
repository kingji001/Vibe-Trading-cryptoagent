import { useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { cn } from "@/lib/utils";
import type { CommitteeSeat } from "@/lib/api";

const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeHighlight];

export function SeatSection({ seat, defaultOpen = true }: { seat: CommitteeSeat; defaultOpen?: boolean }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(defaultOpen);
  return (
    <article className="rounded-md border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 p-3 text-start hover:bg-muted/30"
      >
        {open ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />}
        <span className="font-mono text-sm font-medium">{seat.agent_id}</span>
        {seat.round ? <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">{t("committee.round", { n: seat.round })}</span> : null}
        <span className={cn("ms-auto text-xs", seat.status === "done" ? "text-success" : "text-muted-foreground")}>{seat.status}</span>
      </button>
      {open ? (
        <div className="border-t p-3">
          {seat.error ? (
            <p className="text-sm text-danger">{t("committee.seatError")}: {seat.error}</p>
          ) : seat.report_md ? (
            <div className="prose prose-sm dark:prose-invert max-w-none leading-relaxed prose-hr:hidden">
              <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins}>{seat.report_md}</ReactMarkdown>
            </div>
          ) : (
            <div className="rounded-md border border-dashed p-4 text-center">
              <p className="text-sm font-medium text-muted-foreground">{t("committee.reportUnavailable")}</p>
              <p className="mt-1 text-xs text-muted-foreground">{t("committee.reportUnavailableHint")}</p>
            </div>
          )}
        </div>
      ) : null}
    </article>
  );
}
