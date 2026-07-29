import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { CommitteeRunDetail } from "../CommitteeRunDetail";
import type { CommitteeRunDetail as Detail } from "@/lib/api";

const apiMock = vi.hoisted(() => ({ getCommitteeRun: vi.fn(), swarmSseUrl: vi.fn(() => "") }));
const sseMock = vi.hoisted(() => ({ connect: vi.fn(), disconnect: vi.fn(), onStatusChange: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: apiMock }));
vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({ ...sseMock, getStatus: () => "disconnected" }),
}));

function renderAt(runId: string) {
  return render(
    <MemoryRouter initialEntries={[`/committee/runs/${runId}`]}>
      <Routes><Route path="/committee/runs/:runId" element={<CommitteeRunDetail />} /></Routes>
    </MemoryRouter>,
  );
}
function makeDetail(over: Partial<Detail> = {}): Detail {
  return {
    run: { run_id: "r1", status: "completed" },
    // Statuses are the values GET /committee/runs/{id} actually sends —
    // task.status.value, i.e. completed/degraded/failed/cancelled. "done" is
    // a swarmStatus.ts display value that never reaches this endpoint.
    seats: [
      { agent_id: "market_analyst", phase: "analysts", round: 1, status: "completed", report_md: "# Market view\nBullish." },
      { agent_id: "bull_researcher", phase: "debate", round: 1, status: "completed", report_md: "Bull case." },
      { agent_id: "risk_manager", phase: "risk", round: 1, status: "completed", report_md: null, missing: true },
    ],
    debate: { rounds: 1, order: ["bull-r1", "bear-r1"] },
    decision: { rating: "Buy", price_target: 70000, position_size_pct: 5 },
    journal: { horizons: { "24h": { raw_return: 0.01, alpha: 0.0, direction_correct: true, resolved_at: "2026-07-18T00:00:00Z" }, "7d": {} }, reflection: "Held as planned.", reflected_at: "2026-07-18T12:00:00Z" },
    pnl: { decision_id: "d1", executed: true, realized_pnl: 12.5, unrealized_pnl: 3.0, fees_paid: 0.4 },
    ...over,
  } as Detail;
}

describe("CommitteeRunDetail", () => {
  beforeEach(() => {
    apiMock.getCommitteeRun.mockReset();
    sseMock.connect.mockClear();
  });

  it("renders seats, rendered markdown, decision, journal and pnl", async () => {
    apiMock.getCommitteeRun.mockResolvedValue(makeDetail());
    renderAt("r1");
    expect(await screen.findByText("Buy")).toBeInTheDocument();
    expect(screen.getByText("market_analyst")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Market view" })).toBeInTheDocument(); // markdown rendered
    expect(screen.getByText("Held as planned.")).toBeInTheDocument();
  });

  it("shows an explicit not-available state for a missing report, never blank", async () => {
    apiMock.getCommitteeRun.mockResolvedValue(makeDetail());
    renderAt("r1");
    expect(await screen.findByText("Report not available")).toBeInTheDocument();
  });

  it("renders a not-found state when the run is absent", async () => {
    apiMock.getCommitteeRun.mockResolvedValue(null);
    renderAt("missing");
    expect(await screen.findByText("Committee run not found")).toBeInTheDocument();
  });

  it("treats a missing decision artifact as no-decision, not fabricated data", async () => {
    apiMock.getCommitteeRun.mockResolvedValue(makeDetail({ decision: { missing: true } }));
    renderAt("r1");
    expect(await screen.findByText("No portfolio decision recorded for this run")).toBeInTheDocument();
  });

  // Whole-branch review I4: SeatSection coloured on `seat.status === "done"`,
  // a value this endpoint never sends, so `degraded` rendered in the same
  // muted grey as `completed` — the degradation was invisible exactly where
  // an operator looks for it.
  it("colours a degraded seat as a warning and a completed seat as success", async () => {
    apiMock.getCommitteeRun.mockResolvedValue(
      makeDetail({
        // run status "running" so the header badge doesn't collide with the
        // seat badge this test queries by text.
        run: { run_id: "r1", status: "running" },
        seats: [
          { agent_id: "market_analyst", phase: "analysts", round: 1, status: "completed", report_md: "ok" },
          { agent_id: "research_manager", phase: "research_manager", round: 1, status: "degraded", report_md: null, missing: true },
        ],
      } as Partial<Detail>),
    );
    renderAt("r1");

    const degraded = await screen.findByText("degraded");
    expect(degraded.className).toContain("text-warning");
    expect(screen.getByText("completed").className).toContain("text-success");
  });

  it("renders the degraded seat's reason instead of leaving it unexplained", async () => {
    apiMock.getCommitteeRun.mockResolvedValue(
      makeDetail({
        seats: [
          {
            agent_id: "research_manager",
            phase: "research_manager",
            round: 1,
            status: "degraded",
            report_md: null,
            missing: true,
            status_error: "hit iteration limit (15) without completing the deliverable",
          },
        ],
      } as Partial<Detail>),
    );
    renderAt("r1");

    const reason = await screen.findByText(
      /hit iteration limit \(15\) without completing the deliverable/,
    );
    expect(reason.textContent).toContain("Seat did not complete");
    expect(reason.className).toContain("text-warning");
  });

  // Fix wave 2: committee_routes.py forwards task.error as status_error for
  // EVERY seat, and it is non-empty for failed, blocked and reaped seats — not
  // only degraded ones. Keying the body label on field presence rather than on
  // seat.status labelled a failed seat "Seat did not complete" in degraded
  // orange, beside a correct red `failed` badge. This fixture carries the
  // status_error the producer actually sets; without it the bug is invisible.
  it("colours a failed seat as danger and never labels it degraded", async () => {
    apiMock.getCommitteeRun.mockResolvedValue(
      makeDetail({
        seats: [
          {
            agent_id: "trader",
            phase: "trader",
            round: 1,
            status: "failed",
            report_md: null,
            missing: true,
            status_error: "worker raised RuntimeError: provider returned 500",
          },
        ],
      } as Partial<Detail>),
    );
    renderAt("r1");
    expect((await screen.findByText("failed")).className).toContain("text-danger");

    // The reason is still shown — under the failed label, in danger tone.
    const reason = screen.getByText(/provider returned 500/);
    expect(reason.textContent).toContain("Seat failed");
    expect(reason.textContent).not.toContain("Seat did not complete");
    expect(reason.className).toContain("text-danger");
    expect(reason.className).not.toContain("text-warning");
  });

  // Fix wave 2 hardening: every fixture above used to say `round: null`, which
  // production never sends — committee_routes.py::_debate_round has no None
  // branch (it returns 1 for non-debate task ids) and the key is set
  // unconditionally, so every seat arrives with round: 1. SeatSection keyed the
  // pill on truthiness alone, so the trader, the PM and every analyst carried a
  // "Round 1" badge that means nothing outside the debate — and the fixtures
  // were the only reason no test could see it.
  it("shows no round pill on a non-debate seat, which the API still sends round 1 for", async () => {
    apiMock.getCommitteeRun.mockResolvedValue(
      makeDetail({
        seats: [
          { agent_id: "trader", phase: "trader", round: 1, status: "completed", report_md: "Trade plan." },
        ],
        debate: { rounds: 1, order: [] },
      } as Partial<Detail>),
    );
    renderAt("r1");
    await screen.findByText("trader");
    expect(screen.queryByText("Round 1")).toBeNull();
  });

  it("keeps the round pill on a debate seat, where the number is real", async () => {
    apiMock.getCommitteeRun.mockResolvedValue(
      makeDetail({
        seats: [
          { agent_id: "bear_researcher", phase: "debate", round: 2, status: "completed", report_md: "Bear case." },
        ],
        debate: { rounds: 2, order: ["bear-r2"] },
      } as Partial<Detail>),
    );
    renderAt("r1");
    // Two: the debate group heading CommitteeRunDetail renders per round, and
    // the seat's own pill.
    await waitFor(() => expect(screen.getAllByText("Round 2")).toHaveLength(2));
  });

  // Loop 2: a degraded seat still means "something happened to this run" —
  // the live-follow panel must refetch on it like it does for
  // task_completed/task_failed, or an operator watching a running run would
  // never see the seat settle.
  it("refetches run detail when a task_degraded event arrives while running", async () => {
    apiMock.getCommitteeRun.mockResolvedValue(makeDetail({ run: { run_id: "r1", status: "running" } }));
    renderAt("r1");
    await screen.findByText("running");

    expect(sseMock.connect).toHaveBeenCalledTimes(1);
    const handlers = sseMock.connect.mock.calls[0][1] as Record<string, (data: Record<string, unknown>) => void>;
    expect(handlers.task_degraded).toBeTypeOf("function");

    apiMock.getCommitteeRun.mockClear();
    handlers.task_degraded({ task_id: "task-research-plan" });

    await waitFor(() => expect(apiMock.getCommitteeRun).toHaveBeenCalledWith("r1"));
  });

  // Loop 2 fix round: worker_iteration_limit is emitted by the worker via the
  // same _emit_event path as task_degraded and reaches this SSE stream
  // identically — the live-follow panel must refetch on it too, or an
  // operator watching a running run would never see the seat settle.
  it("refetches run detail when a worker_iteration_limit event arrives while running", async () => {
    apiMock.getCommitteeRun.mockResolvedValue(makeDetail({ run: { run_id: "r1", status: "running" } }));
    renderAt("r1");
    await screen.findByText("running");

    expect(sseMock.connect).toHaveBeenCalledTimes(1);
    const handlers = sseMock.connect.mock.calls[0][1] as Record<string, (data: Record<string, unknown>) => void>;
    expect(handlers.worker_iteration_limit).toBeTypeOf("function");

    apiMock.getCommitteeRun.mockClear();
    handlers.worker_iteration_limit({ task_id: "task-research-plan" });

    await waitFor(() => expect(apiMock.getCommitteeRun).toHaveBeenCalledWith("r1"));
  });
});
