import { describe, expect, it } from "vitest";
import { applySwarmEvent, buildSwarmStatusFromStarted } from "@/lib/swarmStatus";

describe("degraded task status (Loop 2)", () => {
  it("maps a degraded task to the degraded display status, not waiting", () => {
    const status = buildSwarmStatusFromStarted({
      run_id: "r1",
      preset: "crypto_committee",
      status: "failed",
      agents: [{ id: "research_manager", role: "Research Manager" }],
      tasks: [
        {
          id: "task-research-plan",
          agent_id: "research_manager",
          status: "degraded",
          worker_iterations: 15,
          error: "hit iteration limit (15) without completing the deliverable",
        },
      ],
    });

    expect(status?.agents[0].status).toBe("degraded");
  });

  it("settles a live seat on task_degraded instead of leaving it running", () => {
    const start = buildSwarmStatusFromStarted({
      run_id: "r1",
      preset: "crypto_committee",
      status: "running",
      agents: [{ id: "research_manager" }],
      tasks: [{ id: "task-research-plan", agent_id: "research_manager", status: "in_progress" }],
    });
    expect(start).not.toBeNull();

    const next = applySwarmEvent(start!, {
      type: "task_degraded",
      task_id: "task-research-plan",
      agent_id: "research_manager",
      data: { iterations: 15, error: "hit iteration limit (15)" },
      timestamp: "2026-07-28T18:12:57.276010+00:00",
    });

    expect(next.agents[0].status).toBe("degraded");
    expect(next.agents[0].iterations).toBe(15);
    expect(next.agents[0].error).toContain("iteration limit");
  });
});
