import { api } from "@/lib/api";

describe("committee api client", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation(
      async () => new Response("[]", { status: 200, headers: { "content-type": "application/json" } }),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("builds the committee runs query with limit+status+symbol", async () => {
    await api.getCommitteeRuns({ limit: 20, status: "completed", symbol: "BTC-USDT" });
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toBe("/committee/runs?limit=20&status=completed&symbol=BTC-USDT");
  });

  it("percent-encodes the run id in the detail path", async () => {
    await api.getCommitteeRun("run/abc 1");
    expect(fetchMock.mock.calls[0][0]).toBe("/committee/runs/run%2Fabc%201");
  });

  it("hits the fixed paper/scheduler/mcp paths", async () => {
    await api.getPaperStatus();
    await api.getSchedulerHealth();
    await api.getMcpStatus();
    const urls = fetchMock.mock.calls.map((c) => c[0]);
    expect(urls).toEqual(["/paper/status", "/scheduler/health", "/mcp/status"]);
  });
});
