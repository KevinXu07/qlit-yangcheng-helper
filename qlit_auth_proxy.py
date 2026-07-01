"""
qlit_auth_proxy.py —— 系统代理 / CA 证书 / 进程检测的跨平台实现

所有 GUI / 抓包主流程只通过本模块的函数访问平台能力。
每个函数内部按 sys.platform 分支到 macOS 或 Windows 实现。

公开 API：
    find_mitmdump() -> str
    is_ca_trusted() -> bool
    install_ca_cert() -> bool
    is_wechat_running() -> bool
    snapshot_proxy() -> dict | None
    enable_proxy(port: int, snap: dict | None) -> list[str]
    restore_proxy(snap: dict | None, port: int) -> None
    active_proxy_label() -> str
"""

import os
import hashlib
import shlex
import shutil
import socket
import subprocess
import sys
import ssl
from pathlib import Path


CA_CERT = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
_LAST_CA_INSTALL_ERROR = ""
MAC_SECURITY = "/usr/bin/security"
MAC_OSASCRIPT = "/usr/bin/osascript"
MAC_NETWORKSETUP = "/usr/sbin/networksetup"


def last_ca_install_error() -> str:
    return _LAST_CA_INSTALL_ERROR


def _set_ca_install_error(message: str) -> None:
    global _LAST_CA_INSTALL_ERROR
    _LAST_CA_INSTALL_ERROR = message.strip()


# ──────────────────────── 找 mitmdump ────────────────────────

def find_mitmdump() -> str:
    """查找 mitmdump 完整路径，找不到返回 ''。
    优先级：env MITMDUMP_PATH > 打包内 > 系统 PATH > 常见安装路径。"""
    env = os.environ.get("MITMDUMP_PATH")
    if env and Path(env).exists():
        return env

    # 打包内优先，避免别人电脑上装过旧版 mitmproxy 时绕开随包签好的 runtime。
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if sys.platform == "win32":
            for cand in [exe_dir / "mitmdump.exe", exe_dir / "_internal" / "mitmdump.exe"]:
                if cand.exists():
                    return str(cand)
        else:
            for cand in [
                exe_dir.parent / "Resources" / "mitmproxy.app" / "Contents" / "MacOS" / "mitmdump",
                exe_dir / "mitmproxy.app" / "Contents" / "MacOS" / "mitmdump",
            ]:
                if cand.exists():
                    return str(cand)

    which = shutil.which("mitmdump")
    if which:
        return which

    candidates: list[Path] = []
    if sys.platform == "win32":
        candidates += [
            Path(r"C:\Program Files\mitmproxy\bin\mitmdump.exe"),
            Path(r"C:\Program Files (x86)\mitmproxy\bin\mitmdump.exe"),
        ]
    else:
        candidates += [
            Path("/opt/homebrew/bin/mitmdump"),
            Path("/usr/local/bin/mitmdump"),
        ]

    for p in candidates:
        if p.exists():
            return str(p)

    return ""


# ──────────────────────── CA 证书 ────────────────────────

def is_ca_trusted() -> bool:
    if not CA_CERT.exists():
        return False
    if sys.platform == "win32":
        # Win 证书存储里查 "Mitmproxy" 字样
        try:
            out = subprocess.check_output(
                ["certutil", "-store", "Root"], text=True, stderr=subprocess.DEVNULL
            )
        except Exception:
            return False
        return "mitmproxy" in out.lower()
    # macOS：先确认这张 mitmproxy CA 已经进了用户或系统钥匙串，再确认 SSL 信任。
    cert_sha1 = _mac_cert_sha1(CA_CERT)
    if not cert_sha1:
        return False
    return _mac_keychain_contains_cert(cert_sha1) and _mac_cert_is_ssl_trusted(CA_CERT)


def _mac_cert_sha1(cert: Path) -> str:
    try:
        der = ssl.PEM_cert_to_DER_cert(cert.read_text(encoding="utf-8"))
        return hashlib.sha1(der).hexdigest().upper()
    except Exception:
        return ""


def _mac_keychain_contains_cert(cert_sha1: str) -> bool:
    keychains = [
        Path.home() / "Library" / "Keychains" / "login.keychain-db",
        Path("/Library/Keychains/System.keychain"),
    ]
    for keychain in keychains:
        if not keychain.exists():
            continue
        try:
            out = subprocess.check_output(
                [MAC_SECURITY, "find-certificate", "-a", "-Z", "-c", "mitmproxy", str(keychain)],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            continue
        normalized = out.replace(":", "").upper()
        if cert_sha1 in normalized:
            return True
    return False


def _mac_cert_is_ssl_trusted(cert: Path) -> bool:
    try:
        r = subprocess.run(
            [MAC_SECURITY, "verify-cert", "-c", str(cert), "-p", "ssl"],
            capture_output=True, text=True,
        )
    except Exception:
        return False
    text = (r.stdout or "") + (r.stderr or "")
    return "Cert Verify Result: No error" in text


def _mac_run_cert_install(cmd: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        return False, str(e)
    if r.returncode == 0:
        return True, ""
    detail = (r.stderr or r.stdout or "").strip()
    return False, detail or f"exit {r.returncode}"


def _applescript_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def install_ca_cert() -> bool:
    """信任 mitmproxy CA。返回是否成功。"""
    _set_ca_install_error("")
    if not CA_CERT.exists():
        _set_ca_install_error(f"CA 证书不存在：{CA_CERT}")
        return False
    if sys.platform == "win32":
        # certutil -addstore -f "Root" <pem> 不需管理员
        r = subprocess.run(
            ["certutil", "-addstore", "-f", "Root", str(CA_CERT)],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            _set_ca_install_error((r.stderr or r.stdout or "").strip())
        return r.returncode == 0

    # macOS：优先写当前用户钥匙串，不需要管理员密码，别人的电脑上更稳定。
    login_keychain = Path.home() / "Library" / "Keychains" / "login.keychain-db"
    ok, err = _mac_run_cert_install([
        MAC_SECURITY, "add-trusted-cert", "-p", "ssl", "-p", "basic", "-r", "trustRoot",
        "-k", str(login_keychain), str(CA_CERT),
    ])
    if ok:
        return True
    first_error = err

    # 兜底写系统钥匙串，需要管理员权限。GUI app 没 TTY，走 AppleScript 原生授权框。
    shell_cmd = " ".join([
        MAC_SECURITY, "add-trusted-cert", "-d", "-p", "ssl", "-p", "basic", "-r", "trustRoot",
        "-k", "/Library/Keychains/System.keychain", shlex.quote(str(CA_CERT)),
    ])
    script = f"do shell script {_applescript_string(shell_cmd)} with administrator privileges"
    ok, err = _mac_run_cert_install([MAC_OSASCRIPT, "-e", script])
    if ok:
        return True

    if is_ca_trusted():
        return True
    errors = [e for e in (first_error, err) if e]
    _set_ca_install_error("；".join(errors) or "未知错误")
    return False


# ──────────────────────── 微信检测 ────────────────────────

def is_wechat_running() -> bool:
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq WeChat.exe"],
                text=True, stderr=subprocess.DEVNULL
            )
        except Exception:
            return False
        return "WeChat.exe" in out
    # macOS
    for pattern in ["WeChat.app/Contents/MacOS/WeChat",
                    "WeChat.app/Contents/MacOS/WeChatAppEx"]:
        r = subprocess.run(["pgrep", "-f", pattern], capture_output=True)
        if r.returncode == 0:
            return True
    return False


# ──────────────────────── 活动网络服务（仅 macOS 用得到） ────────────────────────

def _mac_run_command(args: list[str], allow_admin: bool = False) -> tuple[bool, str, str]:
    try:
        r = subprocess.run(args, check=False, capture_output=True, text=True)
    except Exception as e:
        return False, "", str(e)
    if r.returncode == 0:
        return True, r.stdout or "", ""
    detail = (r.stderr or r.stdout or "").strip()
    if not allow_admin:
        return False, "", detail or f"exit {r.returncode}"

    shell_cmd = " ".join(shlex.quote(str(part)) for part in args)
    script = f"do shell script {_applescript_string(shell_cmd)} with administrator privileges"
    try:
        r = subprocess.run([MAC_OSASCRIPT, "-e", script], check=False, capture_output=True, text=True)
    except Exception as e:
        return False, "", str(e)
    if r.returncode == 0:
        return True, r.stdout or "", ""
    detail = (r.stderr or r.stdout or "").strip()
    return False, "", detail or f"exit {r.returncode}"


def _mac_network_services(allow_admin: bool = False) -> list[str]:
    """macOS 上所有启用的网络服务名（Wi-Fi / Ethernet / USB / VPN ...）。"""
    ok, out, _ = _mac_run_command(
        [MAC_NETWORKSETUP, "-listallnetworkservices"],
        allow_admin=allow_admin,
    )
    if not ok:
        return []
    services: list[str] = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        services.append(line)
    return services


def _active_network_service(allow_admin: bool = False) -> str | None:
    """日志展示用：优先挑常见主网卡名。代理设置会覆盖所有启用服务。"""
    preferred = ("Wi-Fi", "Ethernet", "USB 10/100/1000 LAN")
    services = _mac_network_services(allow_admin=allow_admin)
    for name in preferred:
        if name in services:
            return name
    return services[0] if services else None


# ──────────────────────── macOS：networksetup ────────────────────────

def _mac_read_proxy_state(service: str, secure: bool, allow_admin: bool = False) -> dict:
    cmd = "-getsecurewebproxy" if secure else "-getwebproxy"
    state = {"enabled": False, "server": "", "port": ""}
    ok, out, _ = _mac_run_command(
        [MAC_NETWORKSETUP, cmd, service],
        allow_admin=allow_admin,
    )
    if not ok:
        return state
    for line in out.splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "enabled":
            state["enabled"] = value.lower() == "yes"
        elif key == "server":
            state["server"] = value
        elif key == "port":
            state["port"] = value
    return state


def _mac_snapshot_proxy() -> dict | None:
    services = _mac_network_services(allow_admin=True)
    if not services:
        return None
    return {
        "services": {
            service: {
                "web": _mac_read_proxy_state(service, secure=False, allow_admin=True),
                "secure": _mac_read_proxy_state(service, secure=True, allow_admin=True),
            }
            for service in services
        },
    }


def _mac_run_networksetup(args: list[str], allow_admin: bool = False) -> tuple[bool, str]:
    ok, _, err = _mac_run_command(args, allow_admin=allow_admin)
    return ok, err


def _mac_proxy_enabled_for(service: str, port: int) -> bool:
    expected_port = str(port)
    for secure in (False, True):
        state = _mac_read_proxy_state(service, secure=secure, allow_admin=True)
        if not state.get("enabled"):
            return False
        if state.get("server") != "127.0.0.1":
            return False
        if str(state.get("port")) != expected_port:
            return False
    return True


def _mac_enable_proxy(port: int) -> list[str]:
    services = _mac_network_services(allow_admin=True)
    if not services:
        raise RuntimeError("未找到启用的 macOS 网络服务，无法设置系统代理")
    host = "127.0.0.1"
    enabled: list[str] = []
    errors: list[str] = []
    for service in services:
        service_errors: list[str] = []
        for args in (
            [MAC_NETWORKSETUP, "-setwebproxy", service, host, str(port)],
            [MAC_NETWORKSETUP, "-setsecurewebproxy", service, host, str(port)],
            [MAC_NETWORKSETUP, "-setwebproxystate", service, "on"],
            [MAC_NETWORKSETUP, "-setsecurewebproxystate", service, "on"],
        ):
            ok, err = _mac_run_networksetup(args, allow_admin=True)
            if not ok:
                service_errors.append(err)
        if not service_errors and _mac_proxy_enabled_for(service, port):
            enabled.append(service)
        else:
            errors.append(f"{service}: {'；'.join(service_errors) or '写入后读回校验失败'}")
    if not enabled:
        raise RuntimeError("系统代理开启失败：" + "；".join(errors))
    return enabled


def _mac_restore_one(service: str, state: dict, port: int, secure: bool) -> None:
    set_cmd = "-setsecurewebproxy" if secure else "-setwebproxy"
    state_cmd = "-setsecurewebproxystate" if secure else "-setwebproxystate"
    server = state.get("server") or "127.0.0.1"
    port = state.get("port") or str(port)
    _mac_run_networksetup(
        [MAC_NETWORKSETUP, set_cmd, service, server, str(port)],
        allow_admin=True,
    )
    _mac_run_networksetup(
        [MAC_NETWORKSETUP, state_cmd, service, "on" if state.get("enabled") else "off"],
        allow_admin=True,
    )


def _mac_restore_proxy(snap: dict | None, port: int) -> None:
    if not snap:
        for service in _mac_network_services(allow_admin=True):
            for args in (
                [MAC_NETWORKSETUP, "-setwebproxystate", service, "off"],
                [MAC_NETWORKSETUP, "-setsecurewebproxystate", service, "off"],
            ):
                _mac_run_networksetup(args, allow_admin=True)
        return
    if "services" in snap:
        for service, state in snap["services"].items():
            _mac_restore_one(service, state.get("web", {}), port=port, secure=False)
            _mac_restore_one(service, state.get("secure", {}), port=port, secure=True)
        return
    service = snap["service"]
    _mac_restore_one(service, snap.get("web", {}), port=port, secure=False)
    _mac_restore_one(service, snap.get("secure", {}), port=port, secure=True)


# ──────────────────────── Windows：netsh winhttp ────────────────────────

def _win_snapshot_proxy() -> dict | None:
    """返回当前 WinHTTP 代理状态。"""
    try:
        out = subprocess.check_output(
            ["netsh", "winhttp", "show", "proxy"], text=True,
            stderr=subprocess.DEVNULL
        )
    except Exception:
        return None
    snap = {"raw": out.strip(), "enabled": False, "server": "", "bypass": ""}
    for line in out.splitlines():
        s = line.strip()
        if s.lower().startswith("proxy server(s)"):
            # "Proxy Server(s) : 127.0.0.1:18888" 或 "直接访问(Direct access)"
            value = s.split(":", 1)[-1].strip()
            if value and "直接" not in value and "Direct" not in value:
                snap["enabled"] = True
                snap["server"] = value
        elif s.lower().startswith("bypass list"):
            snap["bypass"] = s.split(":", 1)[-1].strip()
    return snap


def _win_enable_proxy(port: int) -> None:
    subprocess.run(
        ["netsh", "winhttp", "set", "proxy", f"127.0.0.1:{port}"],
        check=False, capture_output=True
    )


def _win_restore_proxy(snap: dict | None, port: int) -> None:
    """根据快照恢复，绝不盲目 reset（否则会把用户原本的代理清空）。

    - snap 有且 enabled=True：用 snap.server + snap.bypass 还原
    - snap 无 / enabled=False：reset（最干净的"关闭"状态）
    """
    if snap and snap.get("enabled") and snap.get("server"):
        server = snap["server"]
        cmd = ["netsh", "winhttp", "set", "proxy", server]
        if snap.get("bypass") and snap["bypass"] != "<local>":
            # bypass 列表原样回写
            cmd += ["bypass-list=" + snap["bypass"]]
        subprocess.run(cmd, check=False, capture_output=True)
        return
    # 没拍到 / 原本就没代理：reset 到直连
    subprocess.run(
        ["netsh", "winhttp", "reset", "proxy"],
        check=False, capture_output=True
    )


# ──────────────────────── 公开 API（路由到平台实现） ────────────────────────

def snapshot_proxy() -> dict | None:
    if sys.platform == "win32":
        return _win_snapshot_proxy()
    return _mac_snapshot_proxy()


def enable_proxy(port: int, snap: dict | None) -> list[str]:
    if sys.platform == "win32":
        _win_enable_proxy(port)
        return ["WinHTTP"]
    return _mac_enable_proxy(port)


def restore_proxy(snap: dict | None, port: int) -> None:
    if sys.platform == "win32":
        _win_restore_proxy(snap, port)
    else:
        _mac_restore_proxy(snap, port)


def active_proxy_label() -> str:
    """给日志显示用。"""
    if sys.platform == "win32":
        return "WinHTTP"
    svc = _active_network_service() or "默认网络"
    return f"networksetup:{svc}"


# ──────────────────────── 端口选择 ────────────────────────

def choose_listen_port(preferred: int, host: str = "127.0.0.1", span: int = 20) -> int:
    """从 preferred 开始，尝试 span 个端口找一个可绑的。"""
    for port in [preferred] + list(range(preferred + 1, preferred + 1 + span)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"找不到可用代理端口（从 {preferred} 开始尝试了 {span} 个端口）")
