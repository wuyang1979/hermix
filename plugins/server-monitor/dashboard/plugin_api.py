"""Server Monitor plugin — backend API routes.

Mounted at /api/plugins/server-monitor/ by the dashboard plugin system.
Provides real-time CPU, memory, and disk usage data with anomaly alerts.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()


def _run_cmd(cmd: list[str], timeout: int = 5) -> str:
    """Run a shell command and return stdout, or empty string on failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except Exception as e:
        logger.warning(f"Command failed: {' '.join(cmd)} — {e}")
        return ""


def _parse_uptime() -> float:
    """Return system uptime in seconds."""
    out = _run_cmd(["cat", "/proc/uptime"])
    if out:
        try:
            return float(out.split()[0])
        except (IndexError, ValueError):
            pass
    return 0


def get_cpu_usage() -> dict:
    """Get CPU usage percentage (1s average) and load averages."""
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    uptime = _parse_uptime()
    ncpus = os.cpu_count() or 1

    # Read /proc/stat for a 1-second delta
    def _read_stat():
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = line.split()
                    return [int(x) for x in parts[1:]]
        return None

    stat1 = _read_stat()
    if stat1:
        time.sleep(0.5)
        stat2 = _read_stat()
        if stat2:
            idle1 = stat1[3] + stat1[4]  # idle + iowait
            idle2 = stat2[3] + stat2[4]
            total1 = sum(stat1[:8])
            total2 = sum(stat2[:8])
            delta_idle = idle2 - idle1
            delta_total = total2 - total1
            if delta_total > 0:
                pct = round(100.0 * (1.0 - delta_idle / delta_total), 1)
            else:
                pct = 0.0
        else:
            pct = 0.0
    else:
        pct = 0.0

    return {
        "percent": pct,
        "load_1m": round(load[0], 2),
        "load_5m": round(load[1], 2),
        "load_15m": round(load[2], 2),
        "cores": ncpus,
        "uptime_seconds": round(uptime),
    }


def get_memory_usage() -> dict:
    """Get memory and swap usage."""
    try:
        total_kb = 0
        free_kb = 0
        available_kb = 0
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                label = parts[0].rstrip(":")
                if label == "MemTotal":
                    total_kb = int(parts[1])
                elif label == "MemFree":
                    free_kb = int(parts[1])
                elif label == "MemAvailable":
                    available_kb = int(parts[1])
        return {
            "total_gb": round(total_kb / 1048576, 1) if total_kb else 0,
            "used_gb": round((total_kb - available_kb) / 1048576, 1) if total_kb and available_kb else 0,
            "percent": round(100.0 * (total_kb - available_kb) / total_kb, 1) if total_kb and available_kb else 0,
            "free_gb": round(available_kb / 1048576, 1) if available_kb else 0,
        }
    except Exception as e:
        logger.warning(f"Failed to read memory: {e}")
        return {"total_gb": 0, "used_gb": 0, "percent": 0, "free_gb": 0}


def get_swap_usage() -> dict:
    """Get swap usage."""
    try:
        total_kb = 0
        free_kb = 0
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                label = parts[0].rstrip(":")
                if label == "SwapTotal":
                    total_kb = int(parts[1])
                elif label == "SwapFree":
                    free_kb = int(parts[1])
        used_kb = total_kb - free_kb
        return {
            "total_gb": round(total_kb / 1048576, 1) if total_kb else 0,
            "used_gb": round(used_kb / 1048576, 1) if used_kb else 0,
            "percent": round(100.0 * used_kb / total_kb, 1) if total_kb else 0,
        }
    except Exception as e:
        logger.warning(f"Failed to read swap: {e}")
        return {"total_gb": 0, "used_gb": 0, "percent": 0}


def get_disk_usage() -> dict:
    """Get disk usage for root and data partitions."""
    partitions = []
    for path in ["/", "/home", "/www"]:
        try:
            usage = shutil.disk_usage(path)
            total_gb = round(usage.total / (1024**3), 1)
            used_gb = round(usage.used / (1024**3), 1)
            free_gb = round(usage.free / (1024**3), 1)
            percent = round(100.0 * usage.used / usage.total, 1) if usage.total else 0
            partitions.append({
                "path": path,
                "total_gb": total_gb,
                "used_gb": used_gb,
                "free_gb": free_gb,
                "percent": percent,
            })
        except Exception:
            pass
    return {"partitions": partitions}


def get_anomalies(cpu: dict, mem: dict, disk: dict) -> list[dict]:
    """Check for anomalies and return alerts."""
    alerts = []

    # CPU thresholds
    if cpu["percent"] > 90:
        alerts.append({
            "level": "critical",
            "message": f"CPU 使用率 {cpu['percent']}% — 超过 90% 警戒线",
        })
    elif cpu["percent"] > 75:
        alerts.append({
            "level": "warning",
            "message": f"CPU 使用率 {cpu['percent']}% — 超过 75% 预警线",
        })

    # Load average relative to core count
    load_1m = cpu.get("load_1m", 0)
    cores = cpu.get("cores", 1)
    if load_1m > cores * 2:
        alerts.append({
            "level": "critical",
            "message": f"系统负载 {load_1m} — 超过核心数 ({cores}核) 的 2 倍",
        })
    elif load_1m > cores:
        alerts.append({
            "level": "warning",
            "message": f"系统负载 {load_1m} — 超过核心数 ({cores}核)",
        })

    # Memory thresholds
    mem_pct = mem.get("percent", 0)
    if mem_pct > 90:
        alerts.append({
            "level": "critical",
            "message": f"内存使用率 {mem_pct}% — 超过 90% 警戒线",
        })
    elif mem_pct > 80:
        alerts.append({
            "level": "warning",
            "message": f"内存使用率 {mem_pct}% — 超过 80% 预警线",
        })

    # Disk thresholds
    for p in disk.get("partitions", []):
        pct = p.get("percent", 0)
        path = p.get("path", "")
        if pct > 90:
            alerts.append({
                "level": "critical",
                "message": f"磁盘 {path} 使用率 {pct}% — 超过 90% 警戒线",
            })
        elif pct > 80:
            alerts.append({
                "level": "warning",
                "message": f"磁盘 {path} 使用率 {pct}% — 超过 80% 预警线",
            })

    # Sort: critical first, then warning
    alerts.sort(key=lambda a: 0 if a["level"] == "critical" else 1)
    return alerts


def _dir_size(path: Path) -> int:
    """Get total file size in bytes for a directory (or 0 if not exists)."""
    if not path.exists():
        return 0
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except Exception:
        return 0


def get_cache_sizes() -> dict:
    """Get current sizes of pip, npm, uv, and profile pip caches."""
    hermes_home = Path(os.path.expanduser("~/.hermes"))
    home = Path(os.path.expanduser("~"))

    caches = []

    # pip cache (system)
    pip_cache = home / ".cache" / "pip"
    s = _dir_size(pip_cache)
    if s:
        caches.append({"name": "pip 缓存", "path": str(pip_cache), "size_bytes": s})

    # npm cache
    npm_cache = home / ".npm"
    s = _dir_size(npm_cache)
    if s:
        caches.append({"name": "npm 缓存", "path": str(npm_cache), "size_bytes": s})

    # uv cache
    uv_cache = home / ".cache" / "uv"
    s = _dir_size(uv_cache)
    if s:
        caches.append({"name": "uv 缓存", "path": str(uv_cache), "size_bytes": s})

    # Profile pip caches
    if hermes_home.exists():
        for profile_dir in sorted(hermes_home.glob("profiles/*/home/.cache/pip")):
            s = _dir_size(profile_dir)
            if s:
                caches.append({
                    "name": f"{profile_dir.parent.parent.name} pip 缓存",
                    "path": str(profile_dir),
                    "size_bytes": s,
                })

    return {"caches": caches, "total_bytes": sum(c["size_bytes"] for c in caches)}


def _format_duration(seconds: float) -> str:
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


# ============================================================
# API Endpoints
# ============================================================


@router.get("/stats")
async def get_stats():
    """Get all server stats with anomaly alerts."""
    cpu = get_cpu_usage()
    mem = get_memory_usage()
    swap = get_swap_usage()
    disk = get_disk_usage()
    caches = get_cache_sizes()
    alerts = get_anomalies(cpu, mem, disk)

    # Extra system info
    hostname = _run_cmd(["hostname"])
    kernel = _run_cmd(["uname", "-r"])
    proc_count = 0
    top_procs = []
    try:
        ps_out = subprocess.run(
            ["ps", "aux", "--sort=-%cpu"],
            capture_output=True, text=True, timeout=5,
        )
        lines = ps_out.stdout.strip().split("\n")
        proc_count = len(lines) - 1  # subtract header
        # Top 5 CPU consumers
        for line in lines[1:6]:
            parts = line.split(None, 10)
            if len(parts) >= 11:
                top_procs.append({
                    "name": parts[10][:40],
                    "cpu": parts[2],
                    "mem": parts[3],
                })
    except Exception:
        pass

    # Network traffic (rough from /proc/net/dev)
    net_rx = 0
    net_tx = 0
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if ":" in line and "lo" not in line:
                    parts = line.split()
                    net_rx += int(parts[1])
                    net_tx += int(parts[9])
    except Exception:
        pass

    return {
        "code": 0,
        "data": {
            "cpu": cpu,
            "memory": mem,
            "swap": swap,
            "disk": disk,
            "caches": caches,
            "alerts": alerts,
            "hostname": hostname or "localhost",
            "kernel": kernel or "",
            "process_count": proc_count,
            "top_processes": top_procs,
            "network_rx_bytes": net_rx,
            "network_tx_bytes": net_tx,
            "uptime_display": _format_duration(cpu["uptime_seconds"]),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }


@router.post("/clean-cache")
async def clean_cache():
    """Clean pip, npm, and uv caches to free disk space."""
    results = []
    freed_bytes = 0

    # 1. pip cache
    try:
        r = subprocess.run(
            ["pip", "cache", "purge"],
            capture_output=True, text=True, timeout=30,
        )
        freed_bytes += _parse_purge_bytes(r.stdout)
        results.append({"name": "pip 缓存", "ok": True, "detail": r.stdout.strip() or "OK"})
    except Exception as e:
        results.append({"name": "pip 缓存", "ok": False, "detail": str(e)})

    # 2. npm cache
    try:
        r = subprocess.run(
            ["npm", "cache", "clean", "--force"],
            capture_output=True, text=True, timeout=30,
        )
        results.append({"name": "npm 缓存", "ok": True, "detail": r.stdout.strip() or "OK"})
    except Exception as e:
        results.append({"name": "npm 缓存", "ok": False, "detail": str(e)})

    # 3. uv cache (user home)
    uv_cache = Path(os.path.expanduser("~/.cache/uv"))
    if uv_cache.exists():
        try:
            uv_size = sum(
                f.stat().st_size for f in uv_cache.rglob("*") if f.is_file()
            )
            import shutil
            shutil.rmtree(uv_cache)
            freed_bytes += uv_size
            results.append({"name": "uv 缓存", "ok": True, "detail": f"已删除 {uv_size / 1024 / 1024:.0f} MB"})
        except Exception as e:
            results.append({"name": "uv 缓存", "ok": False, "detail": str(e)})
    else:
        results.append({"name": "uv 缓存", "ok": True, "detail": "无缓存"})

    # 4. pip cache in hermes profile homes
    hermes_home = Path(os.path.expanduser("~/.hermes"))
    if hermes_home.exists():
        for profile_dir in hermes_home.glob("profiles/*/home/.cache/pip"):
            if profile_dir.exists():
                try:
                    p_size = sum(
                        f.stat().st_size for f in profile_dir.rglob("*") if f.is_file()
                    )
                    import shutil
                    shutil.rmtree(profile_dir)
                    freed_bytes += p_size
                    results.append({
                        "name": f"profile {profile_dir.parent.parent.name} pip 缓存",
                        "ok": True,
                        "detail": f"已删除 {p_size / 1024 / 1024:.0f} MB",
                    })
                except Exception as e:
                    results.append({
                        "name": f"profile {profile_dir.parent.parent.name} pip 缓存",
                        "ok": False,
                        "detail": str(e),
                    })

    return {
        "code": 0,
        "data": {
            "results": results,
            "freed_mb": round(freed_bytes / 1024 / 1024, 1),
        },
    }


def _parse_purge_bytes(stdout: str) -> int:
    """Parse 'Files removed: 68' from pip cache purge output."""
    import re
    m = re.search(r"Files removed:\s*(\d+)", stdout)
    if m:
        # Approximate 5 MB per file
        return int(m.group(1)) * 5 * 1024 * 1024
    return 0
