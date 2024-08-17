#!/usr/bin/env python3

from PIL import ImageFont
from pystray import MenuItem as item
import tkinter
from tkinter import ttk
import pystray
from PIL import Image, ImageDraw, ImageFont
import socket
import json
import threading
import time
import subprocess
import os
import enum
from dataclasses import dataclass
import math
from io import BytesIO
from cairosvg import svg2png
import argparse

global daemon_manager
daemon_manager = None


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
        self.state = DaemonState.INITIAL
        self.status = {}
        self.config_changed_on_disk = False
        self.state_info = {
            DaemonState.INITIAL: StateInfo(Colors.GRAY, self._get_initial_menu_items, "INIT"),
            DaemonState.STARTING: StateInfo(Colors.YELLOW, self._get_starting_menu_items, "START"),
            DaemonState.RUNNING: StateInfo(Colors.GREEN, self._get_running_menu_items, "RUN"),
            DaemonState.SYNCING: StateInfo(Colors.BLUE, self._get_running_menu_items, "SYNC"),
            DaemonState.ERROR: StateInfo(Colors.GRAY, self._get_error_menu_items, "ERR"),
            DaemonState.SHUTTING_DOWN: StateInfo(Colors.PURPLE, self._get_shutting_down_menu_items, "STOP"),
            DaemonState.SYNC_ISSUES: StateInfo(Colors.ORANGE, self._get_running_menu_items, "WARN"),
            DaemonState.CONFIG_INVALID: StateInfo(Colors.RED, self._get_running_menu_items, "CFG!"),
            DaemonState.CONFIG_CHANGED: StateInfo(Colors.AMBER, self._get_running_menu_items, "CFG?"),
            DaemonState.LIMBO: StateInfo(Colors.YELLOW, self._get_limbo_menu_items, "LIMBO"),
        }

    def update_state(self, status):
        self.status = status
        self.config_changed_on_disk = status.get(
            'config_changed_on_disk', False)
        if status.get("shutting_down"):
            self.state = DaemonState.SHUTTING_DOWN
        elif "error" in status:
            self.state = DaemonState.ERROR
        elif status.get("in_limbo"):
            self.state = DaemonState.LIMBO
        elif status.get("config_invalid"):
            self.state = DaemonState.CONFIG_INVALID
        elif status.get("config_changed_on_disk"):
            self.state = DaemonState.CONFIG_CHANGED
        elif status.get("currently_syncing"):
            self.state = DaemonState.SYNCING
        elif self._has_sync_issues(status):
            self.state = DaemonState.SYNC_ISSUES
        else:
            self.state = DaemonState.RUNNING

    def _has_sync_issues(self, status):
        return any(
            job["sync_status"] not in ["COMPLETED", "NONE", None] or
            job["resync_status"] not in ["COMPLETED", "NONE", None] or
            job.get("hash_warnings", False)
            for job in status.get("sync_jobs", {}).values()
        )

    def _get_initial_menu_items(self):
        return [
            pystray.MenuItem("Starting...", None, enabled=False),
            pystray.MenuItem("Exit", lambda: icon.stop())
        ]

    def _get_starting_menu_items(self):
        return [
            pystray.MenuItem("Starting daemon...", None, enabled=False),
            pystray.MenuItem("Exit", lambda: icon.stop())
        ]

    def _get_error_menu_items(self):
        return [
            pystray.MenuItem("Start Daemon", start_daemon),
            pystray.MenuItem("Exit", lambda: icon.stop())
        ]

    def _get_shutting_down_menu_items(self):
        menu_items = [
            pystray.MenuItem("Shutting down...", None, enabled=False),
        ]

        currently_syncing = self.status.get('currently_syncing')
        if currently_syncing and currently_syncing != 'None':
            menu_items.append(pystray.MenuItem(f"Waiting for sync to finish:\n  {
                              currently_syncing}", None, enabled=False))

        queued_jobs = self.status.get('queued_paths', [])
        if queued_jobs:
            queued_jobs_str = "Queued jobs:\n" + \
                "\n".join(f"  {job}" for job in queued_jobs)
            menu_items.append(pystray.MenuItem(
                queued_jobs_str, None, enabled=False))

        menu_items.append(pystray.MenuItem("Exit", lambda: icon.stop()))
        return menu_items

    def _get_limbo_menu_items(self):
        error_message = self.status.get(
            'config_error_message', 'Unknown error')
        truncated_error = (
            error_message[:50] + '...') if len(error_message) > 50 else error_message

        menu_items = []
        menu_items.append(pystray.MenuItem(
            "Daemon in limbo state", None, enabled=False))
        menu_items.append(pystray.MenuItem(
            f"Error: {truncated_error}", None, enabled=False))
        menu_items.append(pystray.MenuItem(
            "Show Full Error Details", self.show_error_details))
        menu_items.append(pystray.Menu.SEPARATOR)
        menu_items.append(pystray.MenuItem("Reload Config", reload_config))
        if self.status.get("config_changed_on_disk", True):
            menu_items.append(pystray.MenuItem(
                "⚠️ Config changed on disk", None, enabled=False))
        else:
            menu_items.append(pystray.MenuItem(
                "NO Change", None, enabled=False))
        menu_items.append(pystray.Menu.SEPARATOR)
        menu_items.append(pystray.MenuItem("Shutdown Daemon", stop_daemon))
        menu_items.append(pystray.MenuItem("Exit", lambda: icon.stop()))

        if self.status.get("config_invalid", False):
            menu_items.insert(1, pystray.MenuItem(
                "⚠️ Config is invalid", None, enabled=False))
        return menu_items

    def show_error_details(self):
        error_message = self.status.get(
            'config_error_message', 'Unknown error')
        show_text_window("Configuration Error", error_message)

    def _get_running_menu_items(self):
        # Regenerate menu items each time this method is called
        menu_items = []

        if self.status.get("config_invalid", False):
            menu_items.append(pystray.MenuItem(
                "⚠️ Config is invalid", None, enabled=False))

        if self.config_changed_on_disk:
            menu_items.append(pystray.MenuItem(
                "⚠️ Config changed on disk", None, enabled=False))

        if self._has_sync_issues(self.status):
            menu_items.append(pystray.MenuItem(
                "⚠️ Sync issues detected", None, enabled=False))

        currently_syncing = self.status.get('currently_syncing', 'None')
        menu_items.append(pystray.MenuItem(f"Currently syncing:\n  {
                          currently_syncing}", None, enabled=False))

        queued_jobs = self.status.get('queued_paths', [])
        if queued_jobs:
            queued_jobs_str = "Queued jobs:\n" + \
                "\n".join(f"  {job}" for job in queued_jobs)
            menu_items.append(pystray.MenuItem(
                queued_jobs_str, None, enabled=False))
        else:
            menu_items.append(pystray.MenuItem(
                "Queued jobs:\n  None", None, enabled=False))

        menu_items.append(pystray.Menu.SEPARATOR)

        is_shutting_down = self.status.get('shutting_down', False)

        if "sync_jobs" in self.status and not self.status.get('config_invalid', False) and not is_shutting_down:
            jobs_submenu = []
            for job_key, job_status in reversed(self.status["sync_jobs"].items()):
                last_sync = job_status['last_sync'].replace(
                    'T', ' ')[:16] if job_status['last_sync'] else 'Never'
                next_run = job_status['next_run'].replace(
                    'T', ' ')[:16] if job_status['next_run'] else 'Not scheduled'

                job_submenu = pystray.Menu(
                    pystray.MenuItem("    ⚡ Sync Now",
                                     create_sync_now_handler(job_key)),
                    pystray.MenuItem(f"    Last sync: {
                                     last_sync}", None, enabled=False),
                    pystray.MenuItem(f"    Next run: {
                                     next_run}", None, enabled=False),
                    pystray.MenuItem(f"    Sync status: {
                                     job_status['sync_status']}", None, enabled=False),
                    pystray.MenuItem(f"    Resync status: {
                                     job_status['resync_status']}", None, enabled=False),
                )
                jobs_submenu.append(pystray.MenuItem(
                    f"  {job_key}", job_submenu))

            menu_items.append(pystray.MenuItem(
                "Sync Jobs", pystray.Menu(*reversed(jobs_submenu))))
        else:
            menu_items.append(pystray.MenuItem(
                "Sync Jobs", None, enabled=False))

        menu_items.extend([
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Config & Logs", pystray.Menu(
                pystray.MenuItem("Reload Config", reload_config, enabled=self.status.get(
                    'currently_syncing') == None and not is_shutting_down),
                pystray.MenuItem("Open Config Folder", open_config_file),
                pystray.MenuItem("Open Log Folder", open_log_folder)
            )),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Show Status Window",
                             show_status_window, enabled=not is_shutting_down),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Shutting down..." if is_shutting_down else "Stop Daemon",
                             stop_daemon, enabled=not is_shutting_down),
            pystray.MenuItem("Exit", lambda: icon.stop())
        ])

        return menu_items

    def get_icon_color(self):
        return self.state_info[self.state].color

    def get_menu_items(self):
        menu_items = self.state_info[self.state].menu_items
        return menu_items() if callable(menu_items) else menu_items

    def get_icon_text(self):
        return self.state_info[self.state].icon_text


def get_daemon_status():
    socket_path = '/tmp/rclone_bisync_manager_status.sock'
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(socket_path)
        client.sendall(b"STATUS")
        status = json.loads(client.recv(4096).decode())
        client.close()
        return status
    except Exception as e:
        return {"error": str(e)}


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
        print("Daemon is shutting down. Use 'daemon status' to check progress.")
    except Exception as e:
        print(f"Error stopping daemon: {e}")


def start_daemon():
    try:
        subprocess.run(
            ["rclone-bisync-manager", "daemon", "start"], check=True)
        print("Daemon started successfully")
    except subprocess.CalledProcessError as e:
        print(f"Error starting daemon: {e}")


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
                print("Configuration reloaded successfully")
            else:
                print(f"Error reloading configuration: {
                      response_data['message']}")

            current_status = get_daemon_status()
            daemon_manager.update_state(current_status)
            icon.menu = pystray.Menu(*daemon_manager.get_menu_items())
            icon.update_menu()

            return response_data["status"] == "success"
        except json.JSONDecodeError:
            print(f"Error: Invalid JSON response from daemon: {response}")
            return False
    except Exception as e:
        print(f"Error communicating with daemon: {str(e)}")
        return False


def add_to_sync_queue(job_key):
    socket_path = '/tmp/rclone_bisync_manager_add_sync.sock'
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(socket_path)
        client.sendall(json.dumps([job_key]).encode())
        response = client.recv(1024).decode()
        client.close()
        print(f"Add to sync queue response: {response}")
    except Exception as e:
        print(f"Error adding job to sync queue: {str(e)}")


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
    window.geometry("400x300")

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
                      job_status['last_sync'] or 'Never'}").pack(anchor='w')
            ttk.Label(job_frame, text=f"Next run: {
                      job_status['next_run'] or 'Not scheduled'}").pack(anchor='w')
            ttk.Label(job_frame, text=f"Sync status: {
                      job_status['sync_status']}").pack(anchor='w')
            ttk.Label(job_frame, text=f"Resync status: {
                      job_status['resync_status']}").pack(anchor='w')

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
        print("Config file path not found")


def open_log_folder():
    log_file_path = get_log_file_path()
    if log_file_path:
        log_dir = os.path.dirname(log_file_path)
        if os.name == 'nt':  # For Windows
            os.startfile(log_dir)
        elif os.name == 'posix':  # For macOS and Linux
            subprocess.call(('xdg-open', log_dir))
    else:
        print("Log file path not found")


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
    global icon, daemon_manager, args
    daemon_manager = DaemonManager()

    parser = argparse.ArgumentParser()
    parser.add_argument('--icon-style', type=int,
                        choices=[1, 2], default=1, help='Choose icon style: 1 or 2')
    parser.add_argument('--icon-thickness', type=int,
                        default=40, help='Set the thickness of the icon lines')
    args = parser.parse_args()

    icon = pystray.Icon("rclone-bisync-manager",
                        create_status_image(daemon_manager.get_icon_color(),
                                            daemon_manager.get_icon_text(),
                                            style=args.icon_style,
                                            thickness=args.icon_thickness),
                        "RClone BiSync Manager")
    icon.menu = pystray.Menu(*daemon_manager.get_menu_items())

    # Start the status checking thread, passing args
    threading.Thread(target=check_status_and_update,
                     args=(args,), daemon=True).start()

    icon.run()


def check_status_and_update(args):
    global icon, daemon_manager
    initial_startup = True
    last_state = None
    last_status = None

    while True:
        try:
            current_status = get_daemon_status()

            if initial_startup:
                if "error" in current_status:
                    print("Daemon not running. Attempting to start...")
                    start_daemon()
                    time.sleep(2)  # Give the daemon some time to start
                    current_status = get_daemon_status()
                initial_startup = False

            daemon_manager.update_state(current_status)
            current_state = daemon_manager.state

            # Check if state or relevant status details have changed
            status_changed = (
                current_state != last_state or
                current_status.get('currently_syncing') != last_status.get('currently_syncing') or
                current_status.get('queued_paths') != last_status.get('queued_paths') or
                current_status.get('sync_jobs') != last_status.get('sync_jobs') or
                current_status.get('config_invalid') != last_status.get('config_invalid') or
                current_status.get('config_changed_on_disk') != last_status.get(
                    'config_changed_on_disk')
            ) if last_status is not None else True

            if status_changed:
                print(f"State or status changed. Updating menu and icon.")
                new_menu = create_menu(daemon_manager.get_menu_items())
                new_icon = create_status_image(
                    daemon_manager.get_icon_color(),
                    daemon_manager.get_icon_text(),
                    style=args.icon_style,
                    thickness=args.icon_thickness
                )

                icon.menu = new_menu
                icon.icon = new_icon
                icon.update_menu()

                last_state = current_state
                last_status = current_status.copy()  # Create a copy to avoid reference issues

        except Exception as e:
            print(f"Error in check_status_and_update: {e}")

        time.sleep(1)


def create_menu(menu_items):
    return pystray.Menu(*menu_items)


def main():
    run_tray()


if __name__ == "__main__":
    main()
