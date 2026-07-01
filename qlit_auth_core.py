"""
qlit_auth_core.py —— 校园系统 OAuth 凭据抓取（主流程）

与 qlit-cli/qlit_auth.py 公开 API 保持兼容：
    find_mitmdump(), capture_session(on_log, should_stop, timeout), CaptureError
    常量：LISTEN_HOST / LISTEN_PORT / CA_CERT / QLIT_HOST / OAUTH_CALLBACK_PATH / ADMIN_PATH / RESULT_FILE

所有平台相关调用（系统代理 / CA 证书 / 微信检测 / 找 mitmdump）都路由到 qlit_auth_proxy。
"""

import os
import re
import sys
import time
import tempfile
import subprocess
from pathlib import Path
from typing import Callable, Optional

from qlit_auth_proxy import (
    find_mitmdump as _proxy_find_mitmdump,
    is_ca_trusted as _proxy_is_ca_trusted,
    install_ca_cert as _proxy_install_ca_cert,
    last_ca_install_error as _proxy_last_ca_install_error,
    snapshot_proxy as _proxy_snapshot_proxy,
    enable_proxy as _proxy_enable_proxy,
    restore_proxy as _proxy_restore_proxy,
    choose_listen_port,
    CA_CERT as _PROXY_CA_CERT,
)


# ──────────────────────── 公开常量（与 qlit_cli 兼容） ────────────────────────

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18888
# CA 证书路径：mitmproxy 跨平台默认都写 ~/.mitmproxy/mitmproxy-ca-cert.pem
CA_CERT = _PROXY_CA_CERT

QLIT_HOST = "pass.qlit.edu.cn"
OAUTH_CALLBACK_PATH = "/weChat/wxMobile/zaixiaosheng.htm"
ADMIN_PATH = "/student/mobile/admin.jsp"

RESULT_FILE = Path(tempfile.gettempdir()) / "qlit_auth_result.txt"
APP_DIR = Path.home() / ".campus_auth"
SESSION_FILE = APP_DIR / "session.txt"
OLD_SESSION_FILE = Path(__file__).resolve().parent / "session.txt"


# ──────────────────────── 公开异常 ────────────────────────

class CaptureError(Exception):
    pass


# ──────────────────────── 公开 API ────────────────────────

def find_mitmdump() -> str:
    """透传到 proxy 层。"""
    return _proxy_find_mitmdump()


# ──────────────────────── addon 脚本（mitmdump -s 加载） ────────────────────────

def _write_addon_script(result_file: str) -> str:
    """把 mitmproxy addon 脚本写到临时文件，返回路径。
    为什么不用 __file__：PyInstaller / Flet pack 打包后 __file__ 指向虚拟路径，
    mitmdump -s 加载不到真实文件。"""
    script = '''
import re, os
from urllib.parse import urlparse, parse_qs

RESULT_FILE = os.environ.get("QLIT_RESULT_FILE", "")
QLIT_HOST = "pass.qlit.edu.cn"
OAUTH_CALLBACK_PATH = "/weChat/wxMobile/zaixiaosheng.htm"
ADMIN_PATH = "/student/mobile/admin.jsp"


class QlitCapture:
    def response(self, flow):
        try:
            url = urlparse(flow.request.pretty_url)
        except Exception:
            return
        if url.hostname != QLIT_HOST:
            return
        if ADMIN_PATH in url.path:
            cookie = flow.request.headers.get("Cookie", "")
            m = re.search(r"JSESSIONID=([^;\\s]+)", cookie)
            if m:
                _write("JSESSIONID=" + m.group(1))
                print("[mitmproxy] 抓到 student JSESSIONID：" + m.group(1)[:20] + "...", flush=True)
            return
        if OAUTH_CALLBACK_PATH in url.path:
            code = parse_qs(url.query).get("code", [None])[0]
            if code:
                _write("CODE=" + code)
                print("[mitmproxy] 抓到 OAuth code：" + code[:16] + "...", flush=True)


def _write(content):
    try:
        with open(RESULT_FILE, "w") as f:
            f.write(content + "\\n")
    except Exception as e:
        print("[mitmproxy] 写结果失败：" + str(e), flush=True)


addons = [QlitCapture()]
'''
    fd, path = tempfile.mkstemp(suffix=".py", prefix="qlit_addon_")
    with os.fdopen(fd, "w") as f:
        f.write(script)
    return path


# ──────────────────────── 内部：bootstrap CA ────────────────────────

def _bootstrap_ca(mitmdump: str) -> None:
    """首次运行：启动一次 mitmdump 让它生成 ~/.mitmproxy/ 证书。"""
    p = subprocess.Popen(
        [mitmdump, "--listen-host", "127.0.0.1", "-p", str(LISTEN_PORT + 1), "-q"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(3)
    p.terminate()
    try:
        p.wait(timeout=3)
    except subprocess.TimeoutExpired:
        p.kill()


# ──────────────────────── 内部：读结果文件 ────────────────────────

def _read_result_jsession() -> Optional[str]:
    if not RESULT_FILE.exists():
        return None
    jsession = None
    try:
        for line in RESULT_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("JSESSIONID="):
                jsession = line.split("=", 1)[1]
    except Exception:
        pass
    return jsession


# ──────────────────────── 公开 API：抓取主流程 ────────────────────────

def capture_session(
    on_log: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    timeout: int = 300,
) -> str:
    """
    抓取 student JSESSIONID。阻塞调用，GUI 里应在后台线程跑。
    返回 JSESSIONID 字符串，抛 CaptureError。
    """
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)
        else:
            print(msg, flush=True)

    def stopped() -> bool:
        return should_stop() if should_stop else False

    # 1. mitmdump
    mitmdump = find_mitmdump()
    if not mitmdump:
        raise CaptureError("未找到 mitmdump。请先安装：\n"
                           "  macOS:   brew install --cask mitmproxy\n"
                           "  Windows: winget install mitmproxy")
    log(f"✓ mitmproxy 就绪：{mitmdump}")

    # 2. CA 证书
    if not CA_CERT.exists():
        log("首次运行，生成 mitmproxy CA 证书...")
        _bootstrap_ca(mitmdump)
    if not _proxy_is_ca_trusted():
        log("需要信任 mitmproxy CA 证书（优先安装到当前用户钥匙串，必要时才弹管理员密码框）...")
        if not _proxy_install_ca_cert():
            hint = ("macOS:   security add-trusted-cert -r trustRoot "
                    "-k ~/Library/Keychains/login.keychain-db " + str(CA_CERT) + "\n"
                    "         或 sudo security add-trusted-cert -d -r trustRoot "
                    "-k /Library/Keychains/System.keychain " + str(CA_CERT))
            if sys.platform == "win32":
                hint = f"Windows: certutil -addstore -f Root \"{CA_CERT}\""
            detail = _proxy_last_ca_install_error()
            if detail:
                log(f"CA 证书安装失败详情：{detail}")
            raise CaptureError("CA 证书信任失败。请手动执行：\n  " + hint)
    log("✓ CA 证书已信任")

    # 3. 代理
    snap = _proxy_snapshot_proxy()
    listen_port = choose_listen_port(LISTEN_PORT)
    if listen_port != LISTEN_PORT:
        log(f"⚠ 端口 {LISTEN_PORT} 已占用，自动切换到 {listen_port}")

    # 4. 写 addon 脚本
    RESULT_FILE.unlink(missing_ok=True)
    addon_path = _write_addon_script(str(RESULT_FILE))
    env = dict(os.environ, QLIT_RESULT_FILE=str(RESULT_FILE))

    # 5. 启动 mitmdump
    log("启动 mitmdump ...")
    try:
        mitm_proc = subprocess.Popen(
            [mitmdump, "--listen-host", LISTEN_HOST, "-p", str(listen_port),
             "-s", addon_path, "-q"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=env,
        )
    except Exception as e:
        os.unlink(addon_path)
        raise CaptureError(f"mitmdump 启动异常：{e}")

    time.sleep(2)
    if mitm_proc.poll() is not None:
        err = ""
        try:
            err = mitm_proc.stderr.read().decode()[:300]
        except Exception:
            pass
        os.unlink(addon_path)
        raise CaptureError(f"mitmdump 启动失败：{err}")
    log(f"✓ mitmdump 监听 {LISTEN_HOST}:{listen_port}")

    proxy_on = False
    try:
        enabled_services = _proxy_enable_proxy(listen_port, snap)
        proxy_on = True
        if enabled_services:
            log("✓ 系统代理已开启：" + "、".join(enabled_services))
        else:
            log("✓ 系统代理已开启")

        log("")
        log("⚠️  请微信打开「齐鲁理工微服务-在校生服务平台」并等待完成登录")
        log("")
        log(f">>> 等待抓取凭据中...（最多 {timeout} 秒）")

        deadline = time.time() + timeout
        while time.time() < deadline:
            if stopped():
                raise CaptureError("已取消")
            if mitm_proc.poll() is not None:
                err = ""
                try:
                    err = mitm_proc.stderr.read().decode()[:200]
                except Exception:
                    pass
                raise CaptureError(f"mitmdump 意外退出：{err}")
            jsessionid = _read_result_jsession()
            if jsessionid:
                log(f"✓ 抓到 JSESSIONID：{jsessionid[:24]}...")
                return jsessionid
            time.sleep(1)

        raise CaptureError("超时未捕获。请确认已在微信「齐鲁理工微服务-在校生服务平台」完成登录。")

    finally:
        if proxy_on:
            _proxy_restore_proxy(snap, listen_port)
            log("✓ 系统代理已恢复")
        mitm_proc.terminate()
        try:
            mitm_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            mitm_proc.kill()
        RESULT_FILE.unlink(missing_ok=True)
        try:
            os.unlink(addon_path)
        except Exception:
            pass


# ──────────────────────── 命令行入口 ────────────────────────

def main() -> int:
    print("=" * 60)
    print("  qlit_auth —— 校园系统 OAuth 凭据抓取")
    print("=" * 60)
    try:
        jsessionid = capture_session(
            on_log=lambda m: print(m, flush=True),
            timeout=300,
        )
        print()
        print("=" * 60)
        print("✓ 成功！student 域 JSESSIONID：")
        print(f"  {jsessionid}")
        print("=" * 60)
        APP_DIR.mkdir(parents=True, exist_ok=True)
        out = SESSION_FILE
        out.write_text(jsessionid + "\n", encoding="utf-8")
        print(f"已保存到 {out.resolve()}")
        if OLD_SESSION_FILE.exists() and OLD_SESSION_FILE != out:
            print(f"提示：仓库内旧的 {OLD_SESSION_FILE.name} 已不再作为默认存储路径。")
        return 0
    except (CaptureError, KeyboardInterrupt) as e:
        print(f"\n✗ {e}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
