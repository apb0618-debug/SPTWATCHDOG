import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import winreg  # Windows only
except ImportError:
    winreg = None

# Optional tray support
try:
    import pystray
    from PIL import Image, ImageDraw, ImageTk
except Exception:
    pystray = None
    Image = None
    ImageDraw = None
    ImageTk = None


APP_NAME = "WATCHDOG"
APP_DATA_DIR_NAME = "WATCHDOG"
APP_SUBTITLE = "SPT/FIKA Server Monitor"


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


def get_legacy_config_candidates() -> list[Path]:
    candidates = []
    try:
        candidates.append(Path.cwd() / "watchdog_config.json")
    except Exception:
        pass
    try:
        candidates.append(Path(sys.executable).resolve().with_name("watchdog_config.json"))
    except Exception:
        pass
    try:
        candidates.append(Path(__file__).resolve().with_name("watchdog_config.json"))
    except Exception:
        pass

    unique = []
    seen = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


APP_DATA_DIR = get_app_data_dir()
CONFIG_FILE = APP_DATA_DIR / "watchdog_config.json"
LOG_FILE = APP_DATA_DIR / "watchdog.log"


def resource_path(relative_path: str) -> str:
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def set_windows_app_id():
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WATCHDOG.App")
    except Exception:
        pass


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


class ManagedProcess:
    def __init__(self, name: str, log_func):
        self.name = name
        self.log = log_func
        self.process = None
        self.stop_requested = False
        self.last_start = None
        self.lock = threading.Lock()

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, exe_path: str, args: str = "", workdir: str = "") -> bool:
        with self.lock:
            if not exe_path:
                self.log(f"[{self.name}] no path set.")
                return False

            exe = Path(exe_path)
            if not exe.exists():
                self.log(f"[{self.name}] file not found: {exe}")
                return False

            if self.is_running():
                self.log(f"[{self.name}] already running.")
                return True

            cwd = workdir.strip() or str(exe.parent)
            cmd = [str(exe)]

            if args.strip():
                cmd.extend(args.strip().split())

            self.log(f"[{self.name}] launch cmd: {cmd}")
            self.log(f"[{self.name}] cwd: {cwd}")

            try:
                self.stop_requested = False

                creationflags = 0
                if os.name == "nt" and self.name == "Server":
                    creationflags = subprocess.CREATE_NEW_CONSOLE

                self.process = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    shell=False,
                    creationflags=creationflags
                )

                self.last_start = datetime.now()
                self.log(f"[{self.name}] started. PID={self.process.pid}")
                return True

            except Exception as e:
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
                        check=False
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
                    self.log(f"[{self.name}] still alive after stop attempt, forcing kill...")
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except Exception as e:
                        self.log(f"[{self.name}] failed to kill after stop attempt: {e}")
                        return False

                self.log(f"[{self.name}] stopped.")
                self.process = None
                return True

            except subprocess.TimeoutExpired:
                self.log(f"[{self.name}] stop timed out.")
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                    self.log(f"[{self.name}] killed after timeout.")
                    self.process = None
                    return True
                except Exception as e:
                    self.log(f"[{self.name}] failed to kill after timeout: {e}")
                    return False

            except Exception as e:
                self.log(f"[{self.name}] stop failed: {e}")
                return False

    def restart(self, exe_path: str, args: str = "", workdir: str = "", delay_after_stop: int = 2) -> bool:
        stopped = self.stop()
        time.sleep(max(0, delay_after_stop))
        started = self.start(exe_path, args, workdir)
        return stopped and started


class WatchdogApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1180x820")
        self.root.minsize(940, 700)
        self._apply_window_icon()

        self.config = AppConfig()

        self.server_proc = ManagedProcess("Server", self.log)
        self.headless_proc = ManagedProcess("Headless", self.log)

        self.monitor_running = False
        self.monitor_thread = None
        self.monitor_stop_event = threading.Event()
        self.last_scheduled_restart = None
        self.next_restart_time = None
        self.restart_lock = threading.Lock()
        self.restart_in_progress = False
        self.pending_headless_start_id = 0
        self.tray_icon = None
        self.discord_last_monitor_stop_notice = False

        self.log_queue = queue.Queue()
        self._responsive_specs = []
        self.status_cards = {}

        self._build_ui()
        self.load_config()
        self.update_status_labels()

        self.root.after(100, self.process_log_queue)
        self.root.bind("<Configure>", self._on_root_resize)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _apply_window_icon(self):
        ico_path = resource_path("watchdog.ico")
        png_path = resource_path("watchdog.png")

        if os.path.exists(ico_path):
            try:
                self.root.iconbitmap(default=ico_path)
            except Exception as e:
                print(f"[icon] iconbitmap failed: {e}")

        img_source = png_path if os.path.exists(png_path) else ico_path
        if img_source and os.path.exists(img_source) and Image is not None and ImageTk is not None:
            try:
                img = Image.open(img_source)
                self._icon_ref = ImageTk.PhotoImage(img)
                self.root.iconphoto(True, self._icon_ref)
            except Exception as e:
                print(f"[icon] iconphoto failed: {e}")

        try:
            self.root.update_idletasks()
            if os.name == "nt":
                self.root.after(200, lambda: self.root.iconbitmap(default=ico_path) if os.path.exists(ico_path) else None)
                if img_source and os.path.exists(img_source) and getattr(self, "_icon_ref", None) is not None:
                    self.root.after(250, lambda: self.root.iconphoto(True, self._icon_ref))
        except Exception:
            pass

    def _configure_styles(self):
        palette = {
            "bg": "#100D0B",
            "surface": "#171310",
            "surface_alt": "#201915",
            "surface_soft": "#261D18",
            "surface_card": "#1D1612",
            "surface_card_alt": "#241B16",
            "border": "#3D2B20",
            "border_soft": "#2E221B",
            "border_glow": "#714622",
            "text": "#F4EBDD",
            "muted": "#BAAA98",
            "accent": "#C56C2C",
            "accent_glow": "#F0A35F",
            "accent_soft": "#9A5D2D",
            "accent_hover": "#D98545",
            "accent_pressed": "#A85A24",
            "danger": "#8E4033",
            "danger_hover": "#A54A3B",
            "success": "#4E9B6B",
            "success_glow": "#7DD8A0",
            "stopped": "#B44F45",
            "stopped_glow": "#E6877F",
        }

        self.palette = palette
        self.root.configure(bg=palette["bg"])

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(".", background=palette["bg"], foreground=palette["text"], fieldbackground=palette["surface_alt"])
        style.configure("App.TFrame", background=palette["bg"])
        style.configure("Card.TFrame", background=palette["surface"], relief="flat")
        style.configure("Panel.TLabelframe", background=palette["surface"], foreground=palette["text"], bordercolor=palette["border_soft"], relief="solid")
        style.configure("Panel.TLabelframe.Label", background=palette["surface"], foreground=palette["accent_glow"], font=("Segoe UI Semibold", 11))
        style.configure("PanelInner.TFrame", background=palette["surface"])
        style.configure("Header.TLabel", background=palette["bg"], foreground=palette["accent_glow"], font=("Segoe UI Semibold", 28))
        style.configure("Subheader.TLabel", background=palette["bg"], foreground=palette["muted"], font=("Segoe UI", 10))
        style.configure("Section.TLabel", background=palette["surface"], foreground=palette["muted"], font=("Segoe UI Semibold", 10))
        style.configure("Field.TLabel", background=palette["surface"], foreground=palette["text"], font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=palette["surface"], foreground=palette["muted"], font=("Segoe UI", 9))
        style.configure("Value.TLabel", background=palette["surface"], foreground=palette["text"], font=("Segoe UI Semibold", 11))
        style.configure("CardTitle.TLabel", background=palette["surface_card_alt"], foreground=palette["accent_glow"], font=("Segoe UI Semibold", 10))
        style.configure("CardValue.TLabel", background=palette["surface_card_alt"], foreground=palette["text"], font=("Segoe UI Semibold", 17))

        style.configure("TEntry", padding=8, fieldbackground=palette["surface_alt"], foreground=palette["text"], insertcolor=palette["text"], bordercolor=palette["border_soft"], lightcolor=palette["border_soft"], darkcolor=palette["border_soft"])
        style.map("TEntry", bordercolor=[("focus", palette["accent"])], lightcolor=[("focus", palette["accent"])], darkcolor=[("focus", palette["accent"])])

        style.configure("TCheckbutton", background=palette["surface"], foreground=palette["text"], font=("Segoe UI", 10))
        style.map("TCheckbutton", foreground=[("disabled", palette["muted"]), ("active", palette["text"])], background=[("active", palette["surface"])])

        style.configure("Primary.TButton", background=palette["accent"], foreground="#FFF7F1", bordercolor=palette["accent_glow"], focusthickness=1, focuscolor=palette["accent_glow"], padding=(14, 9), font=("Segoe UI Semibold", 10))
        style.map("Primary.TButton", background=[("active", palette["accent_hover"]), ("pressed", palette["accent_pressed"])], bordercolor=[("active", palette["accent_hover"]), ("pressed", palette["accent_pressed"])], foreground=[("disabled", "#E7C8B3")])

        style.configure("Secondary.TButton", background=palette["surface_soft"], foreground=palette["text"], bordercolor=palette["border_soft"], focusthickness=1, focuscolor=palette["accent_glow"], padding=(12, 8), font=("Segoe UI Semibold", 10))
        style.map("Secondary.TButton", background=[("active", palette["surface_alt"]), ("pressed", palette["border"])], bordercolor=[("active", palette["accent_soft"])])

        style.configure("Danger.TButton", background=palette["danger"], foreground="#FFF7F1", bordercolor=palette["danger"], focusthickness=0, focuscolor=palette["danger"], padding=(14, 9), font=("Segoe UI Semibold", 10))
        style.map("Danger.TButton", background=[("active", palette["danger_hover"]), ("pressed", "#763226")], bordercolor=[("active", palette["danger_hover"]), ("pressed", "#763226")])

    def _make_status_card(self, parent, title, textvariable, key=None):
        outer = tk.Frame(parent, bg=self.palette["bg"], highlightthickness=1, highlightbackground=self.palette["accent_soft"])
        outer.pack(side="left", fill="both", expand=True, padx=8, pady=4)

        glow = tk.Frame(outer, bg=self.palette["border_glow"], padx=1, pady=1)
        glow.pack(fill="both", expand=True)

        shell = tk.Frame(glow, bg=self.palette["surface_card"], padx=1, pady=1)
        shell.pack(fill="both", expand=True)

        accent_bar = tk.Frame(shell, bg=self.palette["accent_glow"], height=3)
        accent_bar.pack(fill="x", side="top")

        inner = tk.Frame(shell, bg=self.palette["surface_card_alt"])
        inner.pack(fill="both", expand=True)

        title_row = tk.Frame(inner, bg=self.palette["surface_card_alt"])
        title_row.pack(fill="x", padx=14, pady=(12, 6))

        indicator = tk.Canvas(title_row, width=14, height=14, bg=self.palette["surface_card_alt"], highlightthickness=0, bd=0)
        indicator.pack(side="left", padx=(0, 8))
        ttk.Label(title_row, text=title, style="CardTitle.TLabel").pack(side="left", anchor="w")

        value_label = ttk.Label(inner, textvariable=textvariable, style="CardValue.TLabel", justify="left")
        value_label.pack(anchor="w", padx=14, pady=(0, 14), fill="x")
        self._register_responsive(value_label, base=190)

        if key:
            self.status_cards[key] = {"canvas": indicator, "value": value_label}
            self._paint_status_indicator(indicator, self.palette["muted"], self.palette["border"])

        return outer

    def _register_responsive(self, widget, base=220, minimum=120, pad=0):
        self._responsive_specs.append((widget, base, minimum, pad))

    def _on_root_resize(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        self._apply_responsive_wraps()

    def _apply_responsive_wraps(self):
        width = max(self.root.winfo_width(), self.root.winfo_reqwidth())
        for widget, base, minimum, pad in self._responsive_specs:
            try:
                wrap = max(minimum, min(int(width * 0.24), base))
                widget.configure(wraplength=wrap)
            except Exception:
                pass

    def _paint_status_indicator(self, canvas, fill_color, ring_color):
        try:
            canvas.delete("all")
            canvas.create_oval(1, 1, 13, 13, fill=ring_color, outline="")
            canvas.create_oval(3, 3, 11, 11, fill=fill_color, outline="")
        except Exception:
            pass

    def _build_ui(self):
        self._configure_styles()

        main = ttk.Frame(self.root, padding=22, style="App.TFrame")
        main.pack(fill="both", expand=True)

        header = ttk.Frame(main, style="App.TFrame")
        header.pack(fill="x", pady=(0, 18))
        ttk.Label(header, text=APP_NAME, style="Header.TLabel").pack(anchor="w")
        subtitle = ttk.Label(header, text=APP_SUBTITLE, style="Subheader.TLabel", justify="left")
        subtitle.pack(anchor="w", pady=(2, 0), fill="x")
        self._register_responsive(subtitle, base=460, minimum=240)

        self.server_path_var = tk.StringVar()
        self.headless_path_var = tk.StringVar()
        self.server_workdir_var = tk.StringVar()
        self.headless_workdir_var = tk.StringVar()
        self.headless_delay_var = tk.StringVar()
        self.restart_hours_var = tk.StringVar()
        self.monitor_interval_var = tk.StringVar()
        self.auto_restart_var = tk.BooleanVar()
        self.minimize_to_tray_var = tk.BooleanVar()
        self.start_with_windows_var = tk.BooleanVar()
        self.log_to_file_var = tk.BooleanVar()
        self.discord_enabled_var = tk.BooleanVar()
        self.discord_webhook_var = tk.StringVar()

        top_grid = ttk.Frame(main, style="App.TFrame")
        top_grid.pack(fill="x", pady=(0, 16))
        left_col = ttk.Frame(top_grid, style="App.TFrame")
        right_col = ttk.Frame(top_grid, style="App.TFrame")
        left_col.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right_col.pack(side="left", fill="both", expand=True, padx=(8, 0))

        paths_frame = ttk.LabelFrame(left_col, text="Paths", padding=14, style="Panel.TLabelframe")
        paths_frame.pack(fill="x", pady=(0, 12))
        self._path_row(paths_frame, 0, "Server Executable", self.server_path_var, self.browse_server)
        self._path_row(paths_frame, 1, "Headless Executable", self.headless_path_var, self.browse_headless)
        self._entry_row(paths_frame, 2, "Server Working Directory", self.server_workdir_var)
        self._entry_row(paths_frame, 3, "Headless Working Directory", self.headless_workdir_var)

        settings_frame = ttk.LabelFrame(left_col, text="Runtime Settings", padding=14, style="Panel.TLabelframe")
        settings_frame.pack(fill="x")
        self._entry_row(settings_frame, 0, "Headless Start Delay (seconds)", self.headless_delay_var)
        self._entry_row(settings_frame, 1, "Scheduled Restart Interval (hours)", self.restart_hours_var)
        self._entry_row(settings_frame, 2, "Monitor Check Interval (seconds)", self.monitor_interval_var)

        options_frame = ttk.LabelFrame(right_col, text="Options", padding=14, style="Panel.TLabelframe")
        options_frame.pack(fill="x", pady=(0, 12))
        opt_auto = ttk.Checkbutton(options_frame, text="Auto restart crashed processes", variable=self.auto_restart_var)
        opt_auto.pack(anchor="w", pady=3, fill="x")
        self._register_responsive(opt_auto, base=270, minimum=170)
        opt_tray = ttk.Checkbutton(options_frame, text="Minimize to tray", variable=self.minimize_to_tray_var)
        opt_tray.pack(anchor="w", pady=3, fill="x")
        self._register_responsive(opt_tray, base=230, minimum=170)
        opt_startup = ttk.Checkbutton(options_frame, text="Start with Windows", variable=self.start_with_windows_var)
        opt_startup.pack(anchor="w", pady=3, fill="x")
        self._register_responsive(opt_startup, base=230, minimum=170)
        opt_log = ttk.Checkbutton(options_frame, text="Write log file", variable=self.log_to_file_var)
        opt_log.pack(anchor="w", pady=3, fill="x")
        self._register_responsive(opt_log, base=230, minimum=170)

        discord_frame = ttk.LabelFrame(right_col, text="Discord Webhook", padding=14, style="Panel.TLabelframe")
        discord_frame.pack(fill="x")
        discord_toggle = ttk.Checkbutton(discord_frame, text="Enable Discord notifications", variable=self.discord_enabled_var)
        discord_toggle.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self._register_responsive(discord_toggle, base=260, minimum=180)
        self._entry_row(discord_frame, 1, "Webhook URL", self.discord_webhook_var)
        discord_hint = ttk.Label(discord_frame, text="The full webhook URL stays local on this machine only.", style="Muted.TLabel", justify="left")
        discord_hint.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self._register_responsive(discord_hint, base=320, minimum=190)
        ttk.Button(discord_frame, text="Test Webhook", command=self.test_discord_webhook, style="Secondary.TButton").grid(row=2, column=2, sticky="e", padx=(10, 0), pady=(8, 0))

        status_frame = ttk.LabelFrame(main, text="Status", padding=14, style="Panel.TLabelframe")
        status_frame.pack(fill="x", pady=(0, 14))

        self.server_status_var = tk.StringVar(value="Stopped")
        self.headless_status_var = tk.StringVar(value="Stopped")
        self.monitor_status_var = tk.StringVar(value="Stopped")
        self.next_restart_var = tk.StringVar(value="Not scheduled")

        cards = ttk.Frame(status_frame, style="PanelInner.TFrame")
        cards.pack(fill="x")
        self._make_status_card(cards, "Server", self.server_status_var, key="server")
        self._make_status_card(cards, "Headless", self.headless_status_var, key="headless")
        self._make_status_card(cards, "Monitor", self.monitor_status_var, key="monitor")
        self._make_status_card(cards, "Next Restart", self.next_restart_var, key="next_restart")

        controls = ttk.Frame(main, style="App.TFrame")
        controls.pack(fill="x", pady=(0, 14))
        control_specs = [
            ("Start Monitor", self.start_monitor, "Primary.TButton"),
            ("Stop Monitor", self.stop_monitor, "Danger.TButton"),
            ("Save Config", self.save_config, "Secondary.TButton"),
            ("Load Config", self.load_config, "Secondary.TButton"),
            ("Open Log Folder", self.open_log_folder, "Secondary.TButton"),
            ("Apply Windows Startup", self.apply_startup_setting, "Secondary.TButton"),
        ]
        for index, (label, command, style_name) in enumerate(control_specs):
            btn = ttk.Button(controls, text=label, command=command, style=style_name)
            row, col = divmod(index, 3)
            btn.grid(row=row, column=col, sticky="ew", padx=4, pady=4)
            controls.grid_columnconfigure(col, weight=1)
            self._register_responsive(btn, base=180, minimum=120)

        log_frame = ttk.LabelFrame(main, text="Activity Log", padding=14, style="Panel.TLabelframe")
        log_frame.pack(fill="both", expand=True)

        log_inner = tk.Frame(log_frame, bg=self.palette["surface_card_alt"], highlightthickness=1, highlightbackground=self.palette["border_soft"])
        log_inner.pack(fill="both", expand=True)

        self.log_box = tk.Text(
            log_inner,
            wrap="word",
            height=18,
            bg=self.palette["surface_alt"],
            fg=self.palette["text"],
            insertbackground=self.palette["accent"],
            selectbackground="#4D301D",
            relief="flat",
            borderwidth=0,
            font=("Consolas", 10),
            padx=12,
            pady=12,
        )
        self.log_box.pack(fill="both", expand=True)

        self._apply_responsive_wraps()
        self.log_box.configure(highlightthickness=0)
    def _path_row(self, parent, row, label, variable, browse_cmd):
        ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=variable, width=85).grid(row=row, column=1, sticky="ew", padx=8, pady=6)
        ttk.Button(parent, text="Browse", command=browse_cmd, style="Secondary.TButton").grid(row=row, column=2, pady=6)
        parent.grid_columnconfigure(1, weight=1)

    def _entry_row(self, parent, row, label, variable):
        field_label = ttk.Label(parent, text=label, style="Field.TLabel", justify="left")
        field_label.grid(row=row, column=0, sticky="ew", pady=6)
        self._register_responsive(field_label, base=260, minimum=150)
        ttk.Entry(parent, textvariable=variable, width=85).grid(row=row, column=1, columnspan=2, sticky="ew", padx=8, pady=6)
        parent.grid_columnconfigure(1, weight=1)

    def log(self, message: str):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} {message}"
        self.log_queue.put(line)

    def process_log_queue(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_box.insert("end", line + "\n")
                self.log_box.see("end")

                if self.log_to_file_var.get():
                    try:
                        with LOG_FILE.open("a", encoding="utf-8") as f:
                            f.write(line + "\n")
                    except Exception:
                        pass

                print(line)
        except queue.Empty:
            pass

        self.update_status_labels()
        self.root.after(100, self.process_log_queue)

    def _discord_ready(self) -> bool:
        return bool(self.config.discord_notifications_enabled and self.config.discord_webhook_url.strip())

    def _mask_webhook_url(self, url: str) -> str:
        url = (url or "").strip()
        if not url:
            return "(not set)"
        if len(url) <= 16:
            return "***"
        return f"{url[:32]}...***"

    def _post_discord_webhook(self, message: str, force: bool = False) -> bool:
        url = (self.config.discord_webhook_url or "").strip()
        if not url:
            return False
        if not force and not self.config.discord_notifications_enabled:
            return False

        payload = json.dumps({"content": message}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "SPT-Watchdog-Discord-Webhook"
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=10) as response:
            status = getattr(response, "status", 204)
            return 200 <= status < 300

    def send_discord_webhook_async(self, message: str, force: bool = False):
        def worker():
            try:
                sent = self._post_discord_webhook(message, force=force)
                if force and sent:
                    self.log("[discord] test webhook sent.")
            except urllib.error.HTTPError as e:
                self.log(f"[discord] webhook failed with HTTP {e.code}.")
                if force:
                    self.root.after(0, lambda: messagebox.showerror(APP_NAME, f"Discord webhook failed with HTTP {e.code}."))
            except Exception as e:
                self.log(f"[discord] webhook send failed: {e}")
                if force:
                    self.root.after(0, lambda: messagebox.showerror(APP_NAME, f"Discord webhook send failed.\n\n{e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _format_restart_time(self) -> str:
        if self.next_restart_time is None:
            return "Not scheduled"
        return self.next_restart_time.strftime("%Y-%m-%d %H:%M:%S")

    def test_discord_webhook(self):
        self.config = self._snapshot_config_from_ui()
        url = self.config.discord_webhook_url.strip()
        if not url:
            messagebox.showwarning(APP_NAME, "Paste a Discord webhook URL first.")
            return

        self.send_discord_webhook_async(
            f"[{APP_NAME}] Test notification successful. Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            force=True,
        )

    def browse_server(self):
        path = filedialog.askopenfilename(title="Select Server EXE")
        if path:
            self.server_path_var.set(path)
            if not self.server_workdir_var.get().strip():
                self.server_workdir_var.set(str(Path(path).parent))

    def browse_headless(self):
        path = filedialog.askopenfilename(title="Select Headless EXE")
        if path:
            self.headless_path_var.set(path)
            if not self.headless_workdir_var.get().strip():
                self.headless_workdir_var.set(str(Path(path).parent))

    def _snapshot_config_from_ui(self) -> AppConfig:
        headless_delay_raw = self.headless_delay_var.get().strip()
        restart_hours_raw = self.restart_hours_var.get().strip()
        monitor_interval_raw = self.monitor_interval_var.get().strip()

        try:
            headless_delay = int(headless_delay_raw or str(self.config.headless_start_delay_sec))
        except ValueError:
            headless_delay = self.config.headless_start_delay_sec

        try:
            restart_hours = float(restart_hours_raw or str(self.config.restart_interval_hours))
        except ValueError:
            restart_hours = self.config.restart_interval_hours

        try:
            monitor_interval = int(monitor_interval_raw or str(self.config.monitor_interval_sec))
        except ValueError:
            monitor_interval = self.config.monitor_interval_sec

        return AppConfig(
            server_path=self.server_path_var.get().strip(),
            headless_path=self.headless_path_var.get().strip(),
            server_args="",
            headless_args="",
            server_workdir=self.server_workdir_var.get().strip(),
            headless_workdir=self.headless_workdir_var.get().strip(),
            headless_start_delay_sec=headless_delay,
            restart_interval_hours=restart_hours,
            monitor_interval_sec=monitor_interval,
            auto_restart_on_crash=self.auto_restart_var.get(),
            auto_start_with_monitor=True,
            minimize_to_tray=self.minimize_to_tray_var.get(),
            start_with_windows=self.start_with_windows_var.get(),
            log_to_file=self.log_to_file_var.get(),
            discord_notifications_enabled=self.discord_enabled_var.get(),
            discord_webhook_url=self.discord_webhook_var.get().strip(),
        )

    def collect_config_from_ui(self) -> bool:
        try:
            cfg = self._snapshot_config_from_ui()
        except ValueError:
            messagebox.showerror(APP_NAME, "One or more numeric fields are invalid.")
            return False

        if cfg.headless_start_delay_sec < 0 or cfg.restart_interval_hours <= 0 or cfg.monitor_interval_sec <= 0:
            messagebox.showerror(APP_NAME, "Delay and intervals must be greater than zero.")
            return False

        self.config = cfg
        return True

    def apply_config_to_ui(self):
        self.server_path_var.set(self.config.server_path)
        self.headless_path_var.set(self.config.headless_path)
        self.server_workdir_var.set(self.config.server_workdir)
        self.headless_workdir_var.set(self.config.headless_workdir)
        self.headless_delay_var.set(str(self.config.headless_start_delay_sec))
        self.restart_hours_var.set(str(self.config.restart_interval_hours))
        self.monitor_interval_var.set(str(self.config.monitor_interval_sec))
        self.auto_restart_var.set(self.config.auto_restart_on_crash)
        self.minimize_to_tray_var.set(self.config.minimize_to_tray)
        self.start_with_windows_var.set(self.config.start_with_windows)
        self.log_to_file_var.set(self.config.log_to_file)
        self.discord_enabled_var.set(self.config.discord_notifications_enabled)
        self.discord_webhook_var.set(self.config.discord_webhook_url)

    def save_config(self):
        try:
            self.config = self._snapshot_config_from_ui()
            CONFIG_FILE.write_text(json.dumps(asdict(self.config), indent=2), encoding="utf-8")
            self.apply_startup_setting(silent=True, skip_collect=True)
            self.log(f"[config] saved to {CONFIG_FILE}")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Failed to save config:\n{e}")

    def load_config(self):
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                self.config = AppConfig(**data)
            except Exception as e:
                messagebox.showwarning(APP_NAME, f"Failed to load config. Using defaults.\n\n{e}")
                self.config = AppConfig()
        else:
            self.config = AppConfig()

        self.apply_config_to_ui()
        self.log("[config] loaded.")
        self.update_status_labels()

    def start_server(self):
        if not self.collect_config_from_ui():
            return

        self.log(f"[debug] server_path={self.config.server_path}")
        self.log(f"[debug] server_workdir={self.config.server_workdir}")
        
        threading.Thread(
            target=self.server_proc.start,
            args=(self.config.server_path, "", self.config.server_workdir),
            daemon=True
        ).start()

    def start_headless(self):
        if not self.collect_config_from_ui():
            return
        threading.Thread(target=self._start_headless_with_delay, daemon=True).start()

    def _start_headless_with_delay(self):
        launch_id = self.pending_headless_start_id + 1
        self.pending_headless_start_id = launch_id

        delay = max(0, self.config.headless_start_delay_sec)
        if delay:
            self.log(f"[Headless] waiting {delay}s before start...")

            end_time = time.time() + delay
            while time.time() < end_time:
                if launch_id != self.pending_headless_start_id or self.monitor_stop_event.is_set():
                    self.log("[Headless] delayed start cancelled.")
                    return
                time.sleep(0.25)

        if launch_id != self.pending_headless_start_id:
            self.log("[Headless] delayed start superseded.")
            return

        self.headless_proc.start(
            self.config.headless_path,
            "",
            self.config.headless_workdir
        )

    def start_both(self):
        if not self.collect_config_from_ui():
            return
        threading.Thread(target=self._start_both_impl, daemon=True).start()

    def _start_both_impl(self):
        self.server_proc.start(self.config.server_path, "", self.config.server_workdir)
        threading.Thread(target=self._start_headless_with_delay, daemon=True).start()

    def stop_server(self):
        threading.Thread(target=self._stop_server_impl, daemon=True).start()

    def _stop_server_impl(self):
        self.server_proc.stop()

    def stop_headless(self):
        threading.Thread(target=self._stop_headless_impl, daemon=True).start()

    def _stop_headless_impl(self):
        self.pending_headless_start_id += 1
        self.headless_proc.stop()

    def stop_both(self):
        threading.Thread(target=self._stop_both_impl, daemon=True).start()

    def _stop_both_impl(self):
        self.pending_headless_start_id += 1
        self.headless_proc.stop()
        self.server_proc.stop()

    def restart_both(self):
        if not self.collect_config_from_ui():
            return
        threading.Thread(target=self._restart_both_impl, daemon=True).start()

    def _restart_both_impl(self):
        if not self.restart_lock.acquire(blocking=False):
            self.log("[system] restart already in progress. Skipping duplicate request.")
            return

        self.restart_in_progress = True
        self.pending_headless_start_id += 1

        try:
            self.log("[system] restarting both processes...")
            self.send_discord_webhook_async(f"[{APP_NAME}] Scheduled restart beginning now.")

            self.headless_proc.stop_requested = True
            self.server_proc.stop_requested = True

            self.headless_proc.stop()
            time.sleep(2)
            self.server_proc.stop()

            time.sleep(5)

            self.server_proc.start(self.config.server_path, "", self.config.server_workdir)
            self.pending_headless_start_id += 1
            threading.Thread(target=self._start_headless_with_delay, daemon=True).start()

            now = datetime.now()
            self.last_scheduled_restart = now
            self.next_restart_time = now + timedelta(hours=self.config.restart_interval_hours)
            self.send_discord_webhook_async(
                f"[{APP_NAME}] Scheduled restart completed. Next scheduled restart: {self._format_restart_time()}"
            )

        finally:
            self.server_proc.stop_requested = False
            self.headless_proc.stop_requested = False
            self.restart_in_progress = False
            self.restart_lock.release()
            self.update_status_labels()

    def start_monitor(self):
        if self.monitor_running:
            self.log("[monitor] already running.")
            return

        if not self.collect_config_from_ui():
            return

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
        self.send_discord_webhook_async(
            f"[{APP_NAME}] Monitor started. Next scheduled restart: {self._format_restart_time()}"
        )

        threading.Thread(target=self._start_both_impl, daemon=True).start()
        self._schedule_auto_minimize_to_tray()

    def _schedule_auto_minimize_to_tray(self):
        if not self.config.minimize_to_tray:
            return

        if pystray is None or Image is None or ImageDraw is None:
            self.log("[tray] pystray/Pillow not installed. Auto-minimize skipped.")
            return

        self.root.after(1500, self._auto_minimize_to_tray_if_running)

    def _auto_minimize_to_tray_if_running(self):
        if not self.monitor_running:
            return
        if not self.config.minimize_to_tray:
            return
        if not self.root.winfo_exists():
            return
        if self.root.state() == "withdrawn":
            return

        try:
            self.hide_to_tray()
        except Exception as e:
            self.log(f"[tray] auto-minimize failed: {e}")

    def stop_monitor(self):
        self.monitor_running = False
        self.monitor_stop_event.set()
        self.pending_headless_start_id += 1
        self.next_restart_time = None
        self.log("[monitor] stopping...")
        if not self.discord_last_monitor_stop_notice:
            self.discord_last_monitor_stop_notice = True
            self.send_discord_webhook_async(f"[{APP_NAME}] Monitor stopping.")
        threading.Thread(target=self._stop_both_impl, daemon=True).start()

    def monitor_loop(self):
        while not self.monitor_stop_event.is_set():
            try:
                if not self.restart_in_progress:
                    if self.config.auto_restart_on_crash:
                        if self.server_proc.process is not None and not self.server_proc.is_running() and not self.server_proc.stop_requested:
                            self.log("[monitor] server stopped unexpectedly. Restarting...")
                            self.send_discord_webhook_async(f"[{APP_NAME}] Server stopped unexpectedly. Auto-restart triggered.")
                            self.server_proc.start(self.config.server_path, "", self.config.server_workdir)

                        if self.headless_proc.process is not None and not self.headless_proc.is_running() and not self.headless_proc.stop_requested:
                            self.log("[monitor] headless stopped unexpectedly. Restarting...")
                            self.send_discord_webhook_async(f"[{APP_NAME}] Headless stopped unexpectedly. Auto-restart triggered.")
                            threading.Thread(target=self._start_headless_with_delay, daemon=True).start()

                    if self.next_restart_time is not None and datetime.now() >= self.next_restart_time:
                        self.log("[monitor] scheduled restart interval reached.")
                        threading.Thread(target=self._restart_both_impl, daemon=True).start()

                self.update_status_labels()
            except Exception as e:
                self.log(f"[monitor] error: {e}")

            time.sleep(self.config.monitor_interval_sec)

        self.monitor_running = False
        self.update_status_labels()
        self.log("[monitor] stopped.")
        if not self.discord_last_monitor_stop_notice:
            self.discord_last_monitor_stop_notice = True
            self.send_discord_webhook_async(f"[{APP_NAME}] Monitor stopped.")

    def update_status_labels(self):
        server_running = self.server_proc.is_running()
        headless_running = self.headless_proc.is_running()
        monitor_running = self.monitor_running

        self.server_status_var.set("Running" if server_running else "Stopped")
        self.headless_status_var.set("Running" if headless_running else "Stopped")
        self.monitor_status_var.set("Running" if monitor_running else "Stopped")

        if self.next_restart_time is not None:
            self.next_restart_var.set(self.next_restart_time.strftime("%Y-%m-%d %H:%M:%S"))
        else:
            self.next_restart_var.set("Not scheduled")

        if "server" in self.status_cards:
            self._paint_status_indicator(self.status_cards["server"]["canvas"], self.palette["success_glow"] if server_running else self.palette["stopped_glow"], self.palette["success"] if server_running else self.palette["stopped"])
        if "headless" in self.status_cards:
            self._paint_status_indicator(self.status_cards["headless"]["canvas"], self.palette["success_glow"] if headless_running else self.palette["stopped_glow"], self.palette["success"] if headless_running else self.palette["stopped"])
        if "monitor" in self.status_cards:
            self._paint_status_indicator(self.status_cards["monitor"]["canvas"], self.palette["success_glow"] if monitor_running else self.palette["stopped_glow"], self.palette["success"] if monitor_running else self.palette["stopped"])
        if "next_restart" in self.status_cards:
            active = self.next_restart_time is not None
            self._paint_status_indicator(self.status_cards["next_restart"]["canvas"], self.palette["accent_glow"] if active else self.palette["muted"], self.palette["accent"] if active else self.palette["border"])

    def open_log_folder(self):
        folder = str(LOG_FILE.parent)
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Failed to open folder:\n{e}")

    def apply_startup_setting(self, silent: bool = False, skip_collect: bool = False):
        if not skip_collect and not self.collect_config_from_ui():
            return

        if os.name != "nt" or winreg is None:
            if not silent:
                messagebox.showinfo(APP_NAME, "Windows startup setting is only supported in Windows.")
            return

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        entry_name = APP_NAME
        if getattr(sys, "frozen", False):
            cmd = f'"{Path(sys.executable).resolve()}"'
        else:
            cmd = f'"{sys.executable}" "{Path(__file__).resolve()}"'

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                if self.config.start_with_windows:
                    winreg.SetValueEx(key, entry_name, 0, winreg.REG_SZ, cmd)
                    self.log("[startup] enabled Start with Windows.")
                else:
                    try:
                        winreg.DeleteValue(key, entry_name)
                        self.log("[startup] disabled Start with Windows.")
                    except FileNotFoundError:
                        self.log("[startup] Start with Windows was already disabled.")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Failed to update Windows startup:\n{e}")

    def on_close(self):
        self.save_config()

        if self.minimize_to_tray_var.get():
            if pystray is None or Image is None or ImageDraw is None:
                self.log("[tray] pystray/Pillow not installed. Closing normally.")
            else:
                self.hide_to_tray()
                return

        self.stop_monitor()
        self.root.destroy()

    def hide_to_tray(self):
        self.root.withdraw()
        self.log("[tray] minimized to tray.")

        if self.tray_icon is None:
            self.tray_icon = pystray.Icon(
                APP_NAME,
                self._create_tray_image(),
                APP_NAME,
                menu=pystray.Menu(
                    pystray.MenuItem("Show", self.tray_show),
                    pystray.MenuItem("Start Monitor", self.tray_start_monitor),
                    pystray.MenuItem("Stop Monitor", self.tray_stop_monitor),
                    pystray.MenuItem("Exit", self.tray_exit),
                )
            )

        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _create_tray_image(self):
        image = Image.new("RGB", (64, 64), "black")
        draw = ImageDraw.Draw(image)
        draw.rectangle((8, 8, 56, 56), outline="white", width=3)
        draw.text((18, 20), "SPT", fill="white")
        return image

    def tray_show(self, icon=None, item=None):
        if self.tray_icon:
            self.tray_icon.stop()
        self.tray_icon = None
        self.root.after(0, self.root.deiconify)

    def tray_start_monitor(self, icon=None, item=None):
        self.root.after(0, self.start_monitor)

    def tray_stop_monitor(self, icon=None, item=None):
        self.root.after(0, self.stop_monitor)


    def tray_exit(self, icon=None, item=None):
        if self.tray_icon:
            self.tray_icon.stop()
        self.tray_icon = None
        self.root.after(0, self._final_exit)

    def _final_exit(self):
        self.stop_monitor()
        self.root.destroy()


def main():
    set_windows_app_id()
    root = tk.Tk()
    WatchdogApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()