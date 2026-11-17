"""Agent Manager plugin — backend API routes.

Mounted at /api/plugins/agent-manager/ by the dashboard plugin system.
Provides agent status, gateway/connection control, token usage,
cron jobs, skills management, and knowledge base browsing.
"""

import json
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# Resolve Hermes home dynamically so deployments on different servers
# auto-discover the correct agents, profiles, and knowledge bases.
def _get_hermes_home() -> Path:
    """Resolve HERMES_HOME at import time, accounting for profile mode."""
    try:
        from hermes_constants import get_default_hermes_root as _ghr
        return _ghr()
    except ImportError:
        pass
    val = os.environ.get("HERMES_HOME", "").strip()
    if not val:
        return Path.home() / ".hermes"
    # Detect profile mode: if HERMES_HOME is under ~/.hermes/profiles/,
    # return ~/.hermes as the root
    env_path = Path(val)
    native = Path.home() / ".hermes"
    try:
        env_path.resolve().relative_to(native.resolve())
        return native
    except ValueError:
        return env_path

HERMES_HOME = _get_hermes_home()
PROFILES_DIR = HERMES_HOME / "profiles"
KB_DIR = HERMES_HOME / "hermes-agent" / "wechat_assistants" / "knowledge_bases"

def _resolve_hermes_bin() -> str:
    """Resolve the hermes binary path dynamically for any deployment server."""
    # Try common install locations in order
    candidates = [
        Path.home() / ".local" / "bin" / "hermes",
        Path("/usr/local/bin/hermes"),
        Path("/usr/bin/hermes"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    # Fallback: try $HOME/.local/bin and hope it's on PATH
    return str(Path.home() / ".local" / "bin" / "hermes")

# Map directory names to permission keys (used for agent→kb filtering)
_KB_DIR_TO_KEY = {
    "tech_expert": "tech",
    "business_advisor": "business",
    "creative_designer": "creative",
    "customer_service": "customer",
    "internal_assistant": "internal",
    "shared": "shared",
}

# Chinese display name fallback when config.yaml has no display_name.
# Deliberately empty in the open-source build — configure `display_name`
# in each profile's config.yaml, or add your own entries here.
_CHINESE_NAME_FALLBACK: dict[str, str] = {}

# Agent display order (by AI-XXX number). Agents not listed sort alphabetically at end.
AGENT_ORDER = {
    "main-bot": 1, "default": 1,
    "binance-bot": 2, "fullstack-bot": 3,
    "intel-sentinel": 4, "sales-advisor": 5,
    "xiao-pin": 6, "xiaoce": 7, "xiaojiu": 8,
    "xiaomi": 9, "xiaoyi": 10, "xiaoying": 11,
    "xiaozhu": 12, "xiaozhua": 13, "xiaozi": 14,
    "zhengce": 15,
}


def get_all_agent_processes() -> list[dict]:
    """Scan all running Hermes agent/gateway processes."""
    agents = []
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.split("\n")[1:]:  # skip header
            if not line.strip():
                continue
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            cmdline = parts[10]
            if "hermes" not in cmdline and "hermes_cli" not in cmdline:
                continue

            pid = parts[1]
            cpu = parts[2]
            mem = parts[3]
            start_time_str = parts[8]

            # Determine profile and agent type
            profile = "default"
            agent_type = "unknown"

            if "gateway" in cmdline:
                agent_type = "gateway"
            elif "dashboard" in cmdline:
                agent_type = "dashboard"
            elif "run_agent" in cmdline or "batch_runner" in cmdline:
                agent_type = "agent"

            # Extract profile from -p or --profile flag in the command line
            cmdline_str = parts[10] if len(parts) > 10 else ""
            import re as _re
            m = _re.search(r'(?:-p|--profile)\s+(\S+)', cmdline_str)
            if m:
                profile = m.group(1)

            # For main profile (no -p flag), detect from cmdline
            if profile == "default":
                if " -p " not in cmdline:
                    if " -m hermes_cli.main gateway" in cmdline or ".local/bin/hermes gateway" in cmdline:
                        profile = "main-bot"

            # Get process start time for uptime calculation
            try:
                proc_start = _get_process_start_time(int(pid))
                uptime_seconds = time.time() - proc_start if proc_start else 0
            except Exception:
                uptime_seconds = 0

            agents.append({
                "pid": int(pid),
                "profile": profile,
                "type": agent_type,
                "cmdline": cmdline[:200],
                "cpu_percent": cpu,
                "mem_percent": mem,
                "start_time": start_time_str,
                "uptime_seconds": uptime_seconds,
            })
    except Exception as e:
        logger.error(f"Failed to scan processes: {e}")

    return agents


def _get_process_start_time(pid: int) -> float | None:
    """Get process start time from /proc."""
    try:
        import psutil
        p = psutil.Process(pid)
        return p.create_time()
    except (ImportError, Exception):
        pass
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        parts = stat.split()
        if len(parts) > 21:
            import os as _os
            CLK_TCK = _os.sysconf(_os.sysconf_names["SC_CLK_TCK"])
            with open("/proc/uptime") as f:
                uptime = float(f.read().split()[0])
            start_jiffies = int(parts[21])
            start_time = time.time() - uptime + (start_jiffies / CLK_TCK)
            return start_time
    except Exception:
        pass
    return None


def get_profile_config(profile_name: str) -> dict:
    """Read profile config for a given profile."""
    config_path = PROFILES_DIR / profile_name / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        # Use proper YAML parsing for full config access
        import yaml as _yaml
        content = config_path.read_text()
        full_config = _yaml.safe_load(content) or {}
        config = {}

        # Extract common fields
        config["home_channel"] = full_config.get("WECOM_HOME_CHANNEL", "")
        config["personality"] = full_config.get("personality", "")

        # Model info
        model_cfg = full_config.get("model", {})
        if isinstance(model_cfg, str):
            config["model"] = model_cfg
        elif isinstance(model_cfg, dict):
            config["model"] = model_cfg.get("default", "deepseek-chat")
        else:
            config["model"] = "deepseek-chat"

        # Display name — from dedicated field, fallback to Chinese name map
        display_name = full_config.get("display_name", "")
        if not display_name:
            display_name = _CHINESE_NAME_FALLBACK.get(profile_name, profile_name)
        config["display_name"] = display_name

        return config
    except Exception:
        return {}


def format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}秒"
    elif seconds < 3600:
        return f"{int(seconds // 60)}分钟"
    elif seconds < 86400:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}小时{mins}分钟"
    else:
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        return f"{days}天{hours}小时"


def get_profile_usage_db(profile: str):
    """Get the correct state DB path for a profile."""
    # Profiles use state.db, not sessions.db
    if profile == "default":
        return HERMES_HOME / "state.db"
    db_path = PROFILES_DIR / profile / "state.db"
    if db_path.exists():
        return db_path
    # Fallback to global state.db
    return HERMES_HOME / "state.db"

def get_token_usage(profile: str) -> dict:
    """Get daily token usage from state DB, including per-model breakdown."""
    db_path = get_profile_usage_db(profile)
    if not db_path.exists():
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "by_model": {}, "cost_cny": 0}
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        # Use correct column names: input_tokens / output_tokens (not prompt/completion)
        cursor.execute("""
            SELECT COALESCE(SUM(input_tokens), 0),
                   COALESCE(SUM(output_tokens), 0),
                   COALESCE(SUM(estimated_cost_usd), 0)
            FROM sessions
            WHERE started_at >= ?
        """, (today_start,))
        row = cursor.fetchone()

        # Per-model breakdown
        cursor.execute("""
            SELECT COALESCE(model, 'unknown'),
                   COALESCE(SUM(input_tokens), 0),
                   COALESCE(SUM(output_tokens), 0),
                   COALESCE(SUM(estimated_cost_usd), 0)
            FROM sessions
            WHERE started_at >= ?
            GROUP BY model
        """, (today_start,))
        by_model = {}
        for m in cursor.fetchall():
            raw_name = m[0] or "unknown"
            model_name = MODEL_NAME_MAP.get(raw_name, raw_name)
            if model_name not in by_model:
                by_model[model_name] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}
            by_model[model_name]["prompt_tokens"] += m[1]
            by_model[model_name]["completion_tokens"] += m[2]
            by_model[model_name]["total_tokens"] += m[1] + m[2]
            by_model[model_name]["cost_usd"] += m[3] if m[3] else 0.0
        # Round costs after aggregation
        for mn in by_model:
            by_model[mn]["cost_usd"] = round(by_model[mn]["cost_usd"], 6)
        conn.close()

        prompt = row[0] if row else 0
        completion = row[1] if row else 0
        cost_usd = row[2] if row else 0

        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "by_model": by_model,
            "cost_usd": round(cost_usd, 6),
            "cost_cny": round(cost_usd * 7.2, 4),
        }
    except Exception as e:
        logger.warning(f"Failed to query token usage for {profile}: {e}")
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "by_model": {}, "cost_cny": 0}

# ── Model balances & pricing ──
PRICING_FILE = Path(__file__).parent / "model_pricing.json"
BALANCES_FILE = Path(__file__).parent / "model_balances.json"

def load_pricing():
    if PRICING_FILE.exists():
        with open(PRICING_FILE) as f:
            return json.load(f)
    return {}

def load_balances():
    if BALANCES_FILE.exists():
        with open(BALANCES_FILE) as f:
            return json.load(f)
    return {}

def save_balances(data):
    with open(BALANCES_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# Merge old model names into new ones (deepseek-chat→v4-flash, reasoner→v4-pro)
MODEL_NAME_MAP = {
    "deepseek-chat": "deepseek-v4-flash",
    "deepseek-reasoner": "deepseek-v4-pro",
}


def get_gateway_platforms() -> dict:
    """Get all gateway platform statuses from runtime status files."""
    platforms = {}

    status_path = HERMES_HOME / "gateway_state.json"
    if status_path.exists():
        try:
            data = json.loads(status_path.read_text())
            platforms["default"] = {
                "state": data.get("gateway_state", "unknown"),
                "platforms": data.get("platforms", {}),
                "active_agents": data.get("active_agents", 0),
                "updated_at": data.get("updated_at", ""),
            }
        except Exception:
            pass

    if PROFILES_DIR.exists():
        for profile_dir in PROFILES_DIR.iterdir():
            if not profile_dir.is_dir():
                continue
            profile_name = profile_dir.name
            gw_state = profile_dir / "gateway_state.json"
            if gw_state.exists():
                try:
                    data = json.loads(gw_state.read_text())
                    platforms[profile_name] = {
                        "state": data.get("gateway_state", "unknown"),
                        "platforms": data.get("platforms", {}),
                        "active_agents": data.get("active_agents", 0),
                        "updated_at": data.get("updated_at", ""),
                    }
                except Exception:
                    pass

    return platforms


def _get_knowledge_base_stats() -> list[dict]:
    """Get all knowledge base directories with file counts and sizes."""
    bases = []
    if not KB_DIR.exists():
        return bases
    for kb_dir in sorted(KB_DIR.iterdir(), key=lambda d: (0 if d.name == "shared" else 1, d.name)):
        if not kb_dir.is_dir():
            continue
        files = []
        total_size = 0
        for f in kb_dir.iterdir():
            if f.is_file() and not f.name.startswith("."):
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                })
                total_size += f.stat().st_size
        bases.append({
            "name": kb_dir.name,
            "key": _KB_DIR_TO_KEY.get(kb_dir.name, kb_dir.name),
            "file_count": len(files),
            "total_size": total_size,
            "files": files,
            "path": str(kb_dir.relative_to(KB_DIR)),
        })
    return bases


# ============================================================
# API Endpoints
# ============================================================

@router.get("/agents")
async def list_agents():
    """List all running Hermes agents with detailed status."""
    processes = get_all_agent_processes()
    gateway_platforms = get_gateway_platforms()

    agents_by_profile: dict[str, dict] = {}

    for proc in processes:
        profile = proc["profile"]
        if profile not in agents_by_profile:
            agents_by_profile[profile] = {
                "profile": profile,
                "pid": None,
                "type": proc["type"],
                "running": True,
                "uptime_seconds": 0,
                "platforms": {},
                "config": get_profile_config(profile),
                "gateway_state": "unknown",
            }
        agent = agents_by_profile[profile]

        if proc["type"] == "gateway":
            agent["pid"] = proc["pid"]
            agent["uptime_seconds"] = proc["uptime_seconds"]
            agent["gateway_state"] = gateway_platforms.get(profile, {}).get("state", "running")
            if not agent.get("platforms") and profile in gateway_platforms:
                agent["platforms"] = gateway_platforms[profile].get("platforms", {})
        elif proc["type"] == "dashboard" and agent["pid"] is None:
            agent["pid"] = proc["pid"]
            agent["uptime_seconds"] = proc["uptime_seconds"]

    # Dynamically discover all profiles from the profiles directory
    discovered = set()
    if PROFILES_DIR.exists():
        for d in PROFILES_DIR.iterdir():
            if d.is_dir() and (d / "config.yaml").exists():
                discovered.add(d.name)

    # Also include any running processes with profiles not in the directory
    for pname in agents_by_profile:
        if pname != "default":
            discovered.add(pname)

    # Add stopped profiles (those with a config directory but no running process)
    for pname in sorted(discovered, key=lambda p: (AGENT_ORDER.get(p, 999), p)):
        if pname not in agents_by_profile:
            config_path = PROFILES_DIR / pname / "config.yaml"
            agents_by_profile[pname] = {
                "profile": pname,
                "pid": None,
                "type": "gateway",
                "running": False,
                "uptime_seconds": 0,
                "platforms": {},
                "config": get_profile_config(pname) if config_path.exists() else {},
                "gateway_state": "stopped",
            }
        else:
            agent = agents_by_profile[pname]
            if agent["running"] and pname != "default":
                agent["token_usage"] = get_token_usage(pname)

    result = []
    for pname in sorted(discovered, key=lambda p: (AGENT_ORDER.get(p, 999), p)):
        if pname in agents_by_profile:
            a = agents_by_profile[pname]
            a["uptime_display"] = format_duration(a["uptime_seconds"]) if a["uptime_seconds"] else "—"
            a["connected_channels"] = [
                p for p, info in a.get("platforms", {}).items()
                if info.get("state") == "connected"
            ]
            a["disconnected_channels"] = [
                p for p, info in a.get("platforms", {}).items()
                if info.get("state") not in ("connected",)
            ]
            cfg = a.get("config", {})
            a["model_display"] = cfg.get("model", "deepseek-chat")
            a["home_channel"] = cfg.get("home_channel", "")
            a["personality"] = cfg.get("personality", "default")
            result.append(a)

    return {"code": 0, "data": result}


class GatewayAction(BaseModel):
    profile: str = "main-bot"
    action: str  # "restart" | "stop" | "start"


@router.post("/gateway/control")
async def control_gateway(params: GatewayAction):
    """Start, stop, or restart a gateway for a given profile."""
    action = params.action
    profile = params.profile

    processes = get_all_agent_processes()

    # Collect ALL gateway PIDs for the profile (several may exist due to
    # inconsistent -p flag usage between dashboard start & healthcheck restart)
    target_pids = []
    for proc in processes:
        if proc["profile"] == profile and proc["type"] == "gateway":
            target_pids.append(proc["pid"])

    if action == "stop":
        if not target_pids:
            raise HTTPException(status_code=404, detail=f"未找到 {profile} 的运行中 gateway")
        killed = []
        for pid in target_pids:
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.5)
                # Check if still alive — SIGTERM may be ignored
                try:
                    os.kill(pid, 0)  # test if process exists
                    os.kill(pid, signal.SIGKILL)
                    killed.append(f"PID {pid} (强制终止)")
                except OSError:
                    killed.append(f"PID {pid} (已停止)")
            except Exception as e:
                killed.append(f"PID {pid} (失败: {e})")
        return {"code": 0, "message": f"已停止 {profile} gateway: {'; '.join(killed)}"}

    elif action == "restart":
        # Kill all running instances first
        for pid in target_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        time.sleep(1)

        hermes_bin = _resolve_hermes_bin()
        # Always use -p flag consistently so --replace matches the same identity
        cmd = [hermes_bin, "-p", profile, "gateway", "run", "--replace"]

        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(HERMES_HOME))
            return {"code": 0, "message": f"已重启 {profile} gateway"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"重启失败: {e}")

    elif action == "start":
        if target_pids:
            return {"code": 0, "message": f"{profile} gateway 已在运行中 (PID {target_pids[0]})"}

        hermes_bin = _resolve_hermes_bin()
        cmd = [hermes_bin, "-p", profile, "gateway", "run", "--replace"]

        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(HERMES_HOME))
            return {"code": 0, "message": f"已启动 {profile} gateway"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"启动失败: {e}")

    raise HTTPException(status_code=400, detail=f"未知操作: {action}")


class RenameRequest(BaseModel):
    profile: str
    display_name: str


@router.post("/agent/rename")
async def rename_agent(params: RenameRequest):
    """Rename an agent by updating its config.yaml display_name field."""
    profile = params.profile
    display_name = params.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="名称不能为空")
    if len(display_name) > 50:
        raise HTTPException(status_code=400, detail="名称不能超过50个字符")

    config_path = PROFILES_DIR / profile / "config.yaml"
    if not config_path.exists():
        raise HTTPException(status_code=404, detail=f"未找到 {profile} 的配置文件")

    try:
        import yaml as _yaml
        content = config_path.read_text()
        full_config = _yaml.safe_load(content) or {}
        full_config["display_name"] = display_name

        # Write back with proper YAML formatting
        with open(str(config_path), "w") as f:
            _yaml.dump(full_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        return {"code": 0, "message": f"已将 {profile} 名称修改为「{display_name}」"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重命名失败: {e}")


# ============================================================
# Knowledge Base API
# ============================================================

# Knowledge base paths
_SHARED_KB = HERMES_HOME / "knowledge" / "shared"
_PROFILE_KB_BASE = HERMES_HOME / "knowledge"
_CLASSIFIED_KB_DIR = HERMES_HOME / "hermes-agent" / "wechat_assistants" / "knowledge_bases"

def _get_agent_kb_map() -> dict:
    """Dynamically build agent→KB permission map from profiles AND running agents.
    
    Scans ~/.hermes/profiles/ and running Hermes processes, giving every
    discovered agent access to all classified knowledge bases. This ensures
    even servers without a profiles/ directory (like 龙律师) show their agents.
    """
    mapping = {}
    # First, scan profile directories
    if PROFILES_DIR.exists():
        for d in sorted(PROFILES_DIR.iterdir()):
            if d.is_dir() and (d / "config.yaml").exists():
                mapping[d.name] = list(_KB_DIR_TO_KEY.keys())
    # Then, add agents from running processes that aren't in profiles
    for proc in get_all_agent_processes():
        profile = proc.get("profile", "")
        if profile and profile not in mapping and profile != "default":
            mapping[profile] = list(_KB_DIR_TO_KEY.keys())
    return mapping


def _scan_file_dir(directory: Path) -> list[dict]:
    """Scan a directory and return sorted file info list."""
    if not directory.exists() or not directory.is_dir():
        return []
    files = []
    for f in sorted(directory.iterdir()):
        if f.is_file() and not f.name.startswith("."):
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
    return files


def _detect_conflicts(shared_files: list[dict], exclusive_files: list[dict]) -> list[dict]:
    """Detect filename conflicts between shared and exclusive layers."""
    shared_names = {f["name"] for f in shared_files if f["name"].endswith((".md", ".txt"))}
    conflicts = []
    for ef in exclusive_files:
        if ef["name"] in shared_names:
            sf_size = next((f["size"] for f in shared_files if f["name"] == ef["name"]), 0)
            conflicts.append({
                "filename": ef["name"],
                "shared_size": sf_size,
                "exclusive_size": ef["size"],
                "winner": "exclusive",
            })
    return conflicts


@router.get("/knowledge-flow/{profile}")
async def knowledge_flow(profile: str):
    """Get the layered knowledge base loading flow for a given agent profile."""
    layers = []

    # --- Step 1: Shared knowledge base ---
    shared_files = _scan_file_dir(_SHARED_KB)
    layers.append({
        "name": "shared",
        "label": "共享知识库 (shared/)",
        "order": 1,
        "files": shared_files,
        "source_dir": str(_SHARED_KB),
        "file_count": len(shared_files),
    })

    # --- Step 1.5: Classified permission knowledge bases ---
    agent_kb_map = _get_agent_kb_map()
    permissions = agent_kb_map.get(profile, [])
    perms_files = []
    profiles_with_access = {}
    for cat in permissions:
        cat_dir = _CLASSIFIED_KB_DIR / cat
        cat_files = _scan_file_dir(cat_dir)
        perms_files.extend(cat_files)
        # Count which profiles have access to this category
        for pname, cats in _get_agent_kb_map().items():
            if cat in cats:
                profiles_with_access[cat] = profiles_with_access.get(cat, []) + [pname]
    # Deduplicate perms files by name (last wins)
    seen = {}
    for f in perms_files:
        seen[f["name"]] = f
    perms_files_deduped = list(seen.values())

    layers.append({
        "name": "permissions",
        "label": f"分类权限知识库 ({', '.join(permissions) if permissions else '无'})",
        "order": 1.5,
        "files": perms_files_deduped,
        "categories": permissions,
        "profiles_with_access": {cat: sorted(profiles_with_access.get(cat, [])) for cat in permissions},
        "file_count": len(perms_files_deduped),
    })

    # --- Step 2: Profile-exclusive knowledge base ---
    profile_kb_dir = _PROFILE_KB_BASE / profile
    exclusive_files = _scan_file_dir(profile_kb_dir)
    layers.append({
        "name": "exclusive",
        "label": f"角色专有知识库 (knowledge/{profile}/)",
        "order": 2,
        "files": exclusive_files,
        "source_dir": str(profile_kb_dir),
        "file_count": len(exclusive_files),
    })

    # --- Conflict detection ---
    conflicts = _detect_conflicts(shared_files, exclusive_files)

    # --- Stats ---
    total = len(shared_files) + len(perms_files_deduped) + len(exclusive_files)
    stats = {
        "shared_count": len(shared_files),
        "permissions_count": len(perms_files_deduped),
        "exclusive_count": len(exclusive_files),
        "total": total,
    }

    return {
        "code": 0,
        "data": {
            "profile": profile,
            "layers": layers,
            "conflicts": conflicts,
            "stats": stats,
        }
    }


@router.get("/knowledge/agent-permissions")
async def get_agent_permissions():
    """Return dynamic agent→KB permission mapping for frontend.
    
    Each server returns only its own agents, so the knowledge base
    permission filter dropdown shows the correct agents.
    """
    mapping = _get_agent_kb_map()
    # Generate labels from profile configs
    labels = {}
    for pname in mapping:
        cfg = get_profile_config(pname)
        dn = cfg.get("display_name", "")
        if dn:
            labels[pname] = f"{dn} ({pname})"
        else:
            labels[pname] = pname
    return {
        "code": 0,
        "data": {
            "map": mapping,
            "labels": labels,
        }
    }


@router.get("/knowledge/list")
async def knowledge_list(kb: str = None):
    """List knowledge bases or files within a specific KB."""
    if kb:
        # Check both old and new paths
        kb_path_new = _CLASSIFIED_KB_DIR / kb
        if kb_path_new.exists() and kb_path_new.is_dir():
            files = []
            for f in sorted(kb_path_new.iterdir(), key=lambda x: (x.is_dir(), x.name)):
                if f.name.startswith("."):
                    continue
                files.append({
                    "name": f.name,
                    "type": "directory" if f.is_dir() else "file",
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                })
            return {"code": 0, "data": {"kb": kb, "files": files}}
        raise HTTPException(status_code=404, detail=f"知识库 '{kb}' 不存在")
    else:
        bases = _get_knowledge_base_stats()
        return {"code": 0, "data": bases}


@router.get("/knowledge/view")
async def knowledge_view(kb: str, file: str):
    """Read a knowledge base file content."""
    file_path = _CLASSIFIED_KB_DIR / kb / file
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"文件 '{kb}/{file}' 不存在")
    if file_path.suffix not in (".txt", ".md", ".json", ".yaml", ".yml"):
        raise HTTPException(status_code=400, detail="仅支持查看 .txt/.md/.json/.yaml 文件")
    try:
        content = file_path.read_text(encoding="utf-8")
        return {"code": 0, "data": {"kb": kb, "file": file, "content": content}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {e}")


@router.get("/knowledge/stats")
async def knowledge_stats():
    """Get overall knowledge base statistics."""
    total_files = 0
    total_size = 0
    kb_counts = {}
    if _CLASSIFIED_KB_DIR.exists():
        for kb_dir in _CLASSIFIED_KB_DIR.iterdir():
            if not kb_dir.is_dir():
                continue
            count = 0
            size = 0
            for f in kb_dir.iterdir():
                if f.is_file() and not f.name.startswith("."):
                    count += 1
                    size += f.stat().st_size
            kb_counts[kb_dir.name] = {"files": count, "size": size}
            total_files += count
            total_size += size
    return {
        "code": 0,
        "data": {
            "kb_count": len(kb_counts),
            "total_files": total_files,
            "total_size": total_size,
            "kb_details": kb_counts,
        }
    }

ALLOWED_EXTENSIONS = {".txt", ".md", ".json", ".yaml", ".yml", ".docx"}

def _validate_kb(kb: str) -> Path:
    """Validate and return knowledge base directory path."""
    kb_path = _CLASSIFIED_KB_DIR / kb
    if not kb_path.exists() or not kb_path.is_dir():
        raise HTTPException(status_code=404, detail=f"知识库 '{kb}' 不存在")
    return kb_path

def _validate_filename(filename: str) -> str:
    """Validate and sanitize filename, ensuring unique name."""
    # Remove path separators to prevent directory traversal
    name = Path(filename).name
    if not name:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    if not any(name.endswith(ext) for ext in ALLOWED_EXTENSIONS):
        raise HTTPException(status_code=400,
                            detail=f"仅支持 {'/'.join(ALLOWED_EXTENSIONS)} 格式")
    return name




def _sync_to_agent_knowledge(kb: str, filename: str, raw: bytes, text: str | None):
    """Sync uploaded file to agent's knowledge directory (for SOUL.md integration).
    
    The SOUL.md states that ~/.hermes/knowledge/shared/ is automatically loaded.
    This function ensures uploaded files are available there too.
    """
    try:
        if kb == "shared":
            target = _SHARED_KB
            target.mkdir(parents=True, exist_ok=True)
            dest = target / filename
            if text:
                dest.write_text(text, encoding="utf-8")
            else:
                dest.write_bytes(raw)
            logger.info("Synced %s to %s", filename, target)
    except Exception as e:
        logger.warning("Failed to sync to agent knowledge dir: %s", e)
@router.post("/knowledge/upload")
async def knowledge_upload(kb: str = Form(...), file: UploadFile = File(...)):
    """Upload a file to a knowledge base (supports .docx extraction)."""
    kb_path = _validate_kb(kb)
    original_name = file.filename or "untitled.txt"
    try:
        raw = await file.read()
        # Limit to 10MB
        if len(raw) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="文件大小不能超过 10MB")
        # Handle Word document: extract text and save as .txt
        suffix = Path(original_name).suffix.lower()
        if suffix == ".docx":
            import docx
            import io
            doc = docx.Document(io.BytesIO(raw))
            text = "\n".join(p.text for p in doc.paragraphs)
            filename = Path(original_name).stem + ".txt"
            dest = kb_path / filename
            if dest.exists():
                raise HTTPException(status_code=409, detail=f"文件 '{filename}' 已存在")
            dest.write_text(text, encoding="utf-8")
            _sync_to_agent_knowledge(kb, filename, raw, text)
            return {"code": 0, "data": {"kb": kb, "file": filename, "size": len(text.encode("utf-8")), "note": "Word文档已提取为文本"}}
        else:
            filename = _validate_filename(original_name)
            text = None  # no text extraction for non-docx
            dest = kb_path / filename
            if dest.exists():
                raise HTTPException(status_code=409, detail=f"文件 '{filename}' 已存在")
            dest.write_bytes(raw)
            _sync_to_agent_knowledge(kb, filename, raw, text)
            return {"code": 0, "data": {"kb": kb, "file": filename, "size": len(raw)}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传失败: {e}")


@router.post("/knowledge/add-text")
async def knowledge_add_text(body: dict):
    """Add text content as a new file to a knowledge base."""
    kb = body.get("kb", "").strip()
    filename = body.get("filename", "").strip()
    content = body.get("content", "")
    if not kb or not filename or not content:
        raise HTTPException(status_code=400, detail="kb、filename、content 均为必填")
    kb_path = _validate_kb(kb)
    filename = _validate_filename(filename)
    dest = kb_path / filename
    if dest.exists():
        raise HTTPException(status_code=409, detail=f"文件 '{filename}' 已存在")
    try:
        dest.write_text(content, encoding="utf-8")
        _sync_to_agent_knowledge(kb, filename, content.encode("utf-8"), content)
        return {"code": 0, "data": {"kb": kb, "file": filename, "size": len(content.encode("utf-8"))}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建失败: {e}")


@router.get("/knowledge/memory")
async def knowledge_memory():
    """Read persistent memory (MEMORY.md + USER.md) content."""
    mem_dir = HERMES_HOME / "memories"
    result = {"memory": "", "user": "", "memory_exists": False, "user_exists": False}
    mem_file = mem_dir / "MEMORY.md"
    user_file = mem_dir / "USER.md"
    try:
        if mem_file.exists():
            result["memory"] = mem_file.read_text(encoding="utf-8").strip()
            result["memory_exists"] = True
        if user_file.exists():
            result["user"] = user_file.read_text(encoding="utf-8").strip()
            result["user_exists"] = True
        return {"code": 0, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取记忆失败: {e}")


def _calc_model_cost(model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate cost in CNY from tokens + pricing table. Returns RMB cost."""
    pricing = load_pricing()
    name_lower = model_name.lower()
    # Match by prefix or exact name
    for key, price in pricing.items():
        if key.lower() == name_lower or key.lower() in name_lower or name_lower in key.lower():
            prompt_price = price.get("prompt", price.get("prompt", 0))
            completion_price = price.get("completion", price.get("completion", 0))
            return round(prompt_tokens / 1_000_000 * prompt_price +
                         completion_tokens / 1_000_000 * completion_price, 4)
    # Default: use deepseek-v4-flash pricing as fallback
    return round(prompt_tokens / 1_000_000 * 0.5 +
                 completion_tokens / 1_000_000 * 1.0, 4)


def _get_account_balances() -> dict:
    """Get account-level balances from billing DB, mapped by model name."""
    try:
        import sqlite3
        billing_db = Path(__file__).parent / "billing.db"
        if not billing_db.exists():
            return {}
        conn = sqlite3.connect(str(billing_db))
        cur = conn.cursor()
        cur.execute("""
            SELECT a.provider, a.account_name, a.balance, a.recharged_total, a.currency,
                   GROUP_CONCAT(am.model_name, ',') as models
            FROM provider_accounts a
            LEFT JOIN account_models am ON a.id = am.account_id
            GROUP BY a.id
        """)
        result = {}
        for row in cur.fetchall():
            provider, acct_name, balance, recharged, currency, models_str = row
            models = [m.strip() for m in (models_str or "").split(",") if m.strip()]
            for model in models:
                result[model] = {
                    "provider": provider,
                    "account_name": acct_name,
                    "balance": balance or 0,
                    "recharged_total": recharged or 0,
                    "currency": currency or "CNY",
                }
        conn.close()
        return result
    except Exception:
        return {}


@router.get("/token-usage")
async def daily_token_usage():
    """Get daily token usage summary with model costs and account balances."""
    account_balances = _get_account_balances()
    result = {"profiles": {}, "by_model": {}, "total_cost_cny": 0, "account_balances": account_balances}
    known_profiles = [
        "main-bot", "fullstack-bot", "xiaoyi", "xiaojiu", "xiaozhua",
        "xiaoce", "xiaoying", "binance-bot", "intel-sentinel", "sales-advisor",
        "xiaomi", "xiao-pin", "xiaozhu", "xiaozi", "zhengce",
    ]
    model_totals = {}  # model → {prompt_tokens, completion_tokens, total_tokens, cost_cny}

    for pname in known_profiles:
        db_path = PROFILES_DIR / pname / "state.db"
        if not db_path.exists():
            continue
        usage = get_token_usage(pname)
        if usage["total_tokens"] > 0:
            result["profiles"][pname] = usage
            # Aggregate by model — calculate cost from pricing, NOT from DB
            for model, mdata in usage.get("by_model", {}).items():
                if model not in model_totals:
                    model_totals[model] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_cny": 0}
                model_totals[model]["prompt_tokens"] += mdata["prompt_tokens"]
                model_totals[model]["completion_tokens"] += mdata["completion_tokens"]
                model_totals[model]["total_tokens"] += mdata["total_tokens"]
                # Calculate cost from pricing table instead of DB's estimated_cost_usd
                model_totals[model]["cost_cny"] += _calc_model_cost(
                    model, mdata["prompt_tokens"], mdata["completion_tokens"]
                )
            # Recalculate profile-level cost
            profile_cost = 0
            for model, mdata in usage.get("by_model", {}).items():
                profile_cost += _calc_model_cost(model, mdata["prompt_tokens"], mdata["completion_tokens"])
            usage["cost_cny"] = round(profile_cost, 4)
            usage["cost_usd"] = round(profile_cost / 7.2, 6)
            result["total_cost_cny"] += profile_cost

    # Round
    result["total_cost_cny"] = round(result["total_cost_cny"], 4)
    for m in model_totals:
        model_totals[m]["cost_cny"] = round(model_totals[m]["cost_cny"], 4)
    result["by_model"] = model_totals

    return {"code": 0, "data": result}


@router.get("/model-balances")
async def get_model_balances():
    """Get model balance info with pricing."""
    balances = load_balances()
    pricing = load_pricing()
    result = {}
    for model, bal in balances.items():
        result[model] = {
            "balance": bal.get("balance", 0),
            "recharged_total": bal.get("recharged_total", 0),
            "currency": bal.get("currency", "CNY"),
            "pricing": pricing.get(model, {}),
            "updated_at": bal.get("updated_at", ""),
        }
    return {"code": 0, "data": result}


@router.post("/model-balances/recharge")
async def recharge_model(request: Request):
    """Recharge a model's balance."""
    try:
        body = await request.json()
    except Exception:
        return {"code": -1, "msg": "Invalid JSON body"}
    model = body.get("model", "").strip()
    amount = body.get("amount", 0)
    if not model or amount <= 0:
        return {"code": -1, "msg": "需要有效的 model 和 amount (>0)"}
    balances = load_balances()
    if model not in balances:
        balances[model] = {"balance": 0, "recharged_total": 0, "currency": "CNY", "updated_at": ""}
    balances[model]["balance"] = round(balances[model].get("balance", 0) + amount, 2)
    balances[model]["recharged_total"] = round(balances[model].get("recharged_total", 0) + amount, 2)
    balances[model]["updated_at"] = datetime.now().isoformat()
    save_balances(balances)
    return {"code": 0, "msg": f"成功为 {model} 充值 ¥{amount:.2f}", "data": balances[model]}


# ═══════════════════════════════════════════════════════════════════
# Provider Accounts (Billing DB) — account-level balance + recharge
# ═══════════════════════════════════════════════════════════════════

import sqlite3 as _sqlite3

BILLING_DB = Path(__file__).parent / "billing.db"


def _init_billing_db() -> None:
    """Create billing tables if they don't exist."""
    conn = _sqlite3.connect(str(BILLING_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS provider_accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            provider    TEXT    NOT NULL,            -- e.g. 'deepseek', 'anthropic', 'openai'
            account_name TEXT   NOT NULL,            -- e.g. 'DeepSeek主账户'
            balance     REAL    NOT NULL DEFAULT 0,  -- current balance (CNY)
            currency    TEXT    NOT NULL DEFAULT 'CNY',
            recharged_total REAL NOT NULL DEFAULT 0,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS account_models (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  INTEGER NOT NULL,
            model_name  TEXT    NOT NULL,
            FOREIGN KEY (account_id) REFERENCES provider_accounts(id),
            UNIQUE(account_id, model_name)
        );
        CREATE TABLE IF NOT EXISTS recharge_records (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id    INTEGER NOT NULL,
            amount        REAL    NOT NULL,
            balance_before REAL   NOT NULL,
            balance_after  REAL   NOT NULL,
            note          TEXT    DEFAULT '',
            created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (account_id) REFERENCES provider_accounts(id)
        );
    """)
    conn.commit()
    # Seed default DeepSeek account if empty
    cur = conn.execute("SELECT COUNT(*) FROM provider_accounts")
    if cur.fetchone()[0] == 0:
        # Merge balances from model_balances.json for known DeepSeek models
        bal = load_balances()
        ds_total = (
            bal.get("deepseek-v4-flash", {}).get("balance", 0) +
            bal.get("deepseek-v4-pro", {}).get("balance", 0)
        )
        ds_recharged = (
            bal.get("deepseek-v4-flash", {}).get("recharged_total", 0) +
            bal.get("deepseek-v4-pro", {}).get("recharged_total", 0)
        )
        conn.execute(
            "INSERT INTO provider_accounts (provider, account_name, balance, recharged_total) VALUES (?,?,?,?)",
            ("deepseek", "DeepSeek 主账户", ds_total, ds_recharged),
        )
        acct_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO account_models (account_id, model_name) VALUES (?,?)", (acct_id, "deepseek-v4-flash"))
        conn.execute("INSERT INTO account_models (account_id, model_name) VALUES (?,?)", (acct_id, "deepseek-v4-pro"))
        # Seed initial recharge record for the merged balance if recharged > 0
        if ds_recharged > 0:
            conn.execute(
                "INSERT INTO recharge_records (account_id, amount, balance_before, balance_after, note) VALUES (?,?,?,?,?)",
                (acct_id, ds_recharged, 0, ds_recharged, "初始余额导入"),
            )
        conn.commit()
    conn.close()


# Init DB on module load
_init_billing_db()


def _billing_row_to_dict(row: tuple) -> dict:
    cols = ["id", "provider", "account_name", "balance", "currency", "recharged_total", "created_at", "updated_at"]
    return dict(zip(cols, row))


@router.get("/provider-accounts")
async def list_provider_accounts():
    """List all provider accounts with their mapped models and recent recharge records."""
    conn = _sqlite3.connect(str(BILLING_DB))
    conn.row_factory = _sqlite3.Row
    accounts = []
    for row in conn.execute("SELECT id, provider, account_name, balance, currency, recharged_total, created_at, updated_at FROM provider_accounts ORDER BY id"):
        acct = dict(row)
        # Attach models
        models = conn.execute(
            "SELECT model_name FROM account_models WHERE account_id=? ORDER BY model_name", (acct["id"],)
        ).fetchall()
        acct["models"] = [m["model_name"] for m in models]
        # Attach latest 5 recharge records
        records = conn.execute(
            "SELECT * FROM recharge_records WHERE account_id=? ORDER BY created_at DESC LIMIT 5", (acct["id"],)
        ).fetchall()
        acct["recent_records"] = [dict(r) for r in records]
        accounts.append(acct)
    conn.close()
    return {"code": 0, "data": accounts}


class RechargeBody(BaseModel):
    account_id: int
    amount: float
    note: str = ""


@router.post("/provider-accounts/recharge")
async def recharge_account(body: RechargeBody):
    """Recharge a provider account and record the transaction."""
    if body.amount <= 0:
        return {"code": -1, "msg": "充值金额必须大于0"}
    conn = _sqlite3.connect(str(BILLING_DB))
    cur = conn.execute("SELECT balance, account_name FROM provider_accounts WHERE id=?", (body.account_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return {"code": -1, "msg": "账户不存在"}
    balance_before = row[0]
    account_name = row[1]
    balance_after = round(balance_before + body.amount, 2)
    recharged_total = round(
        conn.execute("SELECT COALESCE(SUM(amount),0) FROM recharge_records WHERE account_id=?", (body.account_id,)).fetchone()[0]
        + body.amount, 2
    )
    conn.execute(
        "UPDATE provider_accounts SET balance=?, recharged_total=?, updated_at=datetime('now','localtime') WHERE id=?",
        (balance_after, recharged_total, body.account_id),
    )
    conn.execute(
        "INSERT INTO recharge_records (account_id, amount, balance_before, balance_after, note) VALUES (?,?,?,?,?)",
        (body.account_id, body.amount, balance_before, balance_after, body.note or ""),
    )
    conn.commit()
    conn.close()
    return {
        "code": 0,
        "msg": f"成功为 {account_name} 充值 ¥{body.amount:.2f}",
        "data": {"balance": balance_after, "recharged_total": recharged_total},
    }


@router.get("/recharge-records")
async def list_recharge_records(account_id: int = None, limit: int = 50, offset: int = 0):
    """List recharge records, optionally filtered by account."""
    conn = _sqlite3.connect(str(BILLING_DB))
    conn.row_factory = _sqlite3.Row
    if account_id:
        rows = conn.execute(
            """SELECT r.*, a.account_name, a.provider
               FROM recharge_records r JOIN provider_accounts a ON r.account_id=a.id
               WHERE r.account_id=? ORDER BY r.created_at DESC LIMIT ? OFFSET ?""",
            (account_id, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT r.*, a.account_name, a.provider
               FROM recharge_records r JOIN provider_accounts a ON r.account_id=a.id
               ORDER BY r.created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
    conn.close()
    return {"code": 0, "data": [dict(r) for r in rows]}


@router.get("/profile/{profile_name}/config")
async def get_profile_config_api(profile_name: str):
    """Get a profile's configuration."""
    cfg = get_profile_config(profile_name)
    return {"code": 0, "data": cfg}


@router.get("/dashboard-stats")
async def dashboard_stats():
    """Get overall dashboard statistics summary."""
    agents_data = await list_agents()
    agents = agents_data.get("data", [])
    online = sum(1 for a in agents if a.get("gateway_state") == "running")
    offline = len(agents) - online

    # Count cron jobs
    cron_count = 0
    try:
        from cron.jobs import CronManager
        cm = CronManager(hermes_home=str(HERMES_HOME))
        cron_count = len(cm.list_jobs())
    except Exception:
        pass

    # Count skills
    skill_count = 0
    try:
        skills_dir = HERMES_HOME / "skills"
        if skills_dir.exists():
            skill_count = len(list(skills_dir.rglob("SKILL.md")))
    except Exception:
        pass

    return {
        "code": 0,
        "data": {
            "total_agents": len(agents),
            "online_agents": online,
            "offline_agents": offline,
            "cron_jobs": cron_count,
            "skills": skill_count,
            "timestamp": datetime.now().isoformat(),
        }
    }


# ============================================================
# File Management API
# ============================================================

import shutil
import stat as stat_module
import mimetypes
from fastapi.responses import FileResponse, StreamingResponse

def _safe_resolve_path(raw_path: str) -> Path | None:
    """Resolve a user-supplied path safely. Returns None if traversal detected."""
    if not raw_path or raw_path.strip() == "":
        raw_path = str(Path.home())
    p = Path(raw_path).resolve()
    # Prevent path traversal beyond the filesystem root
    if ".." in raw_path.split("/"):
        return None
    return p


@router.get("/files/list")
async def list_files(path: str = Query(default="")):
    """List files and directories at the given path."""
    target = _safe_resolve_path(path)
    if target is None:
        raise HTTPException(status_code=400, detail="无效路径")
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {target}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="路径不是目录")

    items = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                st = entry.stat()
                is_dir = entry.is_dir()
                items.append({
                    "name": entry.name,
                    "path": str(entry.resolve()),
                    "is_dir": is_dir,
                    "size": st.st_size if not is_dir else 0,
                    "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
                    "permissions": oct(st.st_mode & 0o777),
                    "type": "dir" if is_dir else _get_file_type(entry.name),
                })
            except (OSError, PermissionError):
                continue
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"无权限访问: {target}")

    # Get parent path info
    parent = str(target.parent) if target != Path("/") else ""

    return {
        "code": 0,
        "data": {
            "current_path": str(target),
            "current_name": target.name or "/",
            "parent_path": parent,
            "writable": os.access(str(target), os.W_OK),
            "items": items,
            "total": len(items),
        }
    }


def _get_file_type(filename: str) -> str:
    """Determine file type from extension."""
    ext = Path(filename).suffix.lower()
    type_map = {
        ".txt": "text", ".md": "text", ".json": "code", ".yaml": "code",
        ".yml": "code", ".xml": "code", ".html": "code", ".css": "code",
        ".js": "code", ".ts": "code", ".py": "code", ".sh": "code",
        ".conf": "code", ".log": "text", ".csv": "data", ".toml": "code",
        ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
        ".webp": "image", ".svg": "image", ".ico": "image",
        ".mp4": "video", ".webm": "video", ".mkv": "video",
        ".mp3": "audio", ".wav": "audio", ".flac": "audio",
        ".zip": "archive", ".tar": "archive", ".gz": "archive",
        ".bz2": "archive", ".xz": "archive", ".7z": "archive",
        ".rar": "archive", ".pdf": "document", ".doc": "document",
        ".docx": "document", ".xls": "data", ".xlsx": "data",
        ".ppt": "document", ".pptx": "document",
        ".db": "database", ".sqlite": "database",
        ".pem": "key", ".key": "key", ".pub": "key",
    }
    return type_map.get(ext, "unknown")


def _format_size(size: int) -> str:
    """Format file size to human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}B"
        size /= 1024
    return f"{size:.1f}PB"


@router.get("/files/download")
async def download_file(path: str = Query(default="")):
    """Download a file from the server."""
    target = _safe_resolve_path(path)
    if target is None:
        raise HTTPException(status_code=400, detail="无效路径")
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {target}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="路径不是文件")

    media_type, _ = mimetypes.guess_type(str(target))
    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type=media_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename=\"{target.name}\"",
            "Content-Length": str(target.stat().st_size),
        }
    )

@router.get("/files/view")
async def view_file(path: str = Query(default="")):
    """View a file inline in the browser."""
    target = _safe_resolve_path(path)
    if target is None:
        raise HTTPException(status_code=400, detail="无效路径")
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {target}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="路径不是文件")

    media_type, _ = mimetypes.guess_type(str(target))
    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type=media_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"inline; filename=\"{target.name}\"",
            "Content-Length": str(target.stat().st_size),
        }
    )


@router.post("/files/upload")
async def upload_file(
    path: str = Form(default=""),
    file: UploadFile = File(...),
):
    """Upload a file to the server."""
    target_dir = _safe_resolve_path(path or ".")
    if target_dir is None:
        raise HTTPException(status_code=400, detail="无效路径")
    if not target_dir.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {target_dir}")
    if not target_dir.is_dir():
        raise HTTPException(status_code=400, detail="路径不是目录")

    # Validate filename — strip path separators to prevent traversal
    safe_name = Path(file.filename).name if file.filename else "untitled"
    dest = target_dir / safe_name

    # Prevent overwriting directories
    if dest.is_dir():
        raise HTTPException(status_code=400, detail=f"目标路径已存在同名目录: {safe_name}")

    # Pre-check writability
    if not os.access(str(target_dir), os.W_OK):
        try:
            owner = target_dir.owner()
        except (NotImplementedError, OSError, AttributeError):
            owner = "?"
        raise HTTPException(
            status_code=403,
            detail=f"无权限写入目录: {target_dir} (owner: {owner}, 权限: {oct(target_dir.stat().st_mode & 0o777)})"
        )

    try:
        content = await file.read()
        with open(dest, "wb") as f:
            f.write(content)
        st = dest.stat()
        return {
            "code": 0,
            "data": {
                "name": safe_name,
                "path": str(dest.resolve()),
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
            },
            "msg": f"上传成功: {safe_name} ({_format_size(st.st_size)})",
        }
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"无权限写入: {dest}")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"写入失败: {str(e)}")


# ── 工作汇报 API ──

REPORTS_FILE = HERMES_HOME / "profiles" / "fullstack-bot" / "reports.json"


def _load_reports():
    if REPORTS_FILE.exists():
        try:
            with open(REPORTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


@router.get("/reports")
async def list_reports(
    type: str = Query(default="", description="daily or weekly"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List work reports with optional type filter."""
    reports = _load_reports()
    if type and type in ("daily", "weekly"):
        reports = [r for r in reports if r.get("type") == type]
    reports.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    total = len(reports)
    page = reports[offset:offset + limit]
    return {"code": 0, "data": {"items": page, "total": total}}


@router.post("/reports/generate")
async def trigger_generate(type: str = Query(default="daily")):
    """Manually trigger report generation (for testing)."""
    import subprocess
    script = Path(__file__).parent / "report_generator.py"
    try:
        result = subprocess.run(
            [sys.executable, str(script), type],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            return {"code": 0, "msg": result.stdout.strip()}
        else:
            return {"code": 1, "msg": result.stderr.strip()}
    except Exception as e:
        return {"code": 1, "msg": str(e)}


# ═══════════════════════════════════════════════
# 管理台 API — Admin Console
# ═══════════════════════════════════════════════

import secrets
import hashlib
import hmac as hmac_lib

# Admin credentials (match combined.js)
ADMIN_USER = "wuyang"
ADMIN_PASS = "Hometeach123"

# Admin sessions: sid -> { expires, created_at }
_admin_sessions: dict[str, dict] = {}
_ADMIN_SESSION_TTL = 86400  # 24h

# Paths to sales data files
_SALES_DIR = Path("/home/agentuser/websites/hermes-sales/pay-server")
_USERS_FILE = _SALES_DIR / "users.json"
_ORDERS_FILE = _SALES_DIR / "orders.json"
_LOGIN_LOGS_FILE = _SALES_DIR / "login_logs.json"

_PERMISSION_LABELS = {
    "free": "免费用户",
    "starter": "入门版用户",
    "pro": "专业版用户",
    "enterprise": "企业版用户",
    "demo": "演示版用户",
    "admin": "管理员",
}

_PERMISSION_ORDER = ["free", "starter", "pro", "enterprise", "demo", "admin"]


def _load_json(path: Path) -> list | dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, FileNotFoundError):
        return [] if path.name.endswith("s.json") and path.name != "orders.json" else {}


def _save_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _require_admin(request):
    """Check admin authentication. Returns session info or raises 401."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.query_params.get("admin_token", "")
    if not token:
        raise HTTPException(status_code=401, detail="未授权")
    session = _admin_sessions.get(token)
    if not session or session["expires"] < time.time():
        raise HTTPException(status_code=401, detail="会话已过期")
    return session


def _require_write(request):
    """Check write permission. Demo users are read-only."""
    session = _require_admin(request)
    if session.get("role") == "demo":
        raise HTTPException(status_code=403, detail="演示用户无操作权限")
    return session


# ── Admin Login ──

@router.post("/admin/login")
async def admin_login(request: Request):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    # 1. Try admin credentials first
    if username == ADMIN_USER and password == ADMIN_PASS:
        sid = secrets.token_urlsafe(32)
        _admin_sessions[sid] = {
            "expires": time.time() + _ADMIN_SESSION_TTL,
            "created_at": time.time(),
            "username": username,
            "role": "admin",
            "display_name": "管理员 wuyang",
        }
        return {"code": 0, "data": {"token": sid, "username": username, "role": "admin"}}

    # 2. Try user phone + password
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    users = _load_json(_USERS_FILE)
    if isinstance(users, dict):
        users = list(users.values())
    for u in users:
        if u.get("phone") == username and u.get("password") == password_hash:
            perm = u.get("permission", "free")
            # Only demo and admin users can access the dashboard
            if perm not in ("demo", "admin"):
                return {"code": -1, "msg": "您的账号无权访问管理台"}
            sid = secrets.token_urlsafe(32)
            role = "admin" if perm == "admin" else "demo"
            _admin_sessions[sid] = {
                "expires": time.time() + _ADMIN_SESSION_TTL,
                "created_at": time.time(),
                "username": u.get("phone"),
                "role": role,
                "display_name": u.get("name", u.get("phone")),
            }
            return {"code": 0, "data": {"token": sid, "username": u.get("phone"), "role": role}}

    return {"code": -1, "msg": "用户名或密码错误"}


@router.get("/admin/check")
async def admin_check(request: Request):
    """Check if current token is valid."""
    try:
        session = _require_admin(request)
        return {"code": 0, "data": {"username": session["username"], "role": session.get("role", "admin")}}
    except HTTPException:
        return {"code": -1, "msg": "未授权"}


@router.post("/admin/logout")
async def admin_logout(request: Request):
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    _admin_sessions.pop(token, None)
    return {"code": 0, "msg": "已退出"}


# ── User Management ──

@router.get("/admin/users")
async def list_users(request: Request):
    _require_admin(request)
    users = _load_json(_USERS_FILE)
    if isinstance(users, dict):
        users = list(users.values())
    # Add permission field if missing, sanitize output
    result = []
    for u in users:
        perm = u.get("permission", "free")
        if perm not in _PERMISSION_ORDER:
            perm = "free"
        result.append({
            "id": u.get("id", ""),
            "phone": u.get("phone", ""),
            "name": u.get("name", ""),
            "email": u.get("email", ""),
            "password": u.get("password", ""),
            "permission": perm,
            "permission_label": _PERMISSION_LABELS.get(perm, "未知"),
            "balance": u.get("balance", 0),
            "plan": u.get("plan", "free"),
            "activeUntil": u.get("activeUntil"),
            "autoRenew": u.get("autoRenew", False),
            "createdAt": u.get("createdAt", ""),
        })
    return {"code": 0, "data": result}


@router.put("/admin/users/{user_id}/permission")
async def update_user_permission(user_id: str, request: Request):
    _require_write(request)
    body = await request.json()
    new_perm = body.get("permission", "")
    if new_perm not in _PERMISSION_ORDER:
        return {"code": -1, "msg": f"无效权限: {new_perm}"}

    users = _load_json(_USERS_FILE)
    if isinstance(users, dict):
        users = list(users.values())
    found = False
    for u in users:
        if u.get("id") == user_id:
            u["permission"] = new_perm
            found = True
            break
    if not found:
        return {"code": -1, "msg": "用户不存在"}
    _save_json(_USERS_FILE, users)
    return {"code": 0, "msg": "权限已更新"}


@router.put("/admin/users/{user_id}")
async def update_user(user_id: str, request: Request):
    """Update user name, phone, email, and optionally reset password."""
    _require_write(request)
    body = await request.json()
    new_name = (body.get("name") or "").strip()
    new_phone = (body.get("phone") or "").strip()
    new_email = (body.get("email") or "").strip()
    new_password = (body.get("password") or "").strip()

    users = _load_json(_USERS_FILE)
    if isinstance(users, dict):
        users = list(users.values())
    found = False
    for u in users:
        if u.get("id") == user_id:
            if new_name:
                u["name"] = new_name
            if new_phone:
                # Check phone uniqueness
                existing = [x for x in users if x.get("phone") == new_phone and x.get("id") != user_id]
                if existing:
                    return {"code": -1, "msg": "手机号已被其他用户使用"}
                u["phone"] = new_phone
            if new_email:
                u["email"] = new_email
            if new_password:
                u["password"] = hashlib.sha256(new_password.encode()).hexdigest()
            found = True
            break
    if not found:
        return {"code": -1, "msg": "用户不存在"}
    _save_json(_USERS_FILE, users)
    return {"code": 0, "msg": "用户信息已更新"}


@router.delete("/admin/users/{user_id}")
async def delete_user(user_id: str, request: Request):
    _require_write(request)
    users = _load_json(_USERS_FILE)
    if isinstance(users, dict):
        users = list(users.values())
    new_users = [u for u in users if u.get("id") != user_id]
    if len(new_users) == len(users):
        return {"code": -1, "msg": "用户不存在"}
    _save_json(_USERS_FILE, new_users)
    return {"code": 0, "msg": "用户已删除"}


# ── Order Management ──

@router.get("/admin/orders")
async def list_orders(request: Request):
    _require_admin(request)
    orders = _load_json(_ORDERS_FILE)
    if isinstance(orders, dict):
        items = list(orders.values())
    else:
        items = orders
    # Sort by createdAt descending
    items.sort(key=lambda o: o.get("createdAt", ""), reverse=True)
    result = []
    for o in items:
        result.append({
            "orderNo": o.get("orderNo", ""),
            "name": o.get("name", ""),
            "phone": o.get("phone", ""),
            "email": o.get("email", ""),
            "productName": o.get("productName", ""),
            "productId": o.get("productId", ""),
            "price": o.get("price", 0),
            "status": o.get("status", "pending"),
            "createdAt": o.get("createdAt", ""),
            "paidAt": o.get("paidAt"),
        })
    return {"code": 0, "data": result}


# ── Login Logs ──

@router.get("/admin/login-logs")
async def list_login_logs(request: Request):
    _require_admin(request)
    logs = _load_json(_LOGIN_LOGS_FILE)
    if isinstance(logs, dict):
        logs = list(logs.values())
    # Sort by time descending
    logs.sort(key=lambda l: l.get("time", ""), reverse=True)
    return {"code": 0, "data": logs}


# ============================================================
# Create New Agent (本地新增机器人)
# ============================================================

@router.post("/agents/create")
async def create_new_agent(params: dict):
    """
    Create a new Hermes Agent profile locally.

    Flow:
    1. Create profile via hermes CLI
    2. Configure .env with WECOM_BOT_ID and WECOM_SECRET
    3. Set display_name and personality in config.yaml
    4. Start gateway
    5. Return status for pairing-poll
    """
    import base64
    import uuid
    import time

    machine_name = (params.get("machineName") or "").strip()
    role = (params.get("role") or "").strip()
    bot_id = (params.get("botId") or "").strip()
    secret = (params.get("secret") or "").strip()

    if not machine_name or not bot_id or not secret:
        raise HTTPException(400, "机器名称、BotId 和 Secret 为必填项")

    safe_name = "".join(c for c in machine_name if c.isalnum() or c in "-_").lower()
    if not safe_name:
        raise HTTPException(400, f"无法从「{machine_name}」生成有效的 Profile 名称")

    hermes_bin = _resolve_hermes_bin()
    profiles_dir = HERMES_HOME / "profiles"

    steps = []
    all_ok = True

    # Step 1: Check if exists
    steps.append({"step": f"检查 Profile「{safe_name}」", "status": "running"})
    exists = (profiles_dir / safe_name).exists()
    steps[-1] = {"step": f"检查 Profile「{safe_name}」", "status": "ok" if not exists else "fail",
                  "detail": "Profile 已存在" if exists else "OK"}
    if exists:
        return {"success": False, "steps": steps, "error": f"Profile「{safe_name}」已存在"}

    # Step 2: Create profile
    steps.append({"step": "创建 Profile", "status": "running"})
    try:
        r = subprocess.run(
            [hermes_bin, "profile", "create", safe_name, "--clone", "--clone-from", "default", "--no-alias"],
            capture_output=True, text=True, timeout=30, cwd=str(HERMES_HOME),
        )
        created = r.returncode == 0
        if not created:
            # Try direct directory creation fallback
            profile_dir = profiles_dir / safe_name
            profile_dir.mkdir(parents=True, exist_ok=True)
            # Copy from default profile
            default_cfg = HERMES_HOME / "config.yaml"
            if default_cfg.exists():
                import shutil
                shutil.copy(str(default_cfg), str(profile_dir / "config.yaml"))
            created = True
        all_ok = all_ok and created
        steps[-1] = {"step": "创建 Profile", "status": "ok" if created else "fail",
                      "detail": r.stderr[:200] if not created else "OK"}
    except Exception as e:
        steps[-1] = {"step": "创建 Profile", "status": "fail", "detail": str(e)}
        return {"success": False, "steps": steps, "error": str(e)}

    if not created:
        return {"success": False, "steps": steps, "error": "Profile 创建失败"}

    # Step 3: Configure .env
    steps.append({"step": "配置 Bot 凭证", "status": "running"})
    env_path = profiles_dir / safe_name / ".env"
    env_content = f"WECOM_BOT_ID={bot_id}\nWECOM_SECRET={secret}\n"
    try:
        env_path.write_text(env_content)
        env_path.chmod(0o600)
        steps[-1] = {"step": "配置 Bot 凭证", "status": "ok"}
    except Exception as e:
        steps[-1] = {"step": "配置 Bot 凭证", "status": "fail", "detail": str(e)}
        return {"success": False, "steps": steps, "error": str(e)}

    # Step 4: Update config.yaml
    steps.append({"step": "设置显示名称", "status": "running"})
    cfg_path = profiles_dir / safe_name / "config.yaml"
    try:
        import yaml as _yaml
        if cfg_path.exists():
            cfg = _yaml.safe_load(cfg_path.read_text()) or {}
            cfg["display_name"] = machine_name
            if role:
                cfg["personality"] = role
            cfg_path.write_text(_yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False))
        steps[-1] = {"step": "设置显示名称", "status": "ok", "detail": f"→ {machine_name}"}
    except Exception as e:
        steps[-1] = {"step": "设置显示名称", "status": "warn", "detail": str(e)}

    # Step 5: Start gateway
    steps.append({"step": "启动 Gateway", "status": "running"})
    try:
        proc = subprocess.Popen(
            [hermes_bin, "-p", safe_name, "gateway", "run", "--replace"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=str(HERMES_HOME),
        )
        time.sleep(3)
        steps[-1] = {"step": "启动 Gateway", "status": "ok", "detail": f"PID {proc.pid}"}
    except Exception as e:
        steps[-1] = {"step": "启动 Gateway", "status": "fail", "detail": str(e)}
        return {"success": False, "steps": steps, "error": str(e)}

    # Step 6: Verify running
    steps.append({"step": "验证运行状态", "status": "running"})
    gw_path = profiles_dir / safe_name / "gateway_state.json"
    is_running = False
    try:
        if gw_path.exists():
            gw_data = json.loads(gw_path.read_text())
            is_running = gw_data.get("gateway_state") == "running"
    except Exception:
        pass
    steps[-1] = {"step": "验证运行状态", "status": "ok" if is_running else "warn",
                  "detail": "Gateway 已运行" if is_running else "Gateway 可能还未完全就绪"}

    # Step 7: Pairing
    steps.append({"step": "等待配对…", "status": "ok",
                   "detail": "请用户向新机器人发送一条消息完成配对"})

    return {
        "success": all_ok,
        "steps": steps,
        "profile": safe_name,
        "display_name": machine_name,
        "gateway_running": is_running,
    }
