import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Committee } from "../Committee";

const apiMock = vi.hoisted(() => ({
  getCommitteeRuns: vi.fn(),
  getPaperStatus: vi.fn(),
  getPaperEquity: vi.fn(),
  getSchedulerHealth: vi.fn(),
  getMcpStatus: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api: apiMock }));
vi.mock("@/components/charts/EquityChart", () => ({ EquityChart: () => <div data-testid="equity-chart" /> }));

describe("Committee dashboard", () => {
  beforeEach(() => {
    Object.values(apiMock).forEach((m) => m.mockReset());
    apiMock.getPaperStatus.mockResolvedValue({ cash: 9000, positions_value: 1000, equity: 10000, positions: [], stale_positions: [] });
    apiMock.getPaperEquity.mockResolvedValue([{ ts: "2026-07-18T00:00:00Z", equity: 10000 }]);
    apiMock.getSchedulerHealth.mockResolvedValue({
      jobs: [{ id: "committee-run", schedule: "0 */8 * * *", status: "ok" }],
      supervisor: { heartbeat_mtime: Date.now() / 1000, last_row: { ts: "2026-07-18T00:00:00Z", ok: true, http: 200, latency_ms: 12 } },
    });
    apiMock.getMcpStatus.mockResolvedValue({ committee_tools_enabled: true, trigger_enabled: false, trigger_budget: 4, triggers_used_today: 0, http_mount: "/mcp", stdio_command: "vibe-trading-mcp" });
  });

  it("renders paper account, scheduler, mcp cards and the runs table", async () => {
    apiMock.getCommitteeRuns.mockResolvedValue([
      { run_id: "r1", created_at: "2026-07-18T10:00:00Z", status: "completed", target: "BTC-USDT", wall_clock_s: 42, input_tokens: 1000, output_tokens: 500, decision_id: "d1", rating: "Buy", journal_status: "pending", pnl_summary: { realized_pnl: 12.5 } },
    ]);
    render(<MemoryRouter><Committee /></MemoryRouter>);

    expect(await screen.findByText("BTC-USDT")).toBeInTheDocument();
    expect(screen.getByText("Buy")).toBeInTheDocument();
    expect(screen.getByTestId("equity-chart")).toBeInTheDocument();
    expect(screen.getByText("committee-run")).toBeInTheDocument();
    expect(screen.getByText("vibe-trading-mcp")).toBeInTheDocument();
    expect(screen.getByText("alive")).toBeInTheDocument();
  });

  it("shows an empty state when there are no runs", async () => {
    apiMock.getCommitteeRuns.mockResolvedValue([]);
    render(<MemoryRouter><Committee /></MemoryRouter>);
    expect(await screen.findByText("No committee runs in the current window")).toBeInTheDocument();
  });

  it("shows stopped when the heartbeat is stale even if the last row was ok", async () => {
    apiMock.getCommitteeRuns.mockResolvedValue([]);
    apiMock.getSchedulerHealth.mockResolvedValue({
      jobs: [{ id: "committee-run", schedule: "0 */8 * * *", status: "ok" }],
      supervisor: { heartbeat_mtime: Date.now() / 1000 - 3600, last_row: { ts: "2026-07-18T00:00:00Z", ok: true, http: 200, latency_ms: 12 } },
    });
    render(<MemoryRouter><Committee /></MemoryRouter>);
    expect(await screen.findByText("stopped")).toBeInTheDocument();
  });
});
