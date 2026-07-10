"""Swarm YAML preset loader.

Reads YAML preset files from the bundled ``presets/`` directory next to this
module and parses them into SwarmRun / SwarmAgentSpec / SwarmTask data models.
Keeping the YAMLs inside the ``src.swarm`` package guarantees identical
behavior under editable installs and built wheels.
"""

from __future__ import annotations

import os
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter

import yaml

from src.swarm.models import RunStatus, SwarmAgentSpec, SwarmRun, SwarmTask, TaskStatus
from src.swarm.task_store import topological_layers, validate_dag

PRESETS_DIR = Path(__file__).resolve().parent / "presets"
# ``round`` is injected by the Phase 4 debate expansion (the round number is
# baked into rebuttal prompt_templates at build time), so it is not a
# user-declared preset variable.
_INTERNAL_TEMPLATE_VARS = {"upstream_context", "round"}

# Hard cap on debate rounds (Phase 4). Rounds above this are rejected at
# preset-build time so a misconfigured debate fails before spending tokens.
_DEBATE_ROUNDS_CAP = 4

# Phase 6 — optional experiment knob. TradingAgents deliberately restricts
# reflection/memory context to the Portfolio Manager; the learning-loop
# preset wiring (crypto_committee.yaml) follows that on purpose — only
# task-decision (portfolio_manager) declares past_lessons: task-reflection.
# VIBE_LESSONS_TO_MANAGER=1 mirrors that same wiring onto the research
# manager's task too, for A/B testing whether earlier-stage judging benefits
# from the same lessons. Default OFF preserves upstream behavior exactly.
LESSONS_TO_MANAGER_ENV = "VIBE_LESSONS_TO_MANAGER"
_LESSONS_TO_MANAGER_TRUE_VALUES = {"1", "true", "yes", "on"}


def _lessons_to_manager_enabled() -> bool:
    return os.getenv(LESSONS_TO_MANAGER_ENV, "").strip().lower() in _LESSONS_TO_MANAGER_TRUE_VALUES


def _inject_lessons_into_manager(tasks: list[SwarmTask]) -> None:
    """Mirror the PM's ``past_lessons`` input onto the ``research_manager`` task.

    No-op when the flag is off (default), when the preset has no
    ``research_manager`` seat, when nothing else in the preset feeds
    ``past_lessons`` to anything (nothing to mirror), or when the preset
    already wires it explicitly (never override an explicit preset choice).
    Mutates the matching task's ``input_from``/``depends_on`` in place —
    same pattern ``_expand_debate`` uses to rewire a sink task's dependency
    bookkeeping (``blocked_by``/``status``) after adding a dependency post
    construction.
    """
    if not _lessons_to_manager_enabled():
        return
    source_task_id = next(
        (t.input_from["past_lessons"] for t in tasks if "past_lessons" in t.input_from),
        None,
    )
    manager_task = next((t for t in tasks if t.agent_id == "research_manager"), None)
    if source_task_id is None or manager_task is None:
        return
    if "past_lessons" in manager_task.input_from:
        return  # preset already wires it explicitly; don't override

    manager_task.input_from = {**manager_task.input_from, "past_lessons": source_task_id}
    if source_task_id not in manager_task.depends_on:
        manager_task.depends_on = list(manager_task.depends_on) + [source_task_id]
        manager_task.blocked_by = list(manager_task.depends_on)
        manager_task.status = TaskStatus.blocked

# Matches a whole-string ${ENV_VAR} or ${ENV_VAR:-default} placeholder. Only
# the entire model_name value is treated as a placeholder — this is not a
# general templating engine, so partial/mixed strings are left untouched.
_MODEL_ENV_PLACEHOLDER_RE = re.compile(
    r"^\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>.*))?\}$"
)


def _resolve_env_placeholder(value):
    """Resolve a whole-string ``${VAR}`` / ``${VAR:-default}`` env placeholder.

    Shared scalar resolver behind both Phase 3 ``model_name`` tiering and the
    Phase 4 ``debates: rounds`` field. Deliberately NOT a general templating
    engine — only a value that is *entirely* a placeholder is substituted.

    Rules:
        - ``None`` and non-``str`` values pass through unchanged.
        - A literal with no placeholder syntax passes through unchanged.
        - ``${VAR}`` -> ``os.environ["VAR"]`` when set and non-empty.
        - ``${VAR:-default}`` -> ``default`` when ``VAR`` is unset/empty.
        - Unset/empty with no (or empty) default -> ``None``.
    """
    if value is None or not isinstance(value, str):
        return value
    match = _MODEL_ENV_PLACEHOLDER_RE.match(value.strip())
    if not match:
        return value
    env_value = os.environ.get(match.group("name"), "")
    if env_value:
        return env_value
    default = match.group("default")
    return default or None


def _resolve_model_name(value: str | None) -> str | None:
    """Resolve a ``${ENV_VAR}`` / ``${ENV_VAR:-default}`` placeholder in a
    preset's ``model_name`` field, at preset-build time.

    This keeps presets provider-agnostic instead of hardcoding vendor model
    names (Phase 3 model tiering). Only ``model_name`` values are resolved
    this way — there is no general templating engine.

    Rules:
        - A literal value with no placeholder syntax passes through
          unchanged (e.g. ``model_name: foo`` stays ``"foo"``).
        - ``${VAR}`` resolves to ``os.environ["VAR"]`` when set and non-empty.
        - ``${VAR:-default}`` resolves to ``default`` when ``VAR`` is unset
          or empty.
        - If the env var is unset/empty and no default is given (or the
          default itself is empty), the result is ``None``, which causes the
          agent to fall back to the run's global model.

    Args:
        value: Raw ``model_name`` string from the preset YAML, or ``None``.

    Returns:
        The resolved model name, or ``None`` to fall back to the global model.
    """
    return _resolve_env_placeholder(value)


def _resolve_debate_rounds(raw, debate_id: str) -> int:
    """Resolve and validate a debate's ``rounds`` field at build time.

    ``raw`` may be a literal int (``rounds: 2``) or an env placeholder string
    (``rounds: ${VIBE_DEBATE_ROUNDS:-1}``). Missing / unresolved -> 1 (today's
    single-pass behavior). Non-integer, ``< 1``, or ``> _DEBATE_ROUNDS_CAP``
    raise ``ValueError`` — the guardrail that fails a misconfigured debate
    before any tokens are spent.
    """
    if raw is None:
        return 1
    if isinstance(raw, bool):
        raise ValueError(f"debate '{debate_id}' rounds must be an integer, got {raw!r}")
    if isinstance(raw, int):
        resolved = raw
    else:
        text = _resolve_env_placeholder(str(raw))
        if text is None or str(text).strip() == "":
            return 1
        try:
            resolved = int(str(text).strip())
        except ValueError as exc:
            raise ValueError(
                f"debate '{debate_id}' rounds must be an integer, got {text!r}"
            ) from exc
    if resolved < 1:
        raise ValueError(
            f"debate '{debate_id}' rounds must be >= 1, got {resolved}"
        )
    if resolved > _DEBATE_ROUNDS_CAP:
        raise ValueError(
            f"debate '{debate_id}' rounds={resolved} exceeds the cap of "
            f"{_DEBATE_ROUNDS_CAP}"
        )
    return resolved


def _make_task(
    task_id: str,
    agent_id: str,
    prompt_template: str,
    depends_on: list[str],
    input_from: dict[str, str],
) -> SwarmTask:
    """Construct a SwarmTask with blocked_by/status derived from depends_on."""
    deps = list(depends_on)
    status = TaskStatus.blocked if deps else TaskStatus.pending
    return SwarmTask(
        id=task_id,
        agent_id=agent_id,
        prompt_template=prompt_template,
        depends_on=deps,
        blocked_by=list(deps),
        input_from=dict(input_from),
        status=status,
    )


def _expand_debate(debate: dict, tasks_by_id: dict[str, SwarmTask]) -> list[SwarmTask]:
    """Unroll one ``debates:`` entry into chained round tasks (Phase 4).

    The engine's DAG stays single-pass and acyclic by construction: rounds are
    materialized here as a serial chain of ordinary tasks that the runtime
    executes natively. For participants ``[p0, p1, ...]`` and ``R`` rounds the
    alternation is ``p0_r1 -> p1_r1 -> ... -> p0_r2 -> ...``. Each round task's
    ``input_from`` carries the seed reports plus every prior round summary; the
    sink (judge/aggregator) receives the full alternation.

    Round 1 reuses each participant's legacy ``task_id`` (so unset envs ==
    today's graph byte-for-byte); rounds >= 2 append ``-r{n}``.

    Args:
        debate: One entry from the preset's ``debates:`` list.
        tasks_by_id: Base tasks already parsed from ``tasks:``; the sink is
            mutated in place to consume the transcript and depend on the final
            round task.

    Returns:
        The newly generated round tasks, in execution order.
    """
    debate_id = debate.get("id", "?")
    rounds = _resolve_debate_rounds(debate.get("rounds"), debate_id)
    participants = debate.get("participants") or []
    if not participants:
        raise ValueError(f"debate '{debate_id}' has no participants")
    seed_inputs: dict[str, str] = dict(debate.get("seed_inputs", {}) or {})
    entry_inputs: dict[str, str] = dict(debate.get("entry_inputs", {}) or {})
    sink_id = debate.get("sink")
    if sink_id not in tasks_by_id:
        raise ValueError(
            f"debate '{debate_id}' sink '{sink_id}' is not a defined task"
        )

    # Entry (round-1, first participant) depends on the seed report tasks only;
    # entry_inputs is context the opener also reads but does not gate on (the
    # engine resolves it transitively, matching the pre-Phase-4 wiring).
    entry_depends = list(dict.fromkeys(seed_inputs.values()))

    new_tasks: list[SwarmTask] = []
    transcript: dict[str, str] = {}  # running {input key -> round task id}
    last_task_id: str | None = None

    for rnd in range(1, rounds + 1):
        for idx, participant in enumerate(participants):
            seat = participant["seat"]
            summary_key = participant["summary_key"]
            base_id = participant["task_id"]
            is_entry = rnd == 1 and idx == 0

            task_id = base_id if rnd == 1 else f"{base_id}-r{rnd}"

            input_from = dict(seed_inputs)
            input_from.update(transcript)
            if idx == 0:
                # The opening seat cites entry_inputs (e.g. research_plan) in
                # EVERY round, not just round 1 — its rebuttal prompt is written
                # to reference that grounding. Applied last (over the transcript)
                # exactly as round 1 does, so round-1 wiring stays byte-for-byte.
                input_from.update(entry_inputs)

            depends_on = list(entry_depends) if is_entry else [last_task_id]

            if rnd == 1:
                prompt = participant["opener"]
            else:
                # Bake the round number in now so the runtime template only ever
                # sees the declared {target}/{timeframe} vars.
                prompt = str(participant["rebuttal"]).replace("{round}", str(rnd))

            new_tasks.append(_make_task(task_id, seat, prompt, depends_on, input_from))

            key = summary_key if rnd == 1 else f"{summary_key}_r{rnd}"
            transcript[key] = task_id
            last_task_id = task_id

    # Wire the sink: it consumes the whole transcript and runs after the last
    # round task. Its non-debate inputs / dependencies (declared in tasks:) are
    # preserved.
    sink = tasks_by_id[sink_id]
    sink.input_from = {**sink.input_from, **transcript}
    if last_task_id and last_task_id not in sink.depends_on:
        sink.depends_on = list(sink.depends_on) + [last_task_id]
        sink.blocked_by = list(sink.depends_on)
        sink.status = TaskStatus.blocked
    return new_tasks


def load_preset(name: str) -> dict:
    """Load a YAML preset by name.

    Args:
        name: Preset name (without .yaml extension).

    Returns:
        Parsed YAML dict.

    Raises:
        FileNotFoundError: If the preset file does not exist.
    """
    path = PRESETS_DIR / f"{name}.yaml"
    if not path.exists():
        available = [p.stem for p in PRESETS_DIR.glob("*.yaml")] if PRESETS_DIR.exists() else []
        raise FileNotFoundError(
            f"Preset {name!r} not found. Available: {available}"
        )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def list_presets() -> list[dict]:
    """Return summary info for all available presets.

    Returns:
        List of dicts with keys: name, title, description, agent_count, variables.
    """
    if not PRESETS_DIR.exists():
        return []

    results: list[dict] = []
    for path in sorted(PRESETS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        results.append({
            "name": data.get("name", path.stem),
            "title": data.get("title", ""),
            "description": data.get("description", ""),
            "agent_count": len(data.get("agents", [])),
            "variables": data.get("variables", []),
        })

    return results


def _declared_variable_names(raw_variables: list) -> set[str]:
    """Extract variable names from the YAML variables section."""
    names: set[str] = set()
    for item in raw_variables:
        if isinstance(item, dict):
            name = item.get("name")
        else:
            name = str(item)
        if name:
            names.add(str(name))
    return names


def _template_variables(template: str) -> set[str]:
    """Return Python format fields referenced by a prompt template."""
    variables: set[str] = set()
    for _, field_name, _, _ in Formatter().parse(template or ""):
        if not field_name:
            continue
        root = field_name.split(".", 1)[0].split("[", 1)[0]
        if root and root not in _INTERNAL_TEMPLATE_VARS:
            variables.add(root)
    return variables


def inspect_preset(name: str) -> dict:
    """Validate a swarm preset and return a dry-run execution plan.

    This does not start workers or call an LLM. It catches common YAML/DAG
    mistakes early and exposes the topological task layers used by the runtime.
    """
    data = load_preset(name)
    run = build_run_from_preset(name, {})

    errors: list[str] = []
    warnings: list[str] = []

    agent_ids = [agent.id for agent in run.agents]
    task_ids = [task.id for task in run.tasks]
    agent_id_set = set(agent_ids)
    task_id_set = set(task_ids)

    for duplicate in sorted(item for item, count in Counter(agent_ids).items() if count > 1):
        errors.append(f"Duplicate agent id: {duplicate}")
    for duplicate in sorted(item for item, count in Counter(task_ids).items() if count > 1):
        errors.append(f"Duplicate task id: {duplicate}")

    for task in run.tasks:
        if task.agent_id not in agent_id_set:
            errors.append(f"Task '{task.id}' references unknown agent '{task.agent_id}'")
        for _, upstream_task_id in task.input_from.items():
            if upstream_task_id not in task_id_set:
                errors.append(
                    f"Task '{task.id}' input_from references unknown task '{upstream_task_id}'"
                )

    layers: list[list[str]] = []
    try:
        validate_dag(run.tasks)
        layers = topological_layers(run.tasks)
    except ValueError as exc:
        errors.append(str(exc))

    dependents: dict[str, list[str]] = defaultdict(list)
    for task in run.tasks:
        for dep in task.depends_on:
            dependents[dep].append(task.id)

    def is_upstream(candidate: str, task_id: str) -> bool:
        """Return whether candidate can reach task_id through dependency edges."""
        seen: set[str] = set()
        stack = [candidate]
        while stack:
            current = stack.pop()
            if current == task_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(dependents.get(current, []))
        return False

    for task in run.tasks:
        for key, upstream_task_id in task.input_from.items():
            if upstream_task_id in task_id_set and not is_upstream(upstream_task_id, task.id):
                warnings.append(
                    f"Task '{task.id}' input_from '{key}' references '{upstream_task_id}', "
                    "which is not upstream in the DAG"
                )

    declared_variables = _declared_variable_names(data.get("variables", []))
    used_variables: set[str] = set()
    for task in data.get("tasks", []):
        try:
            used_variables.update(_template_variables(task.get("prompt_template", "")))
        except ValueError as exc:
            errors.append(f"Task '{task.get('id', '?')}' has invalid prompt template: {exc}")
    # Debate (Phase 4) opener/rebuttal templates also render at runtime; scan
    # them so undeclared variables surface in the dry run too.
    for debate in data.get("debates", []) or []:
        for participant in debate.get("participants", []) or []:
            for field in ("opener", "rebuttal"):
                try:
                    used_variables.update(_template_variables(participant.get(field, "")))
                except ValueError as exc:
                    errors.append(
                        f"Debate '{debate.get('id', '?')}' participant "
                        f"'{participant.get('seat', '?')}' has invalid {field} "
                        f"template: {exc}"
                    )

    missing_declarations = sorted(used_variables - declared_variables)
    unused_declarations = sorted(declared_variables - used_variables)
    if missing_declarations:
        warnings.append(
            "Prompt templates use undeclared variables: " + ", ".join(missing_declarations)
        )
    if unused_declarations:
        warnings.append(
            "Declared variables are not used by task prompt templates: "
            + ", ".join(unused_declarations)
        )

    task_agent = {task.id: task.agent_id for task in run.tasks}
    return {
        "name": data.get("name", name),
        "title": data.get("title", ""),
        "description": data.get("description", ""),
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "variables": sorted(declared_variables),
        "used_variables": sorted(used_variables),
        "agents": [
            {
                "id": agent.id,
                "role": agent.role,
                "tools": agent.tools,
                "skills": agent.skills,
                # Resolved model_name (${ENV_VAR} placeholders already
                # substituted by build_run_from_preset). None means the
                # agent falls back to the run's global model.
                "model": agent.model_name,
            }
            for agent in run.agents
        ],
        "tasks": [
            {
                "id": task.id,
                "agent_id": task.agent_id,
                "depends_on": task.depends_on,
                "input_from": task.input_from,
            }
            for task in run.tasks
        ],
        "layers": [
            [{"task_id": task_id, "agent_id": task_agent.get(task_id, "")} for task_id in layer]
            for layer in layers
        ],
    }


def build_run_from_preset(preset_name: str, user_vars: dict[str, str]) -> SwarmRun:
    """Create a SwarmRun from a preset with user variables applied.

    Steps:
        1. Load preset YAML
        2. Create SwarmAgentSpec list from agents section
        3. Create SwarmTask list from tasks section
        4. Generate run_id: f"swarm-{datetime}-{uuid[:8]}"
        5. Return SwarmRun with all fields populated

    Args:
        preset_name: Name of the preset to load.
        user_vars: User-provided variables for prompt template rendering.

    Returns:
        Fully constructed SwarmRun instance (status=pending).

    Raises:
        FileNotFoundError: If preset does not exist.
        ValueError: If preset YAML is malformed.
    """
    data = load_preset(preset_name)

    # Parse agents
    agents: list[SwarmAgentSpec] = []
    for agent_data in data.get("agents", []):
        agents.append(SwarmAgentSpec(
            id=agent_data["id"],
            role=agent_data.get("role", ""),
            system_prompt=agent_data.get("system_prompt", ""),
            tools=agent_data.get("tools", []),
            skills=agent_data.get("skills", []),
            max_iterations=agent_data.get("max_iterations", 25),
            timeout_seconds=agent_data.get("timeout_seconds", 300),
            model_name=_resolve_model_name(agent_data.get("model_name")),
            max_retries=agent_data.get("max_retries", 2),
        ))

    # Parse tasks, initialize blocked_by from depends_on
    tasks: list[SwarmTask] = []
    for task_data in data.get("tasks", []):
        depends_on = task_data.get("depends_on", [])
        tasks.append(_make_task(
            task_data["id"],
            task_data["agent_id"],
            task_data.get("prompt_template", ""),
            depends_on,
            task_data.get("input_from", {}),
        ))

    # Phase 4: unroll any debates: sugar into chained round tasks. Round tasks
    # are inserted immediately before their sink so the task order mirrors the
    # pre-Phase-4 layout at rounds=1. The sink task is mutated in place.
    tasks_by_id = {t.id: t for t in tasks}
    inserts_before_sink: dict[str, list[SwarmTask]] = {}
    for debate in data.get("debates", []) or []:
        round_tasks = _expand_debate(debate, tasks_by_id)
        inserts_before_sink.setdefault(debate["sink"], []).extend(round_tasks)
    if inserts_before_sink:
        expanded: list[SwarmTask] = []
        for task in tasks:
            expanded.extend(inserts_before_sink.get(task.id, []))
            expanded.append(task)
        tasks = expanded

    # Phase 6 — optional flagged experiment (default OFF): mirror the PM's
    # past_lessons input onto the research_manager task too.
    _inject_lessons_into_manager(tasks)

    # Generate run ID
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d-%H%M%S")
    short_uuid = uuid.uuid4().hex[:8]
    run_id = f"swarm-{ts}-{short_uuid}"

    return SwarmRun(
        id=run_id,
        preset_name=preset_name,
        status=RunStatus.pending,
        user_vars=user_vars,
        agents=agents,
        tasks=tasks,
        created_at=now.isoformat(),
    )
