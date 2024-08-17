#!/usr/bin/env python3

from pystray import MenuItem as item
import tkinter
from tkinter import ttk
import pystray
from PIL import Image
import socket
import json
import threading
import time
import subprocess
import os
import enum
from dataclasses import dataclass
from io import BytesIO
from cairosvg import svg2png
import argparse
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(levelname)s - %(message)s')

global daemon_manager
daemon_manager = None

# At the top of the file, after imports
debug = False


def log_message(message, level=logging.INFO):
    if args.log_level != 'NONE':
        logging.log(level, message)
    elif debug:
        print(message)


class DaemonState(enum.Enum):
    INITIAL = "initial"
    STARTING = "starting"
    RUNNING = "running"
    SYNCING = "syncing"
    ERROR = "error"
    SHUTTING_DOWN = "shutting_down"
    SYNC_ISSUES = "sync_issues"
    CONFIG_INVALID = "config_invalid"
    CONFIG_CHANGED = "config_changed"
    LIMBO = "limbo"
    OFFLINE = "offline"


@dataclass
class StateInfo:
    color: tuple
    menu_items: list
    icon_text: str


class Colors:
    YELLOW = (255, 235, 59)
    GREEN = (76, 175, 80)
    BLUE = (33, 150, 243)
    GRAY = (158, 158, 158)
    PURPLE = (156, 39, 176)
    ORANGE = (255, 152, 0)
    RED = (244, 67, 54)
    AMBER = (255, 193, 7)


class DaemonManager:
    def __init__(self):
        self.last_status = {}
        self.daemon_start_error = None
        self.last_state = DaemonState.INITIAL
        self.first_status_received = False

    def get_current_state(self, status):
        if self.last_state == DaemonState.INITIAL:
            current_state = DaemonState.STARTING
        elif not self.first_status_received:
            if status is None:
                current_state = DaemonState.STARTING
            else:
                self.first_status_received = True
                current_state = self._determine_state(status)
        else:
            current_state = self._determine_state(status)

        if current_state != self.last_state:
            log_message(f"State changed: {
                        self.last_state} -> {current_state}", level=logging.INFO)
            self.last_state = current_state
        return current_state

    def _determine_state(self, status):
        if self.daemon_start_error:
            return DaemonState.ERROR
        elif status is None:
            return DaemonState.OFFLINE
        elif status.get("shutting_down"):
            return DaemonState.SHUTTING_DOWN
        elif status.get("in_limbo"):
            return DaemonState.LIMBO
        elif status.get("config_invalid"):
            return DaemonState.CONFIG_INVALID
        elif status.get("config_changed_on_disk"):
            return DaemonState.CONFIG_CHANGED
        elif status.get("currently_syncing"):
            return DaemonState.SYNCING
        elif self._has_sync_issues(status):
            return DaemonState.SYNC_ISSUES
        else:
            return DaemonState.RUNNING

    def _has_sync_issues(self, status):
        return (
            any(
                job["sync_status"] not in ["COMPLETED", "NONE", None] or
                job["resync_status"] not in ["COMPLETED", "NONE", None] or
                job.get("hash_warnings", False)
                for job in status.get("sync_jobs", {}).values()
            ) or
            bool(status.get("sync_errors"))
        )

    def is_currently_syncing(self, status):
        if status is None:
            return False
        return status.get('currently_syncing') not in [None, 'None', '']

    def get_menu_items(self, status):
        current_state = self.get_current_state(status)
        menu_items = []

        if current_state == DaemonState.ERROR:
            menu_items.extend(self._get_error_menu_items())
        elif current_state == DaemonState.OFFLINE:
            menu_items.extend(self._get_offline_menu_items())
        elif current_state == DaemonState.LIMBO:
            menu_items.extend(self._get_limbo_menu_items(status))
        else:
            menu_items.extend(self._get_normal_menu_items(status))

        # Common menu items for all states
        menu_items.extend([
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Config & Logs", pystray.Menu(
                pystray.MenuItem("Reload Config", reload_config, enabled=not (
                    current_state in [DaemonState.OFFLINE, DaemonState.ERROR, DaemonState.SHUTTING_DOWN] or
                    self.is_currently_syncing(status)
                )),
                pystray.MenuItem("Open Config Folder", open_config_file),
                pystray.MenuItem("Open Log Folder", open_log_folder)
            )),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Show Status Window", show_status_window, enabled=current_state not in [
                             DaemonState.OFFLINE, DaemonState.ERROR, DaemonState.SHUTTING_DOWN]),
            pystray.Menu.SEPARATOR,
        ])

        # Add Start/Stop/Shutting Down menu item
        if current_state in [DaemonState.OFFLINE, DaemonState.ERROR]:
            menu_items.append(pystray.MenuItem("Start Daemon", start_daemon))
        elif current_state == DaemonState.SHUTTING_DOWN:
            menu_items.append(pystray.MenuItem(
                "Shutting Down", lambda: None, enabled=False))
        else:
            menu_items.append(pystray.MenuItem("Stop Daemon", stop_daemon))

        menu_items.append(pystray.MenuItem("Exit", lambda: icon.stop()))

        return menu_items

    def _get_error_menu_items(self):
        return [
            pystray.MenuItem("⚠️ Daemon failed to start", None, enabled=False),
            pystray.MenuItem(
                f"Error: {self.daemon_start_error}", None, enabled=False),
        ]

    def _get_offline_menu_items(self):
        return [
            pystray.MenuItem("⚠️ Daemon is offline", None, enabled=False),
        ]

    def _get_limbo_menu_items(self, status):
        items = [
            pystray.MenuItem("⚠️ Daemon is in limbo state",
                             None, enabled=False),
        ]
        if status.get("config_invalid", False):
            items.append(pystray.MenuItem(
                "⚠️ Config is invalid", None, enabled=False))
            items.append(pystray.MenuItem(f"Error: {status.get(
                'config_error_message', 'Unknown error')[:30]}...", None, enabled=False))
        if status.get("config_changed_on_disk", False):
            items.append(pystray.MenuItem(
                "⚠️ Config changed on disk", None, enabled=False))
        return items

    def _get_normal_menu_items(self, status):
        items = []
        if status.get("config_changed_on_disk"):
            items.append(pystray.MenuItem(
                "⚠️ Config changed on disk", None, enabled=False))

        if status.get("sync_errors"):
            items.append(pystray.MenuItem(
                "⚠️ Sync errors detected", None, enabled=False))

        currently_syncing = status.get('currently_syncing', None)
        if currently_syncing:
            items.append(pystray.MenuItem(
                "Currently syncing:", None, enabled=False))
            if isinstance(currently_syncing, str):
                items.append(pystray.MenuItem(
                    f"  {currently_syncing.strip()}", None, enabled=False))
            elif isinstance(currently_syncing, list):
                for job in currently_syncing:
                    items.append(pystray.MenuItem(
                        f"  {job.strip()}", None, enabled=False))

        queued_jobs = status.get('queued_paths', [])
        if queued_jobs:
            items.append(pystray.MenuItem("Queued jobs:", None, enabled=False))
            for job in queued_jobs:
                items.append(pystray.MenuItem(f"  {job}", None, enabled=False))

        # Add sync jobs submenu
        if "sync_jobs" in status:
            jobs_submenu = []
            for job_key, job_status in status["sync_jobs"].items():
                job_submenu = pystray.Menu(
                    pystray.MenuItem(
                        "⚡ Sync Now", create_sync_now_handler(job_key)),
                    pystray.MenuItem(
                        f"Last sync: {job_status['last_sync'] or 'Never'}", None, enabled=False),
                    pystray.MenuItem(
                        f"Next run: {job_status['next_run'] or 'Not scheduled'}", None, enabled=False),
                    pystray.MenuItem(
                        f"Sync status: {job_status['sync_status']}", None, enabled=False),
                    pystray.MenuItem(f"Resync status: {
                                     job_status['resync_status']}", None, enabled=False),
                )
                jobs_submenu.append(pystray.MenuItem(job_key, job_submenu))
            items.append(pystray.MenuItem(
                "Sync Jobs", pystray.Menu(*jobs_submenu)))
        else:
            items.append(pystray.MenuItem("Sync Jobs", None, enabled=False))

        return items

    def get_icon_color(self, status):
        current_state = self.get_current_state(status)
        if current_state == DaemonState.INITIAL:
            return Colors.GRAY
        elif current_state == DaemonState.STARTING:
            return Colors.YELLOW
        elif current_state == DaemonState.RUNNING:
            return Colors.GREEN
        elif current_state == DaemonState.SYNCING:
            return Colors.BLUE
        elif current_state == DaemonState.ERROR:
            return Colors.RED
        elif current_state == DaemonState.SHUTTING_DOWN:
            return Colors.PURPLE
        elif current_state == DaemonState.SYNC_ISSUES:
            return Colors.RED
        elif current_state == DaemonState.CONFIG_INVALID:
            return Colors.RED
        elif current_state == DaemonState.CONFIG_CHANGED:
            return Colors.AMBER
        elif current_state == DaemonState.LIMBO:
            return Colors.PURPLE
        elif current_state == DaemonState.OFFLINE:
            return Colors.GRAY
        else:
            return Colors.GRAY  # Default color for unknown states

    def get_icon_text(self, status):
        current_state = self.get_current_state(status)
        if current_state == DaemonState.ERROR:
            return "ERR"
        elif current_state == DaemonState.OFFLINE:
            return "OFF"
        elif current_state == DaemonState.SYNCING:
            return "SYNC"
        elif current_state == DaemonState.CONFIG_INVALID:
            return "CFG!"
        elif current_state == DaemonState.CONFIG_CHANGED:
            return "CFG?"
        elif current_state == DaemonState.SYNC_ISSUES:
            return "WARN"
        elif current_state == DaemonState.LIMBO:
            return "LIMBO"
        elif current_state == DaemonState.SHUTTING_DOWN:
            return "STOP"
        else:
            return "RUN"


def get_daemon_status():
    socket_path = '/tmp/rclone_bisync_manager_status.sock'
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)  # Set a timeout of 5 seconds
        client.connect(socket_path)
        client.sendall(b"STATUS")
        status = json.loads(client.recv(4096).decode())
        client.close()
        return status
    except socket.error as e:
        log_message(f"Socket error when getting daemon status: {
                    e}", level=logging.ERROR)
        return None
    except json.JSONDecodeError as e:
        log_message(f"JSON decode error when getting daemon status: {
                    e}", level=logging.ERROR)
        return None
    except Exception as e:
        log_message(f"Unexpected error when getting daemon status: {
                    e}", level=logging.ERROR)
        return None


def create_sync_now_handler(job_key):
    def handler(item):
        add_to_sync_queue(job_key)
    return handler


def stop_daemon():
    socket_path = '/tmp/rclone_bisync_manager_status.sock'
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(socket_path)
        client.sendall(b"STOP")
        response = client.recv(1024).decode()
        client.close()
        log_message(
            "Daemon is shutting down. Use 'daemon status' to check progress.")

        # Immediately update the menu to show "Shutting Down"
        update_menu_and_icon()
    except Exception as e:
        log_message(f"Error stopping daemon: {e}", level=logging.ERROR)


def start_daemon():
    global daemon_manager
    try:
        subprocess.run(
            ["rclone-bisync-manager", "daemon", "start"], check=True)
        log_message("Daemon started successfully")
        daemon_manager.daemon_start_error = None
    except subprocess.CalledProcessError as e:
        log_message(f"Error starting daemon: {e}", level=logging.ERROR)
        daemon_manager.daemon_start_error = str(e)


def reload_config():
    global daemon_manager, icon
    socket_path = '/tmp/rclone_bisync_manager_status.sock'
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(socket_path)
        client.sendall(b"RELOAD")
        response = client.recv(1024).decode()
        client.close()

        try:
            response_data = json.loads(response)
            if response_data["status"] == "success":
                log_message("Configuration reloaded successfully")
            else:
                log_message(f"Error reloading configuration: {
                    response_data['message']}", level=logging.ERROR)

            current_status = get_daemon_status()
            new_menu = pystray.Menu(
                *daemon_manager.get_menu_items(current_status))
            new_icon = create_status_image(
                daemon_manager.get_icon_color(current_status),
                daemon_manager.get_icon_text(current_status),
                style=args.icon_style,
                thickness=args.icon_thickness
            )

            icon.menu = new_menu
            icon.icon = new_icon
            icon.update_menu()

            return response_data["status"] == "success"
        except json.JSONDecodeError:
            log_message(f"Error: Invalid JSON response from daemon: {
                        response}", level=logging.ERROR)
            return False
    except Exception as e:
        log_message(f"Error communicating with daemon: {
                    str(e)}", level=logging.ERROR)
        return False


def add_to_sync_queue(job_key):
    socket_path = '/tmp/rclone_bisync_manager_add_sync.sock'
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(socket_path)
        client.sendall(json.dumps([job_key]).encode())
        response = client.recv(1024).decode()
        client.close()
        log_message(f"Add to sync queue response: {response}")
    except Exception as e:
        log_message(f"Error adding job to sync queue: {
                    str(e)}", level=logging.ERROR)


def determine_arrow_color(color, icon_text):
    if color == (158, 158, 158):  # Gray (error state)
        return "#FFFFFF"  # White for error (daemon not running)
    elif color == (33, 150, 243):  # Blue (syncing)
        return "#FFFFFF"  # White
    elif color == (244, 67, 54):  # Red (config invalid)
        return "#FFFFFF"  # White for invalid config
    elif color == (255, 193, 7):  # Amber (config changed on disk)
        return "#000000"  # Black for config changed on disk
    elif color == (255, 152, 0):  # Orange (sync issues)
        return "#000000"  # Black for sync issues
    else:
        return "#FFFFFF"  # White for normal operation


def create_status_image_style1(color, thickness):
    size = 64

    if isinstance(color, tuple):
        color = '#{:02x}{:02x}{:02x}'.format(color[0], color[1], color[2])

    svg_code = '''
    <svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
    <path d="M505.6 57.6a20.906667 20.906667 0 0 1 6.4 15.36V170.666667a341.333333 341.333333 0 0 1 295.253333 512 22.186667 22.186667 0 0 1-15.786666 10.24 21.333333 21.333333 0 0 1-17.92-5.973334l-31.146667-31.146666a21.333333 21.333333 0 0 1-3.84-25.173334A253.44 253.44 0 0 0 768 512a256 256 0 0 0-256-256v100.693333a20.906667 20.906667 0 0 1-6.4 15.36l-8.533333 8.533334a21.333333 21.333333 0 0 1-30.293334 0L315.733333 229.973333a21.76 21.76 0 0 1 0-30.293333l151.04-150.613333a21.333333 21.333333 0 0 1 30.293334 0z m51.626667 585.813333a21.333333 21.333333 0 0 0-30.293334 0l-8.533333 8.533334a20.906667 20.906667 0 0 0-6.4 15.36V768a256 256 0 0 1-256-256 248.746667 248.746667 0 0 1 29.866667-119.04 21.76 21.76 0 0 0-3.84-25.173333l-31.573334-31.573334a21.333333 21.333333 0 0 0-17.92-5.973333 22.186667 22.186667 0 0 0-15.786666 11.093333A341.333333 341.333333 0 0 0 512 853.333333v97.706667a20.906667 20.906667 0 0 0 6.4 15.36l8.533333 8.533333a21.333333 21.333333 0 0 0 30.293334 0l151.04-150.613333a21.76 21.76 0 0 0 0-30.293333z" 
    fill="{color}" stroke="{color}" stroke-width="{thickness}" stroke-linejoin="round" stroke-linecap="round"/>
    </svg>
    '''

    svg_code = svg_code.format(color=color, thickness=thickness)
    png_data = svg2png(bytestring=svg_code,
                       output_width=size, output_height=size)
    image = Image.open(BytesIO(png_data)).convert('RGBA')

    return image


def create_status_image_style2(color, thickness):
    size = 64

    if isinstance(color, tuple):
        color = '#{:02x}{:02x}{:02x}'.format(color[0], color[1], color[2])

    svg_code = '''
    <svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
    <path d="M917.333 394.667H106.667a32 32 0 0 1 0-64h810.666a32 32 0 0 1 0 64z m0 298.666H106.667a32 32 0 0 1 0-64h810.666a32 32 0 0 1 0 64z" fill="none" stroke="{color}" stroke-width="{thickness}" stroke-linecap="round"/>
    <path d="M106.667 394.667a32 32 0 0 1-22.614-54.614l241.28-241.28A32 32 0 0 1 370.56 144L129.28 385.28a32 32 0 0 1-22.613 9.387z m569.386 539.946A32 32 0 0 1 653.44 880l241.28-241.28a32 32 0 1 1 45.227 45.227l-241.28 241.28a32 32 0 0 1-22.614 9.386z" fill="none" stroke="{color}" stroke-width="{thickness}" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
    '''

    svg_code = svg_code.format(color=color, thickness=thickness)
    png_data = svg2png(bytestring=svg_code,
                       output_width=size, output_height=size)
    image = Image.open(BytesIO(png_data)).convert('RGBA')

    return image


def create_status_image(color, icon_text, style=1, thickness=40):
    if style == 2:
        return create_status_image_style2(color, thickness)
    else:
        return create_status_image_style1(color, thickness)


def determine_text_color(background_color):
    # Simple logic to determine if text should be black or white based on background brightness
    r, g, b = background_color[:3]
    brightness = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#000000" if brightness > 0.5 else "#FFFFFF"


def show_status_window():
    status = get_daemon_status()

    window = tkinter.Tk()
    window.title("RClone BiSync Manager Status")
    window.geometry("500x400")

    style = ttk.Style()
    style.theme_use('clam')

    notebook = ttk.Notebook(window)
    notebook.pack(expand=True, fill='both')

    general_frame = ttk.Frame(notebook)
    notebook.add(general_frame, text='General')

    ttk.Label(general_frame, text=f"Config: {'Valid' if not status.get(
        'config_invalid', False) else 'Invalid'}").pack(pady=5)
    ttk.Label(general_frame, text=f"Config changed on disk: {
              'Yes' if status.get('config_changed_on_disk', False) else 'No'}").pack(pady=5)

    currently_syncing = status.get('currently_syncing', 'None')
    ttk.Label(general_frame, text="Currently syncing:").pack(
        anchor='w', padx=5, pady=(5, 0))
    ttk.Label(general_frame, text=currently_syncing).pack(
        anchor='w', padx=20, pady=(0, 5))

    queued_jobs = status.get('queued_paths', [])
    ttk.Label(general_frame, text="Queued jobs:").pack(
        anchor='w', padx=5, pady=(5, 0))
    if queued_jobs:
        for job in queued_jobs:
            ttk.Label(general_frame, text=job).pack(
                anchor='w', padx=20, pady=(0, 2))
    else:
        ttk.Label(general_frame, text="None").pack(
            anchor='w', padx=20, pady=(0, 5))

    jobs_frame = ttk.Frame(notebook)
    notebook.add(jobs_frame, text='Sync Jobs')

    if "sync_jobs" in status:
        for job_key, job_status in status["sync_jobs"].items():
            job_frame = ttk.LabelFrame(jobs_frame, text=job_key)
            job_frame.pack(pady=5, padx=5, fill='x')

            ttk.Label(job_frame, text=f"Last sync: {
                      job_status['last_sync']}").pack(anchor='w')
            ttk.Label(job_frame, text=f"Next run: {
                      job_status['next_run']}").pack(anchor='w')
            ttk.Label(job_frame, text=f"Sync status: {
                      job_status['sync_status']}").pack(anchor='w')
            ttk.Label(job_frame, text=f"Resync status: {
                      job_status['resync_status']}").pack(anchor='w')

    errors_frame = ttk.Frame(notebook)
    notebook.add(errors_frame, text='Sync Errors')

    if status.get("sync_errors"):
        for path, error_info in status["sync_errors"].items():
            error_frame = ttk.LabelFrame(errors_frame, text=path)
            error_frame.pack(pady=5, padx=5, fill='x')

            ttk.Label(error_frame, text=f"Sync Type: {
                      error_info['sync_type']}").pack(anchor='w')
            ttk.Label(error_frame, text=f"Error Code: {
                      error_info['error_code']}").pack(anchor='w')
            ttk.Label(error_frame, text=f"Message: {
                      error_info['message']}").pack(anchor='w')
            ttk.Label(error_frame, text=f"Timestamp: {
                      error_info['timestamp']}").pack(anchor='w')
    else:
        ttk.Label(errors_frame, text="No sync errors at this time.").pack(pady=20)

    window.mainloop()


def open_config_file():
    config_file_path = get_config_file_path()
    if config_file_path:
        config_dir = os.path.dirname(config_file_path)
        if os.name == 'nt':  # For Windows
            os.startfile(config_dir)
        elif os.name == 'posix':  # For macOS and Linux
            subprocess.call(('xdg-open', config_dir))
    else:
        log_message("Config file path not found", level=logging.ERROR)


def open_log_folder():
    log_file_path = get_log_file_path()
    if log_file_path:
        log_dir = os.path.dirname(log_file_path)
        if os.name == 'nt':  # For Windows
            os.startfile(log_dir)
        elif os.name == 'posix':  # For macOS and Linux
            subprocess.call(('xdg-open', log_dir))
    else:
        log_message("Log file path not found", level=logging.ERROR)


def get_config_file_path():
    status = get_daemon_status()
    return status.get('config_file_location')


def get_log_file_path():
    status = get_daemon_status()
    return status.get('log_file_location')


def show_text_window(title, content):
    root = tkinter.Tk()
    root.title(title)
    root.geometry("600x400")

    text_widget = tkinter.Text(root, wrap=tkinter.WORD)
    text_widget.pack(expand=True, fill='both')
    text_widget.insert(tkinter.END, content)
    text_widget.config(state=tkinter.DISABLED)

    scrollbar = ttk.Scrollbar(root, orient="vertical",
                              command=text_widget.yview)
    scrollbar.pack(side=tkinter.RIGHT, fill=tkinter.Y)
    text_widget.configure(yscrollcommand=scrollbar.set)

    root.mainloop()


def run_tray():
    global icon, daemon_manager, args, debug
    daemon_manager = DaemonManager()

    parser = argparse.ArgumentParser()
    parser.add_argument('--icon-style', type=int,
                        choices=[1, 2], default=1, help='Choose icon style: 1 or 2')
    parser.add_argument('--icon-thickness', type=int,
                        default=40, help='Set the thickness of the icon lines')
    parser.add_argument('--log-level', type=str, choices=['NONE', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        default='NONE', help='Set the logging level')
    args = parser.parse_args()

    # Set up logging based on the argument
    if args.log_level != 'NONE':
        logging.basicConfig(level=getattr(logging, args.log_level),
                            format='%(asctime)s - %(levelname)s - %(message)s')
        debug = (args.log_level == 'DEBUG')
    else:
        logging.disable(logging.CRITICAL)  # Disable all logging
        debug = False

    icon = pystray.Icon("rclone-bisync-manager",
                        create_status_image(daemon_manager.get_icon_color(daemon_manager.last_status),
                                            daemon_manager.get_icon_text(
                                                daemon_manager.last_status),
                                            style=args.icon_style,
                                            thickness=args.icon_thickness),
                        "RClone BiSync Manager")
    icon.menu = pystray.Menu(
        *daemon_manager.get_menu_items(daemon_manager.last_status))

    # Start the status checking thread, passing args
    threading.Thread(target=check_status_and_update,
                     args=(args,), daemon=True).start()

    icon.run()


def check_status_and_update(args):
    global icon, daemon_manager
    last_status_hash = None
    initial_startup = True
    consecutive_errors = 0

    while True:
        try:
            current_status = get_daemon_status()

            if current_status is None:
                consecutive_errors += 1
                log_message(f"Failed to get daemon status. Consecutive errors: {
                            consecutive_errors}", level=logging.WARNING)

                if consecutive_errors >= 5:  # After 5 consecutive errors, assume daemon is offline
                    current_status = {
                        "error": "Daemon is offline or not responding"}
                    log_message(
                        "Assuming daemon is offline due to consecutive errors", level=logging.ERROR)
                else:
                    time.sleep(1)
                    continue
            else:
                consecutive_errors = 0  # Reset error count if we successfully get a status

            current_status_hash = hash(
                json.dumps(current_status, sort_keys=True))

            if initial_startup:
                initial_state = daemon_manager.get_current_state(
                    current_status)
                log_message(f"Initial state: {
                            initial_state.name}", level=logging.INFO)
                update_menu_and_icon()
                initial_startup = False
                last_status_hash = current_status_hash

            if current_status_hash != last_status_hash:
                current_state = daemon_manager.get_current_state(
                    current_status)
                log_message(f"Status changed. New state: {
                            current_state.name}", level=logging.INFO)
                update_menu_and_icon()
                last_status_hash = current_status_hash

            if not daemon_manager.first_status_received and current_status:
                daemon_manager.first_status_received = True
                current_state = daemon_manager.get_current_state(
                    current_status)
                log_message(f"First status received. New state: {
                            current_state.name}", level=logging.INFO)
                update_menu_and_icon()

        except Exception as e:
            log_message(f"Error in check_status_and_update: {
                        e}", level=logging.ERROR)
            # Treat any exception as an error status
            current_status = {"error": str(e)}

        time.sleep(1)


def update_menu_and_icon():
    global icon, daemon_manager, args
    current_status = get_daemon_status()
    current_state = daemon_manager.get_current_state(current_status)
    log_message(f"Updating menu and icon. Current state: {
        current_state.name}", level=logging.INFO)

    # Update menu
    new_menu = pystray.Menu(*daemon_manager.get_menu_items(current_status))
    icon.menu = new_menu
    icon.update_menu()
    log_message("Menu updated", level=logging.DEBUG)

    # Update icon in a separate thread
    threading.Thread(target=update_icon, args=(
        current_status, current_state)).start()


def update_icon(current_status, current_state):
    global icon, daemon_manager, args
    log_message("Starting icon update", level=logging.DEBUG)
    new_icon = create_status_image(
        daemon_manager.get_icon_color(current_status),
        daemon_manager.get_icon_text(current_status),
        style=args.icon_style,
        thickness=args.icon_thickness
    )
    log_message("New icon created", level=logging.DEBUG)
    icon.icon = new_icon
    log_message(f"Icon color updated to: {
        daemon_manager.get_icon_color(current_status)}", level=logging.DEBUG)
    log_message("Icon update completed", level=logging.DEBUG)


def main():
    run_tray()


if __name__ == "__main__":
    main()
