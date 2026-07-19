import { render, screen } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { CommitteeRunDetail } from "../CommitteeRunDetail";
import type { CommitteeRunDetail as Detail } from "@/lib/api";

const apiMock = vi.hoisted(() => ({ getCommitteeRun: vi.fn(), swarmSseUrl: vi.fn(() => "") }));
vi.mock("@/lib/api", () => ({ api: apiMock }));
vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({ connect: vi.fn(), disconnect: vi.fn(), getStatus: () => "disconnected", onStatusChange: vi.fn() }),
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
    seats: [
      { agent_id: "market_analyst", phase: "analysts", round: null, status: "done", report_md: "# Market view\nBullish." },
      { agent_id: "bull_researcher", phase: "debate", round: 1, status: "done", report_md: "Bull case." },
      { agent_id: "risk_manager", phase: "risk", round: null, status: "done", report_md: null, missing: true },
    ],
    debate: { rounds: 1, order: ["bull-r1", "bear-r1"] },
    decision: { rating: "Buy", price_target: 70000, position_size_pct: 5 },
    journal: { horizons: { "24h": { raw_return: 0.01, alpha: 0.0, direction_correct: true, resolved_at: "2026-07-18T00:00:00Z" }, "7d": {} }, reflection: "Held as planned.", reflected_at: "2026-07-18T12:00:00Z" },
    pnl: { decision_id: "d1", executed: true, realized_pnl: 12.5, unrealized_pnl: 3.0, fees_paid: 0.4 },
    ...over,
  } as Detail;
}

describe("CommitteeRunDetail", () => {
  beforeEach(() => apiMock.getCommitteeRun.mockReset());

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
});
