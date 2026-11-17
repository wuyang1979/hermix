"""Workflow Manager plugin — backend API routes.

Mounted at /api/plugins/workflow-manager/ by the dashboard plugin system.
Provides cross-profile cron job listing with categorization.
"""

import json
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()

_HOME = Path("/home/agentuser")
HERMES_HOME = _HOME / ".hermes"
PROFILES_DIR = HERMES_HOME / "profiles"

# Color palette for profiles
_PROFILE_COLORS = ["#60A5FA", "#34D399", "#F59E0B", "#A78BFA", "#F472B6", "#6B7280",
                    "#FB923C", "#38BDF8", "#4ADE80", "#C084FC"]

def _get_profile_meta(profile_name: str) -> dict:
    """Read display_name from config.yaml, derive icon/color dynamically."""
    import yaml as _yaml
    config_path = PROFILES_DIR / profile_name / "config.yaml"
    display_name = profile_name
    if config_path.exists():
        try:
            cfg = _yaml.safe_load(config_path.read_text()) or {}
            dn = cfg.get("display_name", "")
            if dn:
                display_name = dn
        except Exception:
            pass

    # Derive icon from first emoji in name, or use 🎯 default
    import re as _re
    emoji_match = _re.search(r'[\U0001F300-\U0001FFFF\u2600-\u27BF\u2B50]', display_name)
    icon = emoji_match.group(0) if emoji_match else "🎯"

    # Assign color by position hash
    color_idx = hash(profile_name) % len(_PROFILE_COLORS)
    color = _PROFILE_COLORS[color_idx]

    return {"label": display_name, "icon": icon, "color": color}


# Map icon/color overrides for special profiles (keep visual consistency)
_ICON_OVERRIDES = {
    "fullstack-bot": "📝",
    "xiaoying": "📢",
    "xiaozhua": "🐙",
    "xiaozhu": "🏨",
    "intel-sentinel": "🔍",
    "main-bot": "⚙️",
}

def _get_icon(profile_name: str, fallback: str) -> str:
    return _ICON_OVERRIDES.get(profile_name, fallback)


def _parse_cron_list(profile_name: str) -> list[dict]:
    """Run `hermes -p <profile> cron list` and parse structured output."""
    try:
        result = subprocess.run(
            ["hermes", "-p", profile_name, "cron", "list"],
            capture_output=True, text=True, timeout=15,
            cwd=str(_HOME),
        )
        output = result.stdout
        if not output.strip():
            return []
    except Exception as e:
        logger.warning("Failed to list cron for %s: %s", profile_name, e)
        return []

    jobs = []
    lines = output.split("\n")
    current = {}
    for line in lines:
        line = line.strip()
        # Match job ID line: "  a80d639d23bc [active]"
        if len(line) >= 12 and line[:12].strip() and line[0] != "┌" and line[0] != "│" and line[0] != "└":
            parts = line.split(None, 1)
            if parts and len(parts[0]) == 12 and parts[0].isalnum():
                if current and "id" in current:
                    jobs.append(current)
                current = {"id": parts[0], "status": "active"}
                if "[paused]" in line:
                    current["status"] = "paused"
                continue

        if not current:
            continue

        if line.startswith("Name:"):
            current["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Schedule:"):
            current["schedule"] = line.split(":", 1)[1].strip()
        elif line.startswith("Repeat:"):
            current["repeat"] = line.split(":", 1)[1].strip()
        elif line.startswith("Next run:"):
            current["next_run"] = line.split(":", 1)[1].strip()
        elif line.startswith("Deliver:"):
            current["deliver"] = line.split(":", 1)[1].strip()
        elif line.startswith("Script:"):
            current["script"] = line.split(":", 1)[1].strip()
        elif line.startswith("Skills:"):
            current["skills"] = line.split(":", 1)[1].strip()
        elif line.startswith("Workdir:"):
            current["workdir"] = line.split(":", 1)[1].strip()
        elif line.startswith("Last run:"):
            last = line.split(":", 1)[1].strip()
            parts_last = last.rsplit("  ", 1)
            current["last_run"] = parts_last[0].strip()
            current["last_status"] = parts_last[1].strip() if len(parts_last) > 1 else "unknown"

    if current and "id" in current:
        jobs.append(current)

    return jobs


@router.get("/workflows")
async def get_workflows():
    """Return all cron jobs grouped by profile."""
    groups = []
    total = 0

    for profile_dir in sorted(PROFILES_DIR.iterdir()):
        if not profile_dir.is_dir():
            continue
        profile_name = profile_dir.name
        config_path = profile_dir / "config.yaml"
        if not config_path.exists():
            continue

        jobs = _parse_cron_list(profile_name)
        if not jobs:
            continue

        meta = _get_profile_meta(profile_name)
        meta["icon"] = _get_icon(profile_name, meta["icon"])

        # Add profile context to each job
        for job in jobs:
            job["profile"] = profile_name
            job["profile_label"] = meta["label"]
            job["profile_icon"] = meta["icon"]
            job["profile_color"] = meta["color"]

        groups.append({
            "profile": profile_name,
            "label": meta["label"],
            "icon": meta["icon"],
            "color": meta["color"],
            "count": len(jobs),
            "jobs": jobs,
        })
        total += len(jobs)

    return {
        "total": total,
        "groups": groups,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/health")
async def health():
    return {"status": "ok", "plugin": "workflow-manager"}
