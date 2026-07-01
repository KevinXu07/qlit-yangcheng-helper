#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
session_utils.py —— session 有效性检测

GUI 用：定时检测 session 是否过期，显示状态。
命令行：python3 session_utils.py [JSESSIONID]

判定逻辑（探活 admin.jsp，不跟随重定向，看 Location 头）：
- 状态码 3xx 且 Location 含 login/oauth2/zaixiaosheng 关键字 → 已过期
- 状态码 401/403 → 已过期
- 其他 → 有效
"""

from pathlib import Path
from typing import Tuple

import requests
import urllib3

urllib3.disable_warnings()

PROBE_URL = "https://pass.qlit.edu.cn/student/mobile/admin.jsp"
UA = (
    "Mozilla/5.0 (Linux; Android 16; RMX5010 Build/BP2A.250605.015; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/146.0.7680.177 "
    "Mobile Safari/537.36 XWEB/1460055 MMWEBSDK/20260201 MicroMessenger/8.0.69.3040 "
    "WeChat/arm64 Language/zh_CN"
)
EXPIRED_URL_KEYWORDS = ["login", "oauth2/authorize", "zaixiaosheng.htm", "110.baidu"]
APP_DIR = Path.home() / ".campus_auth"
SESSION_FILE = APP_DIR / "session.txt"
OLD_SESSION_FILE = Path(__file__).resolve().parent / "session.txt"


def load_saved_session() -> str:
    for path in (SESSION_FILE, OLD_SESSION_FILE):
        if path.exists():
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
    return ""


def check_session(jsessionid: str) -> Tuple[bool, str]:
    """
    检测 session 是否有效。返回 (有效?, 说明)。
    用 admin.jsp 探活，不跟随重定向。
    """
    if not jsessionid:
        return False, "无 session"
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": f"JSESSIONID={jsessionid}",
    }
    try:
        r = requests.get(PROBE_URL, headers=headers, timeout=15,
                         verify=False, allow_redirects=False)
    except requests.RequestException as e:
        return False, f"网络错误：{e}"

    location = r.headers.get("Location", "")
    final_url = r.url
    for url in (location, final_url):
        for kw in EXPIRED_URL_KEYWORDS:
            if kw in url:
                return False, "已过期（重定向到登录页）"
    if r.status_code in (401, 403):
        return False, f"已过期（HTTP {r.status_code}）"
    # 反向：服务器对任意"无效/过期 session"有时会返 200 + "call 110" 的桩响应
    # （不是登录页也不是业务内容），加这层兜底防止误判
    body = r.text[:2000].lower()
    if "call 110" in body:
        return False, "已过期（服务器返回 call 110）"
    return True, "有效"


def main():
    import sys
    jsessionid = sys.argv[1] if len(sys.argv) > 1 else load_saved_session()
    if not jsessionid:
        print("用法：python3 session_utils.py [JSESSIONID]")
        print(f"默认读取：{SESSION_FILE}")
        return 1
    valid, detail = check_session(jsessionid)
    print(f"{'✓ 有效' if valid else '✗ 过期'}：{detail}")
    print(f"JSESSIONID：{jsessionid}")
    return 0 if valid else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
