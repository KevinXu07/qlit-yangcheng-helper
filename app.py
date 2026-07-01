"""
app.py —— QLIT养成教育助手（Flet 版，跨平台 GUI）

与 customtkinter 版（qlit-cli/gui.py）UI 1:1 等价，但用 Flet 异步事件循环替代
threading + queue + self.after 的多套机制。

业务模块：
    qlit_auth_core —— 抓包（mitmproxy）
    yangcheng_auto —— 鉴权链 + AI 生成 + 提交/覆盖
    session_utils  —— session 有效性检测

settings.json 路径与键 schema 100% 兼容 qlit-cli/。
"""

import asyncio
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from datetime import date, timedelta
from pathlib import Path


APP_NAME = "QLIT养成教育助手"


def _find_app_bundle(root: Path) -> Path | None:
    try:
        for child in sorted(root.iterdir()):
            if child.is_dir() and child.suffix == ".app":
                return child
    except Exception:
        return None
    return None


def _embedded_flet_view_candidates() -> list[Path]:
    """打包后可能存在的 Flet.app 运行时目录。"""
    exe = Path(sys.executable).resolve()
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "flet_desktop" / "app")
    # PyInstaller macOS .app: <App>.app/Contents/MacOS/<exe>
    if len(exe.parents) > 1:
        candidates.append(exe.parents[1] / "Resources" / "flet_desktop" / "app")
    # PyInstaller onedir: dist/<name>/<exe> + _internal/flet_desktop/app
    candidates.append(exe.parent / "_internal" / "flet_desktop" / "app")
    candidates.append(exe.parent / "flet_desktop" / "app")
    return candidates


def _safe_extract_tar(archive: Path, target: Path) -> None:
    target_resolved = target.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            name = Path(member.name).name
            if name.startswith("._") or name == ".DS_Store":
                continue
            member_path = (target / member.name).resolve()
            if target_resolved != member_path and target_resolved not in member_path.parents:
                raise RuntimeError(f"不安全的 Flet 运行时路径: {member.name}")
            tar.extract(member, target)


def _remove_macos_metadata_files(root: Path) -> None:
    for pattern in ("._*", ".DS_Store"):
        for path in root.rglob(pattern):
            try:
                path.unlink()
            except Exception:
                pass


def _cached_flet_view_is_usable(root: Path) -> bool:
    app_path = _find_app_bundle(root)
    if app_path is None:
        return False
    info_plist = app_path / "Contents" / "Info.plist"
    macos_dir = app_path / "Contents" / "MacOS"
    if not info_plist.is_file():
        return False
    if not macos_dir.is_dir():
        return False
    try:
        info = plistlib.loads(info_plist.read_bytes())
    except Exception:
        return False
    executable = str(info.get("CFBundleExecutable") or "")
    if not executable:
        return False
    executable_path = macos_dir / executable
    if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
        return False
    try:
        next(app_path.rglob("._*"))
        return False
    except StopIteration:
        pass
    try:
        next(app_path.rglob(".DS_Store"))
        return False
    except StopIteration:
        return True


def _extract_bundled_flet_view(archive: Path) -> Path | None:
    try:
        stat = archive.stat()
        marker = f"{stat.st_size}:{stat.st_mtime_ns}"
        cache_root = Path.home() / "Library" / "Application Support" / APP_NAME / "flet-runtime"
        marker_path = cache_root / ".source"
        app_path = _find_app_bundle(cache_root)
        if (
            app_path is not None
            and app_path.is_dir()
            and marker_path.exists()
            and marker_path.read_text(encoding="utf-8") == marker
        ):
            _remove_macos_metadata_files(cache_root)
            if _cached_flet_view_is_usable(cache_root):
                return cache_root
            shutil.rmtree(cache_root, ignore_errors=True)

        tmp_root = cache_root.with_name(f"{cache_root.name}.tmp")
        shutil.rmtree(tmp_root, ignore_errors=True)
        tmp_root.mkdir(parents=True, exist_ok=True)
        _safe_extract_tar(archive, tmp_root)
        _remove_macos_metadata_files(tmp_root)
        if not _cached_flet_view_is_usable(tmp_root):
            shutil.rmtree(tmp_root, ignore_errors=True)
            return None

        shutil.rmtree(cache_root, ignore_errors=True)
        tmp_root.rename(cache_root)
        marker_path.write_text(marker)
        return cache_root
    except Exception:
        return None


def _patch_flet_desktop_runtime(runtime_dir: Path) -> None:
    app_path = _find_app_bundle(runtime_dir)
    if app_path is None:
        raise FileNotFoundError(f"找不到内置 Flet runtime: {runtime_dir}")

    import flet_desktop

    def _launch_payload(page_url: str, assets_dir: str | None, hidden: bool):
        pid_file = str(Path(tempfile.gettempdir()).joinpath(os.urandom(10).hex()))
        args = ["open", str(app_path), "-n", "-W", "--args", page_url, pid_file]
        flet_env = {**os.environ}
        if assets_dir:
            args.append(assets_dir)
        if hidden:
            flet_env["FLET_HIDE_WINDOW_ON_START"] = "true"
        return args, flet_env, pid_file

    def _open_flet_view(page_url: str, assets_dir: str | None, hidden: bool):
        args, flet_env, pid_file = _launch_payload(page_url, assets_dir, hidden)
        return subprocess.Popen(args, env=flet_env), pid_file

    async def _open_flet_view_async(
        page_url: str, assets_dir: str | None, hidden: bool
    ):
        args, flet_env, pid_file = _launch_payload(page_url, assets_dir, hidden)
        proc = await asyncio.create_subprocess_exec(args[0], *args[1:], env=flet_env)
        return proc, pid_file

    flet_desktop.ensure_client_cached = lambda: runtime_dir
    flet_desktop.open_flet_view = _open_flet_view
    flet_desktop.open_flet_view_async = _open_flet_view_async


def configure_embedded_flet_view_path() -> Path | None:
    """优先锁定到包内 Flet runtime，避免回退到 ~/.flet/client 默认缓存。"""
    if sys.platform != "darwin":
        return None
    try:
        runtime_dir: Path | None = None
        for candidate in _embedded_flet_view_candidates():
            if _cached_flet_view_is_usable(candidate):
                runtime_dir = candidate
                break
        if runtime_dir is None:
            for candidate in _embedded_flet_view_candidates():
                archive = candidate / "flet-macos.tar.gz"
                if archive.is_file():
                    runtime_dir = _extract_bundled_flet_view(archive)
                    if runtime_dir is not None:
                        break
        if runtime_dir is None:
            return None
        os.environ["FLET_VIEW_PATH"] = str(runtime_dir)
        _patch_flet_desktop_runtime(runtime_dir)
        return runtime_dir
    except Exception:
        return None


def _flet_run_kwargs() -> dict:
    """打包版 macOS 避开 Flet 默认 UDS，改用 localhost TCP。"""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return {}
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
    return {"host": "127.0.0.1", "port": port}

# ── 必须在 import flet 之前设置 FLET_VIEW_PATH ──
# 否则 flet 在 import 时就已经决定用 ~/.flet/client 缓存的 runtime，
# 包内那份（替换过图标/名称的）Flet.app 不会生效。
configure_embedded_flet_view_path()

import flet as ft

import qlit_auth_core as qlit_auth
import yangcheng_auto
import session_utils


def _resource_dir() -> Path:
    """资源目录。开发=脚本同级；打包后=PyInstaller _MEIPASS（Contents/Resources）。"""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
        exe = Path(sys.executable).resolve()
        contents = exe.parents[1]
        res = contents / "Resources"
        return res if res.is_dir() else contents
    return Path(__file__).resolve().parent


def _cn_font_path() -> str:
    """中文字体文件路径（打包后随 assets 进 .app）。"""
    return str(_resource_dir() / "assets" / "fonts" / "NotoSansSC-Regular.otf")


# ──────────────────────── 配置 / 路径 ────────────────────────

APP_NAME = "QLIT养成教育助手"
APP_DIR = Path.home() / ".campus_auth"
APP_DIR.mkdir(exist_ok=True)
SESSION_FILE = APP_DIR / "session.txt"
SETTINGS_FILE = APP_DIR / "settings.json"
OLD_SESSION_FILE = Path(__file__).resolve().parent / "session.txt"

STYLES = ["正式", "活泼", "朴素", "感恩", "励志", "幽默"]
CATEGORY_MODES = ["自动轮转(全部)", "固定一个", "手动指定多个"]


# 颜色（与 qlit-cli/gui.py 一致）
APP_BG_LIGHT = "#f5f7fb"
APP_BG_DARK = "#0f1115"
CARD_BG_LIGHT = "#ffffff"
CARD_BG_DARK = "#181b20"
CARD_BORDER_LIGHT = "#dce3ec"
CARD_BORDER_DARK = "#2b313a"
TEXT_MUTED_LIGHT = "#667085"
TEXT_MUTED_DARK = "#a8b1bd"
ACCENT = ft.Colors.BLUE_600
ACCENT_HOVER = ft.Colors.BLUE_700
SUCCESS = ft.Colors.GREEN_600
SUCCESS_HOVER = ft.Colors.GREEN_700
ERROR = ft.Colors.RED_600
NEUTRAL = ft.Colors.GREY_500
NEUTRAL_HOVER = ft.Colors.GREY_600
TEXT_PRIMARY_LIGHT = "#1f2937"
TEXT_PRIMARY_DARK = "#e5e7eb"
FIELD_BG_LIGHT = "#ffffff"
FIELD_BG_DARK = "#11151c"
FIELD_BORDER_LIGHT = "#cfd8e5"
FIELD_BORDER_DARK = "#3a4250"
DIVIDER_LIGHT = "#e8edf3"
DIVIDER_DARK = "#2b313a"
FONT_FAMILY = "SC"
FIELD_TEXT_SIZE = 13


def _ui_text_style(
    *,
    size: int | float | None = None,
    color: str | ft.Colors | None = None,
    weight: ft.FontWeight | None = None,
    italic: bool = False,
) -> ft.TextStyle:
    return ft.TextStyle(
        font_family=FONT_FAMILY,
        font_family_fallback=[FONT_FAMILY],
        size=size,
        color=color,
        weight=weight,
        italic=italic,
    )


def _field_style_kwargs(
    text_size: int | float = FIELD_TEXT_SIZE,
    *,
    dark: bool = False,
) -> dict:
    text_color = TEXT_PRIMARY_DARK if dark else TEXT_PRIMARY_LIGHT
    muted_color = TEXT_MUTED_DARK if dark else TEXT_MUTED_LIGHT
    border_color = FIELD_BORDER_DARK if dark else FIELD_BORDER_LIGHT
    fill_color = FIELD_BG_DARK if dark else FIELD_BG_LIGHT
    return {
        "text_size": text_size,
        "text_style": _ui_text_style(size=text_size, color=text_color),
        "hint_style": _ui_text_style(size=text_size, color=muted_color),
        "border_color": border_color,
        "focused_border_color": ACCENT,
        "cursor_color": ACCENT,
        "filled": True,
        "fill_color": fill_color,
    }


def _dropdown_style_kwargs(
    text_size: int | float = FIELD_TEXT_SIZE,
    *,
    dark: bool = False,
) -> dict:
    text_color = TEXT_PRIMARY_DARK if dark else TEXT_PRIMARY_LIGHT
    muted_color = TEXT_MUTED_DARK if dark else TEXT_MUTED_LIGHT
    border_color = FIELD_BORDER_DARK if dark else FIELD_BORDER_LIGHT
    fill_color = FIELD_BG_DARK if dark else FIELD_BG_LIGHT
    return {
        "text_size": text_size,
        "text_style": _ui_text_style(size=text_size, color=text_color),
        "hint_style": _ui_text_style(size=text_size, color=muted_color),
        "border_color": border_color,
        "focused_border_color": ACCENT,
        "filled": True,
        "fill_color": fill_color,
    }


# ──────────────────────── 配置读写（与 qlit-cli/gui.py:55-96 等价） ────────────────────────

def load_session() -> str:
    if SESSION_FILE.exists():
        return SESSION_FILE.read_text(encoding="utf-8").strip()
    if OLD_SESSION_FILE.exists():
        return OLD_SESSION_FILE.read_text(encoding="utf-8").strip()
    return ""


def save_session(s: str) -> None:
    SESSION_FILE.write_text(s.strip() + "\n", encoding="utf-8")


def clear_saved_session() -> None:
    SESSION_FILE.unlink(missing_ok=True)
    OLD_SESSION_FILE.unlink(missing_ok=True)


def normalize_session(raw: str) -> str:
    raw = raw.strip()
    m = re.search(r"JSESSIONID=([^;\s]+)", raw)
    return m.group(1) if m else raw


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("prompt_template"):
                data["prompt_template"] = yangcheng_auto.sanitize_prompt_template(
                    data["prompt_template"]
                )
            return data
        except Exception:
            pass
    return {}


def save_settings(d: dict) -> None:
    if isinstance(d, dict) and d.get("prompt_template") is not None:
        d = dict(d)
        d["prompt_template"] = yangcheng_auto.sanitize_prompt_template(d["prompt_template"])
    SETTINGS_FILE.write_text(
        json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8"
    )

# ──────────────────────── App 类 ────────────────────────

class App:
    def __init__(self, page: ft.Page):
        self.page = page
        self.session: str = load_session()
        self.settings: dict = load_settings()
        self.cancel_event = asyncio.Event()
        self.closing = False
        self.busy_context = ""
        self.theme_mode_name = self._normalize_theme_mode(
            self.settings.get("theme_mode", "light")
        )
        self._platform_brightness = getattr(page, "platform_brightness", None)
        self._pending_theme_rebuild = False
        self._apply_page_theme_mode(self.theme_mode_name)
        self.clipboard = self._create_clipboard_service()
        self._preview_future: asyncio.Future | None = None
        self._session_check_task: asyncio.Task | None = None

        # 控件引用
        self.btn_theme: ft.IconButton | None = None
        self.btn_capture: ft.FilledButton | None = None
        self.lbl_session: ft.Text | None = None
        self.lbl_session_status: ft.Text | None = None  # session 校验状态（第二行）
        self.btn_fill: ft.FilledButton | None = None
        self.btn_cancel: ft.OutlinedButton | None = None
        self.progress: ft.ProgressBar | None = None
        self.lbl_progress: ft.Text | None = None
        self.log_view: ft.ListView | None = None
        self.ent_session: ft.TextField | None = None
        self.ent_start: ft.TextField | None = None
        self.ent_end: ft.TextField | None = None
        self.ent_cat: ft.TextField | None = None
        self.om_cat_mode: ft.Dropdown | None = None
        self.om_style: ft.Dropdown | None = None
        self.ent_xwjl_min: ft.TextField | None = None
        self.ent_xwjl_max: ft.TextField | None = None
        self.ent_zjfs_min: ft.TextField | None = None
        self.ent_zjfs_max: ft.TextField | None = None
        self.adv_content: ft.Container | None = None
        self.btn_advanced: ft.FilledButton | None = None
        self.adv_visible = False
        self.ent_ai_base: ft.TextField | None = None
        self.ent_ai_key: ft.TextField | None = None
        self.ent_ai_model: ft.TextField | None = None
        self.txt_prompt: ft.TextField | None = None
        self.lbl_test_ai: ft.Text | None = None
        self.chk_weekend: ft.Checkbox | None = None
        self.chk_preview: ft.Checkbox | None = None
        self.chk_force_override: ft.Checkbox | None = None
        self.ent_exclude_dates: ft.TextField | None = None
        self.ent_interval: ft.TextField | None = None
        self.ent_retry: ft.TextField | None = None

        self._root = self._build_ui()
        self._refresh_session_status()
        self._schedule_session_check(0)
        page.on_close = self._on_window_close
        page.on_platform_brightness_change = self._on_platform_brightness_change
        # 启动时先清理上一次崩溃/强杀留下的 mitmdump 子进程和系统代理残留，
        # 否则浏览器会继续走"代理服务器出现错误"。
        self._cleanup_residual_proxy()

    def _create_clipboard_service(self):
        """Flet 0.80+ 把剪贴板从 Page 方法迁到 Clipboard service。"""
        clipboard_cls = getattr(ft, "Clipboard", None)
        if clipboard_cls is None:
            return None
        try:
            clipboard = clipboard_cls()
            self.page.services.append(clipboard)
            return clipboard
        except Exception:
            return None

    def _normalize_theme_mode(self, mode: object) -> str:
        return str(mode) if str(mode) in ("light", "dark", "system") else "light"

    def _apply_page_theme_mode(self, mode: str) -> None:
        self.theme_mode_name = self._normalize_theme_mode(mode)
        if self.theme_mode_name == "dark":
            self.page.theme_mode = ft.ThemeMode.DARK
        elif self.theme_mode_name == "system":
            self.page.theme_mode = ft.ThemeMode.SYSTEM
        else:
            self.page.theme_mode = ft.ThemeMode.LIGHT

    def _is_dark(self) -> bool:
        if self.theme_mode_name == "system":
            brightness = self._platform_brightness or getattr(
                self.page, "platform_brightness", None
            )
            return brightness == ft.Brightness.DARK
        return self.theme_mode_name == "dark"

    def _tone(self, light: str, dark: str) -> str:
        return dark if self._is_dark() else light

    def _app_bg(self) -> str:
        return self._tone(APP_BG_LIGHT, APP_BG_DARK)

    def _card_bg(self) -> str:
        return self._tone(CARD_BG_LIGHT, CARD_BG_DARK)

    def _card_border(self) -> str:
        return self._tone(CARD_BORDER_LIGHT, CARD_BORDER_DARK)

    def _text_primary(self) -> str:
        return self._tone(TEXT_PRIMARY_LIGHT, TEXT_PRIMARY_DARK)

    def _text_muted(self) -> str:
        return self._tone(TEXT_MUTED_LIGHT, TEXT_MUTED_DARK)

    def _divider(self) -> str:
        return self._tone(DIVIDER_LIGHT, DIVIDER_DARK)

    def _field_kwargs(self, text_size: int | float = FIELD_TEXT_SIZE) -> dict:
        return _field_style_kwargs(text_size, dark=self._is_dark())

    def _dropdown_kwargs(self, text_size: int | float = FIELD_TEXT_SIZE) -> dict:
        return _dropdown_style_kwargs(text_size, dark=self._is_dark())

    def _preview_bg(self) -> str:
        return self._tone("#eef3fa", "#111827")

    def _cancel_button_style(self) -> ft.ButtonStyle:
        return ft.ButtonStyle(
            side=ft.BorderSide(1, self._tone("#9aa6b8", "#64748b")),
            color=self._tone("#374151", "#d1d5db"),
        )

    def _theme_icon(self):
        if self.theme_mode_name == "dark":
            return ft.Icons.LIGHT_MODE
        if self.theme_mode_name == "system":
            return ft.Icons.BRIGHTNESS_6
        return ft.Icons.DARK_MODE

    def _theme_tooltip(self) -> str:
        if self.theme_mode_name == "light":
            return "当前：浅色。点击切换到深色模式"
        if self.theme_mode_name == "dark":
            return "当前：深色。点击切换到跟随系统"
        return "当前：跟随系统。点击切换到浅色模式"

    def _next_theme_mode(self) -> str:
        return {
            "light": "dark",
            "dark": "system",
            "system": "light",
        }[self.theme_mode_name]

    def _cleanup_residual_proxy(self) -> None:
        """启动时清理：杀掉非自己 fork 的 mitmdump，把代理状态归零。"""
        if sys.platform == "darwin":
            try:
                subprocess.run(
                    ["pkill", "-f", "mitmdump --listen-host 127.0.0.1"],
                    check=False, capture_output=True,
                )
            except Exception:
                pass
            try:
                for svc in subprocess.check_output(
                    ["/usr/sbin/networksetup", "-listallnetworkservices"], text=True
                ).splitlines()[1:]:
                    svc = svc.strip()
                    if not svc or svc.startswith("*"):
                        continue
                    subprocess.run(
                        ["/usr/sbin/networksetup", "-setwebproxystate", svc, "off"],
                        check=False, capture_output=True,
                    )
                    subprocess.run(
                        ["/usr/sbin/networksetup", "-setsecurewebproxystate", svc, "off"],
                        check=False, capture_output=True,
                    )
            except Exception:
                pass

    # ---------- 根布局 ----------

    def _build_ui(self) -> ft.Container:
        return ft.Container(
            content=ft.Column(
                controls=[self._build_header(), self._build_body()],
                spacing=0,
                expand=True,
            ),
            bgcolor=self._app_bg(),
            expand=True,
        )

    def _card(self, child: ft.Control) -> ft.Container:
        return ft.Container(
            content=child,
            bgcolor=self._card_bg(),
            border=ft.Border(
                left=ft.BorderSide(1, self._card_border()),
                right=ft.BorderSide(1, self._card_border()),
                top=ft.BorderSide(1, self._card_border()),
                bottom=ft.BorderSide(1, self._card_border()),
            ),
            border_radius=8,
            padding=12,
        )

    def _build_header(self) -> ft.Container:
        self.btn_theme = ft.IconButton(
            icon=self._theme_icon(),
            tooltip=self._theme_tooltip(),
            icon_color=self._text_muted(),
            disabled=bool(self.busy_context),
            on_click=self._toggle_theme,
        )
        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Column(
                        controls=[
                            ft.Text(
                                APP_NAME,
                                size=24,
                                weight=ft.FontWeight.BOLD,
                                color=self._text_primary(),
                            ),
                            ft.Text(
                                "pass.qlit.edu.cn",
                                size=12,
                                color=self._text_muted(),
                                theme_style=ft.TextThemeStyle.BODY_SMALL,
                            ),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    self.btn_theme,
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding(left=18, right=18, top=14, bottom=6),
        )

    def _build_body(self) -> ft.Container:
        body = ft.Row(
            controls=[self._build_left(), self._build_log()],
            spacing=12,
            expand=True,
        )
        return ft.Container(
            content=body,
            padding=ft.Padding(left=16, right=16, top=0, bottom=16),
            expand=True,
        )

    def _build_left(self) -> ft.Container:
        scroll_col = ft.Column(
            controls=[
                self._build_cred_card(),
                self._build_fill_card(),
            ],
            spacing=10,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        # 上：可滚动表单区；下：固定底部操作栏（不随滚动）
        return ft.Container(
            content=ft.Column(
                controls=[scroll_col, self._build_action_bar()],
                spacing=0,
                expand=True,
            ),
            expand=3,
        )

    def _build_log(self) -> ft.Container:
        self.log_view = ft.ListView(
            expand=True,
            spacing=2,
            auto_scroll=True,
            padding=8,
        )
        # 空状态文案：日志区为空时显示，让右侧卡片不显得"漂着"
        self.log_empty_hint = ft.Text(
            "运行过程会显示在这里",
            size=11,
            color=self._text_muted(),
            italic=True,
        )
        self.log_empty_stack = ft.Stack(
            controls=[
                self.log_view,
                ft.Container(
                    content=self.log_empty_hint,
                    alignment=ft.Alignment(0, 0),  # 居中
                ),
            ],
            expand=True,
        )
        log_card = self._card(
            ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Text(
                                "日志",
                                size=15,
                                weight=ft.FontWeight.BOLD,
                                color=self._text_primary(),
                            ),
                            ft.Text(
                                "运行过程",
                                size=11,
                                color=self._text_muted(),
                                italic=True,
                            ),
                            ft.Container(expand=True),  # 把按钮推到右边
                            ft.IconButton(
                                icon=ft.Icons.COPY,
                                tooltip="复制全部日志",
                                icon_size=16,
                                icon_color=self._text_muted(),
                                on_click=self._on_copy_log,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                tooltip="清空日志",
                                icon_size=16,
                                icon_color=self._text_muted(),
                                on_click=self._on_clear_log,
                            ),
                        ],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Container(content=self.log_empty_stack, expand=True),
                ],
                spacing=4,
            )
        )
        return ft.Container(content=log_card, expand=2)

    # ---------- 表单 helper ----------

    def _form_row(self, label: str, control: ft.Control) -> ft.Row:
        """左 label / 右 control 的两列表单行，label 固定 110 宽。"""
        return ft.Row(
            controls=[
                ft.Container(
                    content=ft.Text(label, size=12, color=self._text_primary()),
                    width=110,
                    alignment=ft.Alignment(-1, 0),
                ),
                ft.Container(content=control, expand=True),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _form_row_split(self, label: str, left: ft.Control, right: ft.Control) -> ft.Row:
        """左 label / 右两栏（左 control + 右 control）。"""
        return ft.Row(
            controls=[
                ft.Container(
                    content=ft.Text(label, size=12, color=self._text_primary()),
                    width=110,
                    alignment=ft.Alignment(-1, 0),
                ),
                ft.Container(content=left, expand=True),
                ft.Container(content=right, expand=True),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    # ---------- 凭据区 ----------

    def _build_cred_card(self) -> ft.Container:
        self.btn_capture = ft.FilledButton(
            content=ft.Text("① 抓取凭据", size=14, weight=ft.FontWeight.W_500),
            style=ft.ButtonStyle(bgcolor=ACCENT, color=ft.Colors.WHITE),
            on_click=self.on_capture,
            height=36,
        )
        self.lbl_session = ft.Text("无", size=12, color=self._text_muted())
        self.lbl_session_status = ft.Text("", size=11, color=self._text_muted())
        btn_clear_session = ft.OutlinedButton(
            content=ft.Text("清除", size=12),
            on_click=self.on_clear_session,
        )
        self.ent_session = ft.TextField(
            hint_text="JSESSIONID...",
            expand=True,
            dense=True,
            **self._field_kwargs(),
        )
        btn_import = ft.OutlinedButton(
            content=ft.Text("导入", size=12),
            on_click=self.on_import_session,
        )

        # 第一行：左 抓取、右 清除
        row_capture = ft.Row(
            controls=[self.btn_capture, btn_clear_session],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        # 状态拆两行：上行 session 摘要（"当前：xxx..."），下行校验结果（"✓ 有效" / "✗ 原因"）
        row_status = ft.Column(
            controls=[self.lbl_session, self.lbl_session_status],
            spacing=2,
        )
        # 第三行：粘贴 + 输入 + 导入
        row_paste = ft.Row(
            controls=[
                ft.Text("或粘贴 session：", size=12, color=self._text_muted()),
                self.ent_session,
                btn_import,
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        return self._card(
            ft.Column(
                controls=[
                    ft.Text(
                        "凭据",
                        size=15,
                        weight=ft.FontWeight.BOLD,
                        color=self._text_primary(),
                    ),
                    row_capture,
                    row_status,
                    row_paste,
                ],
                spacing=8,
            )
        )

    # ---------- 填写区 ----------

    def _build_fill_card(self) -> ft.Container:
        s = self.settings
        # 日期：每列上方小标题，下面输入框
        default_start = (date.today() - timedelta(days=6)).isoformat()
        default_end = date.today().isoformat()
        self.ent_start = ft.TextField(
            value=s.get("start_date", default_start),
            expand=True,
            dense=True,
            **self._field_kwargs(),
        )
        self.ent_end = ft.TextField(
            value=s.get("end_date", default_end),
            expand=True,
            dense=True,
            **self._field_kwargs(),
        )
        date_row = ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        ft.Container(
                            content=ft.Text("开始日期", size=11, color=self._text_muted()),
                            expand=True,
                        ),
                        ft.Container(
                            content=ft.Text("结束日期", size=11, color=self._text_muted()),
                            expand=True,
                        ),
                    ],
                    spacing=8,
                ),
                ft.Row(
                    controls=[self.ent_start, self.ent_end],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
            spacing=4,
        )

        # 类别：上下结构（dropdown 一行，类别输入一行）
        self.om_cat_mode = ft.Dropdown(
            options=[ft.dropdown.Option(m) for m in CATEGORY_MODES],
            value=s.get("cat_mode", CATEGORY_MODES[0]),
            expand=True,
            dense=True,
            on_select=lambda e: self._on_cat_mode_change(),
            **self._dropdown_kwargs(),
        )
        self.ent_cat = ft.TextField(
            hint_text="如：爱党爱国  或  爱党爱国,诚实守信",
            expand=True,
            dense=True,
            **self._field_kwargs(),
        )
        if s.get("cat"):
            self.ent_cat.value = s["cat"]
        cat_mode_row = self._form_row("类别模式", self.om_cat_mode)
        cat_input_row = self._form_row("类别输入", self.ent_cat)
        self._cat_input_row_ref = cat_input_row  # 切换模式时控制 visible
        self._on_cat_mode_change()  # 同步 visible + disabled

        # 风格
        self.om_style = ft.Dropdown(
            options=[ft.dropdown.Option(x) for x in STYLES],
            value=s.get("style", "正式"),
            expand=True,
            dense=True,
            **self._dropdown_kwargs(),
        )
        style_row = self._form_row("内容风格", self.om_style)

        # 字数：拆成两行，每行 min ~ max（label/输入框/~ 都固定宽度，整齐）
        self.ent_xwjl_min = ft.TextField(
            value=str(s.get("xwjl_min", 150)),
            width=64,
            dense=True,
            text_align=ft.TextAlign.CENTER,
            **self._field_kwargs(),
        )
        self.ent_xwjl_max = ft.TextField(
            value=str(s.get("xwjl_max", 250)),
            width=64,
            dense=True,
            text_align=ft.TextAlign.CENTER,
            **self._field_kwargs(),
        )
        self.ent_zjfs_min = ft.TextField(
            value=str(s.get("zjfs_min", 120)),
            width=64,
            dense=True,
            text_align=ft.TextAlign.CENTER,
            **self._field_kwargs(),
        )
        self.ent_zjfs_max = ft.TextField(
            value=str(s.get("zjfs_max", 200)),
            width=64,
            dense=True,
            text_align=ft.TextAlign.CENTER,
            **self._field_kwargs(),
        )
        xwjl_row = ft.Row(
            controls=[
                ft.Container(
                    content=ft.Text("行为记录", size=12, color=self._text_primary()),
                    width=110,
                    alignment=ft.Alignment(-1, 0),
                ),
                ft.Container(content=self.ent_xwjl_min, width=64),
                ft.Container(
                    content=ft.Text("~", size=12, color=self._text_muted()),
                    width=16,
                    alignment=ft.Alignment(0, 0),
                ),
                ft.Container(content=self.ent_xwjl_max, width=64),
                ft.Container(expand=True),  # 吃掉剩余空间，让左半边不漂
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        zjfs_row = ft.Row(
            controls=[
                ft.Container(
                    content=ft.Text("总结反思", size=12, color=self._text_primary()),
                    width=110,
                    alignment=ft.Alignment(-1, 0),
                ),
                ft.Container(content=self.ent_zjfs_min, width=64),
                ft.Container(
                    content=ft.Text("~", size=12, color=self._text_muted()),
                    width=16,
                    alignment=ft.Alignment(0, 0),
                ),
                ft.Container(content=self.ent_zjfs_max, width=64),
                ft.Container(expand=True),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # 高级展开按钮 + 提示文案上下结构
        self.btn_advanced = ft.OutlinedButton(
            content=ft.Text(
                "▼ 高级选项" if self.adv_visible else "▶ 高级选项",
                size=12,
            ),
            on_click=self._toggle_advanced,
        )
        adv_hint = ft.Text(
            "AI 配置 / 预览 / 提交节奏",
            size=11,
            color=self._text_muted(),
        )

        # 高级内容（默认隐藏，构造时建好，toggle 控制 visible）
        # 顶部一条很淡的分隔线 + padding-top，柔和过渡，不让高级区"啪"地弹出来
        self.adv_content = ft.Container(
            content=self._build_advanced_content(),
            visible=self.adv_visible,
            padding=ft.Padding(top=10),
            border=ft.Border(top=ft.BorderSide(1, self._divider())),
        )

        return self._card(
            ft.Column(
                controls=[
                    ft.Text(
                        "填写",
                        size=15,
                        weight=ft.FontWeight.BOLD,
                        color=self._text_primary(),
                    ),
                    date_row,
                    cat_mode_row,
                    self._cat_input_row_ref,
                    style_row,
                    xwjl_row,
                    zjfs_row,
                    self.btn_advanced,
                    adv_hint,
                    self.adv_content,
                ],
                spacing=8,
            )
        )

    def _build_action_bar(self) -> ft.Container:
        """左栏底部固定操作栏（不随表单滚动）。"""
        self.btn_fill = ft.FilledButton(
            content=ft.Text("② 开始填写", size=14, weight=ft.FontWeight.W_500),
            style=ft.ButtonStyle(bgcolor=SUCCESS, color=ft.Colors.WHITE),
            on_click=self.on_fill,
            height=36,
            disabled=not self.session,
        )
        self.btn_cancel = ft.OutlinedButton(
            content=ft.Text("取消", size=12, weight=ft.FontWeight.W_500),
            style=self._cancel_button_style(),
            on_click=self.on_cancel,
            disabled=True,
        )
        action_row = ft.Row(
            controls=[self.btn_fill, self.btn_cancel],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.progress = ft.ProgressBar(value=0, expand=True)
        self.lbl_progress = ft.Text(
            "0/0", size=12, color=self._text_muted(),
            weight=ft.FontWeight.W_500,
        )
        progress_row = ft.Row(
            controls=[self.progress, self.lbl_progress],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        # 顶部一条很淡的分隔线 + 一点 padding，把它和上面的表单视觉上分开
        return ft.Container(
            content=ft.Column(
                controls=[action_row, progress_row],
                spacing=8,
            ),
            padding=ft.Padding(top=10),
            border=ft.Border(top=ft.BorderSide(1, self._divider())),
        )

    # ---------- 高级内容 ----------

    def _build_advanced_content(self) -> ft.Column:
        s = self.settings

        # AI 配置
        self.ent_ai_base = ft.TextField(
            hint_text="https://open.bigmodel.cn/api/paas/v4",
            expand=True,
            dense=True,
            **self._field_kwargs(),
        )
        if s.get("ai_base"):
            self.ent_ai_base.value = s["ai_base"]
        self.ent_ai_key = ft.TextField(
            hint_text="sk-...",
            password=True,
            can_reveal_password=True,
            expand=True,
            dense=True,
            **self._field_kwargs(),
        )
        if s.get("ai_key"):
            self.ent_ai_key.value = s["ai_key"]
        self.ent_ai_model = ft.TextField(
            hint_text="glm-4-flash",
            width=220,
            dense=True,
            **self._field_kwargs(),
        )
        if s.get("ai_model"):
            self.ent_ai_model.value = s["ai_model"]

        btn_test_ai = ft.OutlinedButton(
            content=ft.Text("测试连接", size=12),
            on_click=self.on_test_ai,
        )
        btn_clear_ai = ft.OutlinedButton(
            content=ft.Text("清空 Key", size=12),
            on_click=self.on_clear_ai_key,
        )
        self.lbl_test_ai = ft.Text("", size=12, color=self._text_muted())
        test_row = ft.Row(
            controls=[btn_test_ai, btn_clear_ai, self.lbl_test_ai],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        ai_row1 = ft.Row(
            controls=[
                ft.Text("API Base", size=12, color=self._text_primary()),
                self.ent_ai_base,
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        ai_row2 = ft.Row(
            controls=[
                ft.Text("API Key", size=12, color=self._text_primary()),
                self.ent_ai_key,
                ft.Text("Model", size=12, color=self._text_primary()),
                self.ent_ai_model,
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # Prompt：边框更淡 + 默认只占 3 行，字号 10，不抢视觉中心
        self.txt_prompt = ft.TextField(
            value=s.get("prompt_template") or yangcheng_auto.default_prompt_template(),
            multiline=True,
            min_lines=3,
            max_lines=12,
            **self._field_kwargs(text_size=10),
        )
        prompt_reset = ft.OutlinedButton(
            content=ft.Text("恢复默认", size=12),
            on_click=self._reset_prompt_template,
        )
        prompt_header = ft.Row(
            controls=[
                ft.Text(
                    "Prompt（变量：{category}/{rq}/{style}/字数范围）",
                    size=12,
                    color=self._text_primary(),
                ),
                prompt_reset,
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # 行为选项
        self.chk_weekend = ft.Checkbox(
            label="跳过周末(周六日)", value=bool(s.get("skip_weekend", False))
        )
        self.chk_preview = ft.Checkbox(
            label="提交前预览确认每条", value=bool(s.get("preview", False))
        )
        self.chk_force_override = ft.Checkbox(
            label="强制覆盖(无视先前填写仍然提交)",
            value=bool(s.get("force_override", False)),
        )

        self.ent_exclude_dates = ft.TextField(
            hint_text="2026-04-05, 2026-05-01",
            expand=True,
            dense=True,
            **self._field_kwargs(),
        )
        if s.get("exclude_dates"):
            self.ent_exclude_dates.value = s["exclude_dates"]
        exclude_row = ft.Row(
            controls=[
                ft.Text("排除日期", size=12, color=self._text_primary()),
                self.ent_exclude_dates,
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # 提交节奏
        self.ent_interval = ft.TextField(
            value=str(s.get("interval", 0)),
            width=60,
            dense=True,
            **self._field_kwargs(),
        )
        self.ent_retry = ft.TextField(
            value=str(s.get("retry", 0)),
            width=60,
            dense=True,
            **self._field_kwargs(),
        )
        pace_row = ft.Row(
            controls=[
                ft.Text("提交间隔(秒)", size=12, color=self._text_primary()),
                self.ent_interval,
                ft.Text("失败重试次数", size=12, color=self._text_primary()),
                self.ent_retry,
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # 段标题（小、灰，明显弱于主卡片标题"填写"）
        sect_ai = ft.Text(
            "AI 配置（不填用模板）",
            size=11,
            color=self._text_muted(),
            weight=ft.FontWeight.W_600,
        )
        sect_behavior = ft.Text(
            "提交行为",
            size=11,
            color=self._text_muted(),
            weight=ft.FontWeight.W_600,
        )

        return ft.Column(
            controls=[
                # ─── 段 1：AI 配置 ───
                sect_ai,
                ai_row1,
                ai_row2,
                test_row,
                prompt_header,
                self.txt_prompt,
                # 中间一条很淡的分隔线，让两段明确分开
                ft.Container(
                    height=1,
                    bgcolor=self._divider(),
                    margin=ft.Margin(top=6, bottom=6),
                ),
                # ─── 段 2：提交行为 ───
                sect_behavior,
                ft.Column(
                    controls=[
                        self.chk_weekend,
                        self.chk_preview,
                        self.chk_force_override,
                    ],
                    spacing=2,
                ),
                exclude_row,
                pace_row,
            ],
            spacing=8,
        )

    # ---------- 日志 ----------

    def log(self, msg: str, tag: str = "") -> None:
        """只在 page.loop 主线程里直接调。worker 线程请用 log_threadsafe。"""
        if self.closing or not self.log_view:
            return
        # 第一条日志进来时，隐藏空状态提示
        if getattr(self, "log_empty_hint", None) and self.log_empty_hint.visible:
            self.log_empty_hint.visible = False
        color = None
        if tag == "ok":
            color = SUCCESS
        elif tag == "err":
            color = ERROR
        self.log_view.controls.append(
            ft.Text(
                str(msg),
                size=12,
                font_family=FONT_FAMILY,
                color=color,
                selectable=True,
            )
        )
        try:
            self.page.update()
        except Exception:
            pass

    def _on_clear_log(self, e: ft.ControlEvent) -> None:
        if not self.log_view:
            return
        self.log_view.controls.clear()
        # 清空后恢复空状态提示
        if getattr(self, "log_empty_hint", None):
            self.log_empty_hint.visible = True
        try:
            self.page.update()
        except Exception:
            pass

    async def _on_copy_log(self, e: ft.ControlEvent) -> None:
        if not self.log_view:
            return
        lines = []
        for c in self.log_view.controls:
            if isinstance(c, ft.Text):
                lines.append(c.value or "")
        text = "\n".join(lines)
        if not text:
            return
        try:
            if self.clipboard is not None:
                await self.clipboard.set(text)
            elif hasattr(self.page, "set_clipboard"):
                self.page.set_clipboard(text)
            else:
                raise RuntimeError("当前 Flet 运行时没有可用的剪贴板 API")
            self.log(f"✓ 已复制 {len(lines)} 行日志", "ok")
        except Exception as ex:
            self.log(f"✗ 复制失败：{ex}", "err")

    def log_threadsafe(self, msg: str, tag: str = "") -> None:
        """worker 线程专用入口：把 log() 投回 page.loop 主线程，避免直接动 Flet 控件。"""
        if self.closing:
            return
        try:
            self.page.loop.call_soon_threadsafe(self.log, msg, tag)
        except Exception:
            pass

    def set_progress_threadsafe(self, done: int, total: int) -> None:
        """worker 线程专用入口：把进度更新投回 page.loop 主线程。"""
        if self.closing:
            return
        try:
            self.page.loop.call_soon_threadsafe(self._set_progress, done, total)
        except Exception:
            pass

    def _set_progress(self, done: int, total: int) -> None:
        """主线程实际改进度 UI。"""
        if self.progress:
            self.progress.value = 0 if total <= 0 else max(0, min(1, done / total))
        if self.lbl_progress:
            self.lbl_progress.value = f"{done}/{total}"
        try:
            self.page.update()
        except Exception:
            pass

    # ---------- 会话状态 ----------

    def _refresh_session_status(self) -> None:
        # 第一行：session 摘要
        s = self.session
        self.lbl_session.value = ("当前：" + (s[:20] + "...")) if s else "当前：无"
        # 第二行：校验中
        if self.lbl_session_status:
            self.lbl_session_status.value = "检测中..."
            self.lbl_session_status.color = self._text_muted()

    def _schedule_session_check(self, delay_ms: int = 0) -> None:
        # Flet 用后台 task 替代 self.after 定时器
        if self.closing:
            return
        if self._session_check_task and not self._session_check_task.done():
            self._session_check_task.cancel()
        self._session_check_task = asyncio.create_task(self._session_check_loop(delay_ms / 1000))

    async def _session_check_loop(self, initial_delay: float) -> None:
        try:
            await asyncio.sleep(initial_delay)
        except asyncio.CancelledError:
            return
        while not self.closing:
            await self._do_session_check()
            try:
                await asyncio.sleep(5 * 60)
            except asyncio.CancelledError:
                return

    async def _do_session_check(self) -> None:
        # 锁住本轮开始时的 session：如果校验过程中 self.session 被替换，
        # 旧结果不应该写到新 session 的 UI 上。
        checked = self.session
        if not checked or self.closing:
            if checked != self.session:
                return
            if self.lbl_session:
                self.lbl_session.value = "当前：无"
            if self.lbl_session_status:
                self.lbl_session_status.value = ""
            try:
                self.page.update()
            except Exception:
                pass
            return
        try:
            valid, detail = await asyncio.to_thread(session_utils.check_session, checked)
        except Exception as e:
            valid, detail = False, f"网络错误：{e}"
        if self.closing or not self.lbl_session or not self.lbl_session_status:
            return
        # 渲染前再确认：self.session 是不是还是当时校验的那个
        if self.session != checked:
            return
        mark = "✓" if valid else "✗"
        self.lbl_session_status.value = f"{mark} {detail}"
        self.lbl_session_status.color = SUCCESS if valid else ERROR
        try:
            self.page.update()
        except Exception:
            pass

    # ---------- 抓取凭据 ----------

    def on_capture(self, e: ft.ControlEvent) -> None:
        if not qlit_auth.find_mitmdump():
            self.log("✗ 未找到 mitmproxy。请先安装：\n  mac: brew install --cask mitmproxy\n  win: winget install mitmproxy", "err")
            return
        self.busy_context = "capture"
        if self.btn_theme:
            self.btn_theme.disabled = True
        if self.btn_capture:
            # 复用按钮做"取消"：保持可点，点击走 on_cancel("capture")
            self.btn_capture.disabled = False
            self.btn_capture.content = ft.Text("取消抓取", size=14, weight=ft.FontWeight.W_500)
            self.btn_capture.icon = ft.Icons.CLOSE
            self.btn_capture.icon_color = ft.Colors.WHITE
            self.btn_capture.style = ft.ButtonStyle(
                bgcolor=ERROR, color=ft.Colors.WHITE
            )
            self.btn_capture.on_click = lambda _: self.on_cancel(None, context="capture")
            try:
                self.page.update()
            except Exception:
                pass
        self.cancel_event.clear()
        asyncio.create_task(self._run_capture())

    async def _run_capture(self) -> None:
        try:
            jsession = await asyncio.to_thread(
                qlit_auth.capture_session,
                on_log=lambda m: self.log_threadsafe(m),
                should_stop=self.cancel_event.is_set,
                timeout=300,
            )
            self.session = jsession
            save_session(jsession)
            self.log("✓ 抓取成功，已保存", "ok")
            self._refresh_session_status()
            self._schedule_session_check(0)  # 抓包成功后立刻重检一次，不等 5 分钟
            if self.btn_fill:
                self.btn_fill.disabled = False
        except Exception as e:
            self.log(f"✗ 抓取失败：{e}", "err")
        finally:
            if self.btn_capture:
                # 还原成"抓取凭据"按钮
                self.btn_capture.disabled = False
                self.btn_capture.content = ft.Text("① 抓取凭据", size=14, weight=ft.FontWeight.W_500)
                self.btn_capture.icon = None
                self.btn_capture.icon_color = None
                self.btn_capture.style = ft.ButtonStyle(
                    bgcolor=ACCENT, color=ft.Colors.WHITE
                )
                self.btn_capture.on_click = self.on_capture
            self.busy_context = ""
            if self.btn_theme:
                self.btn_theme.disabled = False
            if not self._rebuild_pending_theme_surface():
                try:
                    self.page.update()
                except Exception:
                    pass

    def on_import_session(self, e: ft.ControlEvent) -> None:
        if not self.ent_session:
            return
        s = normalize_session(self.ent_session.value or "")
        if not s:
            self.log("请先粘贴 session", "err")
            return
        self.session = s
        save_session(s)
        self._refresh_session_status()
        self._schedule_session_check(0)  # 导入后立刻重检
        if self.btn_fill:
            self.btn_fill.disabled = False
        self.log("✓ 已导入 session", "ok")

    def on_clear_session(self, e: ft.ControlEvent) -> None:
        self.session = ""
        if self.ent_session:
            self.ent_session.value = ""
        clear_saved_session()
        self._refresh_session_status()
        self._schedule_session_check(0)  # 清除后立刻重检（→ 无）
        if self.btn_fill:
            self.btn_fill.disabled = True
        self.log("✓ 已清除凭据", "ok")

    # ---------- 填写 ----------

    def on_fill(self, e: ft.ControlEvent) -> None:
        if not self.session:
            self.log("✗ 请先抓取或导入凭据", "err")
            return
        try:
            start = date.fromisoformat((self.ent_start.value or "").strip())
            end = date.fromisoformat((self.ent_end.value or "").strip())
        except (ValueError, AttributeError):
            self.log("✗ 日期格式错误，应为 YYYY-MM-DD", "err")
            return
        if start > end:
            self.log("✗ 开始日期不能晚于结束日期", "err")
            return

        def parse_int(name, value, default, min_value=0):
            text = (value or "").strip()
            if not text:
                return default
            try:
                parsed = int(text)
            except ValueError:
                raise ValueError(f"{name} 必须是整数")
            if parsed < min_value:
                raise ValueError(f"{name} 不能小于 {min_value}")
            return parsed

        try:
            xwjl_min = parse_int("行为记录最小字数", self.ent_xwjl_min.value, 150, 1)
            xwjl_max = parse_int("行为记录最大字数", self.ent_xwjl_max.value, 250, 1)
            zjfs_min = parse_int("总结反思最小字数", self.ent_zjfs_min.value, 120, 1)
            zjfs_max = parse_int("总结反思最大字数", self.ent_zjfs_max.value, 200, 1)
            submit_interval = parse_int("提交间隔", self.ent_interval.value, 0, 0)
            retry = parse_int("失败重试次数", self.ent_retry.value, 0, 0)
        except ValueError as ex:
            self.log(f"✗ {ex}", "err")
            return
        if xwjl_min > xwjl_max:
            self.log("✗ 行为记录最小字数不能大于最大字数", "err")
            return
        if zjfs_min > zjfs_max:
            self.log("✗ 总结反思最小字数不能大于最大字数", "err")
            return

        ai = yangcheng_auto.AiConfig(
            base=(self.ent_ai_base.value or "").strip(),
            key=(self.ent_ai_key.value or "").strip(),
            model=(self.ent_ai_model.value or "").strip(),
            style=self.om_style.value or "正式",
            xwjl_min=xwjl_min,
            xwjl_max=xwjl_max,
            zjfs_min=zjfs_min,
            zjfs_max=zjfs_max,
            prompt_template=(self.txt_prompt.value or "").strip(),
        )

        cat_mode = self.om_cat_mode.value or CATEGORY_MODES[0]
        cat_text = (self.ent_cat.value or "").strip()
        if cat_mode == CATEGORY_MODES[0] or not cat_text:
            category = None
        elif cat_mode == CATEGORY_MODES[1]:
            category = cat_text
        else:
            category = [c.strip() for c in cat_text.split(",") if c.strip()]
            if not category:
                self.log("✗ 手动指定多个类别时，请至少填写一个类别", "err")
                return

        exclude_dates = set()
        exclude_text = (self.ent_exclude_dates.value or "").strip()
        if exclude_text:
            parts = [p.strip() for p in re.split(r"[,，\s]+", exclude_text) if p.strip()]
            try:
                exclude_dates = {date.fromisoformat(p).isoformat() for p in parts}
            except ValueError:
                self.log("✗ 排除日期格式错误，应为 YYYY-MM-DD", "err")
                return

        save_settings({
            "theme_mode": self.theme_mode_name,
            "cat_mode": cat_mode, "cat": cat_text, "style": ai.style,
            "xwjl_min": ai.xwjl_min, "xwjl_max": ai.xwjl_max,
            "zjfs_min": ai.zjfs_min, "zjfs_max": ai.zjfs_max,
            "ai_base": ai.base, "ai_key": ai.key, "ai_model": ai.model,
            "prompt_template": ai.prompt_template,
            "skip_weekend": self.chk_weekend.value,
            "preview": self.chk_preview.value,
            "force_override": self.chk_force_override.value,
            "exclude_dates": exclude_text,
            "interval": submit_interval,
            "retry": retry,
        })

        preview_cb = None
        if self.chk_preview.value:
            preview_cb = self._preview_callback

        if self.chk_force_override.value:
            self.log("⚠ 已开启强制覆盖：已填日期也会继续提交", "err")

        if self.btn_fill:
            self.btn_fill.disabled = True
        if self.btn_cancel:
            self.btn_cancel.disabled = False
        if self.progress:
            self.progress.value = 0
        if self.lbl_progress:
            self.lbl_progress.value = "0/0"
        self.cancel_event.clear()
        self.busy_context = "fill"
        # 填写期间禁用左表单所有控件（日志区保持正常）
        self._set_form_disabled(True)
        try:
            self.page.update()
        except Exception:
            pass

        asyncio.create_task(self._run_fill(
            start, end, ai, category,
            self.chk_weekend.value, self.chk_force_override.value,
            exclude_dates, submit_interval, retry, preview_cb,
        ))

    async def _run_fill(self, start, end, ai, category, skip_weekend, force_override,
                        exclude_dates, submit_interval, retry, preview_cb) -> None:
        # on_progress 在 worker 线程里被调用，必须走线程安全入口
        def on_progress(done: int, total: int) -> None:
            self.set_progress_threadsafe(done, total)

        try:
            result = await asyncio.to_thread(
                yangcheng_auto.run_fill,
                self.session, start, end, ai,
                on_log=lambda m: self.log_threadsafe(m),
                should_stop=self.cancel_event.is_set,
                category=category,
                skip_weekend=skip_weekend,
                force_override=force_override,
                exclude_dates=exclude_dates,
                submit_interval=submit_interval,
                retry=retry,
                preview_callback=preview_cb,
                progress_callback=on_progress,
            )
            done = result["ok"] + result["fail"] + result.get("skip", 0)
            total = result["total"] or 1
            self.set_progress_threadsafe(done, total)
        except Exception as e:
            self.log(f"✗ 出错：{e}", "err")
        finally:
            if self.btn_fill:
                self.btn_fill.disabled = False
            if self.btn_cancel:
                self.btn_cancel.disabled = True
            self.busy_context = ""
            self._set_form_disabled(False)
            if not self._rebuild_pending_theme_surface():
                try:
                    self.page.update()
                except Exception:
                    pass

    def on_cancel(self, e: ft.ControlEvent = None, context: str = "fill") -> None:
        """通用取消：context="capture" 抓包中，"fill" 填写中。
        capture_session / run_fill 内部 finally 会恢复代理 / 收尾子进程。"""
        self.cancel_event.set()
        if context == "capture":
            self.log("正在取消抓取...（会恢复系统代理）", "err")
        else:
            self.log("正在取消填写...", "err")

    # ---------- AI 测试 ----------

    def on_test_ai(self, e: ft.ControlEvent) -> None:
        ai = yangcheng_auto.AiConfig(
            base=(self.ent_ai_base.value or "").strip(),
            key=(self.ent_ai_key.value or "").strip(),
            model=(self.ent_ai_model.value or "").strip(),
        )
        if self.lbl_test_ai:
            self.lbl_test_ai.value = "正在连接..."
            self.lbl_test_ai.color = self._text_muted()
        try:
            self.page.update()
        except Exception:
            pass
        asyncio.create_task(self._run_test_ai(ai))

    async def _run_test_ai(self, ai) -> None:
        try:
            ok, msg = await asyncio.to_thread(yangcheng_auto.test_ai_connection, ai)
        except Exception as e:
            ok, msg = False, f"异常：{str(e)[:80]}"
        if self.closing or not self.lbl_test_ai:
            return
        self.lbl_test_ai.value = f"{'✓' if ok else '✗'} {msg}"
        self.lbl_test_ai.color = SUCCESS if ok else ERROR
        self.log(f"AI 连接测试：{'✓' if ok else '✗'} {msg}", "ok" if ok else "err")
        try:
            self.page.update()
        except Exception:
            pass

    def on_clear_ai_key(self, e: ft.ControlEvent) -> None:
        if self.ent_ai_key:
            self.ent_ai_key.value = ""
        if self.lbl_test_ai:
            self.lbl_test_ai.value = "已清空"
            self.lbl_test_ai.color = self._text_muted()
        s = load_settings()
        if s.get("ai_key"):
            s["ai_key"] = ""
            save_settings(s)
        self.log("✓ 已清空 AI Key", "ok")

    # ---------- 高级切换 / 杂项 ----------

    def _collect_transient_settings(self) -> dict:
        s = dict(self.settings)
        def value(control, default=""):
            return (control.value if control is not None else default) or default

        s.update({
            "theme_mode": self.theme_mode_name,
            "start_date": value(self.ent_start),
            "end_date": value(self.ent_end),
            "cat_mode": value(self.om_cat_mode, CATEGORY_MODES[0]),
            "cat": value(self.ent_cat),
            "style": value(self.om_style, "正式"),
            "xwjl_min": value(self.ent_xwjl_min, "150"),
            "xwjl_max": value(self.ent_xwjl_max, "250"),
            "zjfs_min": value(self.ent_zjfs_min, "120"),
            "zjfs_max": value(self.ent_zjfs_max, "200"),
            "ai_base": value(self.ent_ai_base),
            "ai_key": value(self.ent_ai_key),
            "ai_model": value(self.ent_ai_model),
            "prompt_template": value(self.txt_prompt, yangcheng_auto.default_prompt_template()),
            "skip_weekend": bool(self.chk_weekend.value) if self.chk_weekend else False,
            "preview": bool(self.chk_preview.value) if self.chk_preview else False,
            "force_override": bool(self.chk_force_override.value) if self.chk_force_override else False,
            "exclude_dates": value(self.ent_exclude_dates),
            "interval": value(self.ent_interval, "0"),
            "retry": value(self.ent_retry, "0"),
        })
        return s

    def _capture_log_entries(self) -> list[tuple[str, str | ft.Colors | None]]:
        if not self.log_view:
            return []
        entries = []
        for control in self.log_view.controls:
            if isinstance(control, ft.Text):
                entries.append((control.value or "", control.color))
        return entries

    def _restore_log_entries(
        self,
        entries: list[tuple[str, str | ft.Colors | None]],
    ) -> None:
        if not self.log_view:
            return
        self.log_view.controls.clear()
        for value, color in entries:
            self.log_view.controls.append(
                ft.Text(
                    value,
                    size=12,
                    font_family=FONT_FAMILY,
                    color=color,
                    selectable=True,
                )
            )
        if getattr(self, "log_empty_hint", None):
            self.log_empty_hint.visible = not entries

    def _rebuild_theme_surface(self, mode: str | None = None, persist: bool = False) -> None:
        mode = self._normalize_theme_mode(mode or self.theme_mode_name)
        log_entries = self._capture_log_entries()
        status_value = self.lbl_session_status.value if self.lbl_session_status else ""
        status_color = self.lbl_session_status.color if self.lbl_session_status else self._text_muted()
        status_is_muted = status_color in (None, TEXT_MUTED_LIGHT, TEXT_MUTED_DARK)
        progress_value = self.progress.value if self.progress else 0
        progress_label = self.lbl_progress.value if self.lbl_progress else "0/0"

        self.settings = self._collect_transient_settings()
        self.settings["theme_mode"] = mode
        if persist:
            persisted = load_settings()
            persisted["theme_mode"] = mode
            save_settings(persisted)
        self._apply_page_theme_mode(mode)

        self._root = self._build_ui()
        self._refresh_session_status()
        if self.lbl_session_status:
            self.lbl_session_status.value = status_value
            self.lbl_session_status.color = (
                self._text_muted() if status_is_muted else status_color
            )
        if self.progress:
            self.progress.value = progress_value
        if self.lbl_progress:
            self.lbl_progress.value = progress_label
        self._restore_log_entries(log_entries)

        try:
            self.page.controls.clear()
            self.page.controls.append(self._root)
            self.page.update()
        except Exception:
            pass

    def _rebuild_pending_theme_surface(self) -> bool:
        if not self._pending_theme_rebuild:
            return False
        if self.theme_mode_name != "system":
            self._pending_theme_rebuild = False
            return False
        if self.busy_context:
            return False
        self._pending_theme_rebuild = False
        self._rebuild_theme_surface()
        return True

    def _on_platform_brightness_change(self, e: ft.PlatformBrightnessChangeEvent) -> None:
        self._platform_brightness = getattr(e, "brightness", None)
        if self.closing or self.theme_mode_name != "system":
            return
        if self.busy_context:
            self._pending_theme_rebuild = True
            return
        self._pending_theme_rebuild = False
        self._rebuild_theme_surface()

    def _toggle_theme(self, e: ft.ControlEvent) -> None:
        if self.busy_context:
            self.log("请在当前任务结束后切换主题", "err")
            return

        self._pending_theme_rebuild = False
        self._rebuild_theme_surface(self._next_theme_mode(), persist=True)

    def _set_form_disabled(self, disabled: bool) -> None:
        """填写期间禁用左栏所有可交互控件（不动日志区、也不动抓包按钮 / 取消按钮）。"""
        targets = [
            self.btn_theme,
            self.ent_start, self.ent_end,
            self.om_cat_mode, self.ent_cat,
            self.om_style,
            self.ent_xwjl_min, self.ent_xwjl_max,
            self.ent_zjfs_min, self.ent_zjfs_max,
            self.btn_advanced,
            self.ent_ai_base, self.ent_ai_key, self.ent_ai_model,
            self.txt_prompt,
            self.chk_weekend, self.chk_preview, self.chk_force_override,
            self.ent_exclude_dates,
            self.ent_interval, self.ent_retry,
            self.ent_session,
            self.btn_fill,
        ]
        for t in targets:
            if t is not None:
                t.disabled = disabled
        # 取消按钮在填写中启用（让用户可中断）
        if self.btn_cancel is not None:
            self.btn_cancel.disabled = not disabled

    def _toggle_advanced(self, e: ft.ControlEvent) -> None:
        self.adv_visible = not self.adv_visible
        if self.adv_content:
            self.adv_content.visible = self.adv_visible
        if self.btn_advanced:
            self.btn_advanced.content = ft.Text(
                "▼ 高级选项" if self.adv_visible else "▶ 高级选项", size=12
            )
        try:
            self.page.update()
        except Exception:
            pass

    def _reset_prompt_template(self, e: ft.ControlEvent) -> None:
        if self.txt_prompt:
            self.txt_prompt.value = yangcheng_auto.default_prompt_template()
        self.log("✓ 已恢复默认 Prompt", "ok")

    def _on_cat_mode_change(self) -> None:
        if not self.ent_cat or not self.om_cat_mode:
            return
        is_auto = (self.om_cat_mode.value == CATEGORY_MODES[0])
        # 自动轮转：直接隐藏"类别输入"整行（比 disabled 更干净）
        row = getattr(self, "_cat_input_row_ref", None)
        if row is not None:
            row.visible = not is_auto
        self.ent_cat.disabled = is_auto
        try:
            self.page.update()
        except Exception:
            pass

    # ---------- 模态预览 ----------

    def _preview_callback(self, info: dict) -> bool:
        """同步阻塞调用（被 yangcheng_auto.run_fill 在 to_thread 里同步调）。
        把 coroutine 投递到 Flet 主事件循环上等结果。"""
        future = asyncio.run_coroutine_threadsafe(self._ask_preview(info), self.page.loop)
        try:
            return future.result(timeout=300)
        except Exception:
            return False

    async def _ask_preview(self, info: dict) -> bool:
        if self.closing:
            return False

        xwjl_text = ft.Text(
            info["xwjl"],
            selectable=True,
            font_family=FONT_FAMILY,
            size=12,
            color=self._text_primary(),
        )
        zjfs_text = ft.Text(
            info["zjfs"],
            selectable=True,
            font_family=FONT_FAMILY,
            size=12,
            color=self._text_primary(),
        )
        content_col = ft.Column(
            controls=[
                ft.Text(
                    "行为记录：",
                    weight=ft.FontWeight.BOLD,
                    size=13,
                    color=self._text_primary(),
                ),
                ft.Container(
                    content=xwjl_text, bgcolor=self._preview_bg(), padding=8,
                    border_radius=4, height=150,
                ),
                ft.Text(
                    "总结反思：",
                    weight=ft.FontWeight.BOLD,
                    size=13,
                    color=self._text_primary(),
                ),
                ft.Container(
                    content=zjfs_text, bgcolor=self._preview_bg(), padding=8,
                    border_radius=4, height=120,
                ),
            ],
            tight=True,
            spacing=6,
            width=480,
        )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(
                f"日期：{info['rq']}　类别：{info['category']}",
                color=self._text_primary(),
            ),
            content=content_col,
            actions=[
                ft.TextButton("⊘ 跳过", on_click=lambda _: self._resolve_preview(False)),
                ft.FilledButton(
                    content=ft.Text("✓ 提交"),
                    bgcolor=SUCCESS,
                    on_click=lambda _: self._resolve_preview(True),
                ),
            ],
            # on_dismiss 是兜底：用户按 ESC / 点遮罩时会触发；
            # 包装器会调 _resolve_preview，所以如果按钮已 resolve 过，future.done() 会保护
            on_dismiss=lambda _: self._resolve_preview(False),
        )

        self._preview_future = asyncio.get_event_loop().create_future()
        # show_dialog 内部会自己设 dlg.open = True 并入栈；
        # pop_dialog 只处理"仍然 open 的栈顶 dialog"，所以 finally 里调一次即可
        self.page.show_dialog(dlg)
        # 取消 watcher：用户点"取消"时 cancel_event.set() 会触发，
        # 这里立刻把预览当 False resolve 掉，避免 worker 线程在 future.result()
        # 上傻等 300 秒超时。
        cancel_watcher = asyncio.create_task(self._preview_cancel_watcher())
        try:
            return await asyncio.wait_for(self._preview_future, timeout=300)
        except asyncio.TimeoutError:
            return False
        finally:
            cancel_watcher.cancel()
            try:
                await cancel_watcher
            except (asyncio.CancelledError, Exception):
                pass
            self.page.pop_dialog()
            self._preview_future = None

    async def _preview_cancel_watcher(self) -> None:
        """盯着 cancel_event，一旦置位就把当前预览当 False 收掉。"""
        try:
            while not self.closing:
                if self.cancel_event.is_set():
                    self._resolve_preview(False)
                    return
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return

    def _resolve_preview(self, value: bool) -> None:
        if self._preview_future and not self._preview_future.done():
            self._preview_future.set_result(value)

    # ---------- 关窗 ----------

    def _on_window_close(self, e: ft.WindowEvent) -> None:
        if self.closing:
            return
        self.closing = True
        self.cancel_event.set()
        if self._session_check_task and not self._session_check_task.done():
            self._session_check_task.cancel()


# ──────────────────────── 主入口 ────────────────────────
# 放在 class App 定义之后，避免 PyInstaller 打包后回调触发的 NameError

def main(page: ft.Page) -> None:
    configure_embedded_flet_view_path()
    settings = load_settings()
    theme_mode_name = str(settings.get("theme_mode", "light"))
    if theme_mode_name not in ("light", "dark", "system"):
        theme_mode_name = "light"
    page.title = APP_NAME
    page.window.width = 920
    page.window.height = 600
    page.window.min_width = 620
    page.window.min_height = 430
    if theme_mode_name == "dark":
        page.theme_mode = ft.ThemeMode.DARK
    elif theme_mode_name == "system":
        page.theme_mode = ft.ThemeMode.SYSTEM
    else:
        page.theme_mode = ft.ThemeMode.LIGHT
    page.padding = 0
    page.dev_tools = False  # 关掉右下角 Flet 开发者工具栏（python 跳动图标）

    # 注册中文字体。不要重写整套 TextTheme，否则会抹掉 Flutter/Flet 的默认字号、
    # 颜色和控件状态样式，导致界面文字比例失控。
    try:
        cn = _cn_font_path()
        if os.path.exists(cn):
            page.fonts = {FONT_FAMILY: cn}
            page.theme = ft.Theme(font_family=FONT_FAMILY)
            page.dark_theme = ft.Theme(font_family=FONT_FAMILY)
    except Exception:
        pass

    app = App(page)
    page.add(app._root)


if __name__ == "__main__":
    # 必须在 ft.run() 之前设置；Flet 会在 run() 内部先启动桌面 runtime，
    # 放到 main(page) 里已经来不及，Dock 会继续命中 ~/.flet/client 里的默认 Flet.app。
    configure_embedded_flet_view_path()
    ft.run(main, **_flet_run_kwargs())
