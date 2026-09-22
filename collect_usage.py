#!/usr/bin/env python3
"""Collects local Claude Code + Codex CLI token usage into data/usage-data.json.

Reads only local session logs already on this machine:
  - ~/.claude/projects/**/*.jsonl   (Claude Code transcripts)
  - ~/.codex/sessions/**/*.jsonl and ~/.codex/archived_sessions/*.jsonl (Codex CLI rollouts)

Writes only aggregated numbers (dates, models, projects, token counts, estimated cost).
Never writes prompt/response content. No API keys are used or required by this script.
"""
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
CLAUDE_PROJECTS_DIR = HOME / ".claude" / "projects"
CODEX_SESSION_DIRS = [HOME / ".codex" / "sessions", HOME / ".codex" / "archived_sessions"]
OUT_PATH = Path(__file__).parent / "data" / "usage-data.json"

# Approximate USD per 1M tokens, matched by model-name prefix. Anthropic hasn't
# changed these input/output/cache ratios in a long time, but exact per-model
# rates do change — update this table if a new model family shows up in the
# charts with $0.00 cost.
CLAUDE_PRICING = [
    # (prefix, input, output, cache_write, cache_read)  -- USD / 1M tokens
    ("claude-opus", 15.00, 75.00, 18.75, 1.50),
    ("claude-sonnet", 3.00, 15.00, 3.75, 0.30),
    # Fable is a lighter/faster tier per Anthropic's docs; no published price seen
    # yet, so this reuses Haiku-tier rates as a rough placeholder — update when known.
    ("claude-fable", 0.80, 4.00, 1.00, 0.08),
    ("claude-haiku", 0.80, 4.00, 1.00, 0.08),
]

# Internal/non-billable markers Claude Code sometimes writes into usage records
# (e.g. locally-synthesized messages with no real API call behind them).
ZERO_COST_MODELS = {"<synthetic>"}


def claude_cost(model, usage):
    if model in ZERO_COST_MODELS:
        return 0.0
    rates = None
    for prefix, i, o, cw, cr in CLAUDE_PRICING:
        if model and model.startswith(prefix):
            rates = (i, o, cw, cr)
            break
    if rates is None:
        return None
    i, o, cw, cr = rates
    input_tokens = usage.get("input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    cache_write = usage.get("cache_creation_input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    return (
        input_tokens * i
        + output_tokens * o
        + cache_write * cw
        + cache_read * cr
    ) / 1_000_000


def decode_claude_project_dir(dirname: str) -> str:
    """Best-effort recovery of a real path from Claude Code's flattened dir name."""
    raw = dirname.lstrip("-")
    parts = raw.split("-")
    # Try to find the longest suffix of parts that forms a real existing path
    # when joined with "/", merging remaining parts with "-" as we go.
    for split_at in range(0, len(parts)):
        candidate_parts = parts[: split_at + 1]
        rest = parts[split_at + 1 :]
        path_guess = "/" + "/".join(candidate_parts)
        if rest:
            path_guess = path_guess + "-" + "-".join(rest)
        if os.path.exists(path_guess):
            return os.path.basename(path_guess)
    # Fallback: just show the flattened name, swapping dashes for slashes.
    return raw.replace("-", "/").split("/")[-1] or raw


def collect_claude():
    daily = defaultdict(lambda: defaultdict(float))  # date -> field -> value
    by_model = defaultdict(float)
    by_project = defaultdict(float)
    cost_unknown_models = set()

    if not CLAUDE_PROJECTS_DIR.exists():
        return {"daily": [], "byModel": [], "byProject": [], "costUnknownModels": []}

    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        project_label = decode_claude_project_dir(project_dir.name)
        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                with jsonl_file.open("r", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        message = entry.get("message")
                        if not isinstance(message, dict):
                            continue
                        usage = message.get("usage")
                        if not isinstance(usage, dict):
                            continue
                        ts = entry.get("timestamp")
                        if not ts:
                            continue
                        try:
                            date = datetime.fromisoformat(ts.replace("Z", "+00:00")).date().isoformat()
                        except ValueError:
                            continue
                        model = message.get("model") or "unknown"

                        total_tokens = (
                            (usage.get("input_tokens", 0) or 0)
                            + (usage.get("output_tokens", 0) or 0)
                            + (usage.get("cache_creation_input_tokens", 0) or 0)
                            + (usage.get("cache_read_input_tokens", 0) or 0)
                        )
                        cost = claude_cost(model, usage)
                        if cost is None:
                            cost_unknown_models.add(model)
                            cost = 0.0

                        daily[date]["tokens"] += total_tokens
                        daily[date]["cost"] += cost
                        by_model[model] += total_tokens
                        by_project[project_label] += total_tokens
            except OSError:
                continue

    daily_list = [
        {"date": d, "tokens": round(v["tokens"]), "cost": round(v["cost"], 4)}
        for d, v in sorted(daily.items())
    ]
    return {
        "daily": daily_list,
        "byModel": [{"model": m, "tokens": round(t)} for m, t in sorted(by_model.items(), key=lambda x: -x[1])],
        "byProject": [{"project": p, "tokens": round(t)} for p, t in sorted(by_project.items(), key=lambda x: -x[1])],
        "costUnknownModels": sorted(cost_unknown_models),
    }


def collect_codex():
    daily = defaultdict(float)  # date -> total tokens added this day
    by_project = defaultdict(float)
    by_model = defaultdict(float)
    rl_by_date = {}  # date -> {usedPercent, planType, windowMinutes} (max usedPercent seen that day)
    sessions = []  # one row per session file: for spotting the single runaway session
    session_by_id = {}  # session_id -> session summary, for fan-out parent lookups
    children_by_parent = defaultdict(list)  # parent_thread_id -> [child spawn records]

    seen_files = set()
    files = []
    for base in CODEX_SESSION_DIRS:
        if base.exists():
            files.extend(base.rglob("*.jsonl"))

    for jsonl_file in files:
        if jsonl_file in seen_files:
            continue
        seen_files.add(jsonl_file)

        session_cwd = None
        session_model = "unknown"
        session_id = None
        parent_thread_id = None
        agent_nickname = None
        spawn_timestamp = None
        last_total = 0
        first_date = None
        last_date = None
        session_start_percent = None
        session_end_percent = None

        try:
            with jsonl_file.open("r", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    etype = entry.get("type")
                    payload = entry.get("payload") or {}

                    if etype == "session_meta":
                        session_cwd = payload.get("cwd")
                        session_model = payload.get("model") or session_model
                        session_id = payload.get("id") or payload.get("session_id")
                        spawn_timestamp = payload.get("timestamp") or entry.get("timestamp")
                        source = payload.get("source")
                        if isinstance(source, dict):
                            thread_spawn = source.get("subagent", {}).get("thread_spawn")
                            if thread_spawn:
                                parent_thread_id = thread_spawn.get("parent_thread_id")
                                agent_nickname = thread_spawn.get("agent_nickname")

                    if etype == "turn_context":
                        session_model = payload.get("model") or session_model

                    if etype == "event_msg" and payload.get("type") == "token_count":
                        ts = entry.get("timestamp")
                        try:
                            date = datetime.fromisoformat(ts.replace("Z", "+00:00")).date().isoformat()
                        except (ValueError, AttributeError):
                            continue
                        first_date = first_date or date
                        last_date = date

                        info = payload.get("info") or {}
                        total_usage = info.get("total_token_usage") or {}
                        cumulative_total = total_usage.get("total_tokens", 0) or 0

                        # total_token_usage is cumulative for the life of this session
                        # file (including resumes); take the delta since the last
                        # sample so a multi-day session attributes tokens to the day
                        # they were actually used, not all to its first day.
                        delta = cumulative_total - last_total
                        if delta > 0:
                            daily[date] += delta
                            if session_cwd:
                                by_project[os.path.basename(session_cwd.rstrip("/"))] += delta
                            by_model[session_model] += delta
                        last_total = cumulative_total

                        rate_limits = payload.get("rate_limits")
                        if rate_limits and rate_limits.get("primary"):
                            primary = rate_limits["primary"]
                            used_percent = primary.get("used_percent")
                            if session_start_percent is None:
                                session_start_percent = used_percent
                            session_end_percent = used_percent
                            existing = rl_by_date.get(date)
                            if used_percent is not None and (
                                existing is None or used_percent > existing["usedPercent"]
                            ):
                                rl_by_date[date] = {
                                    "date": date,
                                    "usedPercent": used_percent,
                                    "planType": rate_limits.get("plan_type"),
                                    "windowMinutes": primary.get("window_minutes"),
                                    "resetsAt": primary.get("resets_at"),
                                }
        except OSError:
            continue

        project_label = os.path.basename(session_cwd.rstrip("/")) if session_cwd else "unknown"

        if last_total > 0:
            session_record = {
                "file": jsonl_file.name,
                "sessionId": session_id,
                "startDate": first_date,
                "endDate": last_date,
                "project": project_label,
                "model": session_model,
                "tokens": round(last_total),
                "agentNickname": agent_nickname,
                "rateLimitPercentDelta": (
                    round(session_end_percent - session_start_percent, 1)
                    if session_start_percent is not None and session_end_percent is not None
                    else None
                ),
            }
            sessions.append(session_record)
            if session_id:
                session_by_id[session_id] = session_record

        if parent_thread_id:
            try:
                spawn_date = datetime.fromisoformat(
                    (spawn_timestamp or "").replace("Z", "+00:00")
                ).date().isoformat()
            except ValueError:
                spawn_date = first_date
            children_by_parent[parent_thread_id].append(
                {
                    "file": jsonl_file.name,
                    "agentNickname": agent_nickname,
                    "spawnDate": spawn_date,
                    "tokens": round(last_total),
                }
            )

    daily_list = [{"date": d, "tokens": round(t)} for d, t in sorted(daily.items())]
    rate_limit_list = [rl_by_date[d] for d in sorted(rl_by_date)]
    top_sessions = sorted(sessions, key=lambda s: -s["tokens"])[:25]

    # Fan-out detection: a long-running thread that spawned subagents. Each
    # subagent inherits a slice of the PARENT thread's accumulated context, so
    # the cost of a fan-out scales with how old/large the parent thread already
    # was at spawn time — that compounding is the thing worth surfacing.
    fanouts = []
    for parent_id, children in children_by_parent.items():
        parent = session_by_id.get(parent_id)
        spawn_dates = sorted(c["spawnDate"] for c in children if c["spawnDate"])
        thread_age_days = None
        if parent and parent.get("startDate") and spawn_dates:
            try:
                start = datetime.fromisoformat(parent["startDate"])
                first_spawn = datetime.fromisoformat(spawn_dates[0])
                thread_age_days = (first_spawn - start).days
            except ValueError:
                thread_age_days = None
        subagent_total = sum(c["tokens"] for c in children)
        fanouts.append(
            {
                "parentProject": parent["project"] if parent else "unknown",
                "parentStartDate": parent["startDate"] if parent else None,
                "parentTokens": parent["tokens"] if parent else None,
                "subagentCount": len(children),
                "subagentTotalTokens": subagent_total,
                "grandTotalTokens": (parent["tokens"] if parent else 0) + subagent_total,
                "threadAgeAtFirstSpawnDays": thread_age_days,
                "spawnDates": spawn_dates,
                "agentNicknames": [c["agentNickname"] for c in children if c["agentNickname"]],
            }
        )
    fanouts.sort(key=lambda f: -f["grandTotalTokens"])

    return {
        "daily": daily_list,
        "byModel": [{"model": m, "tokens": round(t)} for m, t in sorted(by_model.items(), key=lambda x: -x[1])],
        "byProject": [{"project": p, "tokens": round(t)} for p, t in sorted(by_project.items(), key=lambda x: -x[1])],
        "rateLimitHistory": rate_limit_list,
        "topSessions": top_sessions,
        "threadFanouts": fanouts[:15],
    }


def main():
    data = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "claude": collect_claude(),
        "codex": collect_codex(),
        "grok": None,  # placeholder for future xAI integration
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, indent=2))
    print(f"Wrote {OUT_PATH}")
    print(f"Claude: {len(data['claude']['daily'])} days, {sum(d['tokens'] for d in data['claude']['daily']):,} tokens")
    print(f"Codex:  {len(data['codex']['daily'])} days, {sum(d['tokens'] for d in data['codex']['daily']):,} tokens")
    if data["claude"]["costUnknownModels"]:
        print(f"NOTE: no pricing configured for Claude models: {data['claude']['costUnknownModels']}")


if __name__ == "__main__":
    main()
