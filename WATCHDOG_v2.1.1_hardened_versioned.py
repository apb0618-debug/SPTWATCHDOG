import ctypes
import json
import os
import queue
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import urllib.parse
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path

try:
    from ctypes import wintypes
except ImportError:
    wintypes = None

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, Signal
from PySide6.QtGui import QAction, QIcon, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPushButton, QPlainTextEdit, QSpinBox, QDoubleSpinBox,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QSystemTrayIcon,
    QToolButton, QGraphicsOpacityEffect
)
import webbrowser

try:
    import winreg
except ImportError:
    winreg = None

APP_NAME = "WATCHDOG"
APP_DATA_DIR_NAME = "WATCHDOG"
APP_SUBTITLE = "SPT/FIKA Operations Suite"
ADDITION_TYPES = [
    "Mod(s)", "Version Updates", "Major Version Updates", "Plugins",
    "QoL", "Config Changes", "Hotfixes", "Other"
]
ISSUE_TYPES = [
    "Server", "Client", "Headless", "Mod Conflict", "Plugin",
    "Performance", "Networking", "Other"
]

ACCENT = "#C86B2A"
ACCENT_HOVER = "#DA7B35"
BG = "#0E0C0B"
SIDEBAR = "#12100E"
PANEL = "#181411"
PANEL_ALT = "#1D1714"
PANEL_SOFT = "#231B16"
BORDER = "#3A2418"
BORDER_SOFT = "#2B1A12"
TEXT = "#F3EDE6"
MUTED = "#AE9F92"
GREEN = "#57C67F"
RED = "#D76565"
AMBER = "#D99C48"


APP_VERSION = "2.1.1"
GITHUB_OWNER = "apb0618-debug"
GITHUB_REPO = "SPTWATCHDOG"
GITHUB_LATEST_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_PAGE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

PROCESS_RESTART_COOLDOWN_SEC = 10
MINIMIZE_WINDOW_ATTEMPTS = 18
MINIMIZE_WINDOW_DELAY_SEC = 0.75


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def append_log_line(path: Path, line: str):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def get_legacy_config_candidates():
    candidates = []
    try:
        candidates.append(Path(__file__).with_name("watchdog_config.json"))
    except Exception:
        pass
    try:
        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).with_name("watchdog_config.json"))
    except Exception:
        pass
    seen = set()
    unique = []
    for c in candidates:
        try:
            key = str(c.resolve())
        except Exception:
            key = str(c)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique

def get_app_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"
    app_dir = base / APP_DATA_DIR_NAME
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def resource_path(relative_path: str) -> str:
    try:
        base_path = Path(sys._MEIPASS)
    except Exception:
        try:
            if getattr(sys, "frozen", False):
                base_path = Path(sys.executable).resolve().parent
            else:
                base_path = Path(__file__).resolve().parent
        except Exception:
            base_path = Path.cwd()
    return str(base_path / relative_path)


def set_windows_app_id():
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WATCHDOG.App")
    except Exception:
        pass


APP_DATA_DIR = get_app_data_dir()
CONFIG_FILE = APP_DATA_DIR / "watchdog_config.json"
LOG_FILE = APP_DATA_DIR / "watchdog.log"
ADDITIONS_FILE = APP_DATA_DIR / "additions.json"
ISSUES_FILE = APP_DATA_DIR / "issues.json"
EXPORT_DIR = APP_DATA_DIR / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)


if os.name == "nt" and wintypes is not None:
    _user32 = ctypes.windll.user32
    _SW_MINIMIZE = 6
    _EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    try:
        _user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
        _user32.EnumWindows.restype = wintypes.BOOL
        _user32.IsWindowVisible.argtypes = [wintypes.HWND]
        _user32.IsWindowVisible.restype = wintypes.BOOL
        _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        _user32.GetWindowTextLengthW.restype = ctypes.c_int
        _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        _user32.GetWindowTextW.restype = ctypes.c_int
        _user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        _user32.ShowWindowAsync.restype = wintypes.BOOL
    except Exception:
        _user32 = None
else:
    _user32 = None
    _SW_MINIMIZE = 0
    _EnumWindowsProc = None


def _find_windows_for_pid(pid: int):
    if _user32 is None or not pid:
        return []
    windows = []

    @_EnumWindowsProc
    def callback(hwnd, lparam):
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            proc_id = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
            if int(proc_id.value) != int(pid):
                return True
            length = int(_user32.GetWindowTextLengthW(hwnd) or 0)
            title = ""
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                _user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
            windows.append((int(hwnd), title))
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(callback, 0)
    except Exception:
        return []
    return windows


def minimize_windows_for_pid(
    pid: int,
    log_func=None,
    label: str = "Process",
    attempts: int = MINIMIZE_WINDOW_ATTEMPTS,
    delay: float = MINIMIZE_WINDOW_DELAY_SEC,
):
    if _user32 is None or not pid:
        if log_func:
            log_func(f"[{label}] auto-minimize unavailable on this system; beta feature skipped.")
        return

    attempts = max(1, int(attempts))
    delay = max(0.1, float(delay))

    def worker():
        seen = False
        try:
            for _ in range(attempts):
                for hwnd, _title in _find_windows_for_pid(pid):
                    try:
                        _user32.ShowWindowAsync(hwnd, _SW_MINIMIZE)
                        seen = True
                    except Exception:
                        pass
                time.sleep(delay)
            if log_func and seen:
                log_func(f"[{label}] auto-minimize attempted.")
            elif log_func and not seen:
                log_func(f"[{label}] no visible windows found to auto-minimize.")
        except Exception as exc:
            if log_func:
                log_func(f"[{label}] auto-minimize skipped after error: {exc}")

    threading.Thread(target=worker, daemon=True, name=f"{label}MinimizeWorker").start()


@dataclass
class AppConfig:
    server_path: str = ""
    headless_path: str = ""
    server_args: str = ""
    headless_args: str = ""
    server_workdir: str = ""
    headless_workdir: str = ""
    headless_start_delay_sec: int = 10
    restart_interval_hours: float = 24.0
    monitor_interval_sec: int = 5
    auto_restart_on_crash: bool = True
    auto_start_with_monitor: bool = True
    minimize_to_tray: bool = False
    start_with_windows: bool = False
    log_to_file: bool = True
    discord_notifications_enabled: bool = False
    discord_webhook_url: str = ""
    hide_server_console: bool = False


@dataclass
class AdditionEntry:
    created_at: str
    title: str
    entry_date: str
    addition_type: str
    additions: str
    notes: str


@dataclass
class IssueEntry:
    created_at: str
    title: str
    issue_date: str
    issue_type: str
    description: str
    recent_items: str
    fix_notes: str


class ManagedProcess:
    def __init__(self, name: str, log_func):
        self.name = name
        self.log = log_func
        self.process = None
        self.stop_requested = False
        self.lock = threading.Lock()
        self.reader_thread = None
        self.last_start_monotonic = 0.0
        self.last_stop_monotonic = 0.0
        self.last_auto_restart_attempt = 0.0
        self.last_exit_code = None
        self.last_start_error = ""

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def get_exit_code(self):
        if self.process is None:
            return self.last_exit_code
        try:
            code = self.process.poll()
            if code is not None:
                self.last_exit_code = code
            return code
        except Exception:
            return self.last_exit_code

    def allow_auto_restart(self, cooldown_sec: int = PROCESS_RESTART_COOLDOWN_SEC) -> bool:
        now = time.monotonic()
        if now - self.last_auto_restart_attempt < max(1, int(cooldown_sec)):
            return False
        self.last_auto_restart_attempt = now
        return True

    def _stream_output(self, proc):
        try:
            if proc.stdout is None:
                return
            for raw_line in iter(proc.stdout.readline, ''):
                if raw_line is None:
                    break
                line = raw_line.rstrip()
                if line:
                    self.log(f"[{self.name}Console] {line}")
        except Exception as e:
            self.log(f"[{self.name}] console stream error: {e}")
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass

    def start(self, exe_path: str, workdir: str = "", hide_window: bool = False, capture_output: bool = False) -> bool:
        with self.lock:
            if not exe_path:
                self.log(f"[{self.name}] no path set.")
                self.last_start_error = "No executable path configured."
                return False
            exe = Path(exe_path).expanduser()
            if not exe.exists():
                self.log(f"[{self.name}] file not found: {exe}")
                self.last_start_error = f"Executable not found: {exe}"
                return False
            if self.is_running():
                self.log(f"[{self.name}] already running.")
                return True

            requested_cwd = Path(workdir.strip()).expanduser() if workdir.strip() else exe.parent
            cwd_path = requested_cwd if requested_cwd.exists() and requested_cwd.is_dir() else exe.parent
            if cwd_path != requested_cwd:
                self.log(f"[{self.name}] invalid working directory '{requested_cwd}'. Falling back to '{cwd_path}'.")
            cwd = str(cwd_path)
            cmd = [str(exe)]
            self.log(f"[{self.name}] launch cmd: {cmd}")
            self.log(f"[{self.name}] cwd: {cwd}")
            try:
                self.stop_requested = False
                self.last_start_error = ""
                creationflags = 0
                startupinfo = None
                popen_kwargs = {}
                if os.name == "nt":
                    if hide_window:
                        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                        startupinfo = subprocess.STARTUPINFO()
                        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        startupinfo.wShowWindow = 0
                    elif self.name == "Server":
                        creationflags = subprocess.CREATE_NEW_CONSOLE
                if capture_output:
                    popen_kwargs.update({
                        'stdout': subprocess.PIPE,
                        'stderr': subprocess.STDOUT,
                        'text': True,
                        'bufsize': 1,
                        'encoding': 'utf-8',
                        'errors': 'replace',
                    })
                self.process = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    shell=False,
                    creationflags=creationflags,
                    startupinfo=startupinfo,
                    **popen_kwargs,
                )
                self.last_start_monotonic = time.monotonic()
                self.last_exit_code = None
                if capture_output and self.process.stdout is not None:
                    self.reader_thread = threading.Thread(
                        target=self._stream_output,
                        args=(self.process,),
                        daemon=True,
                        name=f"{self.name}ConsoleReader",
                    )
                    self.reader_thread.start()
                self.log(f"[{self.name}] started. PID={self.process.pid}")
                if hide_window:
                    self.log(f"[{self.name}] launched with hidden console mode.")
                return True
            except Exception as e:
                self.process = None
                self.last_start_error = repr(e)
                self.log(f"[{self.name}] failed to start: {repr(e)}")
                return False

    def stop(self, timeout: int = 15) -> bool:
        with self.lock:
            if not self.is_running():
                self.log(f"[{self.name}] not running.")
                self.process = None
                return True
            self.stop_requested = True
            proc = self.process
            pid = proc.pid
            self.log(f"[{self.name}] stopping PID={pid}...")
            try:
                if os.name == "nt":
                    result = subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        check=False,
                    )
                    if result.stdout.strip():
                        self.log(f"[{self.name}] taskkill: {result.stdout.strip()}")
                    if result.stderr.strip():
                        self.log(f"[{self.name}] taskkill stderr: {result.stderr.strip()}")
                    time.sleep(1)
                else:
                    proc.terminate()
                    proc.wait(timeout=timeout)

                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                self.last_exit_code = proc.poll()
                self.last_stop_monotonic = time.monotonic()
                self.process = None
                self.log(f"[{self.name}] stopped.")
                return True
            except Exception as e:
                try:
                    self.last_exit_code = proc.poll()
                except Exception:
                    pass
                self.log(f"[{self.name}] stop failed: {e}")
                return False
            finally:
                if proc is not None and proc.poll() is not None:
                    self.last_exit_code = proc.poll()
                    self.last_stop_monotonic = time.monotonic()
                    self.process = None


class NavButton(QPushButton):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setMinimumHeight(46)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("navButton")


class StatusCard(QFrame):
    def __init__(self, title: str, value: str, color: str, parent=None):
        super().__init__(parent)
        self.setObjectName("statusCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        inner = QFrame()
        inner.setObjectName("statusInner")
        outer.addWidget(inner)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(8)
        top = QHBoxLayout()
        self.dot = QLabel("●")
        self.dot.setStyleSheet(f"color:{color}; font-size:16px;")
        top.addWidget(self.dot)
        self.title_label = QLabel(title.upper())
        self.title_label.setObjectName("cardTitle")
        top.addWidget(self.title_label)
        top.addStretch(1)
        layout.addLayout(top)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("cardValue")
        self.value_label.setWordWrap(True)
        layout.addWidget(self.value_label)

    def set_status(self, value: str, color: str):
        self.value_label.setText(value)
        self.dot.setStyleSheet(f"color:{color}; font-size:16px;")


class SectionCard(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("sectionCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        inner = QFrame()
        inner.setObjectName("sectionInner")
        outer.addWidget(inner)
        shell = QVBoxLayout(inner)
        shell.setContentsMargins(16, 14, 16, 16)
        shell.setSpacing(12)
        lbl = QLabel(title.upper())
        lbl.setObjectName("sectionTitle")
        shell.addWidget(lbl)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        shell.addLayout(self.body)


class UpdatePopup(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("updatePopup")
        self.setVisible(False)
        self.release_url = ""
        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity_effect)
        self.opacity_effect.setOpacity(0.0)
        self.anim = QPropertyAnimation(self.opacity_effect, b"opacity", self)
        self.anim.setDuration(220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("Update Available")
        title.setObjectName("updatePopupTitle")
        top.addWidget(title)
        top.addStretch(1)
        self.close_btn = QToolButton()
        self.close_btn.setText("✕")
        self.close_btn.setObjectName("popupCloseButton")
        self.close_btn.clicked.connect(self.hide_popup)
        top.addWidget(self.close_btn)
        layout.addLayout(top)

        self.message_label = QLabel("")
        self.message_label.setObjectName("updatePopupText")
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)

        row = QHBoxLayout()
        self.open_btn = QPushButton("Open Latest Release")
        self.open_btn.setObjectName("primaryButton")
        self.open_btn.clicked.connect(self.open_release)
        row.addWidget(self.open_btn)
        row.addStretch(1)
        layout.addLayout(row)

    def show_update(self, latest_version: str, release_url: str):
        self.release_url = release_url or GITHUB_RELEASES_PAGE
        self.message_label.setText(f"A newer version of WATCHDOG is available.\nCurrent: {APP_VERSION}   Latest: {latest_version}")
        self.reposition()
        self.setVisible(True)
        self.raise_()
        self.anim.stop()
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.start()

    def reposition(self):
        if not self.parent():
            return
        parent = self.parent().rect()
        margin = 18
        self.adjustSize()
        self.move(parent.right() - self.width() - margin, margin)

    def hide_popup(self):
        self.setVisible(False)

    def open_release(self):
        webbrowser.open(self.release_url or GITHUB_RELEASES_PAGE)


class WatchdogSuiteWindow(QMainWindow):
    update_check_up_to_date = Signal()
    update_check_available = Signal(str, str)
    update_check_failed = Signal()
    status_refresh_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1560, 940)
        self.setMinimumSize(1240, 780)
        self.config = AppConfig()
        self.additions = []
        self.issues = []
        self.selected_addition_id = None
        self.selected_issue_id = None
        self.server_proc = ManagedProcess("Server", self.log)
        self.headless_proc = ManagedProcess("Headless", self.log)
        self.monitor_running = False
        self.monitor_stop_event = threading.Event()
        self.monitor_thread = None
        self.next_restart_time = None
        self.restart_lock = threading.Lock()
        self.restart_in_progress = False
        self.pending_headless_start_id = 0
        self._headless_launch_lock = threading.Lock()
        self._shutting_down = False
        self.discord_last_monitor_stop_notice = False
        self.log_queue = queue.Queue()
        self.tray_icon = None
        self.latest_release_url = GITHUB_RELEASES_PAGE
        self.latest_version_seen = APP_VERSION
        self.update_flash_timer = None
        self.last_update_check_result = None
        self.last_update_check_time = 0.0

        self.update_check_up_to_date.connect(self._on_update_up_to_date)
        self.update_check_available.connect(self._on_update_available)
        self.update_check_failed.connect(self._on_update_failed)
        self.status_refresh_requested.connect(self.update_status_ui)

        self._build_ui()
        self._apply_styles()
        self._wire_nav()
        self._build_tray()
        self._maybe_migrate_legacy_config()
        self.load_config()
        self.load_additions()
        self.load_issues()
        self.clear_addition_form()
        self.clear_issue_form()
        self.refresh_addition_list()
        self.refresh_issue_list()
        self.update_status_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.process_log_queue)
        self.timer.start(100)
        QTimer.singleShot(1600, self.check_for_updates_silent)

    # ---------- Data persistence ----------
    def _maybe_migrate_legacy_config(self):
        if CONFIG_FILE.exists():
            return
        for candidate in get_legacy_config_candidates():
            try:
                if candidate.exists() and candidate.resolve() != CONFIG_FILE.resolve():
                    atomic_write_text(CONFIG_FILE, candidate.read_text(encoding="utf-8"), encoding="utf-8")
                    self.log(f"[config] migrated legacy config from {candidate}")
                    return
            except Exception:
                continue

    def _next_headless_launch_id(self) -> int:
        with self._headless_launch_lock:
            self.pending_headless_start_id += 1
            return self.pending_headless_start_id

    def _current_headless_launch_id(self) -> int:
        with self._headless_launch_lock:
            return self.pending_headless_start_id

    def save_json(self, path: Path, data):
        atomic_write_text(path, json.dumps(data, indent=2), encoding="utf-8")

    def load_json(self, path: Path, default):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.log(f"[storage] failed to load {path.name}: {exc}")
            return default

    def _coerce_loaded_config(self, raw):
        defaults = asdict(AppConfig())
        if not isinstance(raw, dict):
            return AppConfig()
        clean = defaults.copy()
        for key in defaults:
            if key in raw:
                clean[key] = raw[key]
        try:
            clean["server_path"] = str(clean.get("server_path", "") or "").strip()
            clean["headless_path"] = str(clean.get("headless_path", "") or "").strip()
            clean["server_args"] = str(clean.get("server_args", "") or "")
            clean["headless_args"] = str(clean.get("headless_args", "") or "")
            clean["server_workdir"] = str(clean.get("server_workdir", "") or "").strip()
            clean["headless_workdir"] = str(clean.get("headless_workdir", "") or "").strip()
            clean["headless_start_delay_sec"] = max(0, int(clean.get("headless_start_delay_sec", 10) or 0))
            clean["restart_interval_hours"] = max(0.1, float(clean.get("restart_interval_hours", 24.0) or 24.0))
            clean["monitor_interval_sec"] = max(1, int(clean.get("monitor_interval_sec", 5) or 5))
            clean["auto_restart_on_crash"] = bool(clean.get("auto_restart_on_crash", True))
            clean["auto_start_with_monitor"] = bool(clean.get("auto_start_with_monitor", True))
            clean["minimize_to_tray"] = bool(clean.get("minimize_to_tray", False))
            clean["start_with_windows"] = bool(clean.get("start_with_windows", False))
            clean["log_to_file"] = bool(clean.get("log_to_file", True))
            clean["discord_notifications_enabled"] = bool(clean.get("discord_notifications_enabled", False))
            clean["discord_webhook_url"] = str(clean.get("discord_webhook_url", "") or "").strip()
            clean["hide_server_console"] = bool(clean.get("hide_server_console", False))
            return AppConfig(**clean)
        except Exception as exc:
            self.log(f"[config] invalid config data detected; using defaults. Details: {exc}")
            return AppConfig()

    def load_additions(self):
        self.additions = self.load_json(ADDITIONS_FILE, [])

    def save_additions(self):
        self.save_json(ADDITIONS_FILE, self.additions)

    def load_issues(self):
        self.issues = self.load_json(ISSUES_FILE, [])

    def save_issues(self):
        self.save_json(ISSUES_FILE, self.issues)

    # ---------- Logging ----------
    def log(self, message: str):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_queue.put(f"{stamp} {message}")

    def process_log_queue(self):
        updated_boxes = set()
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.recent_log.appendPlainText(line)
                self.full_log.appendPlainText(line)
                updated_boxes.update({self.recent_log, self.full_log})
                if self.config.log_to_file:
                    append_log_line(LOG_FILE, line)
        except queue.Empty:
            pass
        for box in updated_boxes:
            box.moveCursor(QTextCursor.End)
        self.update_status_ui()

    # ---------- Discord ----------
    def _format_restart_time(self):
        return self.next_restart_time.strftime("%Y-%m-%d %H:%M:%S") if self.next_restart_time else "Not scheduled"

    def _post_discord_webhook(self, message: str, force=False):
        url = self.config.discord_webhook_url.strip()
        if not url:
            return False
        if not force and not self.config.discord_notifications_enabled:
            return False
        payload = json.dumps({"content": message}).encode("utf-8")
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json", "User-Agent": "WATCHDOG"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            status = getattr(response, "status", 204)
            return 200 <= status < 300

    def send_discord_webhook_async(self, message: str, force=False):
        def worker():
            try:
                ok = self._post_discord_webhook(message, force=force)
                if force and ok:
                    self.log("[discord] test webhook sent.")
            except urllib.error.HTTPError as e:
                self.log(f"[discord] webhook failed with HTTP {e.code}.")
            except Exception as e:
                self.log(f"[discord] webhook send failed: {e}")
        threading.Thread(target=worker, daemon=True).start()

    # ---------- UI build ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())
        self.stack = QStackedWidget()
        self.main_stack_parent = central
        root.addWidget(self.stack, 1)
        self.pages = [
            self._build_dashboard_page(),
            self._build_additions_page(),
            self._build_issues_page(),
            self._build_logs_page(),
            self._build_settings_page(),
        ]
        for p in self.pages:
            self.stack.addWidget(p)

        self.update_popup = UpdatePopup(self.main_stack_parent if hasattr(self, "main_stack_parent") else self.centralWidget())
        self.update_popup.resize(360, 122)
        self.update_popup.hide_popup()

    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(255)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 20, 18, 20)
        layout.setSpacing(12)
        brand = QLabel("WATCHDOG")
        brand.setObjectName("brandTitle")
        layout.addWidget(brand)
        subtitle = QLabel("Operations Suite")
        subtitle.setObjectName("brandSubtitle")
        layout.addWidget(subtitle)
        layout.addSpacing(10)
        self.nav_dashboard = NavButton("Dashboard")
        self.nav_additions = NavButton("Additions")
        self.nav_issues = NavButton("Issue Tracker")
        self.nav_logs = NavButton("Activity Log")
        self.nav_settings = NavButton("Settings")
        self.nav_dashboard.setChecked(True)
        self.nav_buttons = [self.nav_dashboard, self.nav_additions, self.nav_issues, self.nav_logs, self.nav_settings]
        for btn in self.nav_buttons:
            layout.addWidget(btn)
        layout.addStretch(1)
        footer = QFrame()
        footer.setObjectName("sidebarFooter")
        fl = QVBoxLayout(footer)
        fl.setContentsMargins(12, 12, 12, 12)
        fl.addWidget(self._label("Developed by NOCTIDE", "footerPrimary"))
        fl.addWidget(self._label(f"Version {APP_VERSION}", "footerSecondary"))
        fl.addWidget(self._label("Do not redistribute.", "footerSecondary"))
        layout.addWidget(footer)
        return sidebar

    def _page_shell(self, title: str, subtitle: str):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(16)
        layout.addWidget(self._label(title, "pageTitle"))
        sub = self._label(subtitle, "pageSubtitle")
        sub.setWordWrap(True)
        layout.addWidget(sub)
        return page, layout

    def _label(self, text, obj_name):
        lbl = QLabel(text)
        lbl.setObjectName(obj_name)
        return lbl

    def _today_short(self):
        return datetime.now().strftime("%#m/%#d/%y") if os.name == "nt" else datetime.now().strftime("%-m/%-d/%y")

    def _build_dashboard_page(self):
        page, layout = self._page_shell("WATCHDOG Dashboard", "Monitor status, restart timing, quick controls, and a concise operational summary.")
        row = QHBoxLayout(); row.setSpacing(14)
        self.server_card = StatusCard("Server", "Stopped", RED)
        self.headless_card = StatusCard("Headless", "Stopped", RED)
        self.monitor_card = StatusCard("Monitor", "Stopped", RED)
        self.restart_card = StatusCard("Next Restart", "Not scheduled", AMBER)
        for card in [self.server_card, self.headless_card, self.monitor_card, self.restart_card]:
            row.addWidget(card, 1)
        layout.addLayout(row)
        body = QHBoxLayout(); body.setSpacing(16)
        left = QVBoxLayout(); right = QVBoxLayout(); left.setSpacing(16); right.setSpacing(16)

        controls = SectionCard("Quick Controls")
        cg = QGridLayout(); cg.setHorizontalSpacing(10); cg.setVerticalSpacing(10)
        self.btn_start = QPushButton("Start Monitor"); self.btn_start.setObjectName("primaryButton"); self.btn_start.clicked.connect(self.start_monitor)
        self.btn_stop = QPushButton("Stop Monitor"); self.btn_stop.setObjectName("dangerButton"); self.btn_stop.clicked.connect(self.stop_monitor)
        self.btn_save = QPushButton("Save Config"); self.btn_save.setObjectName("secondaryButton"); self.btn_save.clicked.connect(self.save_config)
        self.btn_test = QPushButton("Test Discord"); self.btn_test.setObjectName("secondaryButton"); self.btn_test.clicked.connect(self.test_webhook)
        self.btn_load = QPushButton("Load Config"); self.btn_load.setObjectName("secondaryButton"); self.btn_load.clicked.connect(self.load_config)
        self.btn_open_logs = QPushButton("Open Log Folder"); self.btn_open_logs.setObjectName("secondaryButton"); self.btn_open_logs.clicked.connect(self.open_log_folder)
        self.btn_check_updates = QPushButton("Check for Updates"); self.btn_check_updates.setObjectName("secondaryButton"); self.btn_check_updates.clicked.connect(self.check_for_updates_manual)
        for i, btn in enumerate([self.btn_start, self.btn_stop, self.btn_save, self.btn_test, self.btn_load, self.btn_open_logs, self.btn_check_updates]):
            cg.addWidget(btn, i // 2, i % 2)
        controls.body.addLayout(cg)
        left.addWidget(controls)

        self.summary = SectionCard("Current Summary")
        self.summary_labels = [self._label("Monitor: Stopped", "summaryItem"), self._label("Discord: Disabled", "summaryItem"), self._label("Tray Mode: Disabled", "summaryItem"), self._label("Windows Startup: Disabled", "summaryItem")]
        for lbl in self.summary_labels:
            self.summary.body.addWidget(lbl)
        left.addWidget(self.summary)
        left.addStretch(1)

        recent = SectionCard("Recent Activity")
        self.recent_log = QPlainTextEdit(); self.recent_log.setReadOnly(True); self.recent_log.setObjectName("logBox")
        recent.body.addWidget(self.recent_log)
        right.addWidget(recent, 1)
        body.addLayout(left, 2); body.addLayout(right, 3)
        layout.addLayout(body, 1)
        return page

    def _build_additions_page(self):
        page, layout = self._page_shell("Additions", "Track mods, plugins, updates, and host-machine changes without relying on plain text files.")
        body = QHBoxLayout(); body.setSpacing(16)
        form_card = SectionCard("New / Edit Addition")
        form = QGridLayout(); form.setHorizontalSpacing(10); form.setVerticalSpacing(10); form.setColumnStretch(1, 1)
        self.add_title = QLineEdit(); self.add_date = QLineEdit(); self.add_type = QComboBox(); self.add_type.addItems(ADDITION_TYPES)
        self.additions_text = QTextEdit(); self.add_notes = QTextEdit()
        self.add_title.setPlaceholderText("Entry title"); self.add_date.setPlaceholderText("3/16/26")
        self.additions_text.setPlaceholderText("New additions..."); self.add_notes.setPlaceholderText("Notes...")
        widgets = [("Title", self.add_title), ("Date", self.add_date), ("Addition Type", self.add_type), ("New Additions", self.additions_text), ("Notes", self.add_notes)]
        for i, (lab, wid) in enumerate(widgets):
            form.addWidget(self._label(lab, "fieldLabel"), i, 0)
            form.addWidget(wid, i, 1)
        form_card.body.addLayout(form)
        row = QHBoxLayout()
        self.add_save = QPushButton("Save Entry"); self.add_save.setObjectName("primaryButton"); self.add_save.clicked.connect(self.save_addition)
        self.add_new = QPushButton("New Blank"); self.add_new.setObjectName("secondaryButton"); self.add_new.clicked.connect(self.clear_addition_form)
        self.add_delete = QPushButton("Delete Selected"); self.add_delete.setObjectName("dangerButton"); self.add_delete.clicked.connect(self.delete_addition)
        self.add_export = QPushButton("Export TXT"); self.add_export.setObjectName("secondaryButton"); self.add_export.clicked.connect(self.export_addition)
        for btn in [self.add_save, self.add_new, self.add_delete, self.add_export]:
            row.addWidget(btn)
        form_card.body.addLayout(row)
        history = SectionCard("Addition History")
        self.addition_list = QListWidget(); self.addition_list.setObjectName("historyList"); self.addition_list.itemClicked.connect(self.open_addition)
        history.body.addWidget(self.addition_list)
        body.addWidget(form_card, 3); body.addWidget(history, 2)
        layout.addLayout(body, 1)
        return page

    def _build_issues_page(self):
        page, layout = self._page_shell("Issue Tracker", "Log issues, recent additions, and successful fixes in one place.")
        body = QHBoxLayout(); body.setSpacing(16)
        form_card = SectionCard("New / Edit Issue")
        form = QGridLayout(); form.setHorizontalSpacing(10); form.setVerticalSpacing(10); form.setColumnStretch(1, 1)
        self.issue_title = QLineEdit(); self.issue_date = QLineEdit(); self.issue_type = QComboBox(); self.issue_type.addItems(ISSUE_TYPES)
        self.issue_desc = QTextEdit(); self.issue_recent = QTextEdit(); self.issue_fix = QTextEdit()
        self.issue_title.setPlaceholderText("Issue title"); self.issue_date.setPlaceholderText("3/16/26")
        self.issue_desc.setPlaceholderText("Issue description..."); self.issue_recent.setPlaceholderText("Most recently installed items..."); self.issue_fix.setPlaceholderText("Fix or resolution...")
        widgets = [("Title", self.issue_title), ("Date", self.issue_date), ("Issue Type", self.issue_type), ("Issue Description", self.issue_desc), ("Recent Items", self.issue_recent), ("Fix / Resolution", self.issue_fix)]
        for i, (lab, wid) in enumerate(widgets):
            form.addWidget(self._label(lab, "fieldLabel"), i, 0)
            form.addWidget(wid, i, 1)
        form_card.body.addLayout(form)
        row = QHBoxLayout()
        self.issue_save = QPushButton("Save Issue"); self.issue_save.setObjectName("primaryButton"); self.issue_save.clicked.connect(self.save_issue)
        self.issue_new = QPushButton("New Blank"); self.issue_new.setObjectName("secondaryButton"); self.issue_new.clicked.connect(self.clear_issue_form)
        self.issue_delete = QPushButton("Delete Selected"); self.issue_delete.setObjectName("dangerButton"); self.issue_delete.clicked.connect(self.delete_issue)
        self.issue_export = QPushButton("Export TXT"); self.issue_export.setObjectName("secondaryButton"); self.issue_export.clicked.connect(self.export_issue)
        for btn in [self.issue_save, self.issue_new, self.issue_delete, self.issue_export]:
            row.addWidget(btn)
        form_card.body.addLayout(row)
        history = SectionCard("Issue History")
        self.issue_list = QListWidget(); self.issue_list.setObjectName("historyList"); self.issue_list.itemClicked.connect(self.open_issue)
        history.body.addWidget(self.issue_list)
        body.addWidget(form_card, 3); body.addWidget(history, 2)
        layout.addLayout(body, 1)
        return page

    def _build_logs_page(self):
        page, layout = self._page_shell("Activity Log", "Operational output and watchdog events.")
        actions = QHBoxLayout()
        btn_open = QPushButton("Open Log Folder"); btn_open.setObjectName("secondaryButton"); btn_open.clicked.connect(self.open_log_folder)
        btn_clear = QPushButton("Clear View"); btn_clear.setObjectName("secondaryButton"); btn_clear.clicked.connect(lambda: (self.recent_log.clear(), self.full_log.clear()))
        actions.addWidget(btn_open)
        actions.addWidget(btn_clear)
        actions.addStretch(1)
        layout.addLayout(actions)
        card = SectionCard("Live Log Output")
        self.full_log = QPlainTextEdit(); self.full_log.setReadOnly(True); self.full_log.setObjectName("logBox")
        card.body.addWidget(self.full_log)
        layout.addWidget(card, 1)
        return page

    def _build_settings_page(self):
        page, layout = self._page_shell("Settings", "Paths, runtime behavior, tray options, startup behavior, and Discord configuration.")
        body = QHBoxLayout(); body.setSpacing(16)
        left = QVBoxLayout(); right = QVBoxLayout(); left.setSpacing(16); right.setSpacing(16)

        paths = SectionCard("Paths")
        grid = QGridLayout(); grid.setHorizontalSpacing(10); grid.setVerticalSpacing(10); grid.setColumnStretch(1, 1)
        self.server_path = QLineEdit(); self.headless_path = QLineEdit(); self.server_workdir = QLineEdit(); self.headless_workdir = QLineEdit()
        rows = [("Server Executable", self.server_path, self.browse_server), ("Headless Executable", self.headless_path, self.browse_headless), ("Server Working Directory", self.server_workdir, None), ("Headless Working Directory", self.headless_workdir, None)]
        for i, (label, widget, handler) in enumerate(rows):
            grid.addWidget(self._label(label, "fieldLabel"), i, 0)
            grid.addWidget(widget, i, 1)
            if handler:
                btn = QPushButton("Browse"); btn.setObjectName("secondaryButton"); btn.clicked.connect(handler); grid.addWidget(btn, i, 2)
        paths.body.addLayout(grid)
        left.addWidget(paths)

        runtime = SectionCard("Runtime Settings")
        rt = QGridLayout(); rt.setHorizontalSpacing(10); rt.setVerticalSpacing(10)
        self.headless_delay = QSpinBox(); self.headless_delay.setRange(0, 9999)
        self.restart_hours = QDoubleSpinBox(); self.restart_hours.setRange(0.1, 9999.0); self.restart_hours.setDecimals(2)
        self.monitor_interval = QSpinBox(); self.monitor_interval.setRange(1, 9999)
        vals = [("Headless Start Delay (seconds)", self.headless_delay), ("Scheduled Restart Interval (hours)", self.restart_hours), ("Monitor Check Interval (seconds)", self.monitor_interval)]
        for i, (label, spin) in enumerate(vals):
            rt.addWidget(self._label(label, "fieldLabel"), i, 0)
            rt.addWidget(spin, i, 1)
        runtime.body.addLayout(rt)
        left.addWidget(runtime)
        left.addStretch(1)

        options = SectionCard("Options")
        self.chk_auto_restart = QCheckBox("Auto restart crashed processes")
        self.chk_minimize = QCheckBox("Minimize to tray")
        self.chk_start_windows = QCheckBox("Start with Windows")
        self.chk_log_file = QCheckBox("Write log file")
        self.chk_hide_server_console = QCheckBox("Auto-minimize process windows (beta)")
        note = self._label("Tries to minimize Server and Headless windows after launch.", "helperText")
        note.setWordWrap(True)
        for chk in [self.chk_auto_restart, self.chk_minimize, self.chk_start_windows, self.chk_log_file, self.chk_hide_server_console]:
            options.body.addWidget(chk)
        options.body.addWidget(note)
        right.addWidget(options)

        discord = SectionCard("Discord")
        self.chk_discord = QCheckBox("Enable Discord webhook notifications")
        self.discord_webhook = QLineEdit(); self.discord_webhook.setPlaceholderText("Paste Discord webhook URL here")
        btn = QPushButton("Test Webhook"); btn.setObjectName("secondaryButton"); btn.clicked.connect(self.test_webhook)
        note = self._label("Webhook URL stays local to this machine.", "helperText")
        note.setWordWrap(True)
        discord.body.addWidget(self.chk_discord); discord.body.addWidget(self.discord_webhook); discord.body.addWidget(btn); discord.body.addWidget(note)
        right.addWidget(discord)
        right.addStretch(1)
        body.addLayout(left, 3); body.addLayout(right, 2)
        layout.addLayout(body, 1)
        return page

    # ---------- UI helpers ----------
    def _wire_nav(self):
        mapping = {self.nav_dashboard: 0, self.nav_additions: 1, self.nav_issues: 2, self.nav_logs: 3, self.nav_settings: 4}
        for btn, idx in mapping.items():
            btn.clicked.connect(lambda checked=False, b=btn, i=idx: self._switch_page(b, i))

    def _switch_page(self, active_btn, index):
        for btn in self.nav_buttons:
            btn.setChecked(btn is active_btn)
        self.stack.setCurrentIndex(index)

    def _build_tray(self):
        icon = QIcon(resource_path("watchdog.ico")) if os.path.exists(resource_path("watchdog.ico")) else self.windowIcon()
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip(APP_NAME)
        menu = QMenu(self)
        act_show = QAction("Show", self); act_show.triggered.connect(self.show_from_tray)
        act_start = QAction("Start Monitor", self); act_start.triggered.connect(self.tray_start_monitor)
        act_stop = QAction("Stop Monitor", self); act_stop.triggered.connect(self.tray_stop_monitor)
        act_exit = QAction("Exit", self); act_exit.triggered.connect(self.tray_exit)
        for act in [act_show, act_start, act_stop, act_exit]:
            menu.addAction(act)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(lambda reason: self.show_from_tray() if reason == QSystemTrayIcon.Trigger else None)

    def _apply_styles(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; font-family: 'Segoe UI'; font-size: 13px; }}
            QLabel {{ background: transparent; color: {TEXT}; }}
            #sidebar {{ background: {SIDEBAR}; border-right: 1px solid {BORDER_SOFT}; }}
            #brandTitle {{ font-size: 24px; font-weight: 800; color: {TEXT}; background: transparent; }}
            #brandSubtitle, #footerSecondary, #pageSubtitle, #helperText {{ color: {MUTED}; font-size: 12px; background: transparent; }}
            #footerPrimary {{ color: #E4B58C; font-size: 12px; font-weight: 700; background: transparent; }}
            #sidebarFooter {{ background: transparent; border: none; }}
            #navButton {{ background: {PANEL}; border: 1px solid {BORDER_SOFT}; border-radius: 12px; text-align: left; padding: 11px 14px; color: {TEXT}; }}
            #navButton:hover {{ background: {PANEL_ALT}; border-color: {ACCENT}; }}
            #navButton:checked {{ background: {PANEL_SOFT}; border: 1px solid {ACCENT}; color: white; }}
            #pageTitle {{ font-size: 28px; font-weight: 800; color: {TEXT}; background: transparent; }}
            #statusCard, #sectionCard {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 16px; }}
            #statusInner, #sectionInner {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {PANEL_ALT}, stop:1 {PANEL}); border: none; border-radius: 15px; }}
            #cardTitle {{ color: {MUTED}; font-size: 11px; font-weight: 700; letter-spacing: 0.8px; background: transparent; }}
            #cardValue {{ color: {TEXT}; font-size: 23px; font-weight: 800; background: transparent; }}
            #sectionTitle {{ color: #E0A06A; font-size: 12px; font-weight: 800; letter-spacing: 0.8px; background: transparent; }}
            #summaryItem, #fieldLabel {{ color: {TEXT}; font-size: 13px; background: transparent; }}
            QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QListWidget {{ background: {PANEL_ALT}; border: 1px solid {BORDER_SOFT}; border-radius: 10px; padding: 10px 12px; color: {TEXT}; selection-background-color: {ACCENT}; }}
            QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {ACCENT}; }}
            QListWidget::item {{ padding: 8px; border-bottom: 1px solid #241711; background: transparent; }}
            QListWidget::item:selected {{ background: #2A1D15; border: 1px solid {ACCENT}; }}
            QCheckBox {{ spacing: 10px; color: {TEXT}; background: transparent; }}
            QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER}; background: {PANEL_ALT}; }}
            QCheckBox::indicator:checked {{ background: {ACCENT}; border: 1px solid {ACCENT_HOVER}; }}
            QPushButton {{ background: {PANEL_ALT}; border: 1px solid {BORDER_SOFT}; border-radius: 10px; padding: 11px 14px; color: {TEXT}; font-weight: 600; }}
            QPushButton:hover {{ border-color: {ACCENT}; }}
            #primaryButton {{ background: {ACCENT}; border: 1px solid {ACCENT_HOVER}; color: white; }}
            #primaryButton:hover {{ background: {ACCENT_HOVER}; }}
            #dangerButton {{ background: #7D332E; border: 1px solid #A84E47; color: white; }}
            #dangerButton:hover {{ background: #8B3B36; }}
            #secondaryButton {{ background: #211915; border: 1px solid {BORDER}; color: {TEXT}; }}
            #secondaryButton:hover {{ background: #2A201B; border-color: {ACCENT}; }}
            #logBox {{ background: #15110F; border: 1px solid #261812; border-radius: 12px; font-family: 'Consolas'; font-size: 12px; color: #E6DDD3; }}
            #updatePopup {{ background: rgba(18, 15, 13, 0.96); border: 1px solid {ACCENT}; border-radius: 14px; }}
            #updatePopupTitle {{ color: {TEXT}; font-size: 14px; font-weight: 800; background: transparent; }}
            #updatePopupText {{ color: {MUTED}; font-size: 12px; background: transparent; }}
            #popupCloseButton {{ background: transparent; border: none; color: {MUTED}; font-size: 13px; padding: 0 4px; }}
            #popupCloseButton:hover {{ color: {TEXT}; }}
        """)

    # ---------- Config ----------
    def snapshot_config_from_ui(self):
        return AppConfig(
            server_path=self.server_path.text().strip(),
            headless_path=self.headless_path.text().strip(),
            server_args="",
            headless_args="",
            server_workdir=self.server_workdir.text().strip(),
            headless_workdir=self.headless_workdir.text().strip(),
            headless_start_delay_sec=int(self.headless_delay.value()),
            restart_interval_hours=float(self.restart_hours.value()),
            monitor_interval_sec=int(self.monitor_interval.value()),
            auto_restart_on_crash=self.chk_auto_restart.isChecked(),
            auto_start_with_monitor=True,
            minimize_to_tray=self.chk_minimize.isChecked(),
            start_with_windows=self.chk_start_windows.isChecked(),
            log_to_file=self.chk_log_file.isChecked(),
            discord_notifications_enabled=self.chk_discord.isChecked(),
            discord_webhook_url=self.discord_webhook.text().strip(),
            hide_server_console=self.chk_hide_server_console.isChecked(),
        )

    def collect_config_from_ui(self):
        try:
            cfg = self.snapshot_config_from_ui()
        except ValueError:
            QMessageBox.critical(self, APP_NAME, "One or more numeric fields are invalid.")
            return False
        if cfg.headless_start_delay_sec < 0 or cfg.restart_interval_hours <= 0 or cfg.monitor_interval_sec <= 0:
            QMessageBox.critical(self, APP_NAME, "Delay and intervals must be greater than zero.")
            return False
        self.config = cfg
        return True

    def apply_config_to_ui(self):
        self.server_path.setText(self.config.server_path)
        self.headless_path.setText(self.config.headless_path)
        self.server_workdir.setText(self.config.server_workdir)
        self.headless_workdir.setText(self.config.headless_workdir)
        self.headless_delay.setValue(self.config.headless_start_delay_sec)
        self.restart_hours.setValue(self.config.restart_interval_hours)
        self.monitor_interval.setValue(self.config.monitor_interval_sec)
        self.chk_auto_restart.setChecked(self.config.auto_restart_on_crash)
        self.chk_minimize.setChecked(self.config.minimize_to_tray)
        self.chk_start_windows.setChecked(self.config.start_with_windows)
        self.chk_log_file.setChecked(self.config.log_to_file)
        self.chk_hide_server_console.setChecked(self.config.hide_server_console)
        self.chk_discord.setChecked(self.config.discord_notifications_enabled)
        self.discord_webhook.setText(self.config.discord_webhook_url)

    def save_config(self):
        try:
            self.config = self.snapshot_config_from_ui()
            atomic_write_text(CONFIG_FILE, json.dumps(asdict(self.config), indent=2), encoding="utf-8")
            self.apply_startup_setting(silent=True, skip_collect=True)
            self.update_status_ui()
            self.log(f"[config] saved to {CONFIG_FILE}")
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Failed to save config:\n{e}")

    def load_config(self):
        if CONFIG_FILE.exists():
            try:
                raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                self.config = self._coerce_loaded_config(raw)
            except Exception as exc:
                self.log(f"[config] failed to load config; using defaults. Details: {exc}")
                self.config = AppConfig()
        else:
            self.config = AppConfig()
        self.apply_config_to_ui()
        self.update_status_ui()
        self.log("[config] loaded.")

    def apply_startup_setting(self, silent=False, skip_collect=False):
        if not skip_collect and not self.collect_config_from_ui():
            return
        if os.name != "nt" or winreg is None:
            return
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        entry_name = APP_NAME
        cmd = f'"{Path(sys.executable).resolve()}"' if getattr(sys, "frozen", False) else f'"{sys.executable}" "{Path(__file__).resolve()}"'
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                if self.config.start_with_windows:
                    winreg.SetValueEx(key, entry_name, 0, winreg.REG_SZ, cmd)
                    self.log("[startup] enabled Start with Windows.")
                else:
                    try:
                        winreg.DeleteValue(key, entry_name)
                        self.log("[startup] disabled Start with Windows.")
                    except FileNotFoundError:
                        pass
        except Exception as e:
            if not silent:
                QMessageBox.critical(self, APP_NAME, f"Failed to update Windows startup:\n{e}")

    # ---------- Paths ----------
    def browse_server(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Server EXE")
        if path:
            self.server_path.setText(path)
            if not self.server_workdir.text().strip():
                self.server_workdir.setText(str(Path(path).parent))

    def browse_headless(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Headless EXE")
        if path:
            self.headless_path.setText(path)
            if not self.headless_workdir.text().strip():
                self.headless_workdir.setText(str(Path(path).parent))

    # ---------- Additions ----------
    def refresh_addition_list(self):
        self.addition_list.clear()
        for entry in sorted(self.additions, key=lambda x: x["created_at"], reverse=True):
            item = QListWidgetItem(f'{entry["entry_date"]} — {entry["addition_type"]} — {entry["title"]}')
            item.setData(Qt.UserRole, entry["created_at"])
            self.addition_list.addItem(item)

    def clear_addition_form(self):
        self.selected_addition_id = None
        self.add_title.clear(); self.add_date.setText(self._today_short()); self.additions_text.clear(); self.add_notes.clear(); self.add_type.setCurrentIndex(0)

    def save_addition(self):
        entry_id = self.selected_addition_id or datetime.now().isoformat(timespec="seconds")
        entry = asdict(AdditionEntry(entry_id, self.add_title.text().strip() or "Untitled Entry", self.add_date.text().strip() or datetime.now().strftime("%m/%d/%y"), self.add_type.currentText(), self.additions_text.toPlainText().strip(), self.add_notes.toPlainText().strip()))
        self.additions = [e for e in self.additions if e["created_at"] != entry_id]
        self.additions.append(entry)
        self.selected_addition_id = entry_id
        self.save_additions(); self.refresh_addition_list(); self.log(f"[additions] saved entry '{entry['title']}'.")

    def open_addition(self, item):
        entry_id = item.data(Qt.UserRole)
        entry = next((e for e in self.additions if e["created_at"] == entry_id), None)
        if not entry:
            return
        self.selected_addition_id = entry_id
        self.add_title.setText(entry["title"]); self.add_date.setText(entry["entry_date"]); self.add_type.setCurrentText(entry["addition_type"]); self.additions_text.setPlainText(entry["additions"]); self.add_notes.setPlainText(entry["notes"])

    def delete_addition(self):
        if not self.selected_addition_id:
            return
        self.additions = [e for e in self.additions if e["created_at"] != self.selected_addition_id]
        self.save_additions(); self.refresh_addition_list(); self.clear_addition_form(); self.log("[additions] deleted selected entry.")

    def export_addition(self):
        if not self.selected_addition_id:
            return
        entry = next((e for e in self.additions if e["created_at"] == self.selected_addition_id), None)
        if not entry:
            return
        safe = "".join(c if c.isalnum() or c in ("_", "-", " ") else "_" for c in entry["title"]).strip() or "entry"
        path = EXPORT_DIR / f"{safe}.txt"
        path.write_text(f"{APP_NAME}\n\nDate: {entry['entry_date']}\nTitle: {entry['title']}\nType: {entry['addition_type']}\n\nNEW ADDITIONS:\n{entry['additions'] or 'None'}\n\nNOTES:\n{entry['notes'] or 'None'}\n", encoding="utf-8")
        self.log(f"[additions] exported to {path.name}.")

    # ---------- Issues ----------
    def refresh_issue_list(self):
        self.issue_list.clear()
        for entry in sorted(self.issues, key=lambda x: x["created_at"], reverse=True):
            item = QListWidgetItem(f'{entry["issue_date"]} — {entry["issue_type"]} — {entry["title"]}')
            item.setData(Qt.UserRole, entry["created_at"])
            self.issue_list.addItem(item)

    def clear_issue_form(self):
        self.selected_issue_id = None
        self.issue_title.clear(); self.issue_date.setText(self._today_short()); self.issue_desc.clear(); self.issue_recent.clear(); self.issue_fix.clear(); self.issue_type.setCurrentIndex(0)

    def save_issue(self):
        entry_id = self.selected_issue_id or datetime.now().isoformat(timespec="seconds")
        entry = asdict(IssueEntry(entry_id, self.issue_title.text().strip() or "Untitled Issue", self.issue_date.text().strip() or datetime.now().strftime("%m/%d/%y"), self.issue_type.currentText(), self.issue_desc.toPlainText().strip(), self.issue_recent.toPlainText().strip(), self.issue_fix.toPlainText().strip()))
        self.issues = [e for e in self.issues if e["created_at"] != entry_id]
        self.issues.append(entry)
        self.selected_issue_id = entry_id
        self.save_issues(); self.refresh_issue_list(); self.log(f"[issues] saved issue '{entry['title']}'.")

    def open_issue(self, item):
        entry_id = item.data(Qt.UserRole)
        entry = next((e for e in self.issues if e["created_at"] == entry_id), None)
        if not entry:
            return
        self.selected_issue_id = entry_id
        self.issue_title.setText(entry["title"]); self.issue_date.setText(entry["issue_date"]); self.issue_type.setCurrentText(entry["issue_type"]); self.issue_desc.setPlainText(entry["description"]); self.issue_recent.setPlainText(entry["recent_items"]); self.issue_fix.setPlainText(entry["fix_notes"])

    def delete_issue(self):
        if not self.selected_issue_id:
            return
        self.issues = [e for e in self.issues if e["created_at"] != self.selected_issue_id]
        self.save_issues(); self.refresh_issue_list(); self.clear_issue_form(); self.log("[issues] deleted selected issue.")

    def export_issue(self):
        if not self.selected_issue_id:
            return
        entry = next((e for e in self.issues if e["created_at"] == self.selected_issue_id), None)
        if not entry:
            return
        safe = "".join(c if c.isalnum() or c in ("_", "-", " ") else "_" for c in entry["title"]).strip() or "issue"
        path = EXPORT_DIR / f"issue_{safe}.txt"
        path.write_text(f"{APP_NAME} - ISSUE TRACKER\n\nDate: {entry['issue_date']}\nTitle: {entry['title']}\nType: {entry['issue_type']}\n\nISSUE DESCRIPTION:\n{entry['description'] or 'None'}\n\nMOST RECENTLY INSTALLED ITEMS:\n{entry['recent_items'] or 'None'}\n\nFIX / RESOLUTION:\n{entry['fix_notes'] or 'None'}\n", encoding="utf-8")
        self.log(f"[issues] exported to {path.name}.")

    # ---------- Watchdog ----------
    def _start_headless_with_delay(self):
        if not self.config.headless_path.strip():
            self.log("[Headless] no executable configured. Skipping headless launch.")
            return
        launch_id = self._next_headless_launch_id()
        delay = max(0, self.config.headless_start_delay_sec)
        if delay:
            self.log(f"[Headless] waiting {delay}s before start...")
            end_time = time.time() + delay
            while time.time() < end_time:
                if launch_id != self._current_headless_launch_id() or self.monitor_stop_event.is_set() or self._shutting_down:
                    self.log("[Headless] delayed start cancelled.")
                    return
                time.sleep(0.25)
        if launch_id != self._current_headless_launch_id():
            self.log("[Headless] delayed start superseded.")
            return
        started = self.headless_proc.start(self.config.headless_path, self.config.headless_workdir)
        if started and self.config.hide_server_console and self.headless_proc.process is not None:
            minimize_windows_for_pid(self.headless_proc.process.pid, self.log, "Headless")

    def _start_server_with_minimize(self):
        if not self.config.server_path.strip():
            self.log("[Server] no executable configured. Skipping server launch.")
            return
        started = self.server_proc.start(self.config.server_path, self.config.server_workdir, hide_window=False, capture_output=False)
        if started and self.config.hide_server_console and self.server_proc.process is not None:
            minimize_windows_for_pid(self.server_proc.process.pid, self.log, "Server")

    def start_server(self):
        if not self.collect_config_from_ui():
            return
        threading.Thread(target=self._start_server_with_minimize, daemon=True).start()

    def start_headless(self):
        if not self.collect_config_from_ui():
            return
        threading.Thread(target=self._start_headless_with_delay, daemon=True).start()

    def start_both(self):
        if not self.collect_config_from_ui():
            return
        threading.Thread(target=self._start_both_impl, daemon=True).start()

    def stop_server(self):
        threading.Thread(target=self.server_proc.stop, daemon=True).start()

    def stop_headless(self):
        self._next_headless_launch_id()
        threading.Thread(target=self.headless_proc.stop, daemon=True).start()

    def stop_both(self):
        threading.Thread(target=self._stop_both_impl, daemon=True).start()

    def restart_both(self):
        if not self.collect_config_from_ui():
            return
        threading.Thread(target=self._restart_both_impl, daemon=True).start()

    def _start_both_impl(self):
        self._start_server_with_minimize()
        threading.Thread(target=self._start_headless_with_delay, daemon=True).start()

    def _stop_both_impl(self):
        self._next_headless_launch_id()
        self.headless_proc.stop()
        self.server_proc.stop()

    def _restart_both_impl(self):
        if not self.restart_lock.acquire(blocking=False):
            self.log("[system] restart already in progress. Skipping duplicate request.")
            return
        self.restart_in_progress = True
        self._next_headless_launch_id()
        try:
            self.log("[system] restarting both processes...")
            self.send_discord_webhook_async(f"[{APP_NAME}] Scheduled restart beginning now.")
            self.headless_proc.stop_requested = True
            self.server_proc.stop_requested = True
            self.headless_proc.stop(); time.sleep(2); self.server_proc.stop(); time.sleep(5)
            self._start_server_with_minimize()
            self._next_headless_launch_id()
            threading.Thread(target=self._start_headless_with_delay, daemon=True).start()
            self.next_restart_time = datetime.now() + timedelta(hours=self.config.restart_interval_hours)
            self.send_discord_webhook_async(f"[{APP_NAME}] Scheduled restart completed. Next scheduled restart: {self._format_restart_time()}")
        finally:
            self.server_proc.stop_requested = False
            self.headless_proc.stop_requested = False
            self.restart_in_progress = False
            self.restart_lock.release()
            self.status_refresh_requested.emit()

    def start_monitor(self):
        if self.monitor_running:
            self.log("[monitor] already running.")
            return
        if not self.collect_config_from_ui():
            return
        self._shutting_down = False
        self.monitor_running = True
        self.monitor_stop_event.clear()
        now = datetime.now()
        self.last_scheduled_restart = now
        self.next_restart_time = now + timedelta(hours=self.config.restart_interval_hours)
        self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.monitor_thread.start()
        self.discord_last_monitor_stop_notice = False
        self.log("[monitor] started.")
        self.log(f"[monitor] next scheduled restart at {self.next_restart_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.send_discord_webhook_async(f"[{APP_NAME}] Monitor started. Next scheduled restart: {self._format_restart_time()}")
        threading.Thread(target=self._start_both_impl, daemon=True).start()
        self._schedule_auto_minimize_to_tray()

    def _schedule_auto_minimize_to_tray(self):
        if not self.config.minimize_to_tray:
            return
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.log("[tray] system tray unavailable. Auto-minimize skipped.")
            return
        QTimer.singleShot(1500, self._auto_minimize_to_tray_if_running)

    def _auto_minimize_to_tray_if_running(self):
        if self.config.minimize_to_tray and self.monitor_running:
            self.hide_to_tray()

    def stop_monitor(self):
        if not self.monitor_running and not self.server_proc.is_running() and not self.headless_proc.is_running():
            return
        self.monitor_running = False
        self.monitor_stop_event.set()
        self._next_headless_launch_id()
        self.next_restart_time = None
        self.log("[monitor] stopping...")
        if not self.discord_last_monitor_stop_notice:
            self.discord_last_monitor_stop_notice = True
            self.send_discord_webhook_async(f"[{APP_NAME}] Monitor stopping.")
        threading.Thread(target=self._stop_both_impl, daemon=True).start()
        self.update_status_ui()

    def monitor_loop(self):
        while not self.monitor_stop_event.is_set():
            try:
                if not self.restart_in_progress:
                    if self.config.auto_restart_on_crash:
                        if self.server_proc.process is not None and not self.server_proc.is_running() and not self.server_proc.stop_requested:
                            if self.server_proc.allow_auto_restart():
                                exit_code = self.server_proc.get_exit_code()
                                self.log(f"[monitor] server stopped unexpectedly (exit={exit_code}). Restarting...")
                                self.send_discord_webhook_async(f"[{APP_NAME}] Server stopped unexpectedly. Auto-restart triggered.")
                                self._start_server_with_minimize()
                        if self.headless_proc.process is not None and not self.headless_proc.is_running() and not self.headless_proc.stop_requested:
                            if self.headless_proc.allow_auto_restart():
                                exit_code = self.headless_proc.get_exit_code()
                                self.log(f"[monitor] headless stopped unexpectedly (exit={exit_code}). Restarting...")
                                self.send_discord_webhook_async(f"[{APP_NAME}] Headless stopped unexpectedly. Auto-restart triggered.")
                                threading.Thread(target=self._start_headless_with_delay, daemon=True).start()
                    if self.next_restart_time and datetime.now() >= self.next_restart_time:
                        self.log("[monitor] scheduled restart interval reached.")
                        threading.Thread(target=self._restart_both_impl, daemon=True).start()
                self.status_refresh_requested.emit()
            except Exception as e:
                self.log(f"[monitor] error: {e}")
            time.sleep(max(1, self.config.monitor_interval_sec))
        self.monitor_running = False
        self.status_refresh_requested.emit()
        self.log("[monitor] stopped.")
        if not self.discord_last_monitor_stop_notice:
            self.discord_last_monitor_stop_notice = True
            self.send_discord_webhook_async(f"[{APP_NAME}] Monitor stopped.")

    def update_status_ui(self):
        server_running = self.server_proc.is_running()
        headless_running = self.headless_proc.is_running()
        monitor_running = self.monitor_running
        self.server_card.set_status("Running" if server_running else "Stopped", GREEN if server_running else RED)
        self.headless_card.set_status("Running" if headless_running else "Stopped", GREEN if headless_running else RED)
        self.monitor_card.set_status("Running" if monitor_running else "Stopped", GREEN if monitor_running else RED)
        self.restart_card.set_status(self._format_restart_time(), AMBER if self.next_restart_time else MUTED)
        self.summary_labels[0].setText(f"Monitor: {'Running' if monitor_running else 'Stopped'}")
        self.summary_labels[1].setText(f"Discord: {'Enabled' if self.config.discord_notifications_enabled else 'Disabled'}")
        self.summary_labels[2].setText(f"Tray Mode: {'Enabled' if self.config.minimize_to_tray else 'Disabled'}")
        self.summary_labels[3].setText(f"Windows Startup: {'Enabled' if self.config.start_with_windows else 'Disabled'}")

    def tray_start_monitor(self):
        self.start_monitor()

    def tray_stop_monitor(self):
        self.stop_monitor()

    def tray_exit(self):
        self.exit_app()

    # ---------- Updates ----------
    def _flash_update_button_green(self):
        if not hasattr(self, "btn_check_updates"):
            return
        self.btn_check_updates.setStyleSheet(
            "QPushButton { background: #2E6B43; border: 1px solid #57C67F; border-radius: 10px; padding: 11px 14px; color: white; font-weight: 700; }"
            "QPushButton:hover { background: #377D4F; border: 1px solid #6ED890; }"
        )
        if self.update_flash_timer is not None:
            self.update_flash_timer.stop()
            self.update_flash_timer.deleteLater()
        self.update_flash_timer = QTimer(self)
        self.update_flash_timer.setSingleShot(True)
        self.update_flash_timer.timeout.connect(self._reset_update_button_style)
        self.update_flash_timer.start(1200)

    def _reset_update_button_style(self):
        if hasattr(self, "btn_check_updates"):
            self.btn_check_updates.setStyleSheet("")

    def _on_update_up_to_date(self):
        self._flash_update_button_green()

    def _on_update_available(self, latest: str, release_url: str):
        self._reset_update_button_style()
        self.update_popup.show_update(latest, release_url)

    def _on_update_failed(self):
        self._reset_update_button_style()
        QMessageBox.warning(self, APP_NAME, "Could not check for updates right now.")

    def _parse_version_tuple(self, version_text: str):
        cleaned = (version_text or "").strip().lstrip("vV")
        parts = []
        for part in cleaned.split("."):
            digits = ''.join(ch for ch in part if ch.isdigit())
            parts.append(int(digits or 0))
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])

    def _github_headers(self):
        return {
            "User-Agent": f"{APP_NAME}/{APP_VERSION} (+https://github.com/{GITHUB_OWNER}/{GITHUB_REPO})",
            "Accept": "application/vnd.github+json",
        }

    def _extract_tag_from_url(self, url: str) -> str:
        try:
            path = urllib.parse.urlparse(url).path
            marker = "/releases/tag/"
            if marker in path:
                return path.split(marker, 1)[1].strip("/")
        except Exception:
            pass
        return ""

    def _fetch_latest_release_api(self):
        req = urllib.request.Request(GITHUB_LATEST_API, headers=self._github_headers())
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = (data.get("tag_name") or data.get("name") or "").strip()
        release_url = data.get("html_url") or GITHUB_RELEASES_PAGE
        if not latest:
            raise RuntimeError("Latest release response did not include a tag name.")
        return latest, release_url

    def _fetch_latest_release_redirect(self):
        req = urllib.request.Request(GITHUB_RELEASES_PAGE, headers=self._github_headers())
        with urllib.request.urlopen(req, timeout=12) as resp:
            final_url = resp.geturl()
            html = resp.read().decode("utf-8", errors="ignore")
        latest = self._extract_tag_from_url(final_url)
        if not latest:
            match = re.search(r'/releases/tag/([^"\'#?<>\\s]+)', html)
            if match:
                latest = match.group(1)
        if not latest:
            raise RuntimeError("Could not determine latest release tag from redirect fallback.")
        release_url = final_url if "/releases/tag/" in final_url else GITHUB_RELEASES_PAGE
        return latest, release_url

    def _fetch_latest_release(self):
        errors = []
        for fetcher in (self._fetch_latest_release_api, self._fetch_latest_release_redirect):
            try:
                return fetcher()
            except Exception as exc:
                errors.append(str(exc))
        raise RuntimeError(" | ".join(errors) if errors else "Unknown update check failure.")

    def _run_update_check(self, silent=False):
        now_ts = time.time()
        if self.last_update_check_result and (now_ts - self.last_update_check_time) < 120:
            result = self.last_update_check_result
            status = result.get("status")
            if status == "available":
                self.update_check_available.emit(result["latest"], result["release_url"])
            elif status == "up_to_date":
                if not silent:
                    self.update_check_up_to_date.emit()
            else:
                if not silent:
                    self.update_check_failed.emit()
            return
        try:
            latest, release_url = self._fetch_latest_release()
            self.latest_release_url = release_url
            self.latest_version_seen = latest or APP_VERSION
            self.last_update_check_time = now_ts
            if latest and self._parse_version_tuple(latest) > self._parse_version_tuple(APP_VERSION):
                self.last_update_check_result = {"status": "available", "latest": latest, "release_url": release_url}
                self.update_check_available.emit(latest, release_url)
            else:
                self.last_update_check_result = {"status": "up_to_date", "latest": latest or APP_VERSION, "release_url": release_url}
                if not silent:
                    self.update_check_up_to_date.emit()
        except Exception:
            self.last_update_check_time = now_ts
            self.last_update_check_result = {"status": "failed"}
            if not silent:
                self.update_check_failed.emit()

    def check_for_updates_silent(self):
        threading.Thread(target=lambda: self._run_update_check(silent=True), daemon=True).start()

    def check_for_updates_manual(self):
        threading.Thread(target=lambda: self._run_update_check(silent=False), daemon=True).start()

    # ---------- Misc ----------
    def test_webhook(self):
        self.config = self.snapshot_config_from_ui()
        if not self.config.discord_webhook_url.strip():
            QMessageBox.warning(self, APP_NAME, "Paste a Discord webhook URL first.")
            return
        self.send_discord_webhook_async(f"[{APP_NAME}] Test notification successful. Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", force=True)

    def open_log_folder(self):
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        folder = str(LOG_FILE.parent)
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Failed to open folder:\n{e}")

    def hide_to_tray(self):
        if not self.config.minimize_to_tray or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.hide()
        self.tray_icon.show()
        self.log("[tray] minimized to tray.")

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self.tray_icon:
            self.tray_icon.hide()

    def exit_app(self):
        self._shutting_down = True
        self.monitor_stop_event.set()
        self.monitor_running = False
        self._next_headless_launch_id()
        self._stop_both_impl()
        if self.tray_icon:
            self.tray_icon.hide()
        QApplication.instance().quit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "update_popup") and self.update_popup.isVisible():
            self.update_popup.reposition()

    def closeEvent(self, event):
        self.save_config()
        if self.config.minimize_to_tray and QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.hide_to_tray()
            return
        self._shutting_down = True
        self.monitor_stop_event.set()
        self.monitor_running = False
        self._next_headless_launch_id()
        self._stop_both_impl()
        event.accept()


def install_global_exception_logging():
    original_sys_excepthook = sys.excepthook

    def _sys_excepthook(exc_type, exc_value, exc_traceback):
        formatted = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)).rstrip()
        append_log_line(LOG_FILE, f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [fatal] Unhandled exception\n{formatted}")
        try:
            original_sys_excepthook(exc_type, exc_value, exc_traceback)
        except Exception:
            pass

    sys.excepthook = _sys_excepthook

    if hasattr(threading, "excepthook"):
        original_threading_excepthook = threading.excepthook

        def _threading_excepthook(args):
            formatted = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)).rstrip()
            append_log_line(LOG_FILE, f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [thread-fatal] Unhandled thread exception\n{formatted}")
            try:
                original_threading_excepthook(args)
            except Exception:
                pass

        threading.excepthook = _threading_excepthook


def main():
    set_windows_app_id()
    install_global_exception_logging()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    if os.path.exists(resource_path("watchdog.ico")):
        app.setWindowIcon(QIcon(resource_path("watchdog.ico")))
    window = WatchdogSuiteWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
