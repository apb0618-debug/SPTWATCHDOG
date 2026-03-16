import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
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


APP_NAME = "SPT/Fika Watchdog"
CONFIG_FILE = Path(__file__).with_name("watchdog_config.json")
LOG_FILE = Path(__file__).with_name("watchdog.log")


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
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SPTWatchdog.App")
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
        self.root.geometry("950x700")
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

        self.log_queue = queue.Queue()

        self._build_ui()
        self.load_config()
        self.update_status_labels()

        self.root.after(100, self.process_log_queue)
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

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        paths_frame = ttk.LabelFrame(main, text="Program Paths", padding=10)
        paths_frame.pack(fill="x", pady=(0, 10))

        self.server_path_var = tk.StringVar()
        self.headless_path_var = tk.StringVar()
        self.server_args_var = tk.StringVar()
        self.headless_args_var = tk.StringVar()
        self.server_workdir_var = tk.StringVar()
        self.headless_workdir_var = tk.StringVar()

        self._path_row(paths_frame, 0, "Server EXE", self.server_path_var, self.browse_server)
        self._path_row(paths_frame, 1, "Headless EXE", self.headless_path_var, self.browse_headless)
        self._entry_row(paths_frame, 2, "Server Args", self.server_args_var)
        self._entry_row(paths_frame, 3, "Headless Args", self.headless_args_var)
        self._entry_row(paths_frame, 4, "Server Working Dir", self.server_workdir_var)
        self._entry_row(paths_frame, 5, "Headless Working Dir", self.headless_workdir_var)

        settings_frame = ttk.LabelFrame(main, text="Watchdog Settings", padding=10)
        settings_frame.pack(fill="x", pady=(0, 10))

        self.headless_delay_var = tk.StringVar()
        self.restart_hours_var = tk.StringVar()
        self.monitor_interval_var = tk.StringVar()

        self.auto_restart_var = tk.BooleanVar()
        self.auto_start_var = tk.BooleanVar()
        self.minimize_to_tray_var = tk.BooleanVar()
        self.start_with_windows_var = tk.BooleanVar()
        self.log_to_file_var = tk.BooleanVar()

        self._entry_row(settings_frame, 0, "Headless Start Delay (sec)", self.headless_delay_var)
        self._entry_row(settings_frame, 1, "Scheduled Restart Interval (hours)", self.restart_hours_var)
        self._entry_row(settings_frame, 2, "Monitor Check Interval (sec)", self.monitor_interval_var)

        checks = ttk.Frame(settings_frame)
        checks.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        ttk.Checkbutton(checks, text="Auto restart crashed processes", variable=self.auto_restart_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(checks, text="Auto start programs when monitor starts", variable=self.auto_start_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(checks, text="Minimize to tray", variable=self.minimize_to_tray_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(checks, text="Start with Windows", variable=self.start_with_windows_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(checks, text="Write log file", variable=self.log_to_file_var).pack(side="left")

        controls = ttk.LabelFrame(main, text="Controls", padding=10)
        controls.pack(fill="x", pady=(0, 10))

        ttk.Button(controls, text="Start Server", command=self.start_server).pack(side="left", padx=4)
        ttk.Button(controls, text="Start Headless", command=self.start_headless).pack(side="left", padx=4)
        ttk.Button(controls, text="Start Both", command=self.start_both).pack(side="left", padx=4)
        ttk.Button(controls, text="Stop Server", command=self.stop_server).pack(side="left", padx=12)
        ttk.Button(controls, text="Stop Headless", command=self.stop_headless).pack(side="left", padx=4)
        ttk.Button(controls, text="Stop Both", command=self.stop_both).pack(side="left", padx=4)
        ttk.Button(controls, text="Restart Both", command=self.restart_both).pack(side="left", padx=12)
        ttk.Button(controls, text="Start Monitor", command=self.start_monitor).pack(side="left", padx=4)
        ttk.Button(controls, text="Stop Monitor", command=self.stop_monitor).pack(side="left", padx=4)

        status = ttk.LabelFrame(main, text="Status", padding=10)
        status.pack(fill="x", pady=(0, 10))

        self.server_status_var = tk.StringVar(value="Stopped")
        self.headless_status_var = tk.StringVar(value="Stopped")
        self.monitor_status_var = tk.StringVar(value="Stopped")
        self.next_restart_var = tk.StringVar(value="Not scheduled")

        ttk.Label(status, text="Server:").grid(row=0, column=0, sticky="w")
        ttk.Label(status, textvariable=self.server_status_var).grid(row=0, column=1, sticky="w", padx=(6, 20))
        ttk.Label(status, text="Headless:").grid(row=0, column=2, sticky="w")
        ttk.Label(status, textvariable=self.headless_status_var).grid(row=0, column=3, sticky="w", padx=(6, 20))
        ttk.Label(status, text="Monitor:").grid(row=0, column=4, sticky="w")
        ttk.Label(status, textvariable=self.monitor_status_var).grid(row=0, column=5, sticky="w", padx=(6, 20))
        ttk.Label(status, text="Next Scheduled Restart:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(status, textvariable=self.next_restart_var).grid(row=1, column=1, columnspan=5, sticky="w", padx=(6, 0), pady=(8, 0))

        log_frame = ttk.LabelFrame(main, text="Log", padding=10)
        log_frame.pack(fill="both", expand=True)

        self.log_box = tk.Text(log_frame, wrap="word", height=18)
        self.log_box.pack(fill="both", expand=True)

        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(10, 0))

        ttk.Button(bottom, text="Save Config", command=self.save_config).pack(side="left", padx=4)
        ttk.Button(bottom, text="Load Config", command=self.load_config).pack(side="left", padx=4)
        ttk.Button(bottom, text="Open Log File Folder", command=self.open_log_folder).pack(side="left", padx=12)
        ttk.Button(bottom, text="Apply Windows Startup Setting", command=self.apply_startup_setting).pack(side="left", padx=4)

    def _path_row(self, parent, row, label, variable, browse_cmd):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable, width=85).grid(row=row, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(parent, text="Browse", command=browse_cmd).grid(row=row, column=2, pady=4)
        parent.grid_columnconfigure(1, weight=1)

    def _entry_row(self, parent, row, label, variable):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable, width=85).grid(row=row, column=1, columnspan=2, sticky="ew", padx=6, pady=4)
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

    def collect_config_from_ui(self) -> bool:
        try:
            cfg = AppConfig(
                server_path=self.server_path_var.get().strip(),
                headless_path=self.headless_path_var.get().strip(),
                server_args=self.server_args_var.get().strip(),
                headless_args=self.headless_args_var.get().strip(),
                server_workdir=self.server_workdir_var.get().strip(),
                headless_workdir=self.headless_workdir_var.get().strip(),
                headless_start_delay_sec=int(self.headless_delay_var.get().strip() or "10"),
                restart_interval_hours=float(self.restart_hours_var.get().strip() or "24"),
                monitor_interval_sec=int(self.monitor_interval_var.get().strip() or "5"),
                auto_restart_on_crash=self.auto_restart_var.get(),
                auto_start_with_monitor=self.auto_start_var.get(),
                minimize_to_tray=self.minimize_to_tray_var.get(),
                start_with_windows=self.start_with_windows_var.get(),
                log_to_file=self.log_to_file_var.get(),
            )
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
        self.server_args_var.set(self.config.server_args)
        self.headless_args_var.set(self.config.headless_args)
        self.server_workdir_var.set(self.config.server_workdir)
        self.headless_workdir_var.set(self.config.headless_workdir)
        self.headless_delay_var.set(str(self.config.headless_start_delay_sec))
        self.restart_hours_var.set(str(self.config.restart_interval_hours))
        self.monitor_interval_var.set(str(self.config.monitor_interval_sec))
        self.auto_restart_var.set(self.config.auto_restart_on_crash)
        self.auto_start_var.set(self.config.auto_start_with_monitor)
        self.minimize_to_tray_var.set(self.config.minimize_to_tray)
        self.start_with_windows_var.set(self.config.start_with_windows)
        self.log_to_file_var.set(self.config.log_to_file)

    def save_config(self):
        if not self.collect_config_from_ui():
            return
        try:
            CONFIG_FILE.write_text(json.dumps(asdict(self.config), indent=2), encoding="utf-8")
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
        self.log(f"[debug] server_args={self.config.server_args}")

        threading.Thread(
            target=self.server_proc.start,
            args=(self.config.server_path, self.config.server_args, self.config.server_workdir),
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
            self.config.headless_args,
            self.config.headless_workdir
        )

    def start_both(self):
        if not self.collect_config_from_ui():
            return
        threading.Thread(target=self._start_both_impl, daemon=True).start()

    def _start_both_impl(self):
        self.server_proc.start(self.config.server_path, self.config.server_args, self.config.server_workdir)
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

            self.headless_proc.stop_requested = True
            self.server_proc.stop_requested = True

            self.headless_proc.stop()
            time.sleep(2)
            self.server_proc.stop()

            time.sleep(5)

            self.server_proc.start(self.config.server_path, self.config.server_args, self.config.server_workdir)
            self.pending_headless_start_id += 1
            threading.Thread(target=self._start_headless_with_delay, daemon=True).start()

            now = datetime.now()
            self.last_scheduled_restart = now
            self.next_restart_time = now + timedelta(hours=self.config.restart_interval_hours)

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
        self.log("[monitor] started.")
        self.log(f"[monitor] next scheduled restart at {self.next_restart_time.strftime('%Y-%m-%d %H:%M:%S')}")

        if self.config.auto_start_with_monitor:
            threading.Thread(target=self._start_both_impl, daemon=True).start()

    def stop_monitor(self):
        self.monitor_running = False
        self.monitor_stop_event.set()
        self.pending_headless_start_id += 1
        self.next_restart_time = None
        self.log("[monitor] stopping...")

    def monitor_loop(self):
        while not self.monitor_stop_event.is_set():
            try:
                if not self.restart_in_progress:
                    if self.config.auto_restart_on_crash:
                        if self.server_proc.process is not None and not self.server_proc.is_running() and not self.server_proc.stop_requested:
                            self.log("[monitor] server stopped unexpectedly. Restarting...")
                            self.server_proc.start(self.config.server_path, self.config.server_args, self.config.server_workdir)

                        if self.headless_proc.process is not None and not self.headless_proc.is_running() and not self.headless_proc.stop_requested:
                            self.log("[monitor] headless stopped unexpectedly. Restarting...")
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

    def update_status_labels(self):
        self.server_status_var.set("Running" if self.server_proc.is_running() else "Stopped")
        self.headless_status_var.set("Running" if self.headless_proc.is_running() else "Stopped")
        self.monitor_status_var.set("Running" if self.monitor_running else "Stopped")

        if self.next_restart_time is not None:
            self.next_restart_var.set(self.next_restart_time.strftime("%Y-%m-%d %H:%M:%S"))
        else:
            self.next_restart_var.set("Not scheduled")

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

    def apply_startup_setting(self):
        if not self.collect_config_from_ui():
            return

        if os.name != "nt" or winreg is None:
            messagebox.showinfo(APP_NAME, "Windows startup setting is only supported in Windows.")
            return

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        entry_name = APP_NAME
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
                    pystray.MenuItem("Restart Both", self.tray_restart_both),
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

    def tray_restart_both(self, icon=None, item=None):
        self.root.after(0, self.restart_both)

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
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass

    WatchdogApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()