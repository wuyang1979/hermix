"""
Multi-Server Manager plugin — backend API routes.

Mounted at /api/plugins/multi-server-manager/ by the dashboard plugin system.
Manages remote Hermes servers via SSH: Dashboard start/stop/status/deploy.
"""
import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# Data storage
_HOME = Path("/home/agentuser")
DATA_FILE = _HOME / ".hermes" / "plugins" / "multi-server-manager" / "servers.json"
SSH_KEY_DIR = _HOME / ".hermes" / "plugins" / "multi-server-manager" / "keys"

# Ensure directories exist
DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
SSH_KEY_DIR.mkdir(parents=True, exist_ok=True)


# ── Models ──

class SSHKeyRef(BaseModel):
    label: str = ""
    private_key: str  # PEM content or path on server


class ServerConfig(BaseModel):
    id: str = ""
    name: str
    host: str
    port: int = 22
    username: str = "root"
    auth_mode: str = "key"  # "key" or "password"
    password: str = ""
    ssh_key_id: str = ""
    hermes_home: str = "~/.hermes"
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""


class ServerInfo(ServerConfig):
    """Server with id and timestamps auto-filled."""
    pass


# ── Data Helpers ──

def _load_servers() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _save_servers(servers: list[dict]):
    DATA_FILE.write_text(json.dumps(servers, indent=2, ensure_ascii=False))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── SSH Helpers ──

def _ssh_cmd(server: dict, remote_cmd: str, timeout: int = 30) -> dict:
    """Execute a command on a remote server via SSH and return result."""
    host = server["host"]
    port = server.get("port", 22)
    user = server.get("username", "root")
    auth_mode = server.get("auth_mode", "key")

    ssh_opts = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15",
    ]

    # Build SSH command
    if auth_mode == "password" and server.get("password"):
        # Use sshpass for password auth
        cmd_parts = ["sshpass", "-p", server["password"], "ssh"] + ssh_opts + [
            "-p", str(port), f"{user}@{host}", remote_cmd
        ]
    else:
        # Use SSH key (preferred)
        key_id = server.get("ssh_key_id", "")
        key_path = SSH_KEY_DIR / f"{key_id}.pem" if key_id else ""
        if key_id and key_path.exists():
            ssh_opts += ["-i", str(key_path)]
        cmd_parts = ["ssh"] + ssh_opts + ["-p", str(port), f"{user}@{host}", remote_cmd]

    try:
        result = subprocess.run(
            cmd_parts,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "success": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip()[:1000],
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "exit_code": -1, "stdout": "", "stderr": "SSH command timed out"}
    except FileNotFoundError:
        return {"success": False, "exit_code": -1, "stdout": "", "stderr": "SSH or sshpass not found on this server"}
    except Exception as e:
        return {"success": False, "exit_code": -1, "stdout": "", "stderr": str(e)}


def _test_connection(server: dict) -> dict:
    """Test SSH connection to the server."""
    result = _ssh_cmd(server, "echo HERMES_OK && hostname", timeout=15)
    if result["success"] and "HERMES_OK" in result["stdout"]:
        hostname = result["stdout"].replace("HERMES_OK", "").strip()
        return {"connected": True, "hostname": hostname}
    return {"connected": False, "error": result["stderr"] or "Connection failed"}


def _check_hermes_installed(server: dict) -> dict:
    """Check if Hermes Agent is installed on the remote server.

    Tries multiple strategies:
    1. which hermes (standard PATH lookup)
    2. Common binary paths (~/.local/bin/hermes, ~/hermes)
    3. Check ~/.hermes directory exists (indicates Hermes installed even if binary not in PATH)
    """
    hermes_home = server.get("hermes_home", "~/.hermes")

    # Strategy 1: which hermes + version
    result = _ssh_cmd(server, "which hermes 2>/dev/null && hermes --version 2>/dev/null || echo 'NOT_FOUND'", timeout=15)
    if result["success"] and "NOT_FOUND" not in result["stdout"]:
        lines = result["stdout"].strip().split("\n")
        hermes_path = lines[0] if lines else ""
        version = lines[1] if len(lines) > 1 else ""
        return {"installed": True, "path": hermes_path, "version": version}

    # Strategy 2: Check common binary locations
    # Resolve hermes_home: if it starts with ~/, expand to the remote user's home
    remote_user = server.get("username", "root")
    resolved_home = hermes_home
    if hermes_home.startswith("~/"):
        resolved_home = f"/home/{remote_user}/.{hermes_home[3:]}" if remote_user != "root" else f"/root/{hermes_home[3:]}"

    common_paths = [
        "~/.local/bin/hermes",
        "~/hermes",
        f"{hermes_home}/hermes-agent/venv/bin/hermes",
        f"/home/ubuntu/.local/bin/hermes",     # Common fallback
        f"/home/ubuntu/hermes",                 # User reported this path
        f"{resolved_home}/hermes-agent/venv/bin/hermes",  # Absolute path
    ]
    for p in common_paths:
        r = _ssh_cmd(server, f"test -f {p} && {p} --version 2>/dev/null || echo 'NO'", timeout=10)
        if r["success"] and r["stdout"].strip() != "NO":
            return {"installed": True, "path": p, "version": r["stdout"].strip()}

    # Strategy 3: Check if ~/.hermes directory exists (suggests Hermes installed)
    r = _ssh_cmd(server, f"test -d {hermes_home} && echo 'DIR_EXISTS' || echo 'NO_DIR'", timeout=10)
    if r["success"] and "DIR_EXISTS" in r["stdout"]:
        version_check = _ssh_cmd(server, f"cat {hermes_home}/SOUL.md 2>/dev/null | head -3; echo '---'; ls {hermes_home}/config.yaml 2>/dev/null || echo 'no config'", timeout=10)
        return {"installed": True, "path": f"{hermes_home} (detected by directory)", "version": version_check["stdout"][:200] if version_check["success"] else "unknown"}

    return {"installed": False}


def _check_dashboard_status(server: dict) -> dict:
    """Check if Dashboard is running on the remote server."""
    # Check for hermes dashboard process
    ps_cmd = "ps aux | grep 'hermes.*dashboard' | grep -v grep | head -5"
    result = _ssh_cmd(server, ps_cmd, timeout=15)

    dashboards = []
    for line in result["stdout"].split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 11:
            cmdline = " ".join(parts[10:])
            # Try to extract port
            port = "8080"
            for p in parts:
                if p.startswith("--port="):
                    port = p.split("=")[1]
                elif p.startswith("--port"):
                    idx = parts.index(p) + 1
                    if idx < len(parts):
                        port = parts[idx]

            dashboards.append({
                "pid": parts[1],
                "port": port,
                "uptime": parts[8],
                "cmdline": cmdline[:150],
            })

    # Also check if port 8080 is listening
    port_check = _ssh_cmd(server, "ss -tlnp | grep -E ':8080|:9119' | head -5", timeout=10)

    return {
        "running": len(dashboards) > 0,
        "processes": dashboards,
        "port_status": port_check["stdout"][:500],
    }


def _get_remote_user(server: dict) -> str:
    """Extract the remote user from server config, with smart fallback."""
    ssh_user = server.get("username", "root")
    hh = server.get("hermes_home", "~/.hermes")
    # If SSH user is root or unknown, use root directly
    if ssh_user in ("root", ""):
        return "root"
    # Use the configured SSH username (non-root)
    return ssh_user


def _run_remote_cmd(server: dict, remote_cmd: str, target_user: str, timeout: int = 30) -> dict:
    """Execute a command on the remote server, smartly wrapping with user switch.

    If SSH user == target_user, run directly (no su/sudo needed).
    Otherwise use sudo -iu (works without password when caller has sudo rights).
    """
    ssh_user = server.get("username", "root")
    if ssh_user == target_user:
        # Already running as the target user — no switch needed
        return _ssh_cmd(server, remote_cmd, timeout=timeout)
    # Need to switch: root → ubuntu, etc. Use sudo (no password needed)
    wrapped = f"sudo -iu {target_user} -- bash -c '{remote_cmd}'"
    return _ssh_cmd(server, wrapped, timeout=timeout)


def _probe_remote_hermes(server: dict, configured_home: str, configured_user: str) -> dict:
    """Probe remote server to find the actual Hermes installation directory and user.

    If configured hermes_home is wrong (e.g. /home/ubuntu/.hermes when Hermes
    is under /root/.hermes), this probes common alternatives to find the truth.
    """
    paths = [
        configured_home,
        "/root/.hermes",
        "/home/ubuntu/.hermes",
    ]
    ssh_user = server.get("username", "root")
    if ssh_user != "root":
        paths.append(f"/home/{ssh_user}/.hermes")
    unique = list(dict.fromkeys(paths))
    for probe_home in unique:
        if not probe_home.startswith("/"):
            continue
        result = _ssh_cmd(server, f"test -d {probe_home}/hermes-agent && echo FOUND || echo NOT_FOUND", timeout=8)
        if result.get("success") and "FOUND" in (result.get("stdout") or "") and "NOT_FOUND" not in (result.get("stdout") or ""):
            users_to_check = [configured_user, ssh_user, "root"]
            seen_users = set()
            for u in users_to_check:
                if u in seen_users:
                    continue
                seen_users.add(u)
                if u == "root":
                    return {"found": True, "hermes_home": probe_home, "user": "root"}
                check = _ssh_cmd(server, f"id {u} 2>/dev/null && echo OK || echo NO", timeout=5)
                if check.get("success") and "OK" in (check.get("stdout") or ""):
                    return {"found": True, "hermes_home": probe_home, "user": u}
            return {"found": True, "hermes_home": probe_home, "user": "root"}
    return {"found": False, "hermes_home": configured_home, "user": configured_user}


@router.get("/servers")
async def list_servers():
    """List all configured remote servers."""
    return _load_servers()


@router.post("/servers")
async def add_server(config: ServerConfig):
    """Add a new remote server configuration."""
    servers = _load_servers()

    # Validate no duplicate host
    for s in servers:
        if s["host"] == config.host:
            raise HTTPException(400, f"Server {config.host} already exists")

    server = config.model_dump()
    server["id"] = uuid.uuid4().hex[:12]
    server["created_at"] = _now()
    server["updated_at"] = _now()

    # Save SSH key if provided
    if config.auth_mode == "key" and config.ssh_key_id:
        key_id = config.ssh_key_id
        # key content should already be saved via the keys endpoint

    servers.append(server)
    _save_servers(servers)
    return {"success": True, "server": server}


@router.put("/servers/{server_id}")
async def update_server(server_id: str, config: ServerConfig):
    """Update a remote server configuration."""
    servers = _load_servers()
    for i, s in enumerate(servers):
        if s["id"] == server_id:
            updated = config.model_dump()
            updated["id"] = server_id
            updated["created_at"] = s.get("created_at", _now())
            updated["updated_at"] = _now()
            servers[i] = updated
            _save_servers(servers)
            return {"success": True, "server": updated}

    raise HTTPException(404, f"Server {server_id} not found")


@router.delete("/servers/{server_id}")
async def delete_server(server_id: str):
    """Remove a remote server configuration."""
    servers = _load_servers()
    new_servers = [s for s in servers if s["id"] != server_id]
    if len(new_servers) == len(servers):
        raise HTTPException(404, f"Server {server_id} not found")
    _save_servers(new_servers)
    return {"success": True}


@router.post("/servers/{server_id}/test")
async def test_connection(server_id: str):
    """Test SSH connection to the remote server."""
    servers = _load_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        raise HTTPException(404, f"Server {server_id} not found")

    connection = _test_connection(server)
    hermes = _check_hermes_installed(server) if connection["connected"] else {"installed": False}

    return {
        "connection": connection,
        "hermes": hermes,
    }


def _check_agent_profiles(server: dict) -> list:
    """Get all Hermes Agent profiles and their status from the remote server."""
    hermes_home = server.get("hermes_home", "~/.hermes")
    agents = []

    # Check default profile (root gateway_state.json)
    result = _ssh_cmd(server, f"cat {hermes_home}/gateway_state.json 2>/dev/null || echo '{{}}'", timeout=15)
    try:
        gw_state = json.loads(result["stdout"]) if result["success"] else {}
        if gw_state and gw_state.get("gateway_state"):
            platforms = gw_state.get("platforms", {})
            plat_summary = {}
            for p, info in platforms.items():
                if info and info.get("state"):
                    plat_summary[p] = info["state"]
            agents.append({
                "name": "default",
                "status": "running" if gw_state.get("gateway_state") == "running" else "stopped",
                "pid": str(gw_state.get("pid", "")),
                "platforms": plat_summary,
            })
    except Exception:
        pass

    # Check profiles directory
    result = _ssh_cmd(server, f"ls -d {hermes_home}/profiles/*/ 2>/dev/null || true", timeout=10)
    if result["success"] and result["stdout"]:
        for line in result["stdout"].split("\n"):
            line = line.strip()
            if not line: continue
            prof_name = line.rstrip("/").split("/")[-1]
            if not prof_name or prof_name == "*": continue

            # Get profile gateway_state
            ps_result = _ssh_cmd(server, f"cat {hermes_home}/profiles/{prof_name}/gateway_state.json 2>/dev/null || echo '{{}}'", timeout=10)
            prof_state = {}
            try:
                prof_state = json.loads(ps_result["stdout"]) if ps_result["success"] else {}
            except Exception: pass

            # Get display_name
            dn_result = _ssh_cmd(server, f"grep 'display_name:' {hermes_home}/profiles/{prof_name}/config.yaml 2>/dev/null || true", timeout=5)
            display_name = prof_name
            if dn_result["success"] and dn_result["stdout"]:
                m = dn_result["stdout"].split(":", 1)
                if len(m) > 1:
                    display_name = m[1].strip().strip("'\"")

            platforms = prof_state.get("platforms", {})
            plat_summary = {}
            for p, info in platforms.items():
                if info and info.get("state"):
                    plat_summary[p] = info["state"]

            agents.append({
                "name": prof_name,
                "display_name": display_name,
                "status": "running" if prof_state.get("gateway_state") == "running" else "stopped",
                "pid": str(prof_state.get("pid", "")) if prof_state.get("pid") else "",
                "platforms": plat_summary,
            })

    # Check PS for any running gateways not captured by state files
    ps_result = _ssh_cmd(server, "ps aux | grep 'hermes.*gateway' | grep -v grep | head -10", timeout=10)
    if ps_result["success"] and ps_result["stdout"]:
        running_pids = set()
        for line in ps_result["stdout"].split("\n"):
            line = line.strip()
            if not line: continue
            parts = line.split()
            if len(parts) >= 11:
                running_pids.add(parts[1])
        for agent in agents:
            if agent.get("pid") and agent["pid"] in running_pids:
                agent["status"] = "running"

    return agents


@router.post("/servers/{server_id}/status")
async def get_server_status(server_id: str):
    """Get full status of the remote server (connection, Hermes, Dashboard, Agents)."""
    servers = _load_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        raise HTTPException(404, f"Server {server_id} not found")

    connection = _test_connection(server)
    if not connection["connected"]:
        return {
            "connection": connection,
            "hermes": {"installed": False},
            "dashboard": {"running": False, "error": "Not connected"},
        }

    hermes = _check_hermes_installed(server)
    dashboard = _check_dashboard_status(server) if hermes["installed"] else {"running": False, "error": "Hermes not installed"}
    agents = _check_agent_profiles(server) if hermes["installed"] else []

    return {
        "connection": connection,
        "hermes": hermes,
        "dashboard": dashboard,
        "agents": agents,
    }


@router.post("/servers/{server_id}/dashboard/start")
async def start_dashboard(server_id: str, port: int = Query(8080)):
    """Start Hermes Dashboard on the remote server."""
    servers = _load_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        raise HTTPException(404, f"Server {server_id} not found")

    # Check if already running
    status = _check_dashboard_status(server)
    if status["running"]:
        return {
            "success": False,
            "message": "Dashboard is already running",
            "processes": status["processes"],
        }

    # Resolve hermes binary path on remote server
    hermes_home = server.get('hermes_home', '~/.hermes')
    # Common locations for hermes binary
    hermes_bin = "hermes"  # fallback
    # Use full path based on hermes_home or common patterns
    if hermes_home.startswith('/'):
        # Absolute path — derive binary location
        candidate = f"{hermes_home}/../.local/bin/hermes"
    else:
        candidate = f"~/.local/bin/hermes"

    # Start Dashboard in background
    # Run as the remote user (not root) since hermes is installed under that user
    remote_user = _get_remote_user(server)
    cmd = (
        f"export PATH=\"{candidate.rsplit('/', 1)[0]}:$PATH\" && "
        f"cd {hermes_home}/.. && "
        f"nohup hermes dashboard --port {port} --host 0.0.0.0 --insecure --no-open "
        f"> /home/{remote_user}/dashboard.log 2>&1 & echo 'DASHBOARD_STARTED'"
    )
    result = _run_remote_cmd(server, cmd, remote_user, timeout=15)

    if result["success"] and "DASHBOARD_STARTED" in result["stdout"]:
        # Wait a moment and verify
        time.sleep(3)
        verify = _check_dashboard_status(server)
        return {
            "success": verify["running"],
            "message": "Dashboard started" if verify["running"] else "Start command sent but verify failed",
            "dashboard": verify,
        }

    return {
        "success": False,
        "message": "Failed to start Dashboard",
        "ssh_output": result["stdout"][:500],
        "ssh_error": result["stderr"][:500],
    }


@router.post("/servers/{server_id}/dashboard/stop")
async def stop_dashboard(server_id: str):
    """Stop Hermes Dashboard on the remote server."""
    servers = _load_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        raise HTTPException(404, f"Server {server_id} not found")

    remote_user = _get_remote_user(server)

    # Kill all hermes dashboard processes
    stop_cmd = "pkill -f 'hermes.*dashboard' 2>/dev/null && echo DASHBOARD_STOPPED || echo NO_PROCESS_FOUND"
    result = _run_remote_cmd(server, stop_cmd, remote_user, timeout=15)

    time.sleep(2)
    verify = _check_dashboard_status(server)

    return {
        "success": not verify["running"],
        "message": "Dashboard stopped" if not verify["running"] else "Stop failed - process still running",
        "dashboard": verify,
    }


@router.post("/servers/{server_id}/dashboard/restart")
async def restart_dashboard(server_id: str, port: int = Query(8080)):
    """Restart Hermes Dashboard on the remote server."""
    servers = _load_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        raise HTTPException(404, f"Server {server_id} not found")

    # Kill first, then start
    stop_cmd = "pkill -f 'hermes.*dashboard' 2>/dev/null; echo 'KILLED'"
    _ssh_cmd(server, stop_cmd, timeout=10)
    time.sleep(2)

    # Resolve hermes binary path
    hermes_home_restart = server.get('hermes_home', '~/.hermes')
    remote_user = _get_remote_user(server)
    if hermes_home_restart.startswith('/'):
        candidate_r = f"{hermes_home_restart}/../.local/bin/hermes"
    else:
        candidate_r = f"~/.local/bin/hermes"

    start_cmd = (
        f"export PATH=\"{candidate_r.rsplit('/', 1)[0]}:$PATH\" && "
        f"cd {hermes_home_restart}/.. && "
        f"nohup hermes dashboard --port {port} --host 0.0.0.0 --insecure --no-open "
        f"> /home/{remote_user}/dashboard.log 2>&1 & echo 'DASHBOARD_STARTED'"
    )
    result = _run_remote_cmd(server, start_cmd, remote_user, timeout=15)

    time.sleep(3)
    verify = _check_dashboard_status(server)

    return {
        "success": verify["running"],
        "message": "Dashboard restarted" if verify["running"] else "Restart failed",
        "dashboard": verify,
    }


@router.post("/servers/{server_id}/dashboard/logs")
async def get_dashboard_logs(server_id: str, lines: int = Query(50)):
    """Get Dashboard log tail from the remote server."""
    servers = _load_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        raise HTTPException(404, f"Server {server_id} not found")

    result = _ssh_cmd(server, f"tail -{lines} ~/dashboard.log 2>/dev/null || echo 'NO_LOG'", timeout=15)

    return {
        "logs": result["stdout"] if "NO_LOG" not in result["stdout"] else "",
        "has_log": "NO_LOG" not in result["stdout"],
    }


@router.post("/servers/{server_id}/dashboard/deploy")
async def deploy_dashboard(server_id: str):
    """Install dependencies and deploy Dashboard on the remote server."""
    servers = _load_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        raise HTTPException(404, f"Server {server_id} not found")

    steps = []
    all_ok = True

    hh = server.get("hermes_home", "~/.hermes")
    remote_user = _get_remote_user(server)

    # Helper to wrap a command with smart user switch
    def _run_as_user(cmd: str, timeout: int = 30) -> dict:
        return _run_remote_cmd(server, cmd, remote_user, timeout=timeout)

    # Step 1: Install pip dependencies
    steps.append({"step": "安装 fastapi + uvicorn", "status": "running"})
    # Use python3 -m pip (the venv may lack pip binary but has pip module)
    venv_python = f"{hh}/hermes-agent/venv/bin/python3"
    dep_cmd = f"{venv_python} -m pip install uvicorn -q && echo DEPS_OK || echo DEPS_FAIL"
    r = _run_as_user(dep_cmd, timeout=60)
    dep_ok = "DEPS_OK" in r["stdout"]
    all_ok = all_ok and dep_ok
    steps[-1] = {"step": "安装 fastapi + uvicorn", "status": "ok" if dep_ok else "fail", "detail": r["stdout"][:200]}

    # Step 2: Build web UI
    steps.append({"step": "构建前端 (npm run build)", "status": "running"})
    # Extract remote user's home dir for HOME env (su - doesn't always set it correctly)
    remote_home = f"/home/{remote_user}"
    build_cmd = (
        f"cd {hh}/hermes-agent/web && "
        "rm -rf node_modules/.tmp 2>/dev/null && "
        f"chmod -R u+w {hh}/hermes_cli/web_dist 2>/dev/null; "
        f"rm -rf {hh}/hermes_cli/web_dist/assets 2>/dev/null; "
        f"HOME={remote_home} npm install --silent 2>/dev/null && "
        f"HOME={remote_home} npm run build 2>&1 && "
        "echo BUILD_OK || echo BUILD_FAIL"
    )
    r = _run_as_user(build_cmd, timeout=120)
    build_ok = "BUILD_OK" in r["stdout"] or "BUILD_OK" in r["stderr"]
    all_ok = all_ok and build_ok
    steps[-1] = {"step": "构建前端 (npm run build)", "status": "ok" if build_ok else "fail", "detail": r["stdout"][:200]}

    # Step 3: Start Dashboard via systemd (installs service if needed)
    if all_ok:
        steps.append({"step": "启动 Dashboard (port 8080)", "status": "running"})
        hermes_bin = f"{hh}/hermes-agent/venv/bin/hermes"
        # Create systemd service and start
        sysd_cmd = (
            f"cat > /tmp/hermes-dashboard.service << 'SERVICEOF'\\n"
            f"[Unit]\\n"
            f"Description=Hermix Dashboard\\n"
            f"After=network.target\\n"
            f"\\n"
            f"[Service]\\n"
            f"Type=simple\\n"
            f"User={remote_user}\\n"
            f"Environment=PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\\n"
            f"Environment=HERMES_WEB_DIST={hh}/hermes-agent/hermes_cli/web_dist\\n"
            f"WorkingDirectory={hh}/hermes-agent\\n"
            f"ExecStart={hermes_bin} dashboard --port 8080 --host 0.0.0.0 --insecure --no-open\\n"
            f"Restart=on-failure\\n"
            f"RestartSec=5\\n"
            f"\\n"
            f"[Install]\\n"
            f"WantedBy=multi-user.target\\n"
            f"SERVICEOF\\n"
            f"cp /tmp/hermes-dashboard.service /etc/systemd/system/ && "
            f"systemctl daemon-reload && "
            f"systemctl enable hermes-dashboard.service && "
            f"systemctl restart hermes-dashboard.service && "
            f"echo STARTED || echo FAILED"
        )
        r = _ssh_cmd(server, sysd_cmd, timeout=15)
        time.sleep(3)
        verify = _check_dashboard_status(server)
        start_ok = verify["running"]
        steps[-1] = {
            "step": "启动 Dashboard (port 8080)",
            "status": "ok" if start_ok else "fail",
            "detail": f"PID: {verify['processes'][0]['pid'] if verify.get('processes') else 'N/A'}" if start_ok else "启动失败",
        }
        all_ok = all_ok and start_ok

    return {
        "success": all_ok,
        "steps": steps,
    }


@router.get("/ssh-keys")
async def list_ssh_keys():
    """List saved SSH keys."""
    keys = []
    for f in SSH_KEY_DIR.glob("*.pem"):
        keys.append({
            "id": f.stem,
            "label": f.stem,
            "path": str(f),
            "size": f.stat().st_size,
        })
    return keys


@router.post("/ssh-keys")
async def upload_ssh_key(key_id: str = Query(...), label: str = Query(""), content: str = Query(...)):
    """Save an SSH private key."""
    safe_id = "".join(c for c in key_id if c.isalnum() or c in "_-")
    if not safe_id:
        raise HTTPException(400, "Invalid key ID")

    key_path = SSH_KEY_DIR / f"{safe_id}.pem"
    key_path.write_text(content)
    key_path.chmod(0o600)  # Secure permissions

    return {"success": True, "id": safe_id, "path": str(key_path)}


@router.delete("/ssh-keys/{key_id}")
async def delete_ssh_key(key_id: str):
    """Delete an SSH private key."""
    safe_id = "".join(c for c in key_id if c.isalnum() or c in "_-")
    key_path = SSH_KEY_DIR / f"{safe_id}.pem"
    if key_path.exists():
        key_path.unlink()
        return {"success": True}
    raise HTTPException(404, f"Key {key_id} not found")


@router.post("/test-connection")
async def test_connection_no_save(config: ServerConfig):
    """Test SSH connection to a server without saving it.
    
    Returns connection status, hostname, and Hermes installation info.
    Used by the 'Test Connection' button in the Add Server form.
    """
    server = config.model_dump()
    connection = _test_connection(server)
    hermes = _check_hermes_installed(server) if connection["connected"] else {"installed": False}
    return {
        "connected": connection["connected"],
        "hostname": connection.get("hostname", ""),
        "error": connection.get("error", ""),
        "hermes_installed": hermes.get("installed", False),
        "hermes_version": hermes.get("version", ""),
    }


@router.post("/deploy-new-server")
async def deploy_to_new_server(config: ServerConfig):
    """Full deployment: add server, test connection, install deps, build, start Dashboard."""
    server = config.model_dump()
    server["id"] = uuid.uuid4().hex[:12]
    server["created_at"] = _now()
    server["updated_at"] = _now()

    # 1. Test connection
    connection = _test_connection(server)
    if not connection["connected"]:
        return {"success": False, "step": "连接测试失败", "error": connection.get("error")}

    # 1b. Auto-detect correct hermes_home and remote_user
    #     (user may have entered wrong path like /home/ubuntu/.hermes when it's under root)
    hh = server.get("hermes_home", "~/.hermes")
    remote_user = _get_remote_user(server)
    probed = _probe_remote_hermes(server, hh, remote_user)
    if probed["found"]:
        if probed["hermes_home"] != hh:
            server["hermes_home"] = probed["hermes_home"]
            hh = probed["hermes_home"]
            logger.info(f"Auto-corrected hermes_home: {hh}")
        if probed["user"] != remote_user:
            server["username"] = probed["user"]
            remote_user = probed["user"]
            logger.info(f"Auto-corrected remote user: {remote_user}")

    # 2. Check Hermes installed
    hermes = _check_hermes_installed(server)
    if not hermes["installed"]:
        return {"success": False, "step": "Hermes未安装", "error": "目标服务器未检测到 Hermes Agent"}

    # 3. Install deps (run as remote user) — handle missing pip gracefully
    hh = server.get("hermes_home", "~/.hermes")
    remote_user = _get_remote_user(server)
    venv_python = f"{hh}/hermes-agent/venv/bin/python3"
    # Strategy: ensure pip is available, then install uvicorn
    dep_cmd = (
        f"\"{venv_python}\" -m pip --version >/dev/null 2>&1 || ( "
        f"  \"{venv_python}\" -m ensurepip --upgrade >/dev/null 2>&1 || "
        f"  apt-get install -y python3-pip python3-uvicorn python3-fastapi -qq >/dev/null 2>&1 "
        f") && "
        f"\"{venv_python}\" -m pip install uvicorn -q && "
        f"echo DEPS_OK || echo DEPS_FAIL"
    )
    r = _run_remote_cmd(server, dep_cmd, remote_user, timeout=120)
    if "DEPS_OK" not in r["stdout"]:
        return {"success": False, "step": "依赖安装失败", "error": r["stderr"][:300]}

    # 4. Build web UI (run as remote user)
    build_cmd = (
        f"cd {hh}/hermes-agent/web && "
        "npm install --silent 2>/dev/null && "
        "npm run build 2>&1 && "
        "echo BUILD_OK || echo BUILD_FAIL"
    )
    r = _run_remote_cmd(server, build_cmd, remote_user, timeout=120)
    if "BUILD_OK" not in r["stdout"] and "BUILD_OK" not in r["stderr"]:
        return {"success": False, "step": "前端构建失败", "error": r["stderr"][:300]}

    # 5. Start Dashboard as systemd service
    hermes_bin = f"{hh}/hermes-agent/venv/bin/hermes"
    sysd_cmd = (
        f"cat > /etc/systemd/system/hermes-dashboard.service << 'SERVICEOF'\n"
        f"[Unit]\n"
        f"Description=Hermix Dashboard\n"
        f"After=network.target\n"
        f"\n"
        f"[Service]\n"
        f"Type=simple\n"
        f"User={remote_user}\n"
        f"Environment=PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
        f"Environment=HERMES_WEB_DIST={hh}/hermes-agent/hermes_cli/web_dist\n"
        f"WorkingDirectory={hh}/hermes-agent\n"
        f"ExecStart={hermes_bin} dashboard --port 8080 --host 0.0.0.0 --insecure --no-open\n"
        f"Restart=on-failure\n"
        f"RestartSec=5\n"
        f"\n"
        f"[Install]\n"
        f"WantedBy=multi-user.target\n"
        f"SERVICEOF\n"
        f"systemctl daemon-reload && "
        f"systemctl enable hermes-dashboard.service && "
        f"systemctl restart hermes-dashboard.service && "
        f"echo STARTED || echo FAILED"
    )
    _ssh_cmd(server, sysd_cmd, timeout=15)
    time.sleep(3)
    verify = _check_dashboard_status(server)

    if not verify["running"]:
        return {"success": False, "step": "Dashboard启动失败"}

    # 6. Save server config (deduplicate by host)
    servers = _load_servers()
    existing = [s for s in servers if s.get("host") == server.get("host")]
    if existing:
        # Update existing entry instead of appending
        for i, s in enumerate(servers):
            if s.get("host") == server.get("host"):
                server["id"] = s["id"]  # Preserve original ID
                server["created_at"] = s.get("created_at", server.get("created_at", ""))
                servers[i] = server
                break
    else:
        servers.append(server)
    _save_servers(servers)

    return {
        "success": True,
        "server": server,
        "dashboard": verify,
        "hostname": connection.get("hostname"),
    }


# ============================================================
# Remote Agent Management — Add/Remove/Debug Bot Profiles
# ============================================================

@router.post("/servers/{server_id}/agents")
async def add_agent_to_server(server_id: str, params: dict):
    """
    Add a new Hermes Agent bot to the remote server.

    Flow:
    1. Create profile on remote server via SSH
    2. Configure .env with WECOM_BOT_ID and WECOM_SECRET
    3. Start gateway
    4. Return status for pairing-poll
    """
    servers = _load_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        raise HTTPException(404, f"Server {server_id} not found")

    machine_name = (params.get("machineName") or "").strip()
    role = (params.get("role") or "").strip()
    bot_id = (params.get("botId") or "").strip()
    secret = (params.get("secret") or "").strip()

    if not machine_name or not bot_id or not secret:
        raise HTTPException(400, "机器名称、BotId 和 Secret 为必填项")

    # Sanitize machine_name for profile
    safe_name = "".join(c for c in machine_name if c.isalnum() or c in "-_").lower()
    if not safe_name:
        raise HTTPException(400, f"无法从「{machine_name}」生成有效的 Profile 名称")

    # Test connection first
    connection = _test_connection(server)
    if not connection["connected"]:
        raise HTTPException(400, f"无法连接远程服务器: {connection.get('error', '未知错误')}")

    remote_user = _get_remote_user(server)
    hh = server.get("hermes_home", "~/.hermes")

    def _run(cmd: str, timeout: int = 30) -> dict:
        return _run_remote_cmd(server, cmd, remote_user, timeout=timeout)

    steps = []
    all_ok = True

    # Step 1: Check if profile already exists
    steps.append({"step": f"检查 Profile「{safe_name}」", "status": "running"})
    r = _run(f"test -d {hh}/profiles/{safe_name} && echo 'EXISTS' || echo 'NOT_EXISTS'")
    exists = "EXISTS" in r["stdout"]
    steps[-1] = {"step": f"检查 Profile「{safe_name}」", "status": "ok" if not exists else "fail",
                  "detail": "Profile 已存在" if exists else "OK"}
    if exists:
        return {"success": False, "steps": steps, "error": f"Profile「{safe_name}」在远程服务器上已存在"}

    # Step 2: Clone from a source profile (try default or existing one)
    steps.append({"step": "创建 Profile", "status": "running"})
    # First try cloning from default, otherwise create from scratch
    clone_cmd = f"cd {hh} && hermes profile create {safe_name} --clone --clone-from default --no-alias 2>&1 && echo 'CREATED' || echo 'CREATE_FAIL'"
    r = _run(clone_cmd, timeout=30)
    created = "CREATED" in r["stdout"]
    all_ok = all_ok and created
    steps[-1] = {"step": "创建 Profile", "status": "ok" if created else "fail",
                  "detail": r["stdout"][:200] if not created else "OK"}

    if not created:
        return {"success": False, "steps": steps, "error": "Profile 创建失败"}

    # Step 3: Configure .env with bot credentials
    steps.append({"step": "配置 Bot 凭证", "status": "running"})
    env_path = f"{hh}/profiles/{safe_name}/.env"
    env_content = (
        f"WECOM_BOT_ID={bot_id}\n"
        f"WECOM_SECRET={secret}\n"
    )
    import base64
    env_b64 = base64.b64encode(env_content.encode()).decode()
    env_cmd = f"echo '{env_b64}' | base64 -d > {env_path} && chmod 600 {env_path} && echo ENV_OK || echo ENV_FAIL"
    r = _run(env_cmd, timeout=10)
    env_ok = "ENV_OK" in r["stdout"]
    all_ok = all_ok and env_ok
    steps[-1] = {"step": "配置 Bot 凭证", "status": "ok" if env_ok else "fail",
                  "detail": r["stderr"][:200] if not env_ok else "OK"}
    if not env_ok:
        return {"success": False, "steps": steps, "error": "配置 .env 失败"}

    # Step 4: Set display_name in config.yaml
    steps.append({"step": "设置显示名称", "status": "running"})
    cfg_path = f"{hh}/profiles/{safe_name}/config.yaml"
    # Use Python to inject display_name into YAML
    py_cmd = (
        f"python3 -c \""
        f"import yaml; "
        f"c=yaml.safe_load(open('{cfg_path}')); "
        f"c['display_name']='{machine_name}'; "
        f"c['personality']='{role}' if '{role}' else None; "
        f"yaml.dump(c, open('{cfg_path}','w'), default_flow_style=False, allow_unicode=True, sort_keys=False); "
        f"print('CFG_OK')"
        f"\" 2>&1 || echo 'CFG_FAIL'"
    )
    r = _run(py_cmd, timeout=15)
    cfg_ok = "CFG_OK" in r["stdout"]
    all_ok = all_ok and cfg_ok
    steps[-1] = {"step": "设置显示名称", "status": "ok" if cfg_ok else "fail",
                  "detail": r["stderr"][:200] if not cfg_ok else f"→ {machine_name}"}

    # Step 5: Start gateway
    steps.append({"step": "启动 Gateway", "status": "running"})
    gw_cmd = f"hermes gateway run --replace --profile {safe_name} > /dev/null 2>&1 & echo 'GW_STARTED'"
    r = _run(gw_cmd, timeout=15)
    time.sleep(3)  # Wait for gateway to initialize
    gw_ok = "GW_STARTED" in r["stdout"]
    all_ok = all_ok and gw_ok
    steps[-1] = {"step": "启动 Gateway", "status": "ok" if gw_ok else "fail"}

    # Step 6: Check gateway state
    steps.append({"step": "验证运行状态", "status": "running"})
    gw_check = _run(f"cat {hh}/profiles/{safe_name}/gateway_state.json 2>/dev/null || echo '{{}}'")
    gw_state = {}
    try:
        gw_state = json.loads(gw_check["stdout"]) if gw_check["success"] else {}
    except Exception:
        pass
    is_running = gw_state.get("gateway_state") == "running"
    steps[-1] = {"step": "验证运行状态", "status": "ok" if is_running else "warn",
                  "detail": "Gateway 已运行" if is_running else "Gateway 可能还未完全就绪"}

    # Step 7: Get pairing info
    steps.append({"step": "等待配对…", "status": "running"})
    pairing_cmd = f"hermes pairing list --profile {safe_name} 2>/dev/null || echo 'NO_PAIRING_DATA'"
    r = _run(pairing_cmd, timeout=10)
    pairing_data = r["stdout"][:500] if r["stdout"] else ""
    steps[-1] = {"step": "等待配对…", "status": "ok",
                  "detail": "请用户向新机器人发送一条消息完成配对"}

    return {
        "success": all_ok,
        "steps": steps,
        "profile": safe_name,
        "display_name": machine_name,
        "gateway_running": is_running,
        "pairing_hint": "用户需向新机器人发送消息以触发配对码",
    }


@router.post("/servers/{server_id}/agents/{agent_name}/status")
async def get_remote_agent_status(server_id: str, agent_name: str):
    """Get detailed status of a specific agent on the remote server."""
    servers = _load_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        raise HTTPException(404, f"Server {server_id} not found")

    remote_user = _get_remote_user(server)
    hh = server.get("hermes_home", "~/.hermes")

    def _run(cmd, timeout=15):
        return _run_remote_cmd(server, cmd, remote_user, timeout=timeout)

    # Gateway state
    gw = _run(f"cat {hh}/profiles/{agent_name}/gateway_state.json 2>/dev/null || echo '{{}}'")
    gw_state = {}
    try:
        gw_state = json.loads(gw["stdout"]) if gw["success"] else {}
    except Exception:
        pass

    # Pairing info
    pairing = _run(f"hermes pairing list --profile {agent_name} 2>/dev/null || echo 'EMPTY_PAIRING'")

    # Config info
    cfg = _run(f"grep -E 'display_name|personality' {hh}/profiles/{agent_name}/config.yaml 2>/dev/null || true")

    return {
        "gateway_state": gw_state.get("gateway_state", "unknown"),
        "platforms": gw_state.get("platforms", {}),
        "pid": gw_state.get("pid"),
        "pairing_info": pairing["stdout"][:500],
        "config": cfg["stdout"][:300],
    }


@router.delete("/servers/{server_id}/agents/{agent_name}")
async def remove_agent_from_server(server_id: str, agent_name: str):
    """Remove (stop + delete) a Hermes Agent bot from the remote server."""
    servers = _load_servers()
    server = next((s for s in servers if s["id"] == server_id), None)
    if not server:
        raise HTTPException(404, f"Server {server_id} not found")

    remote_user = _get_remote_user(server)
    hh = server.get("hermes_home", "~/.hermes")

    def _run(cmd, timeout=15):
        return _run_remote_cmd(server, cmd, remote_user, timeout=timeout)

    steps = []

    # 1. Stop gateway
    steps.append({"step": "停止 Gateway", "status": "running"})
    r = _run(f"hermes gateway stop --profile {agent_name} 2>&1; pkill -f 'hermes.*gateway.*{agent_name}' 2>/dev/null; echo 'STOPPED'")
    steps[-1] = {"step": "停止 Gateway", "status": "ok"}

    # 2. Verify stopped
    time.sleep(2)
    r = _run(f"ps aux | grep 'hermes.*gateway.*{agent_name}' | grep -v grep || echo 'NOT_RUNNING'")
    stopped = "NOT_RUNNING" in r["stdout"]
    if not stopped:
        # Force kill
        _run(f"pkill -9 -f 'hermes.*gateway.*{agent_name}' 2>/dev/null || true")

    # 3. Delete profile
    steps.append({"step": "删除 Profile", "status": "running"})
    r = _run(f"rm -rf {hh}/profiles/{agent_name} && echo 'DELETED' || echo 'DELETE_FAIL'")
    deleted = "DELETED" in r["stdout"]
    steps[-1] = {"step": "删除 Profile", "status": "ok" if deleted else "fail",
                  "detail": r["stderr"][:200] if not deleted else f"已移除 {agent_name}"}

    return {"success": deleted, "steps": steps}
