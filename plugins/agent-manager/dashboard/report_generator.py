"""
工作汇报生成器 — 从 SessionDB 查询数据，生成日报/周报。
被 cron 任务调用，也支持手动触发。
"""

import json
import os
import sys
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Use the same path convention as plugin_api.py
_HOME = Path("/home/agentuser")
HERMES_HOME = _HOME / ".hermes"
PROFILE_HOME = HERMES_HOME / "profiles" / "fullstack-bot"

REPORTS_FILE = PROFILE_HOME / "reports.json"

BEIJING_TZ = timezone(timedelta(hours=8))


def _get_db_path():
    """Locate the sessions database."""
    candidates = [
        PROFILE_HOME / "state.db",
        HERMES_HOME / "state.db",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


def _load_reports():
    """Load existing reports from JSON."""
    if REPORTS_FILE.exists():
        try:
            with open(REPORTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def _save_reports(reports):
    """Save reports to JSON."""
    REPORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORTS_FILE, "w", encoding="utf-8") as f:
        json.dump(reports, f, ensure_ascii=False, indent=2)


def collect_sessions(db_path, since_dt, until_dt=None):
    """Query sessions within a time range."""
    if not db_path:
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if until_dt is None:
        until_dt = datetime.now(BEIJING_TZ)

    since_ts = since_dt.timestamp()
    until_ts = until_dt.timestamp()

    query = """
        SELECT id, title, started_at, ended_at, source as platform, user_id
        FROM sessions
        WHERE started_at >= ? AND started_at < ?
        ORDER BY started_at DESC
    """
    cur = conn.execute(query, (since_ts, until_ts))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def generate_daily_report():
    """Generate today's work report for all agents."""
    db_path = _get_db_path()
    now = datetime.now(BEIJING_TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    sessions = collect_sessions(db_path, today_start, now)

    # Aggregate by agent title (or platform)
    agents = {}
    for s in sessions:
        agent_name = s.get("title") or s.get("platform") or "默认Agent"
        if agent_name not in agents:
            agents[agent_name] = {"sessions": [], "platforms": set(), "users": set()}
        agents[agent_name]["sessions"].append(s)
        if s.get("platform"):
            agents[agent_name]["platforms"].add(s["platform"])
        if s.get("user_id"):
            agents[agent_name]["users"].add(s["user_id"])

    report_items = []
    for agent_name, data in agents.items():
        sessions_list = data["sessions"]
        total_duration = 0
        for s in sessions_list:
            if s.get("started_at") and s.get("ended_at"):
                try:
                    started = s["started_at"]
                    ended = s["ended_at"]
                    total_duration += ended - started
                except (ValueError, TypeError):
                    pass

        report_items.append({
            "agent": agent_name,
            "session_count": len(sessions_list),
            "platforms": list(data["platforms"]),
            "users": list(data["users"]),
            "duration_seconds": total_duration,
            "session_ids": [s["id"] for s in sessions_list[:20]],
        })

    report = {
        "id": now.strftime("daily_%Y%m%d"),
        "type": "daily",
        "title": "%s 日报" % now.strftime("%Y-%m-%d"),
        "created_at": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "weekday": now.weekday(),
        "agents": report_items,
        "total_sessions": len(sessions),
        "total_agents": len(agents),
    }

    reports = _load_reports()
    # Replace existing daily report for today if exists
    reports = [r for r in reports if r.get("id") != report["id"]]
    reports.insert(0, report)
    _save_reports(reports)
    return report


def generate_weekly_report():
    """Generate this week's work summary."""
    db_path = _get_db_path()
    now = datetime.now(BEIJING_TZ)
    # Monday of this week
    week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

    sessions = collect_sessions(db_path, week_start, now)

    # Aggregate by agent
    agents = {}
    for s in sessions:
        agent_name = s.get("title") or s.get("platform") or "默认Agent"
        if agent_name not in agents:
            agents[agent_name] = {
                "sessions": [], "platforms": set(), "users": set(),
                "daily_counts": {}
            }
        agents[agent_name]["sessions"].append(s)
        if s.get("platform"):
            agents[agent_name]["platforms"].add(s["platform"])
        if s.get("user_id"):
            agents[agent_name]["users"].add(s["user_id"])
        # Track per-day counts
        started = s.get("started_at", 0)
        day = datetime.fromtimestamp(started, tz=BEIJING_TZ).strftime("%Y-%m-%d") if started else "unknown"
        if day not in agents[agent_name]["daily_counts"]:
            agents[agent_name]["daily_counts"][day] = 0
        agents[agent_name]["daily_counts"][day] += 1

    report_items = []
    total_duration = 0
    for agent_name, data in agents.items():
        sessions_list = data["sessions"]
        agent_duration = 0
        for s in sessions_list:
            if s.get("started_at") and s.get("ended_at"):
                try:
                    agent_duration += s["ended_at"] - s["started_at"]
                except (ValueError, TypeError):
                    pass
        total_duration += agent_duration

        report_items.append({
            "agent": agent_name,
            "session_count": len(sessions_list),
            "platforms": list(data["platforms"]),
            "users": list(data["users"]),
            "duration_seconds": agent_duration,
            "daily_counts": data["daily_counts"],
            "session_ids": [s["id"] for s in sessions_list[:50]],
        })

    report = {
        "id": "weekly_%s" % now.strftime("%Y%m%d"),
        "type": "weekly",
        "title": "%s 周报" % now.strftime("%Y年第%W周"),
        "created_at": now.isoformat(),
        "week_start": week_start.strftime("%Y-%m-%d"),
        "week_end": now.strftime("%Y-%m-%d"),
        "agents": report_items,
        "total_sessions": len(sessions),
        "total_agents": len(agents),
        "total_duration_seconds": total_duration,
    }

    reports = _load_reports()
    # Replace existing weekly report for this week
    reports = [r for r in reports if r.get("id") != report["id"]]
    reports.insert(0, report)
    _save_reports(reports)
    return report


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if mode == "daily":
        r = generate_daily_report()
        print("Daily report generated:", r.get("title"), "-", r.get("total_sessions"), "sessions")
    elif mode == "weekly":
        r = generate_weekly_report()
        print("Weekly report generated:", r.get("title"), "-", r.get("total_sessions"), "sessions")
    elif mode == "list":
        reports = _load_reports()
        for r in reports[:5]:
            print("[%s] %s - %s agents, %s sessions" % (r.get("type"), r.get("title"), r.get("total_agents"), r.get("total_sessions")))
    else:
        print("Usage: python report_generator.py [daily|weekly|list]")
