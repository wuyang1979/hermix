"""
Dashboard Overview plugin — backend API routes.

Mounted at /api/plugins/dashboard-overview/ by the dashboard plugin system.
Provides aggregated summary data for the main dashboard view:
system stats, agent health, platform status, cron summary, recent activity.
"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()

_HOME = Path("/home/agentuser")
HERMES_HOME = _HOME / ".hermes"
PROFILES_DIR = HERMES_HOME / "profiles"


def _run_cmd(cmd: list[str], timeout: int = 5) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def get_system_stats() -> dict:
    """Get CPU, memory, disk, uptime for the local machine."""
    # CPU
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = [int(x) for x in line.split()[1:]]
                    break
        time.sleep(0.3)
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts2 = [int(x) for x in line.split()[1:]]
                    break
        idle1 = parts[3] + parts[4]
        idle2 = parts2[3] + parts2[4]
        total1 = sum(parts[:8])
        total2 = sum(parts2[:8])
        cpu_pct = round(100.0 * (1.0 - (idle2 - idle1) / (total2 - total1)), 1) if (total2 - total1) > 0 else 0
    except Exception:
        cpu_pct = 0
        load = (0, 0, 0)

    # Memory
    try:
        with open("/proc/meminfo") as f:
            meminfo = f.read()
        total_kb = 0
        avail_kb = 0
        for line in meminfo.split("\n"):
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail_kb = int(line.split()[1])
        mem_total_gb = round(total_kb / 1048576, 1)
        mem_used_gb = round((total_kb - avail_kb) / 1048576, 1)
        mem_pct = round(100.0 * (total_kb - avail_kb) / total_kb, 1) if total_kb else 0
    except Exception:
        mem_total_gb = mem_used_gb = mem_pct = 0

    # Disk
    disk_items = []
    for path in ["/", "/home"]:
        try:
            s = os.statvfs(path)
            total = s.f_frsize * s.f_blocks
            free = s.f_frsize * s.f_bfree
            used = total - free
            disk_items.append({
                "path": path,
                "total_gb": round(total / (1024**3), 1),
                "used_gb": round(used / (1024**3), 1),
                "percent": round(100.0 * used / total, 1) if total else 0,
            })
        except Exception:
            pass

    # Uptime
    uptime_seconds = 0
    try:
        with open("/proc/uptime") as f:
            uptime_seconds = round(float(f.read().split()[0]))
    except Exception:
        pass

    # Hostname
    hostname = _run_cmd(["hostname"]) or "localhost"

    # Top processes
    top_procs = []
    try:
        ps_out = subprocess.run(
            ["ps", "aux", "--sort=-%cpu"],
            capture_output=True, text=True, timeout=5,
        )
        for line in ps_out.stdout.strip().split("\n")[1:6]:
            parts = line.split(None, 10)
            if len(parts) >= 11:
                top_procs.append({"name": parts[10][:35], "cpu": parts[2]})
    except Exception:
        pass

    return {
        "cpu_percent": cpu_pct,
        "load_1m": round(load[0], 2),
        "memory": {"total_gb": mem_total_gb, "used_gb": mem_used_gb, "percent": mem_pct},
        "disk": disk_items,
        "uptime_seconds": uptime_seconds,
        "hostname": hostname,
        "top_processes": top_procs,
    }


def get_agent_summary() -> dict:
    """Get agent counts and platform connection status."""
    total = 0
    online = 0
    platforms = {}
    agents_list = []

    # Check default profile
    gw_file = HERMES_HOME / "gateway_state.json"
    if gw_file.exists():
        try:
            data = json.loads(gw_file.read_text())
            state = data.get("gateway_state") == "running"
            total += 1
            if state:
                online += 1
            for p, info in (data.get("platforms") or {}).items():
                ps = info.get("state") if isinstance(info, dict) else ""
                platforms[p] = platforms.get(p, 0) + (1 if ps == "connected" else 0)
        except Exception:
            pass

    # Check profile gateways
    if PROFILES_DIR.exists():
        for d in sorted(PROFILES_DIR.iterdir()):
            if not d.is_dir():
                continue
            total += 1
            gw = d / "gateway_state.json"
            running = False
            try:
                data = json.loads(gw.read_text())
                running = data.get("gateway_state") == "running"
                for p, info in (data.get("platforms") or {}).items():
                    ps = info.get("state") if isinstance(info, dict) else ""
                    platforms[p] = platforms.get(p, 0) + (1 if ps == "connected" else 0)
            except Exception:
                pass
            if running:
                online += 1
            agents_list.append({
                "name": d.name,
                "running": running,
            })

    # Platform labels
    plat_labels = {"wecom": "企业微信", "weixin": "微信", "feishu": "飞书",
                   "discord": "Discord", "telegram": "Telegram", "dingtalk": "钉钉"}

    platform_status = []
    for pid, count in sorted(platforms.items()):
        platform_status.append({
            "id": pid,
            "label": plat_labels.get(pid, pid),
            "connected_count": count,
        })

    return {
        "total": total,
        "online": online,
        "offline": total - online,
        "online_rate": round(online / total * 100, 1) if total else 0,
        "platforms": platform_status,
        "agents": agents_list,
    }


def get_cron_summary() -> dict:
    """Get cron jobs summary count — reads jobs.json or cron.db."""
    # Try cron.db first
    cron_db = HERMES_HOME / "cron.db"
    if cron_db.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(cron_db))
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM jobs")
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                return {"total": row[0]}
        except Exception:
            pass
    # Fallback: parse jobs.json for actual job count
    jobs_file = HERMES_HOME / "cron" / "jobs.json"
    if jobs_file.exists():
        try:
            data = json.loads(jobs_file.read_text())
            if isinstance(data, list):
                return {"total": len(data)}
            if isinstance(data, dict):
                for key in ("jobs", "data", "items"):
                    if key in data and isinstance(data[key], list):
                        return {"total": len(data[key])}
                return {"total": len(data)}
        except Exception:
            pass
    return {"total": 0}


def get_recent_sessions() -> dict:
    """Get session stats for today — aggregates across all profiles."""
    import sqlite3
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    total_sessions = 0
    total_tokens = 0
    dbs_checked = []
    # Main state.db
    main_db = HERMES_HOME / "state.db"
    if main_db.exists():
        dbs_checked.append(main_db)
    # Profile state.db files
    if PROFILES_DIR.exists():
        for d in sorted(PROFILES_DIR.iterdir()):
            if d.is_dir():
                pdb = d / "state.db"
                if pdb.exists():
                    dbs_checked.append(pdb)
    for db_path in dbs_checked:
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*), COALESCE(SUM(input_tokens + output_tokens), 0)
                FROM sessions WHERE started_at >= ?
            """, (today_start,))
            row = cur.fetchone()
            conn.close()
            if row:
                total_sessions += row[0]
                total_tokens += row[1]
        except Exception:
            pass
    return {
        "today_sessions": total_sessions,
        "today_tokens": total_tokens,
    }


def get_model_usage_stats() -> dict:
    """Get model usage stats for the current month — across all profiles + ARK tracking.
    Includes cost calculation in RMB."""
    import sqlite3
    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).timestamp()

    # Merge old model names into new ones
    MODEL_NAME_MAP = {
        "deepseek-chat": "deepseek-v4-flash",
        "deepseek-reasoner": "deepseek-v4-pro",
    }

    # ── 模型定价（人民币/百万tokens） ──
    # 基于 DeepSeek 官网 USD 定价 × 7.25 汇率
    # deepseek-chat / deepseek-v4-flash: $0.14 input, $0.28 output
    # deepseek-reasoner / deepseek-v4-pro: $0.435 input, $0.87 output
    MODEL_PRICING = {
        "deepseek-v4-pro":   {"input": 3.15, "output": 6.3},
        "deepseek-v4-flash": {"input": 1.0,  "output": 2.0},
    }
    # ARK 生图/生视频模型 — 按 tokens 计费，参考价
    ARK_PRICING = {
        "doubao-seedream":    {"input": 0,    "output": 10.0},   # 生图 ¥10/百万tokens
        "doubao-seedance":    {"input": 0,    "output": 20.0},   # 生视频 ¥20/百万tokens
        "wan2":               {"input": 0,    "output": 20.0},
    }
    DEFAULT_PRICE = {"input": 1.0, "output": 4.0}

    def _calc_cost(model_name: str, inp_tok: int, out_tok: int) -> float:
        """计算人民币费用"""
        name_lower = model_name.lower()
        # 检查 ARK 模型
        for prefix, price in ARK_PRICING.items():
            if prefix in name_lower:
                return (inp_tok / 1_000_000 * price["input"] +
                        out_tok / 1_000_000 * price["output"])
        # 检查会话模型
        if name_lower in MODEL_PRICING:
            p = MODEL_PRICING[name_lower]
        else:
            p = DEFAULT_PRICE
        return (inp_tok / 1_000_000 * p["input"] +
                out_tok / 1_000_000 * p["output"])

    models = {}
    dbs_checked = []
    main_db = HERMES_HOME / "state.db"
    if main_db.exists():
        dbs_checked.append(main_db)
    if PROFILES_DIR.exists():
        for d in sorted(PROFILES_DIR.iterdir()):
            if d.is_dir():
                pdb = d / "state.db"
                if pdb.exists():
                    dbs_checked.append(pdb)
    for db_path in dbs_checked:
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            # 查询按模型分组的 input/output tokens 和调用次数
            cur.execute("""
                SELECT model, COUNT(*),
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(input_tokens + output_tokens), 0)
                FROM sessions
                WHERE started_at >= ? AND model IS NOT NULL AND model != ''
                GROUP BY model
                ORDER BY COUNT(*) DESC
            """, (month_start,))
            for row in cur.fetchall():
                m = MODEL_NAME_MAP.get(row[0], row[0])
                calls = row[1]
                inp_tok = row[2]
                out_tok = row[3]
                total_tok = row[4]
                if m not in models:
                    models[m] = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "tokens": 0}
                models[m]["calls"] += calls
                models[m]["input_tokens"] += inp_tok
                models[m]["output_tokens"] += out_tok
                models[m]["tokens"] += total_tok
            conn.close()
        except Exception:
            pass

    # Merge in ARK image/video generation model usage from tracker
    tracker_file = HERMES_HOME / "model_usage_extra.json"
    if tracker_file.exists():
        try:
            tracker_data = json.loads(tracker_file.read_text())
            now = datetime.now(timezone.utc)
            month_prefix = now.strftime("%Y-%m")
            for r in tracker_data.get("records", []):
                ts = r.get("timestamp", "")
                if not ts.startswith(month_prefix):
                    continue
                mid = r.get("model_name", r.get("model_id", "unknown"))
                calls = r.get("count", 1)
                tokens = r.get("tokens", 0)
                if mid not in models:
                    models[mid] = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "tokens": 0}
                models[mid]["calls"] += calls
                models[mid]["tokens"] += tokens
                models[mid]["output_tokens"] += tokens  # 生图/视频 tokens 记在 output
        except Exception:
            pass

    # Sort by tokens descending, calculate cost
    sorted_models = sorted(models.items(), key=lambda x: -x[1]["tokens"])
    result = []
    total_cost = 0.0
    for name, stats in sorted_models:
        cost = _calc_cost(name, stats["input_tokens"], stats["output_tokens"])
        total_cost += cost
        result.append({
            "name": name,
            "calls": stats["calls"],
            "input_tokens": stats["input_tokens"],
            "output_tokens": stats["output_tokens"],
            "tokens": stats["tokens"],
            "cost_rmb": round(cost, 2),
        })
    return {"models": result, "total_cost_rmb": round(total_cost, 2)}


def get_daily_usage() -> list:
    """Get daily token consumption and cost for current month — across all profiles."""
    import sqlite3

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    month_end = None
    # next month start as upper bound
    if now.month == 12:
        month_end = now.replace(year=now.year + 1, month=1, day=1,
                                hour=0, minute=0, second=0, microsecond=0).timestamp()
    else:
        month_end = now.replace(month=now.month + 1, day=1,
                                hour=0, minute=0, second=0, microsecond=0).timestamp()

    # Pricing (same as get_model_usage_stats)
    # Merge old model names into new ones
    MODEL_NAME_MAP = {
        "deepseek-chat": "deepseek-v4-flash",
        "deepseek-reasoner": "deepseek-v4-pro",
    }
    MODEL_PRICING = {
        "deepseek-v4-pro":   {"input": 3.15, "output": 6.3},
        "deepseek-v4-flash": {"input": 1.0,  "output": 2.0},
    }
    ARK_PRICING = {
        "doubao-seedream": {"input": 0, "output": 10.0},
        "doubao-seedance": {"input": 0, "output": 20.0},
        "wan2":            {"input": 0, "output": 20.0},
    }
    DEFAULT_PRICE = {"input": 1.0, "output": 4.0}

    def _calc_cost(model_name: str, inp_tok: int, out_tok: int) -> float:
        name_lower = model_name.lower()
        for prefix, price in ARK_PRICING.items():
            if prefix in name_lower:
                return inp_tok / 1_000_000 * price["input"] + out_tok / 1_000_000 * price["output"]
        p = MODEL_PRICING.get(name_lower, DEFAULT_PRICE)
        return inp_tok / 1_000_000 * p["input"] + out_tok / 1_000_000 * p["output"]

    # Aggregate per day: {date_str: {tokens, cost}}
    daily = {}
    dbs_checked = []
    main_db = HERMES_HOME / "state.db"
    if main_db.exists():
        dbs_checked.append(main_db)
    if PROFILES_DIR.exists():
        for d in sorted(PROFILES_DIR.iterdir()):
            if d.is_dir():
                pdb = d / "state.db"
                if pdb.exists():
                    dbs_checked.append(pdb)

    for db_path in dbs_checked:
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute("""
                SELECT model, started_at, COALESCE(input_tokens,0), COALESCE(output_tokens,0)
                FROM sessions
                WHERE started_at >= ? AND started_at < ?
                  AND model IS NOT NULL AND model != ''
            """, (month_start, month_end))
            for row in cur.fetchall():
                model = MODEL_NAME_MAP.get(row[0], row[0])
                ts = row[1]
                inp = row[2]
                out = row[3]
                # Get date string in Asia/Shanghai timezone
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                # Use UTC+8 for China time
                from datetime import timedelta
                local_dt = dt + timedelta(hours=8)
                day_key = local_dt.strftime("%m-%d")
                if day_key not in daily:
                    daily[day_key] = {"tokens": 0, "cost": 0.0}
                daily[day_key]["tokens"] += (inp + out)
                daily[day_key]["cost"] += _calc_cost(model, inp, out)
            conn.close()
        except Exception:
            pass

    # Build sorted array
    result = []
    for day in sorted(daily.keys()):
        result.append({
            "date": day,
            "tokens": daily[day]["tokens"],
            "cost_rmb": round(daily[day]["cost"], 2),
        })
    return result


def get_dashboard_lifetime() -> dict:
    """Get Dashboard process uptime."""
    try:
        r = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=5,
        )
        for line in r.stdout.split("\n"):
            if "hermes" in line and "dashboard" in line and "grep" not in line:
                parts = line.split()
                if len(parts) >= 11:
                    start_str = parts[8]
                    # Try to parse start time from /proc
                    pid = parts[1]
                    try:
                        with open(f"/proc/{pid}/stat") as f:
                            stat_line = f.read()
                        stat_parts = stat_line.split()
                        import os as _os
                        CLK_TCK = _os.sysconf(_os.sysconf_names["SC_CLK_TCK"])
                        with open("/proc/uptime") as f:
                            up = float(f.read().split()[0])
                        start_jiffies = int(stat_parts[21])
                        proc_uptime = time.time() - (time.time() - up + start_jiffies / CLK_TCK)
                        return {"uptime_seconds": round(proc_uptime)}
                    except Exception:
                        pass
        return {"uptime_seconds": 0}
    except Exception:
        return {"uptime_seconds": 0}


@router.get("/summary")
async def get_summary():
    """Aggregated dashboard overview data."""
    system = get_system_stats()
    agents = get_agent_summary()
    cron = get_cron_summary()
    sessions = get_recent_sessions()
    dashboard = get_dashboard_lifetime()
    topology = get_topology_data()
    model_usage = get_model_usage_stats()
    daily_usage = get_daily_usage()

    return {
        "code": 0,
        "data": {
            "system": system,
            "agents": agents,
            "cron": cron,
            "sessions": sessions,
            "dashboard": dashboard,
            "topology": topology,
            "model_usage": model_usage,
            "daily_usage": daily_usage,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }


@router.get("/models")
async def get_models():
    """Get model usage stats."""
    return {"code": 0, "data": get_model_usage_stats()}


# ============================================================
# Topology Data — Server & Agent Connection Graph
# ============================================================

def _ssh_test(host: str, port: int, username: str, auth_mode: str, password: str) -> dict:
    """Quick SSH connection test to a remote host."""
    ssh_opts = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=8",
        "-o", "ServerAliveInterval=10",
    ]
    try:
        if auth_mode == "password" and password:
            cmd = ["sshpass", "-p", password, "ssh"] + ssh_opts + \
                  ["-p", str(port), f"{username}@{host}", "echo OK && hostname && cat ~/.hermes/profiles/*/gateway_state.json 2>/dev/null | head -c 2000 || true"]
        else:
            cmd = ["ssh"] + ssh_opts + ["-p", str(port), f"{username}@{host}",
                   "echo OK && hostname && cat ~/.hermes/profiles/*/gateway_state.json 2>/dev/null | head -c 2000 || true"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        stdout = r.stdout.strip()
        if r.returncode == 0 and "OK" in stdout:
            lines = stdout.split("\n")
            hostname = ""
            profiles_json = ""
            for i, line in enumerate(lines):
                if line == "OK":
                    if i + 1 < len(lines):
                        hostname = lines[i + 1].strip()
                    if i + 2 < len(lines):
                        profiles_json = "\n".join(lines[i + 2:])
                    break
            # Parse gateway states for agent profiles
            agents_on_server = []
            if profiles_json:
                # Try to parse individual JSON objects
                import re
                for m in re.finditer(r'\{[^{}]*"gateway_state"[^{}]*\}', profiles_json):
                    try:
                        gw = json.loads(m.group())
                        agents_on_server.append({
                            "state": gw.get("gateway_state", "unknown"),
                            "platforms": list((gw.get("platforms") or {}).keys()),
                        })
                    except Exception:
                        pass
            return {
                "connected": True,
                "hostname": hostname or host,
                "agents": agents_on_server,
            }
        stderr = r.stderr.strip()[:200]
        return {"connected": False, "error": stderr or "Connection failed"}
    except subprocess.TimeoutExpired:
        return {"connected": False, "error": "Connection timed out"}
    except FileNotFoundError:
        return {"connected": False, "error": "sshpass not available"}
    except Exception as e:
        return {"connected": False, "error": str(e)[:200]}


def _infer_location(host: str, server_name: str = "") -> str:
    """Infer server location from IP prefix or server name."""
    # Check server name keywords
    name_lower = server_name.lower()
    for kw, loc in [("深圳","深圳"),("广州","广州"),("上海","上海"),("北京","北京"),
                    ("华东","华东"),("华北","华北"),("华南","华南"),("香港","香港"),
                    ("东京","东京"),("新加坡","新加坡"),("美西","美西"),("硅谷","硅谷")]:
        if kw in name_lower:
            return loc
    # IP-based inference
    if host.startswith("118.") or host.startswith("119."):
        return "华北"
    if host.startswith("124.") or host.startswith("120."):
        return "华南"
    if host.startswith("101.") or host.startswith("103."):
        return "华东"
    if host.startswith("8.") or host.startswith("23.") or host.startswith("45."):
        return "海外"
    return "—"


def get_topology_data() -> dict:
    """Build topology graph: local node + remote servers + connections."""
    # Local node
    local_hostname = _run_cmd(["hostname"]) or "localhost"
    local_agents = get_agent_summary()

    # Build local agents list
    local_agents_list = []
    if PROFILES_DIR.exists():
        for d in sorted(PROFILES_DIR.iterdir()):
            if not d.is_dir():
                continue
            gw = d / "gateway_state.json"
            running = False
            platforms = []
            try:
                data = json.loads(gw.read_text())
                running = data.get("gateway_state") == "running"
                platforms = list((data.get("platforms") or {}).keys())
            except Exception:
                pass
            local_agents_list.append({
                "name": d.name,
                "status": "running" if running else "stopped",
                "platforms": platforms,
            })

    local_node = {
        "id": "local",
        "name": local_hostname,
        "type": "local",
        "location": "本机",
        "agents": local_agents_list,
        "agent_count": len(local_agents_list),
        "online_agents": sum(1 for a in local_agents_list if a["status"] == "running"),
    }

    # Remote servers from multi-server-manager
    msm_file = _HOME / ".hermes" / "plugins" / "multi-server-manager" / "servers.json"
    remote_nodes = []
    edges = []

    if msm_file.exists():
        try:
            servers = json.loads(msm_file.read_text())
            for svr in servers:
                result = _ssh_test(
                    host=svr["host"],
                    port=svr.get("port", 22),
                    username=svr.get("username", "root"),
                    auth_mode=svr.get("auth_mode", "password"),
                    password=svr.get("password", ""),
                )
                remote_agents = []
                if result.get("connected") and result.get("agents"):
                    for ag in result["agents"]:
                        remote_agents.append({
                            "name": "agent",
                            "status": ag.get("state", "unknown"),
                            "platforms": ag.get("platforms", []),
                        })
                node = {
                    "id": svr["id"],
                    "name": svr.get("name", svr["host"]),
                    "host": svr["host"],
                    "type": "remote",
                    "location": _infer_location(svr["host"], svr.get("name", "")),
                    "connected": result.get("connected", False),
                    "hostname": result.get("hostname", ""),
                    "error": result.get("error", ""),
                    "agents": remote_agents,
                    "agent_count": len(remote_agents),
                    "online_agents": sum(1 for a in remote_agents if a["status"] == "running"),
                }
                remote_nodes.append(node)
                edges.append({
                    "source": "local",
                    "target": svr["id"],
                    "status": "connected" if result.get("connected") else "disconnected",
                })
        except Exception as e:
            logger.warning(f"Failed to load topology: {e}")

    return {
        "local": local_node,
        "remotes": remote_nodes,
        "edges": edges,
    }
