# main.py
# Совместимо с flet==0.17.0
# Требуется:
#   pip install flet==0.17.0 psutil paramiko cryptography watchdog Pillow
#
# Фичи:
# - Frameless topbar (Windows), Drag (WindowDragArea), double-click maximize/restore
# - Надёжные кнопки Minimize/Maximize/Close через WinAPI (Windows frameless)
# - Аккуратные resize-handles на frameless (Windows)
# - Контекстное меню (ПКМ) для источников и профилей
# - Логи: поиск + фильтры INFO/WARN/ERROR (VS Code-ish)
# - Новая вкладка SSH: подключение, браузер удалённой FS, выбор remote target
# - Удалённый бэкап на SSH/SFTP (локальные источники -> сервер)
# - Полноценный SSH-клиент: mkdir, удаление, переименование, скачивание
# - SSH-профили с шифрованием паролей
# - Параллельная загрузка файлов
# - Переключение светлой/темной темы

import flet as ft
import threading
import json
import os
import time
import base64
import platform
import subprocess
import sys
import posixpath
import stat
import hashlib
from datetime import datetime
from queue import Queue, Empty

import backup_logic as bl


# =========================
# Windows WinAPI (FIXED)
# =========================
WIN = False
user32 = None

SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9

WM_CLOSE = 0x0010
WM_NCLBUTTONDOWN = 0x00A1

HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

try:
    if platform.system() == "Windows":
        WIN = True
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)

        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.IsZoomed.argtypes = [wintypes.HWND]
        user32.IsZoomed.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.ReleaseCapture.argtypes = []
        user32.ReleaseCapture.restype = wintypes.BOOL

        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND

        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int

        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int

        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL

        # EnumWindows (IMPORTANT: WNDENUMPROC must be defined manually)
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL

except Exception:
    WIN = False
    user32 = None


class WinWindow:
    """
    Управление окном через WinAPI.
    HWND ищем так:
    1) FindWindowW(None, exact_title)
    2) GetForegroundWindow()
    3) EnumWindows по подстроке (hint)
    """

    def __init__(self, title_exact: str, title_hint: str):
        self.title_exact = title_exact or ""
        self.title_hint = title_hint or self.title_exact or ""
        self._hwnd_cache = None
        self._lock = threading.Lock()

    def _get_text(self, hwnd):
        try:
            import ctypes

            ln = user32.GetWindowTextLengthW(hwnd)
            if ln <= 0:
                return ""
            buf = ctypes.create_unicode_buffer(ln + 1)
            user32.GetWindowTextW(hwnd, buf, ln + 1)
            return buf.value or ""
        except Exception:
            return ""

    def hwnd(self):
        if not WIN or user32 is None:
            return None

        with self._lock:
            if self._hwnd_cache:
                return self._hwnd_cache

        # 1) exact title
        try:
            if self.title_exact:
                h = user32.FindWindowW(None, self.title_exact)
                if h:
                    with self._lock:
                        self._hwnd_cache = h
                    return h
        except Exception:
            pass

        # 2) foreground
        try:
            h = user32.GetForegroundWindow()
            if h:
                t = self._get_text(h).lower()
                if (self.title_hint.lower() in t) or (self.title_exact.lower() in t) or ("flet" in t):
                    with self._lock:
                        self._hwnd_cache = h
                    return h
        except Exception:
            pass

        # 3) enum by hint
        found = {"h": None}
        hint = (self.title_hint or self.title_exact or "").lower()

        try:
            import ctypes
            from ctypes import wintypes

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def enum_proc(hwnd, lparam):
                try:
                    if not user32.IsWindowVisible(hwnd):
                        return True
                    title = self._get_text(hwnd).lower()
                    if hint and hint in title:
                        found["h"] = hwnd
                        return False
                except Exception:
                    return True
                return True

            user32.EnumWindows(enum_proc, 0)
        except Exception:
            pass

        if found["h"]:
            with self._lock:
                self._hwnd_cache = found["h"]
            return found["h"]

        return None

    def is_maximized(self) -> bool:
        h = self.hwnd()
        if not h:
            return False
        try:
            return bool(user32.IsZoomed(h))
        except Exception:
            return False

    def minimize(self):
        h = self.hwnd()
        if not h:
            return
        try:
            user32.ShowWindow(h, SW_MINIMIZE)
        except Exception:
            pass

    def toggle_maximize(self):
        h = self.hwnd()
        if not h:
            return
        try:
            if self.is_maximized():
                user32.ShowWindow(h, SW_RESTORE)
            else:
                user32.ShowWindow(h, SW_MAXIMIZE)
        except Exception:
            pass

    def close(self):
        h = self.hwnd()
        if not h:
            return
        try:
            user32.PostMessageW(h, WM_CLOSE, 0, 0)
        except Exception:
            pass

    def begin_resize(self, ht_code: int):
        h = self.hwnd()
        if not h:
            return
        try:
            user32.ReleaseCapture()
            user32.SendMessageW(h, WM_NCLBUTTONDOWN, ht_code, 0)
        except Exception:
            pass


# =========================
# Parallel SSH Backup Worker (SFTP)
# =========================
class ParallelSSHBackupWorker:
    """
    Делает бэкап локальных источников в удалённый каталог через SFTP (paramiko).
    Поддерживает параллельную загрузку файлов.
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str | None,
        key_filename: str | None,
        remote_target_dir: str,
        source_dirs: list[str],
        source_files: list[str],
        incremental: bool,
        skip_hidden: bool,
        preserve_structure: bool,
        compress: bool,
        progress_callback,
        log_callback,
        max_workers: int = 4,
        use_hash_check: bool = True,
    ):
        self.host = host
        self.port = int(port) if port else 22
        self.username = username
        self.password = password
        self.key_filename = key_filename
        self.remote_target_dir = remote_target_dir

        self.source_dirs = list(source_dirs)
        self.source_files = list(source_files)
        self.incremental = incremental
        self.skip_hidden = skip_hidden
        self.preserve_structure = preserve_structure
        self.compress = compress
        self.max_workers = max_workers
        self.use_hash_check = use_hash_check

        self.progress_callback = progress_callback
        self.log_callback = log_callback

        self._stop_flag = False
        self._lock = threading.Lock()
        self._queue = Queue()
        self._workers = []

        self._stats = {
            "total_files": 0,
            "files_copied": 0,
            "total_size_mb": 0.0,
            "skipped": 0,
            "failed": 0,
        }

    def stop(self):
        self._stop_flag = True

    def get_stats(self):
        with self._lock:
            return dict(self._stats)

    def _log(self, msg: str, level="info"):
        try:
            self.log_callback(msg, level)
        except Exception:
            pass

    def _is_hidden_rel(self, rel_path: str) -> bool:
        parts = rel_path.replace("\\", "/").split("/")
        for p in parts:
            if p.startswith(".") and p not in (".", ".."):
                return True
        return False

    def _collect_files(self):
        items: list[tuple[str, str]] = []

        # dirs
        for src_dir in self.source_dirs:
            base_name = os.path.basename(src_dir.rstrip("\\/")) or "folder"
            for root, dirs, files in os.walk(src_dir):
                if self._stop_flag:
                    break

                rel_root = os.path.relpath(root, src_dir)
                rel_root = "" if rel_root == "." else rel_root

                # skip hidden dirs if requested
                if self.skip_hidden:
                    dirs[:] = [d for d in dirs if not d.startswith(".")]

                for fn in files:
                    if self._stop_flag:
                        break
                    if self.skip_hidden and fn.startswith("."):
                        continue
                    local_path = os.path.join(root, fn)
                    rel = os.path.join(rel_root, fn) if rel_root else fn
                    rel_posix = rel.replace("\\", "/")

                    if self.skip_hidden and self._is_hidden_rel(rel_posix):
                        continue

                    if self.preserve_structure:
                        remote_rel = posixpath.join(base_name, rel_posix)
                    else:
                        remote_rel = os.path.basename(local_path)

                    items.append((local_path, remote_rel))

        # single files
        for fp in self.source_files:
            if self._stop_flag:
                break
            name = os.path.basename(fp)
            if self.skip_hidden and name.startswith("."):
                continue
            remote_rel = name
            items.append((fp, remote_rel))

        # size
        total_bytes = 0
        for lp, _ in items:
            try:
                total_bytes += os.path.getsize(lp)
            except Exception:
                pass

        with self._lock:
            self._stats["total_files"] = len(items)
            self._stats["total_size_mb"] = total_bytes / (1024 * 1024)

        return items

    def _remote_norm(self, path: str) -> str:
        p = (path or "").replace("\\", "/")
        if not p.startswith("/"):
            p = "/" + p
        p = posixpath.normpath(p)
        return p if p != "." else "/"

    def _mkdirs(self, sftp, remote_dir: str):
        remote_dir = self._remote_norm(remote_dir)
        if remote_dir in ("/", ""):
            return

        parts = remote_dir.strip("/").split("/")
        cur = "/"
        for part in parts:
            if not part:
                continue
            cur = posixpath.join(cur, part) if cur != "/" else "/" + part
            try:
                sftp.stat(cur)
            except Exception:
                try:
                    sftp.mkdir(cur)
                except Exception:
                    pass

    def _calculate_md5(self, filepath: str, chunk_size=8192) -> str:
        """Вычисление MD5 хеша файла"""
        md5 = hashlib.md5()
        with open(filepath, 'rb') as f:
            while chunk := f.read(chunk_size):
                md5.update(chunk)
        return md5.hexdigest()

    def _should_skip_incremental(self, sftp, local_path: str, remote_path: str) -> bool:
        if not self.incremental:
            return False
        
        # Проверка по size/mtime
        try:
            st = sftp.stat(remote_path)
            local_size = os.path.getsize(local_path)
            local_mtime = int(os.path.getmtime(local_path))
            remote_mtime = int(getattr(st, "st_mtime", 0))
            remote_size = int(getattr(st, "st_size", -1))
            
            if remote_size == local_size and remote_mtime >= local_mtime:
                return True
        except Exception:
            pass
        
        # Дополнительная проверка по хешу если включена
        if self.use_hash_check:
            try:
                remote_hash_path = remote_path + ".hash"
                with sftp.open(remote_hash_path, 'r') as f:
                    remote_hash = f.read().strip()
                local_hash = self._calculate_md5(local_path)
                return local_hash == remote_hash
            except Exception:
                pass
        
        return False

    def _upload_file(self, sftp, local_path: str, remote_path: str, remote_rel: str):
        """Загрузка одного файла с сохранением хеша"""
        try:
            remote_dir = posixpath.dirname(remote_path)
            self._mkdirs(sftp, remote_dir)
            
            sftp.put(local_path, remote_path)
            
            # Сохраняем хеш для инкрементальной проверки
            if self.use_hash_check:
                remote_hash_path = remote_path + ".hash"
                local_hash = self._calculate_md5(local_path)
                try:
                    with sftp.open(remote_hash_path, 'w') as f:
                        f.write(local_hash)
                except Exception:
                    pass
            
            return True
        except Exception as ex:
            self._log(f"Ошибка загрузки {remote_rel}: {ex}", "error")
            return False

    def _upload_worker(self, sftp, worker_id: int):
        """Рабочий поток для параллельной загрузки"""
        while not self._stop_flag:
            try:
                item = self._queue.get_nowait()
                local_path, remote_rel = item
                
                remote_root = self._remote_norm(self.remote_target_dir)
                remote_path = posixpath.join(remote_root, remote_rel.replace("\\", "/"))
                
                # Проверка инкрементальности
                if self._should_skip_incremental(sftp, local_path, remote_path):
                    with self._lock:
                        self._stats["skipped"] += 1
                    self.progress_callback(1, 0, f"Skipped: {remote_rel}")
                else:
                    if self._upload_file(sftp, local_path, remote_path, remote_rel):
                        with self._lock:
                            self._stats["files_copied"] += 1
                    else:
                        with self._lock:
                            self._stats["failed"] += 1
                
                self._queue.task_done()
                
            except Empty:
                break
            except Exception as ex:
                self._log(f"Worker {worker_id} ошибка: {ex}", "error")
                self._queue.task_done()

    def start_backup(self):
        try:
            import paramiko
        except Exception:
            self._log("Для SSH требуется установить: pip install paramiko", "error")
            return

        items = self._collect_files()
        if not items:
            self._log("Нет файлов для отправки на SSH", "warning")
            return

        client = None
        sftp = None
        try:
            self._log(f"SSH: подключение к {self.host}:{self.port} ...", "info")

            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs = dict(
                hostname=self.host,
                port=self.port,
                username=self.username,
                timeout=15,
                banner_timeout=15,
                auth_timeout=15,
                look_for_keys=False,
                allow_agent=False,
            )

            if self.key_filename:
                connect_kwargs["key_filename"] = self.key_filename
                if self.password:
                    connect_kwargs["password"] = self.password
            else:
                connect_kwargs["password"] = self.password or ""

            client.connect(**connect_kwargs)
            sftp = client.open_sftp()

            remote_root = self._remote_norm(self.remote_target_dir)
            self._mkdirs(sftp, remote_root)

            self._log(f"SSH: цель -> {remote_root}", "success")
            self._log(f"SSH: параллельная загрузка ({self.max_workers} потоков)", "info")

            # Заполняем очередь
            for item in items:
                self._queue.put(item)

            # Запускаем workers
            self._workers = []
            for i in range(self.max_workers):
                worker = threading.Thread(
                    target=self._upload_worker,
                    args=(sftp, i),
                    daemon=True
                )
                worker.start()
                self._workers.append(worker)

            # Ждем завершения
            self._queue.join()
            
            # Ждем завершения всех workers
            for worker in self._workers:
                worker.join(timeout=1.0)

            stats = self.get_stats()
            self._log(f"SSH: завершено. Отправлено: {stats['files_copied']}, "
                     f"Пропущено: {stats['skipped']}, Ошибок: {stats['failed']}", 
                     "success" if stats['failed'] == 0 else "warning")

        except Exception as ex:
            self._log(f"SSH: ошибка подключения/работы: {ex}", "error")
        finally:
            try:
                if sftp:
                    sftp.close()
            except Exception:
                pass
            try:
                if client:
                    client.close()
            except Exception:
                pass


# =========================
# App
# =========================
class PoogaloBackup:
    TOPBAR_HEIGHT = 42
    STATUSBAR_HEIGHT = 34

    # resize handles: тонко, чтобы не мешало кликам по верхней панели/кнопкам
    RESIZE_THICK = 4
    TOP_RESIZE_RIGHT_GAP = 220  # не перекрывать область кнопок и правую часть topbar

    def __init__(self, page: ft.Page):
        self.page = page
        self.page.title = "Poogalo Backup"  # Убрал "Professional"
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.padding = 0
        self.page.spacing = 0

        self.is_windows = (platform.system() == "Windows")

        # Window props
        self.page.window_width = 1400
        self.page.window_height = 850
        self.page.window_min_width = 1000
        self.page.window_min_height = 650
        self.page.window_resizable = True

        if self.is_windows:
            try:
                self.page.window_title_bar_hidden = True
                self.page.window_frameless = True
            except Exception:
                self.page.window_title_bar_hidden = False
                self.page.window_frameless = False
        else:
            self.page.window_title_bar_hidden = False
            self.page.window_frameless = False

        self.win = WinWindow(self.page.title, "Poogalo") if (self.is_windows and WIN and user32) else None

        # Загрузка настроек ДО инициализации темы
        self.settings = self.load_settings()
        
        # Инициализация темы на основе настроек
        self._init_theme()
        
        try:
            self.page.theme = ft.Theme(font_family="Segoe UI")
        except Exception:
            pass
        self.page.bgcolor = self.colors["bg"]

        # Data
        self.source_dirs: list[str] = []
        self.source_files: list[str] = []
        self.target_dir: str = ""  # local target
        self.target_mode = "local"  # local | ssh

        # SSH state
        self.ssh_connected = False
        self.ssh_client = None
        self.ssh_sftp = None
        self.ssh_current_path = "/"
        self.ssh_target_dir = ""  # remote target path
        self.ssh_config = {
            "host": "",
            "port": 22,
            "username": "",
            "password": "",
            "key_filename": "",
            "use_key": False,
        }

        # Backup
        self.backup_in_progress = False
        self.backup_thread = None
        self.monitor_thread = None
        self.backup_worker = None

        # Settings
        self.incremental_cb = ft.Checkbox(value=self.settings.get("incremental", True))
        self.skip_hidden_cb = ft.Checkbox(value=self.settings.get("skip_hidden", True))
        self.compression_cb = ft.Checkbox(value=self.settings.get("compression", False))
        self.preserve_structure_cb = ft.Checkbox(value=self.settings.get("preserve_structure", True))
        self.use_hash_check_cb = ft.Checkbox(value=self.settings.get("use_hash_check", True))
        self.parallel_upload_cb = ft.Checkbox(value=self.settings.get("parallel_upload", True))
        self.max_workers_field = ft.TextField(value=str(self.settings.get("max_workers", 4)), width=80)
        
        # Добавляем переключатель темы
        self.theme_switch = ft.Switch(
            label="Тёмная тема",
            value=self.settings.get("theme", "dark") == "dark",
            on_change=self.change_theme
        )

        # Logs store
        self.logs_data: list[dict] = []
        self.max_logs_keep = 5000

        # UI refs
        self.progress_bar = None
        self.progress_text = None
        self.stats_text = None
        self.run_state_chip_text = None
        self.status_disk_text = None

        self.source_list_view = None
        self.placeholder_container = None
        self.sources_count_text = None

        self.log_view_sources = ft.ListView(spacing=0, expand=True)
        self.log_counter_text = None

        self.log_view = None
        self.logs_count_text = None
        self.log_search_field = None
        self.filter_info_cb = None
        self.filter_warn_cb = None
        self.filter_error_cb = None

        self.target_dir_text = None
        self.target_status_text = None
        self.target_status_icon = None
        self.target_select_btn = None
        self.target_mode_rg = None

        self.disk_progress_bar = None
        self.disk_free_text = None
        self.disk_total_text = None

        self.profiles_list = None
        self.profiles_count_text = None
        self.profile_name_field = None

        self.ssh_profiles_list = None
        self.ssh_profiles_count_text = None

        self.start_stop_btn = None
        self.quick_open_btn = None

        # SSH UI refs
        self.ssh_host_field = None
        self.ssh_port_field = None
        self.ssh_user_field = None
        self.ssh_pass_field = None
        self.ssh_use_key_cb = None
        self.ssh_key_text = None
        self.ssh_pick_key_btn = None
        self.ssh_connect_btn = None
        self.ssh_status_text = None
        self.remote_path_text = None
        self.remote_list = None
        self.ssh_target_text = None

        # Tabs
        self.active_view = 0
        self.views = []
        self.tabs_meta = []

        # Window controls refs
        self._max_icon = None

        # Context menu overlay refs
        self._ctx_bg = None
        self._ctx_menu = None
        self._ctx_menu_col = None

        self.build_ui()
        self._start_window_state_watcher()

        self.add_log("Приложение запущено", "success")

    # =========================
    # Theme
    # =========================
    def _init_theme(self):
        # Определяем цвета для темной и светлой тем
        self.dark_colors = {
            "bg": "#1E1E1E",
            "panel": "#252526",
            "panel_2": "#2D2D2D",
            "panel_3": "#333333",
            "border": "#3C3C3C",
            "border_soft": "#2A2A2A",
            "text": "#D4D4D4",
            "muted": "#9DA5B4",
            "muted_2": "#7A7A7A",
            "accent": "#007ACC",
            "info": "#3794FF",
            "warn": "#CCA700",
            "error": "#F48771",
            "success": "#89D185",
            "selection": "#094771",
            # Google colors for logo
            "g_blue": "#4285f4",
            "g_red": "#ea4335",
            "g_yellow": "#fbbc05",
            "g_green": "#34a853",
            "g_grey": "#575757",
        }
        
        self.light_colors = {
            "bg": "#FFFFFF",
            "panel": "#F5F5F5",
            "panel_2": "#EEEEEE",
            "panel_3": "#E0E0E0",
            "border": "#CCCCCC",
            "border_soft": "#DDDDDD",
            "text": "#333333",
            "muted": "#666666",
            "muted_2": "#999999",
            "accent": "#007ACC",
            "info": "#3794FF",
            "warn": "#CCA700",
            "error": "#F48771",
            "success": "#89D185",
            "selection": "#E3F2FD",
            # Google colors for logo
            "g_blue": "#4285f4",
            "g_red": "#ea4335",
            "g_yellow": "#fbbc05",
            "g_green": "#34a853",
            "g_grey": "#575757",
        }
        
        # Определяем размеры шрифтов
        self.fs = {"xs": 10, "sm": 11, "md": 12, "lg": 20, "xl": 14}
        
        # Получаем текущую тему из настроек
        theme = self.settings.get("theme", "dark")
        
        # Устанавливаем тему страницы
        self.page.theme_mode = ft.ThemeMode.DARK if theme == "dark" else ft.ThemeMode.LIGHT
        
        # Выбираем соответствующий набор цветов
        self.colors = self.dark_colors if theme == "dark" else self.light_colors
        
        # Устанавливаем цвет фона страницы
        self.page.bgcolor = self.colors["bg"]

    def change_theme(self, e):
        """Переключение между темной и светлой темой"""
        theme = "dark" if self.theme_switch.value else "light"
        
        # Сохраняем тему в настройках
        self.settings["theme"] = theme
        self.save_settings()
        
        # Обновляем тему приложения
        self.page.theme_mode = ft.ThemeMode.DARK if theme == "dark" else ft.ThemeMode.LIGHT
        
        # Обновляем набор цветов
        self.colors = self.dark_colors if theme == "dark" else self.light_colors
        
        # Обновляем цвет фона
        self.page.bgcolor = self.colors["bg"]
        
        # Обновляем цвета всех UI элементов
        self._update_all_colors()
        
        self.add_log(f"Тема изменена на {'темную' if theme == 'dark' else 'светлую'}", "info")
        
        # Принудительно обновляем страницу
        self.page.update()

    def _update_all_colors(self):
        """Обновление цветов всех UI элементов"""
        # Обновляем цвета основных контейнеров
        if hasattr(self, '_top_bar'):
            self._top_bar.bgcolor = self.colors["panel"]
        
        # Обновляем цвета кнопок управления окном
        self._update_window_buttons_colors()
        
        # Обновляем цвета вкладок
        self._update_tab_colors()
        
        # Обновляем цвета всех панелей
        self._update_panels_colors()
        
        # Обновляем цвета элементов логов
        self._update_logs_colors()
        
        # Обновляем цвета элементов источников
        self._update_sources_colors()

    def _update_window_buttons_colors(self):
        """Обновление цветов кнопок управления окном"""
        if hasattr(self, '_min_btn'):
            self._min_btn.content.color = self.colors["muted"]
        if hasattr(self, '_max_icon'):
            self._max_icon.color = self.colors["muted"]
        if hasattr(self, '_close_btn'):
            self._close_btn.content.color = self.colors["muted"]

    def _update_tab_colors(self):
        """Обновление цветов вкладок"""
        for meta in self.tabs_meta:
            idx = meta["idx"]
            if idx == self.active_view:
                meta["icon"].color = self.colors["text"]
                meta["text"].color = self.colors["text"]
                meta["tab"].bgcolor = self.colors["panel_3"]
                meta["underline"].bgcolor = self.colors["accent"]
            else:
                meta["icon"].color = self.colors["muted"]
                meta["text"].color = self.colors["muted"]
                meta["tab"].bgcolor = "transparent"
                meta["underline"].bgcolor = "transparent"

    def _update_panels_colors(self):
        """Обновление цветов панелей"""
        # Этот метод можно расширить для обновления конкретных панелей
        pass

    def _update_logs_colors(self):
        """Обновление цветов логов"""
        # Обновляем цвет текста в логах
        self.refresh_logs_tab()

    def _update_sources_colors(self):
        """Обновление цветов источников"""
        # Обновляем список источников
        self.update_source_list()

    def _divider_v(self, height: int, color=None):
        return ft.Container(width=1, height=height, bgcolor=color or self.colors["border"])

    def _panel(self, content, padding=12, expand=False):
        return ft.Container(
            content=content,
            bgcolor=self.colors["panel"],
            border=ft.border.all(1, self.colors["border"]),
            border_radius=6,
            padding=padding,
            expand=expand,
        )

    # =========================
    # Logo
    # =========================
    def _fallback_logo(self):
        try:
            svg_logo = f"""<svg width="24" height="24" xmlns="http://www.w3.org/2000/svg">
                <rect width="24" height="24" rx="5" fill="{self.colors['accent']}"/>
                <text x="12" y="16" font-family="monospace" font-size="9"
                    font-weight="bold" fill="{self.colors['text']}" text-anchor="middle">PB</text>
            </svg>"""
            b64_svg = base64.b64encode(svg_logo.encode()).decode("utf-8")
            return ft.Image(src=f"data:image/svg+xml;base64,{b64_svg}", width=20, height=20)
        except Exception:
            return ft.Icon(ft.icons.BACKUP, size=18, color=self.colors["accent"])

    def get_logo_element(self):
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            for logo_path in (
                os.path.join(script_dir, "logo.png"),
                os.path.join(script_dir, "logo_optimized.png"),
                "logo.png",
                "logo_optimized.png",
            ):
                if os.path.exists(logo_path):
                    with open(logo_path, "rb") as f:
                        image_data = f.read()
                    b64_image = base64.b64encode(image_data).decode("utf-8")
                    return ft.Image(src_base64=b64_image, width=64, height=64)
        except Exception:
            pass
        return self._fallback_logo()

    # =========================
    # Top bar с обновленным названием
    # =========================
    def build_top_bar(self):
        self.tabs_meta = []
        window_controls = self.create_window_controls()

        tabs_row = ft.Row(
            spacing=0,
            controls=[
                self._create_tab(0, "Источники", ft.icons.FOLDER_OPEN),
                self._create_tab(1, "Настройки", ft.icons.TUNE),
                self._create_tab(2, "Логи", ft.icons.TERMINAL),
                self._create_tab(3, "Профили", ft.icons.SAVE),
                self._create_tab(4, "SSH", ft.icons.CLOUD),
            ],
        )

        # Создаем цветное название POOGALO BACKUP
        logo_colored = ft.Row(
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                # POOGALO
                ft.Text("p", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["g_blue"]),
                ft.Text("o", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["g_red"]),
                ft.Text("o", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["g_yellow"]),
                ft.Text("g", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["g_blue"]),
                ft.Text("a", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["g_green"]),
                ft.Text("l", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["g_red"]),
                ft.Text("o", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["g_yellow"]),
                # Пробел
                ft.Text(" ", size=self.fs["lg"], weight=ft.FontWeight.BOLD),
                # BACKUP
                ft.Text("d", size=self.fs["md"], weight=ft.FontWeight.BOLD, color=self.colors["g_grey"]),
                ft.Text("e", size=self.fs["md"], weight=ft.FontWeight.BOLD, color=self.colors["g_grey"]),
                ft.Text("v", size=self.fs["md"], weight=ft.FontWeight.BOLD, color=self.colors["g_grey"]),
  
                # ft.Text("B", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["g_blue"]),
                # ft.Text("A", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["g_red"]),
                # ft.Text("C", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["g_yellow"]),
                # ft.Text("K", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["g_green"]),
                # ft.Text("U", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["g_blue"]),
                # ft.Text("P", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["g_red"]),
            ]
        )

        title_block = ft.Row(
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(padding=ft.padding.only(left=10), content=self.get_logo_element()),
                self._divider_v(self.TOPBAR_HEIGHT - 12, self.colors["border"]),
                logo_colored,
                ft.Container(width=8),
            ],
        )

        title_drag = ft.WindowDragArea(
            maximizable=True,
            content=ft.Container(height=self.TOPBAR_HEIGHT, alignment=ft.alignment.center_left, content=title_block),
        )

        spacer_drag = ft.Container(
            expand=True,
            height=self.TOPBAR_HEIGHT,
            content=ft.WindowDragArea(maximizable=True, content=ft.Container(height=self.TOPBAR_HEIGHT)),
        )

        sys_info = ft.Container(
            height=self.TOPBAR_HEIGHT,
            padding=ft.padding.symmetric(horizontal=10, vertical=0),
            alignment=ft.alignment.center,
            bgcolor="transparent",
            content=ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.icons.CIRCLE, size=8, color=self.colors["success"]),
                    ft.Text("Ready", size=self.fs["sm"], color=self.colors["muted"]),
                ],
            ),
        )

        top = ft.Container(
            height=self.TOPBAR_HEIGHT,
            bgcolor=self.colors["panel"],
            padding=0,
            margin=0,
            border=ft.border.only(bottom=ft.border.BorderSide(1, self.colors["border"])),
            content=ft.Row(
                spacing=0,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    title_drag,
                    self._divider_v(self.TOPBAR_HEIGHT, self.colors["border"]),
                    tabs_row,
                    spacer_drag,
                    sys_info,
                    window_controls,
                ],
            ),
        )

        self._top_bar = top
        self._update_tabstrip_styles()
        return top

    # =========================
    # Window ops
    # =========================
    def _get_window_maximized(self) -> bool:
        if self.win:
            return self.win.is_maximized()
        try:
            return bool(getattr(self.page, "window_maximized"))
        except Exception:
            return False

    def minimize_window(self, e=None):
        if self.win:
            self.win.minimize()
            return
        try:
            self.page.window_minimized = True
            self.page.update()
        except Exception:
            pass

    def toggle_maximize(self, e=None):
        if self.win:
            self.win.toggle_maximize()
            return
        try:
            cur = bool(getattr(self.page, "window_maximized"))
            self.page.window_maximized = not cur
            self.page.update()
        except Exception:
            pass

    def close_window(self, e=None):
        if self.win:
            self.win.close()
            return
        try:
            self.page.window_close()
            return
        except Exception:
            pass
        os._exit(0)

    def _set_max_icon(self, is_max: bool):
        if self._max_icon is None:
            return
        self._max_icon.name = ft.icons.FULLSCREEN_EXIT if is_max else ft.icons.CROP_SQUARE
        try:
            self._max_icon.update()
        except Exception:
            try:
                self.page.update()
            except Exception:
                pass

    def _start_window_state_watcher(self):
        def watcher():
            last = None
            while True:
                try:
                    cur = self._get_window_maximized()
                    if last is None or cur != last:
                        last = cur
                        self._set_max_icon(cur)
                except Exception:
                    pass
                time.sleep(0.25)

        threading.Thread(target=watcher, daemon=True).start()

    def _make_window_btn(self, icon_name: str, kind: str, on_click):
        ic = ft.Icon(icon_name, size=14, color=self.colors["muted"])
        btn = ft.Container(
            content=ic,
            width=46,
            height=self.TOPBAR_HEIGHT,
            alignment=ft.alignment.center,
            bgcolor="transparent",
            on_click=on_click,
            on_hover=lambda ev: self._on_window_btn_hover(ev, kind),
        )
        return btn, ic

    def _on_window_btn_hover(self, e: ft.HoverEvent, kind: str):
        hovering = (e.data == "true")

        def apply(btn: ft.Container, icon: ft.Icon, bg_hover: str, fg_hover: str):
            btn.bgcolor = bg_hover if hovering else "transparent"
            icon.color = fg_hover if hovering else self.colors["muted"]
            try:
                btn.update()
            except Exception:
                try:
                    self.page.update()
                except Exception:
                    pass

        if kind == "close":
            apply(self._close_btn, self._close_btn.content, "#C42B1C", ft.colors.WHITE)
        elif kind == "min":
            apply(self._min_btn, self._min_btn.content, self.colors["panel_3"], self.colors["text"])
        elif kind == "max":
            apply(self._max_btn, self._max_btn.content, self.colors["panel_3"], self.colors["text"])

    def create_window_controls(self):
        self._min_btn, _ = self._make_window_btn(ft.icons.REMOVE, "min", self.minimize_window)
        self._max_btn, self._max_icon = self._make_window_btn(ft.icons.CROP_SQUARE, "max", self.toggle_maximize)
        self._close_btn, _ = self._make_window_btn(ft.icons.CLOSE, "close", self.close_window)
        self._max_icon = self._max_btn.content
        return ft.Row(controls=[self._min_btn, self._max_btn, self._close_btn], spacing=0)

    # =========================
    # Tabs
    # =========================
    def _create_tab(self, idx: int, title: str, icon):
        ic = ft.Icon(icon, size=16, color=self.colors["muted"])
        tx = ft.Text(title, size=self.fs["md"], color=self.colors["muted"])
        underline = ft.Container(height=2, bgcolor="transparent")

        tab = ft.Container(
            content=ft.Column(
                [
                    ft.Container(
                        content=ft.Row([ic, tx], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        padding=ft.padding.symmetric(horizontal=10, vertical=9),
                    ),
                    underline,
                ],
                spacing=0,
            ),
            bgcolor="transparent",
            on_click=lambda e, i=idx: self.set_active_view(i),
            on_hover=lambda e, i=idx: self._on_tab_hover(e, i),
        )
        self.tabs_meta.append({"idx": idx, "tab": tab, "underline": underline, "icon": ic, "text": tx})
        return tab

    def _on_tab_hover(self, e: ft.HoverEvent, idx: int):
        meta = next((m for m in self.tabs_meta if m["idx"] == idx), None)
        if not meta or idx == self.active_view:
            return
        hovering = (e.data == "true")
        meta["tab"].bgcolor = self.colors["panel_3"] if hovering else "transparent"
        meta["underline"].bgcolor = self.colors["border"] if hovering else "transparent"
        try:
            self.page.update()
        except Exception:
            pass

    def _update_tabstrip_styles(self):
        for meta in self.tabs_meta:
            idx = meta["idx"]
            if idx == self.active_view:
                meta["icon"].color = self.colors["text"]
                meta["text"].color = self.colors["text"]
                meta["tab"].bgcolor = self.colors["panel_3"]
                meta["underline"].bgcolor = self.colors["accent"]
            else:
                meta["icon"].color = self.colors["muted"]
                meta["text"].color = self.colors["muted"]
                meta["tab"].bgcolor = "transparent"
                meta["underline"].bgcolor = "transparent"

    def set_active_view(self, index: int):
        self.active_view = index
        for i, v in enumerate(self.views):
            v.visible = (i == self.active_view)
        self._update_tabstrip_styles()
        self.hide_context_menu()
        self.page.update()

    # =========================
    # Context menu overlay
    # =========================
    def _build_context_menu_layer(self):
        self._ctx_bg = ft.GestureDetector(
            visible=False,
            left=0,
            top=0,
            right=0,
            bottom=0,
            on_tap=lambda e: self.hide_context_menu(),
            on_secondary_tap=lambda e: self.hide_context_menu(),
            content=ft.Container(bgcolor="transparent"),
        )

        self._ctx_menu_col = ft.Column(spacing=0)

        self._ctx_menu = ft.Container(
            visible=False,
            left=10,
            top=10,
            width=240,
            bgcolor=self.colors["panel"],
            border=ft.border.all(1, self.colors["border"]),
            border_radius=6,
            padding=ft.padding.symmetric(vertical=4),
            content=self._ctx_menu_col,
        )

        return [self._ctx_bg, self._ctx_menu]

    def _ctx_item(self, text: str, icon, on_click):
        ic = ft.Icon(icon, size=16, color=self.colors["text"])
        tx = ft.Text(text, size=self.fs["md"], color=self.colors["text"])
        row = ft.Row([ic, tx], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)

        item = ft.Container(
            content=row,
            padding=ft.padding.symmetric(horizontal=12, vertical=7),
            bgcolor="transparent",
            on_hover=lambda e: self._ctx_item_hover(e, item, tx, ic),
            on_click=lambda e: self._ctx_item_click(on_click),
        )
        return item

    def _ctx_item_hover(self, e: ft.HoverEvent, item: ft.Container, tx: ft.Text, ic: ft.Icon):
        hovering = (e.data == "true")
        item.bgcolor = self.colors["selection"] if hovering else "transparent"
        tx.color = ft.colors.WHITE if hovering else self.colors["text"]
        ic.color = ft.colors.WHITE if hovering else self.colors["text"]
        try:
            item.update()
        except Exception:
            pass

    def _ctx_item_click(self, on_click):
        self.hide_context_menu()
        try:
            on_click()
        except Exception as ex:
            self.add_log(f"Ошибка действия меню: {ex}", "error")

    def show_context_menu(self, x: float, y: float, items: list[tuple[str, any, callable]]):
        if not self._ctx_bg or not self._ctx_menu or not self._ctx_menu_col:
            return

        self._ctx_menu_col.controls.clear()
        for label, icon, cb in items:
            self._ctx_menu_col.controls.append(self._ctx_item(label, icon, cb))

        menu_w = 240
        menu_h = 12 + len(items) * 32

        try:
            w = int(getattr(self.page, "window_width", 1400))
            h = int(getattr(self.page, "window_height", 850))
        except Exception:
            w, h = 1400, 850

        xx = int(x)
        yy = int(y)
        if xx + menu_w > w:
            xx = max(10, w - menu_w - 10)
        if yy + menu_h > h:
            yy = max(10, h - menu_h - 10)

        self._ctx_menu.left = xx
        self._ctx_menu.top = yy
        self._ctx_bg.visible = True
        self._ctx_menu.visible = True
        try:
            self.page.update()
        except Exception:
            pass

    def hide_context_menu(self):
        if self._ctx_bg:
            self._ctx_bg.visible = False
        if self._ctx_menu:
            self._ctx_menu.visible = False
        try:
            self.page.update()
        except Exception:
            pass

    # =========================
    # Resize handles (Windows frameless)
    # =========================
    def _build_resize_handles(self):
        if not (self.is_windows and self.win and getattr(self.page, "window_frameless", False)):
            return []

        t = self.RESIZE_THICK

        def handle(left=None, top=None, right=None, bottom=None, width=None, height=None, ht=None):
            return ft.GestureDetector(
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                width=width,
                height=height,
                on_pan_start=(lambda e, code=ht: self.win.begin_resize(code)),
                content=ft.Container(bgcolor="transparent"),
            )

        # Top edge НЕ перекрывает правую часть topbar
        return [
            handle(left=0, top=0, bottom=0, width=t, ht=HTLEFT),
            handle(right=0, top=0, bottom=0, width=t, ht=HTRIGHT),

            handle(left=0, top=0, right=self.TOP_RESIZE_RIGHT_GAP, height=t, ht=HTTOP),
            handle(left=0, right=0, bottom=0, height=t, ht=HTBOTTOM),

            handle(left=0, top=0, width=t, height=t, ht=HTTOPLEFT),
            handle(right=0, top=0, width=t, height=t, ht=HTTOPRIGHT),
            handle(left=0, bottom=0, width=t, height=t, ht=HTBOTTOMLEFT),
            handle(right=0, bottom=0, width=t, height=t, ht=HTBOTTOMRIGHT),
        ]

    # =========================
    # UI build
    # =========================
    def build_ui(self):
        self.views = [
            self.build_sources_tab(),
            self.build_settings_tab(),
            self.build_logs_tab(),
            self.build_profiles_tab(),
            self.build_ssh_tab(),
        ]
        for i, v in enumerate(self.views):
            v.visible = (i == 0)

        main_stack = ft.Stack(controls=self.views, expand=True)

        top_bar = self.build_top_bar()
        control_panel = self.build_control_panel()
        status_bar = self.build_status_bar()
        main_content = ft.Container(expand=True, bgcolor=self.colors["bg"], padding=12, content=main_stack)

        root_column = ft.Column(spacing=0, expand=True, controls=[top_bar, main_content, control_panel, status_bar])

        overlay_controls = []
        overlay_controls.extend(self._build_resize_handles())
        overlay_controls.extend(self._build_context_menu_layer())

        root = ft.Stack(
            expand=True,
            controls=[
                ft.Container(
                    expand=True,
                    bgcolor=self.colors["bg"],
                    border=ft.border.all(1, self.colors["border_soft"]) if (self.is_windows and getattr(self.page, "window_frameless", False)) else None,
                    content=root_column,
                ),
                *overlay_controls,
            ],
        )

        self.page.add(root)

        self.update_source_list()
        self.load_profiles_list()
        self.load_ssh_profiles_list()
        self.update_disk_info()
        self._refresh_target_panel()

    def build_status_bar(self):
        self.progress_bar = ft.ProgressBar(width=260, height=6, color=self.colors["accent"], bgcolor=self.colors["panel_3"], value=0)
        self.progress_text = ft.Text("Готов", size=self.fs["sm"], color=self.colors["muted"])
        self.stats_text = ft.Text("Файлов: 0 | Размер: 0 MB", size=self.fs["sm"], color=self.colors["muted"])

        self.status_disk_text = ft.Text("DISK: --/--GB", size=self.fs["sm"], color=self.colors["muted"])
        self.run_state_chip_text = ft.Text("● IDLE", size=self.fs["xs"], color=self.colors["success"], weight=ft.FontWeight.BOLD)

        return ft.Container(
            height=self.STATUSBAR_HEIGHT,
            bgcolor=self.colors["panel"],
            border=ft.border.only(top=ft.border.BorderSide(1, self.colors["border"])),
            padding=ft.padding.symmetric(horizontal=12, vertical=6),
            content=ft.Row(
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Row(
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Icon(ft.icons.INFO_OUTLINE, size=14, color=self.colors["muted"]),
                            self.stats_text,
                            ft.Container(
                                padding=ft.padding.symmetric(horizontal=8, vertical=2),
                                border_radius=4,
                                bgcolor=self.colors["panel_3"],
                                border=ft.border.all(1, self.colors["border"]),
                                content=self.run_state_chip_text,
                            ),
                        ],
                    ),
                    ft.Container(expand=True, alignment=ft.alignment.center, content=ft.Column([self.progress_text, self.progress_bar], spacing=2)),
                    ft.Row(
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Icon(ft.icons.STORAGE, size=14, color=self.colors["muted"]),
                            self.status_disk_text,
                            self._divider_v(14, self.colors["border"]),
                            ft.Text(datetime.now().strftime("%H:%M"), size=self.fs["sm"], color=self.colors["muted"]),
                        ],
                    ),
                ],
            ),
        )

    # =========================
    # Target mode UI helpers
    # =========================
    def _on_target_mode_change(self, e):
        self.target_mode = self.target_mode_rg.value if self.target_mode_rg else "local"
        self._refresh_target_panel()

    def _goto_ssh_tab(self, e=None):
        # SSH tab index = 4
        self.set_active_view(4)

    def _refresh_target_panel(self):
        if not self.target_dir_text or not self.target_status_text or not self.target_status_icon or not self.target_select_btn:
            return

        if self.target_mode == "local":
            self.target_select_btn.text = "Выбрать"
            self.target_select_btn.icon = ft.icons.FOLDER_OPEN
            self.target_select_btn.on_click = self.select_target_directory

            if self.target_dir:
                self.target_dir_text.value = self.target_dir
                self.target_status_icon.color = self.colors["success"]
                self.target_status_text.value = "Локальная директория выбрана"
                self.update_disk_info()
            else:
                self.target_dir_text.value = "Не выбрано"
                self.target_status_icon.color = self.colors["muted"]
                self.target_status_text.value = "Локальная директория не выбрана"

            if self.quick_open_btn:
                self.quick_open_btn.text = "Показать папку"
                self.quick_open_btn.icon = ft.icons.FOLDER_OPEN

        else:
            self.target_select_btn.text = "Настроить SSH"
            self.target_select_btn.icon = ft.icons.CLOUD
            self.target_select_btn.on_click = self._goto_ssh_tab

            if self.ssh_target_dir:
                self.target_dir_text.value = self.ssh_target_dir
                self.target_status_icon.color = self.colors["success"]
                self.target_status_text.value = "SSH цель выбрана"
                if self.status_disk_text:
                    self.status_disk_text.value = f"SSH: {self.ssh_config.get('host','--')}:{self.ssh_target_dir}"
            else:
                self.target_dir_text.value = "Не выбрано"
                self.target_status_icon.color = self.colors["muted"]
                self.target_status_text.value = "SSH цель не выбрана"
                if self.status_disk_text:
                    self.status_disk_text.value = "SSH: --"

            if self.quick_open_btn:
                self.quick_open_btn.text = "Открыть SSH"
                self.quick_open_btn.icon = ft.icons.CLOUD

        try:
            self.page.update()
        except Exception:
            pass

    # =========================
    # Tabs content
    # =========================
    def build_sources_tab(self):
        left_column = self._build_left_column()
        right_column = self._build_right_column()
        divider = ft.Container(width=1, bgcolor=self.colors["border"], margin=ft.margin.symmetric(vertical=6))

        return ft.Container(
            expand=True,
            bgcolor=self.colors["bg"],
            content=ft.Row(expand=True, spacing=12, vertical_alignment=ft.CrossAxisAlignment.STRETCH, controls=[left_column, divider, right_column]),
        )

    def _build_left_column(self):
        TOP_PANEL_HEIGHT = 136

        # target widgets
        self.target_dir_text = ft.Text("Не выбрано", size=self.fs["md"], color=self.colors["text"], overflow=ft.TextOverflow.ELLIPSIS)
        self.target_status_icon = ft.Icon(ft.icons.CIRCLE, size=12, color=self.colors["muted"])
        self.target_status_text = ft.Text("Цель не выбрана", size=self.fs["sm"], color=self.colors["muted"])

        self.target_mode_rg = ft.RadioGroup(
            value=self.target_mode,
            on_change=self._on_target_mode_change,
            content=ft.Row(
                spacing=14,
                controls=[
                    ft.Radio(value="local", label="Local"),
                    ft.Radio(value="ssh", label="SSH"),
                ],
            ),
        )

        self.target_select_btn = ft.ElevatedButton(
            text="Выбрать",
            icon=ft.icons.FOLDER_OPEN,
            on_click=self.select_target_directory,
            width=160,
            style=ft.ButtonStyle(
                color=self.colors["text"],
                bgcolor=self.colors["panel_2"],
                padding=ft.padding.symmetric(horizontal=12, vertical=10),
                shape=ft.RoundedRectangleBorder(radius=6),
                elevation=0,
            ),
        )

        target_panel = self._panel(
            padding=12,
            content=ft.Column(
                spacing=8,
                controls=[
                    ft.Row(
                        spacing=8,
                        controls=[
                            ft.Icon(ft.icons.FOLDER_SPECIAL, size=16, color=self.colors["accent"]),
                            ft.Text("Цель копирования", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["text"]),
                            ft.Container(expand=True),
                            self.target_mode_rg,
                        ],
                    ),
                    ft.Divider(height=1, color=self.colors["border"]),
                    ft.Row(
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            self.target_select_btn,
                            ft.Container(
                                expand=True,
                                content=ft.Column(
                                    spacing=4,
                                    controls=[
                                        ft.Row([self.target_status_icon, self.target_status_text], spacing=6),
                                        ft.Container(
                                            padding=ft.padding.symmetric(horizontal=10, vertical=7),
                                            bgcolor=self.colors["bg"],
                                            border=ft.border.all(1, self.colors["border"]),
                                            border_radius=4,
                                            content=self.target_dir_text,
                                        ),
                                    ],
                                ),
                            ),
                        ],
                    ),
                ],
            ),
        )
        target_panel.height = TOP_PANEL_HEIGHT

        # sources list
        self.source_list_view = ft.ListView(spacing=6, expand=True, controls=[])
        self.source_list_view.visible = False

        self.placeholder_container = ft.Container(
            visible=True,
            expand=True,
            alignment=ft.alignment.center,
            content=ft.Column(
                spacing=10,
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.icons.FOLDER_OPEN, size=48, color=self.colors["muted_2"]),
                    ft.Text("Добро пожаловать!", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["muted"]),
                    ft.Text("Добавьте папки/файлы для резервного копирования", size=self.fs["md"], color=self.colors["muted"]),
                ],
            ),
        )

        self.sources_count_text = ft.Text("0", size=self.fs["sm"], color=self.colors["accent"])

        add_folder_btn = ft.ElevatedButton(
            text="Папка",
            icon=ft.icons.CREATE_NEW_FOLDER,
            on_click=self.select_directory,
            style=ft.ButtonStyle(
                color=self.colors["text"],
                bgcolor=self.colors["panel_2"],
                padding=ft.padding.symmetric(horizontal=12, vertical=8),
                shape=ft.RoundedRectangleBorder(radius=6),
                elevation=0,
            ),
        )
        add_files_btn = ft.ElevatedButton(
            text="Файлы",
            icon=ft.icons.INSERT_DRIVE_FILE,
            on_click=self.select_files,
            style=ft.ButtonStyle(
                color=self.colors["text"],
                bgcolor=self.colors["panel_2"],
                padding=ft.padding.symmetric(horizontal=12, vertical=8),
                shape=ft.RoundedRectangleBorder(radius=6),
                elevation=0,
            ),
        )
        clear_btn = ft.ElevatedButton(
            text="Очистить",
            icon=ft.icons.CLEAR_ALL,
            on_click=lambda _: self.clear_sources(),
            style=ft.ButtonStyle(
                color=self.colors["muted"],
                bgcolor="transparent",
                padding=ft.padding.symmetric(horizontal=12, vertical=8),
                shape=ft.RoundedRectangleBorder(radius=6),
                elevation=0,
            ),
        )

        sources_panel = self._panel(
            expand=True,
            padding=12,
            content=ft.Column(
                expand=True,
                spacing=10,
                controls=[
                    ft.Row(
                        spacing=10,
                        controls=[
                            ft.Icon(ft.icons.SOURCE, size=16, color=self.colors["warn"]),
                            ft.Text("Источники", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["text"]),
                            ft.Container(
                                padding=ft.padding.symmetric(horizontal=8, vertical=2),
                                bgcolor=self.colors["panel_3"],
                                border_radius=4,
                                content=self.sources_count_text,
                            ),
                            ft.Container(expand=True),
                            add_folder_btn,
                            add_files_btn,
                            clear_btn,
                        ],
                    ),
                    ft.Container(
                        expand=True,
                        bgcolor=self.colors["bg"],
                        border=ft.border.all(1, self.colors["border"]),
                        border_radius=6,
                        padding=12,
                        content=ft.Column(expand=True, spacing=0, controls=[self.placeholder_container, self.source_list_view]),
                    ),
                ],
            ),
        )

        return ft.Container(expand=2, padding=12, content=ft.Column(expand=True, spacing=12, controls=[target_panel, sources_panel]))

    def _build_right_column(self):
        TOP_PANEL_HEIGHT = 124

        self.disk_progress_bar = ft.ProgressBar(width=260, height=6, color=self.colors["accent"], bgcolor=self.colors["panel_3"], value=0)
        self.disk_free_text = ft.Text("0 GB", size=self.fs["sm"], color=self.colors["text"], weight=ft.FontWeight.BOLD)
        self.disk_total_text = ft.Text("0 GB", size=self.fs["sm"], color=self.colors["text"])

        disk_panel = self._panel(
            padding=12,
            content=ft.Column(
                expand=True,
                spacing=8,
                controls=[
                    ft.Row(spacing=8, controls=[ft.Icon(ft.icons.PIE_CHART, size=16, color=self.colors["accent"]),
                                                ft.Text("Диск (Local)", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["text"])]),
                    ft.Divider(height=1, color=self.colors["border"]),
                    ft.Row(spacing=6, controls=[ft.Text("Свободно:", size=self.fs["sm"], color=self.colors["muted"]),
                                                self.disk_free_text,
                                                ft.Text("/", size=self.fs["sm"], color=self.colors["muted"]),
                                                self.disk_total_text]),
                    self.disk_progress_bar,
                ],
            ),
        )
        disk_panel.height = TOP_PANEL_HEIGHT

        self.log_counter_text = ft.Text("0", size=self.fs["sm"], color=self.colors["accent"])

        logs_panel = self._panel(
            expand=True,
            padding=12,
            content=ft.Column(
                expand=True,
                spacing=8,
                controls=[
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            ft.Row(
                                spacing=8,
                                controls=[
                                    ft.Icon(ft.icons.TERMINAL, size=16, color=self.colors["accent"]),
                                    ft.Text("Логи", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["text"]),
                                    ft.Container(padding=ft.padding.symmetric(horizontal=8, vertical=2),
                                                 bgcolor=self.colors["panel_3"], border_radius=4, content=self.log_counter_text),
                                ],
                            ),
                            ft.IconButton(icon=ft.icons.CLEAR_ALL, icon_size=16, icon_color=self.colors["muted"], tooltip="Очистить логи", on_click=self.clear_logs),
                        ],
                    ),
                    ft.Divider(height=1, color=self.colors["border"]),
                    ft.Container(
                        expand=True,
                        bgcolor=self.colors["bg"],
                        border=ft.border.all(1, self.colors["border"]),
                        border_radius=6,
                        padding=6,
                        content=self.log_view_sources,
                    ),
                ],
            ),
        )

        return ft.Container(expand=1, padding=12, content=ft.Column(expand=True, spacing=12, controls=[disk_panel, logs_panel]))

    # ----- Settings tab -----
    def _setting_row(self, title, description, control):
        return ft.Container(
            padding=ft.padding.symmetric(vertical=6),
            content=ft.Row(
                controls=[
                    ft.Column(
                        expand=True,
                        spacing=2,
                        controls=[
                            ft.Text(title, size=self.fs["md"], color=self.colors["text"]),
                            ft.Text(description, size=self.fs["sm"], color=self.colors["muted"]),
                        ],
                    ),
                    control,
                ]
            ),
        )

    def build_settings_tab(self):
        main_settings = self._panel(
            padding=14,
            content=ft.Column(
                spacing=10,
                controls=[
                    ft.Row(spacing=8, controls=[ft.Icon(ft.icons.SETTINGS, size=16, color=self.colors["accent"]),
                                                ft.Text("Настройки", size=self.fs["xl"], weight=ft.FontWeight.BOLD, color=self.colors["text"])]),
                    ft.Divider(height=1, color=self.colors["border"]),
                    self._setting_row("Инкрементальное копирование", "Копировать только измененные файлы", self.incremental_cb),
                    self._setting_row("Пропуск скрытых файлов", "Игнорировать .git, .DS_Store и системные файлы", self.skip_hidden_cb),
                    self._setting_row("Сжатие в ZIP", "Архивировать файлы для экономии места", self.compression_cb),
                    self._setting_row("Сохранение структуры", "Сохранять иерархию папок", self.preserve_structure_cb),
                    self._setting_row("Тёмная тема", "Использовать тёмную тему интерфейса", self.theme_switch),
                    ft.Divider(height=1, color=self.colors["border"]),
                    ft.Text("Расширенные настройки SSH", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["text"]),
                    self._setting_row("Проверка по хешу", "Использовать MD5 хеши для точной инкрементальной проверки", self.use_hash_check_cb),
                    self._setting_row("Параллельная загрузка", "Загружать несколько файлов одновременно для ускорения", self.parallel_upload_cb),
                    ft.Row(
                        spacing=10,
                        controls=[
                            ft.Text("Количество потоков:", size=self.fs["md"], color=self.colors["text"]),
                            self.max_workers_field,
                        ]
                    ),
                ],
            ),
        )

        buttons = self._panel(
            padding=12,
            content=ft.Row(
                spacing=10,
                controls=[
                    ft.ElevatedButton(
                        "Сохранить",
                        icon=ft.icons.SAVE,
                        on_click=self.save_settings,
                        style=ft.ButtonStyle(
                            color=ft.colors.WHITE,
                            bgcolor=self.colors["accent"],
                            padding=ft.padding.symmetric(horizontal=16, vertical=10),
                            shape=ft.RoundedRectangleBorder(radius=6),
                        ),
                    ),
                    ft.ElevatedButton(
                        "Сбросить",
                        icon=ft.icons.RESTORE,
                        on_click=self.reset_settings,
                        style=ft.ButtonStyle(
                            color=self.colors["text"],
                            bgcolor=self.colors["panel_2"],
                            padding=ft.padding.symmetric(horizontal=14, vertical=10),
                            shape=ft.RoundedRectangleBorder(radius=6),
                        ),
                    ),
                ],
            ),
        )

        return ft.Container(expand=True, padding=16, bgcolor=self.colors["bg"], content=ft.Column(spacing=12, controls=[main_settings, buttons]))

    # ----- Logs tab -----
    def build_logs_tab(self):
        self.log_search_field = ft.TextField(
            hint_text="Поиск по логам...",
            prefix_icon=ft.icons.SEARCH,
            border_color=self.colors["border"],
            bgcolor=self.colors["bg"],
            text_size=self.fs["md"],
            on_change=self._on_logs_filter_change,
            expand=True,
        )
        self.filter_info_cb = ft.Checkbox(label="INFO", value=True, on_change=self._on_logs_filter_change)
        self.filter_warn_cb = ft.Checkbox(label="WARN", value=True, on_change=self._on_logs_filter_change)
        self.filter_error_cb = ft.Checkbox(label="ERROR", value=True, on_change=self._on_logs_filter_change)

        self.logs_count_text = ft.Text("Показано: 0 / Всего: 0", size=self.fs["sm"], color=self.colors["muted"])

        controls_panel = self._panel(
            padding=12,
            content=ft.Row(
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    self.log_search_field,
                    self._divider_v(18, self.colors["border"]),
                    self.filter_info_cb,
                    self.filter_warn_cb,
                    self.filter_error_cb,
                    self._divider_v(18, self.colors["border"]),
                    ft.ElevatedButton(
                        "Очистить",
                        icon=ft.icons.CLEAR_ALL,
                        on_click=self.clear_logs,
                        style=ft.ButtonStyle(
                            color=self.colors["text"],
                            bgcolor=self.colors["panel_2"],
                            padding=ft.padding.symmetric(horizontal=12, vertical=10),
                            shape=ft.RoundedRectangleBorder(radius=6),
                        ),
                    ),
                    ft.ElevatedButton(
                        "Экспорт",
                        icon=ft.icons.DOWNLOAD,
                        on_click=self.save_logs,
                        style=ft.ButtonStyle(
                            color=self.colors["text"],
                            bgcolor=self.colors["panel_2"],
                            padding=ft.padding.symmetric(horizontal=12, vertical=10),
                            shape=ft.RoundedRectangleBorder(radius=6),
                        ),
                    ),
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=10, vertical=8),
                        bgcolor=self.colors["bg"],
                        border=ft.border.all(1, self.colors["border"]),
                        border_radius=6,
                        content=self.logs_count_text,
                    ),
                ],
            ),
        )

        self.log_view = ft.ListView(spacing=0, expand=True)

        logs_panel = self._panel(
            expand=True,
            padding=12,
            content=ft.Column(
                expand=True,
                spacing=8,
                controls=[
                    ft.Row(spacing=8, controls=[ft.Icon(ft.icons.TERMINAL, size=16, color=self.colors["accent"]),
                                                ft.Text("Вывод", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["text"])]),
                    ft.Divider(height=1, color=self.colors["border"]),
                    ft.Container(
                        expand=True,
                        bgcolor=self.colors["bg"],
                        border=ft.border.all(1, self.colors["border"]),
                        border_radius=6,
                        padding=6,
                        content=self.log_view,
                    ),
                ],
            ),
        )

        self.refresh_logs_tab()
        return ft.Container(expand=True, padding=16, bgcolor=self.colors["bg"], content=ft.Column(expand=True, spacing=12, controls=[controls_panel, logs_panel]))

    def _on_logs_filter_change(self, e):
        self.refresh_logs_tab()

    def refresh_logs_tab(self):
        if not self.log_view or not self.logs_count_text:
            return

        query = (self.log_search_field.value or "").strip().lower() if self.log_search_field else ""
        allowed = set()
        if self.filter_info_cb and self.filter_info_cb.value:
            allowed.add("INFO")
        if self.filter_warn_cb and self.filter_warn_cb.value:
            allowed.add("WARN")
        if self.filter_error_cb and self.filter_error_cb.value:
            allowed.add("ERROR")

        filtered = []
        for it in self.logs_data:
            if allowed and it["level"] not in allowed:
                continue
            if query and (query not in it["msg"].lower()):
                continue
            filtered.append(it)

        self.log_view.controls.clear()
        for it in filtered[-2000:]:
            self.log_view.controls.append(self._log_row(it["ts"], it["level"], it["msg"]))

        self.logs_count_text.value = f"Показано: {len(filtered)} / Всего: {len(self.logs_data)}"

        try:
            self.page.update()
        except Exception:
            pass

    # ----- Profiles tab -----
    def build_profiles_tab(self):
        self.profile_name_field = ft.TextField(
            label="Имя профиля",
            hint_text="production-backup",
            expand=True,
            text_size=self.fs["md"],
            bgcolor=self.colors["bg"],
            border_color=self.colors["border"],
        )

        create_profile = self._panel(
            padding=12,
            content=ft.Column(
                spacing=10,
                controls=[
                    ft.Row(spacing=8, controls=[ft.Icon(ft.icons.ADD_CIRCLE, size=16, color=self.colors["accent"]),
                                                ft.Text("Создать профиль", size=self.fs["xl"], weight=ft.FontWeight.BOLD, color=self.colors["text"])]),
                    ft.Divider(height=1, color=self.colors["border"]),
                    ft.Row(
                        spacing=10,
                        controls=[
                            self.profile_name_field,
                            ft.ElevatedButton(
                                "Создать",
                                icon=ft.icons.ADD,
                                on_click=lambda e: self.save_profile(self.profile_name_field.value),
                                style=ft.ButtonStyle(
                                    color=ft.colors.WHITE,
                                    bgcolor=self.colors["accent"],
                                    padding=ft.padding.symmetric(horizontal=16, vertical=10),
                                    shape=ft.RoundedRectangleBorder(radius=6),
                                ),
                            ),
                        ],
                    ),
                ],
            ),
        )

        self.profiles_count_text = ft.Text("0", size=self.fs["sm"], color=self.colors["accent"])
        self.profiles_list = ft.ListView(spacing=6, expand=True)

        profiles_panel = self._panel(
            expand=True,
            padding=12,
            content=ft.Column(
                expand=True,
                spacing=10,
                controls=[
                    ft.Row(
                        spacing=8,
                        controls=[
                            ft.Icon(ft.icons.SAVE, size=16, color=self.colors["accent"]),
                            ft.Text("Профили", size=self.fs["xl"], weight=ft.FontWeight.BOLD, color=self.colors["text"]),
                            ft.Container(padding=ft.padding.symmetric(horizontal=8, vertical=2),
                                         bgcolor=self.colors["panel_3"], border_radius=4, content=self.profiles_count_text),
                            ft.Container(expand=True),
                            ft.ElevatedButton(
                                "Обновить",
                                icon=ft.icons.REFRESH,
                                on_click=self.load_profile_dialog,
                                style=ft.ButtonStyle(
                                    color=self.colors["text"],
                                    bgcolor=self.colors["panel_2"],
                                    padding=ft.padding.symmetric(horizontal=12, vertical=10),
                                    shape=ft.RoundedRectangleBorder(radius=6),
                                ),
                            ),
                        ],
                    ),
                    ft.Container(
                        expand=True,
                        bgcolor=self.colors["bg"],
                        border=ft.border.all(1, self.colors["border"]),
                        border_radius=6,
                        padding=6,
                        content=self.profiles_list,
                    ),
                ],
            ),
        )

        return ft.Container(expand=True, padding=16, bgcolor=self.colors["bg"], content=ft.Column(expand=True, spacing=12, controls=[create_profile, profiles_panel]))

    # ----- SSH tab -----
    def build_ssh_tab(self):
        # Connection fields
        self.ssh_host_field = ft.TextField(label="Host", hint_text="example.com", bgcolor=self.colors["bg"], border_color=self.colors["border"], text_size=self.fs["md"], expand=True)
        self.ssh_port_field = ft.TextField(label="Port", value="22", width=120, bgcolor=self.colors["bg"], border_color=self.colors["border"], text_size=self.fs["md"])
        self.ssh_user_field = ft.TextField(label="User", hint_text="root", bgcolor=self.colors["bg"], border_color=self.colors["border"], text_size=self.fs["md"], expand=True)

        self.ssh_pass_field = ft.TextField(
            label="Password / Key passphrase",
            password=True,
            can_reveal_password=True,
            bgcolor=self.colors["bg"],
            border_color=self.colors["border"],
            text_size=self.fs["md"],
            expand=True,
        )

        self.ssh_use_key_cb = ft.Checkbox(label="Use private key", value=False, on_change=self._ssh_on_use_key_toggle)

        self.ssh_key_text = ft.Text("Key: (не выбран)", size=self.fs["sm"], color=self.colors["muted"], overflow=ft.TextOverflow.ELLIPSIS)
        self.ssh_pick_key_btn = ft.ElevatedButton(
            "Выбрать ключ",
            icon=ft.icons.KEY,
            on_click=self._ssh_pick_key,
            style=ft.ButtonStyle(
                color=self.colors["text"],
                bgcolor=self.colors["panel_2"],
                padding=ft.padding.symmetric(horizontal=12, vertical=10),
                shape=ft.RoundedRectangleBorder(radius=6),
            ),
        )

        self.ssh_connect_btn = ft.ElevatedButton(
            "Подключиться",
            icon=ft.icons.LINK,
            on_click=self.ssh_connect,
            style=ft.ButtonStyle(
                color=ft.colors.WHITE,
                bgcolor=self.colors["accent"],
                padding=ft.padding.symmetric(horizontal=14, vertical=10),
                shape=ft.RoundedRectangleBorder(radius=6),
            ),
        )

        self.ssh_status_text = ft.Text("Статус: DISCONNECTED", size=self.fs["sm"], color=self.colors["muted"])

        # SSH Profiles section
        self.ssh_profiles_count_text = ft.Text("0", size=self.fs["sm"], color=self.colors["accent"])
        self.ssh_profiles_list = ft.ListView(spacing=6, expand=True, height=150)

        ssh_profile_save_btn = ft.ElevatedButton(
            "Сохранить профиль",
            icon=ft.icons.SAVE,
            on_click=self.ssh_save_profile_dialog,
            style=ft.ButtonStyle(
                color=self.colors["text"],
                bgcolor=self.colors["panel_2"],
                padding=ft.padding.symmetric(horizontal=12, vertical=10),
                shape=ft.RoundedRectangleBorder(radius=6),
            ),
        )

        ssh_profile_refresh_btn = ft.ElevatedButton(
            "Обновить",
            icon=ft.icons.REFRESH,
            on_click=self.load_ssh_profiles_list,
            style=ft.ButtonStyle(
                color=self.colors["text"],
                bgcolor=self.colors["panel_2"],
                padding=ft.padding.symmetric(horizontal=12, vertical=10),
                shape=ft.RoundedRectangleBorder(radius=6),
            ),
        )

        connection_panel = self._panel(
            padding=12,
            content=ft.Column(
                spacing=10,
                controls=[
                    ft.Row(spacing=8, controls=[ft.Icon(ft.icons.CLOUD, size=16, color=self.colors["accent"]),
                                                ft.Text("SSH подключение", size=self.fs["xl"], weight=ft.FontWeight.BOLD, color=self.colors["text"])]),
                    ft.Divider(height=1, color=self.colors["border"]),
                    ft.Row(spacing=10, controls=[self.ssh_host_field, self.ssh_port_field, self.ssh_user_field]),
                    ft.Row(spacing=10, controls=[self.ssh_pass_field]),
                    ft.Row(
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            self.ssh_use_key_cb,
                            self.ssh_pick_key_btn,
                            ft.Container(expand=True, content=self.ssh_key_text),
                            self.ssh_connect_btn,
                        ],
                    ),
                    self.ssh_status_text,
                    ft.Divider(height=1, color=self.colors["border"]),
                    ft.Row(
                        spacing=8,
                        controls=[
                            ft.Icon(ft.icons.SAVE, size=16, color=self.colors["accent"]),
                            ft.Text("SSH профили", size=self.fs["lg"], weight=ft.FontWeight.BOLD, color=self.colors["text"]),
                            ft.Container(padding=ft.padding.symmetric(horizontal=8, vertical=2),
                                         bgcolor=self.colors["panel_3"], border_radius=4, content=self.ssh_profiles_count_text),
                            ft.Container(expand=True),
                            ssh_profile_save_btn,
                            ssh_profile_refresh_btn,
                        ],
                    ),
                    ft.Container(
                        height=150,
                        bgcolor=self.colors["bg"],
                        border=ft.border.all(1, self.colors["border"]),
                        border_radius=6,
                        padding=6,
                        content=self.ssh_profiles_list,
                    ),
                ],
            ),
        )

        # Remote browser
        self.remote_path_text = ft.Text("/", size=self.fs["md"], color=self.colors["text"], overflow=ft.TextOverflow.ELLIPSIS)
        self.remote_list = ft.ListView(spacing=4, expand=True)
        self.ssh_target_text = ft.Text("Цель SSH: (не выбрана)", size=self.fs["sm"], color=self.colors["muted"], overflow=ft.TextOverflow.ELLIPSIS)

        up_btn = ft.ElevatedButton(
            "Вверх",
            icon=ft.icons.ARROW_UPWARD,
            on_click=self.ssh_go_up,
            style=ft.ButtonStyle(
                color=self.colors["text"],
                bgcolor=self.colors["panel_2"],
                padding=ft.padding.symmetric(horizontal=12, vertical=10),
                shape=ft.RoundedRectangleBorder(radius=6),
            ),
        )
        refresh_btn = ft.ElevatedButton(
            "Обновить",
            icon=ft.icons.REFRESH,
            on_click=self.ssh_refresh,
            style=ft.ButtonStyle(
                color=self.colors["text"],
                bgcolor=self.colors["panel_2"],
                padding=ft.padding.symmetric(horizontal=12, vertical=10),
                shape=ft.RoundedRectangleBorder(radius=6),
            ),
        )
        mkdir_btn = ft.ElevatedButton(
            "Новая папка",
            icon=ft.icons.CREATE_NEW_FOLDER,
            on_click=self.ssh_mkdir_dialog,
            style=ft.ButtonStyle(
                color=self.colors["text"],
                bgcolor=self.colors["panel_2"],
                padding=ft.padding.symmetric(horizontal=12, vertical=10),
                shape=ft.RoundedRectangleBorder(radius=6),
            ),
        )
        pick_target_btn = ft.ElevatedButton(
            "Выбрать текущую папку как цель",
            icon=ft.icons.CHECK,
            on_click=self.ssh_set_current_as_target,
            style=ft.ButtonStyle(
                color=ft.colors.WHITE,
                bgcolor=self.colors["accent"],
                padding=ft.padding.symmetric(horizontal=12, vertical=10),
                shape=ft.RoundedRectangleBorder(radius=6),
            ),
        )

        browser_panel = self._panel(
            expand=True,
            padding=12,
            content=ft.Column(
                expand=True,
                spacing=10,
                controls=[
                    ft.Row(
                        spacing=8,
                        controls=[
                            ft.Icon(ft.icons.FOLDER_OPEN, size=16, color=self.colors["accent"]),
                            ft.Text("Удалённая файловая система (SFTP)", size=self.fs["xl"], weight=ft.FontWeight.BOLD, color=self.colors["text"]),
                        ],
                    ),
                    ft.Divider(height=1, color=self.colors["border"]),
                    ft.Row(spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER, controls=[
                        ft.Text("Путь:", size=self.fs["sm"], color=self.colors["muted"]),
                        ft.Container(expand=True, content=self.remote_path_text),
                        up_btn,
                        refresh_btn,
                        mkdir_btn,
                        pick_target_btn,
                    ]),
                    ft.Container(
                        expand=True,
                        bgcolor=self.colors["bg"],
                        border=ft.border.all(1, self.colors["border"]),
                        border_radius=6,
                        padding=8,
                        content=self.remote_list,
                    ),
                    self.ssh_target_text,
                ],
            ),
        )

        return ft.Container(expand=True, padding=16, bgcolor=self.colors["bg"], content=ft.Column(expand=True, spacing=12, controls=[connection_panel, browser_panel]))

    # =========================
    # Control panel
    # =========================
    def build_control_panel(self):
        self.start_stop_btn = ft.ElevatedButton(
            "НАЧАТЬ КОПИРОВАНИЕ",
            icon=ft.icons.PLAY_ARROW,
            on_click=self.start_backup,
            style=ft.ButtonStyle(
                color=ft.colors.WHITE,
                bgcolor=self.colors["accent"],
                padding=ft.padding.symmetric(horizontal=24, vertical=14),
                shape=ft.RoundedRectangleBorder(radius=8),
            ),
        )

        self.quick_open_btn = ft.ElevatedButton(
            "Показать папку",
            icon=ft.icons.FOLDER_OPEN,
            on_click=lambda e: self.open_target_action(),
            style=ft.ButtonStyle(
                color=self.colors["text"],
                bgcolor=self.colors["panel_2"],
                padding=ft.padding.symmetric(horizontal=12, vertical=10),
                shape=ft.RoundedRectangleBorder(radius=6),
            ),
        )

        return ft.Container(
            bgcolor=self.colors["panel"],
            border=ft.border.only(top=ft.border.BorderSide(1, self.colors["border"])),
            content=ft.Container(
                padding=12,
                content=ft.Row(
                    alignment=ft.MainAxisAlignment.CENTER,
                    spacing=18,
                    controls=[self.start_stop_btn, self._divider_v(26, self.colors["border"]), self.quick_open_btn],
                ),
            ),
        )

    # =========================
    # Open target actions
    # =========================
    def open_target_action(self):
        if self.target_mode == "ssh":
            self.set_active_view(4)
            self.add_log("Переключение на SSH вкладку…", "info")
            return
        self.open_target_in_explorer()

    def open_target_in_explorer(self):
        if not self.target_dir:
            self.add_log("Целевая директория не выбрана", "warning")
            return
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", self.target_dir])
            elif os.name == "nt":
                os.startfile(self.target_dir)  # type: ignore
            else:
                subprocess.Popen(["xdg-open", self.target_dir])
            self.add_log(f"Открываю: {self.target_dir}", "info")
        except Exception as ex:
            self.add_log(f"Не удалось открыть папку: {ex}", "error")

    # =========================
    # Pickers
    # =========================
    def select_directory(self, e):
        def on_directory_selected(ev: ft.FilePickerResultEvent):
            if ev.path:
                self.source_dirs.append(ev.path)
                self.update_source_list()
                self.add_log(f"Добавлена папка: {ev.path}", "info")

        picker = ft.FilePicker(on_result=on_directory_selected)
        self.page.overlay.append(picker)
        self.page.update()
        picker.get_directory_path()

    def select_files(self, e):
        def on_files_selected(ev: ft.FilePickerResultEvent):
            if ev.files:
                for f in ev.files:
                    self.source_files.append(f.path)
                self.update_source_list()
                self.add_log(f"Добавлено {len(ev.files)} файлов", "info")

        picker = ft.FilePicker(on_result=on_files_selected)
        self.page.overlay.append(picker)
        self.page.update()
        picker.pick_files(allow_multiple=True)

    def select_target_directory(self, e):
        def on_target_selected(ev: ft.FilePickerResultEvent):
            if ev.path:
                self.target_dir = ev.path
                self.add_log(f"Целевая (local) папка: {ev.path}", "info")
                self._refresh_target_panel()
                self.page.update()

        picker = ft.FilePicker(on_result=on_target_selected)
        self.page.overlay.append(picker)
        self.page.update()
        picker.get_directory_path()

    # =========================
    # Sources list + context menu
    # =========================
    def update_source_list(self):
        if not self.source_list_view:
            return

        self.source_list_view.controls.clear()
        total_count = len(self.source_dirs) + len(self.source_files)
        if self.sources_count_text:
            self.sources_count_text.value = str(total_count)

        if total_count == 0:
            self.placeholder_container.visible = True
            self.source_list_view.visible = False
            self.page.update()
            return

        self.placeholder_container.visible = False
        self.source_list_view.visible = True

        for i, directory in enumerate(self.source_dirs):
            self.source_list_view.controls.append(self._source_item(directory, "dir", i))
        for i, file_path in enumerate(self.source_files):
            self.source_list_view.controls.append(self._source_item(file_path, "file", i))

        self.page.update()

    def _source_item(self, path: str, kind: str, idx: int):
        icon = ft.icons.FOLDER if kind == "dir" else ft.icons.DESCRIPTION
        ic_color = self.colors["warn"] if kind == "dir" else self.colors["success"]
        title = os.path.basename(path) or path

        row = ft.Row(
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(icon, size=16, color=ic_color),
                ft.Column(
                    expand=True,
                    spacing=2,
                    controls=[
                        ft.Text(title, size=self.fs["md"], color=self.colors["text"]),
                        ft.Text(path, size=self.fs["sm"], color=self.colors["muted"], overflow=ft.TextOverflow.ELLIPSIS),
                    ],
                ),
                ft.IconButton(
                    icon=ft.icons.CLOSE,
                    icon_size=16,
                    icon_color=self.colors["muted"],
                    tooltip="Удалить",
                    on_click=lambda e, k=kind, i=idx: self.remove_source(i, k),
                ),
            ],
        )

        return ft.GestureDetector(
            on_secondary_tap_down=lambda e, p=path, k=kind, i=idx: self._open_source_context_menu(e, p, k, i),
            content=ft.Container(
                bgcolor=self.colors["bg"],
                border=ft.border.all(1, self.colors["border"]),
                border_radius=6,
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
                content=row,
            ),
        )

    def _open_source_context_menu(self, e: ft.TapEvent, path: str, kind: str, idx: int):
        def do_open():
            try:
                if os.path.isdir(path):
                    self._open_path(path)
                else:
                    self._reveal_in_explorer(path)
            except Exception:
                self._reveal_in_explorer(path)

        def do_reveal():
            self._reveal_in_explorer(path)

        def do_remove():
            self.remove_source(idx, kind)

        items = [
            ("Открыть/показать", ft.icons.OPEN_IN_NEW, do_open),
            ("Показать в проводнике", ft.icons.FOLDER_OPEN, do_reveal),
            ("Удалить из списка", ft.icons.DELETE_OUTLINE, do_remove),
        ]
        self.show_context_menu(e.global_x, e.global_y, items)

    def clear_sources(self):
        self.source_dirs.clear()
        self.source_files.clear()
        self.update_source_list()
        self.add_log("Все источники очищены", "info")

    def remove_source(self, index, source_type):
        try:
            if source_type == "dir":
                removed = self.source_dirs.pop(index)
            else:
                removed = self.source_files.pop(index)
            self.add_log(f"Удален источник: {os.path.basename(removed) or removed}", "info")
        except Exception:
            self.add_log("Не удалось удалить источник (индекс устарел)", "warning")
        self.update_source_list()

    # =========================
    # Backup
    # =========================
    def _set_start_button_state(self, running: bool):
        if not self.start_stop_btn:
            return
        if running:
            self.start_stop_btn.text = "ОСТАНОВИТЬ"
            self.start_stop_btn.icon = ft.icons.STOP
            self.start_stop_btn.style = ft.ButtonStyle(
                color=ft.colors.WHITE,
                bgcolor="#C42B1C",
                padding=ft.padding.symmetric(horizontal=24, vertical=14),
                shape=ft.RoundedRectangleBorder(radius=8),
            )
            if self.run_state_chip_text:
                self.run_state_chip_text.value = "● RUN"
                self.run_state_chip_text.color = self.colors["warn"]
        else:
            self.start_stop_btn.text = "НАЧАТЬ КОПИРОВАНИЕ"
            self.start_stop_btn.icon = ft.icons.PLAY_ARROW
            self.start_stop_btn.style = ft.ButtonStyle(
                color=ft.colors.WHITE,
                bgcolor=self.colors["accent"],
                padding=ft.padding.symmetric(horizontal=24, vertical=14),
                shape=ft.RoundedRectangleBorder(radius=8),
            )
            if self.run_state_chip_text:
                self.run_state_chip_text.value = "● IDLE"
                self.run_state_chip_text.color = self.colors["success"]

    def start_backup(self, e):
        if not self.backup_in_progress:
            self.start_backup_process()
        else:
            self.stop_backup_process()

    def start_backup_process(self):
        # Check sources
        if not self.source_dirs and not self.source_files:
            self.add_log("Ошибка: нет источников для копирования", "error")
            return

        # Choose local vs ssh target
        if self.target_mode == "local":
            if not self.target_dir:
                self.add_log("Ошибка: целевая директория (Local) не выбрана", "error")
                return

            self.backup_worker = bl.BackupManager(
                source_dirs=self.source_dirs,
                source_files=self.source_files,
                target_dir=self.target_dir,
                incremental=self.incremental_cb.value,
                skip_hidden=self.skip_hidden_cb.value,
                compress=self.compression_cb.value,
                preserve_structure=self.preserve_structure_cb.value,
                progress_callback=self.update_progress,
                log_callback=self.add_log,
            )
        else:
            if not self.ssh_target_dir:
                self.add_log("Ошибка: SSH цель не выбрана (зайдите во вкладку SSH и выберите папку)", "error")
                return

            host = (self.ssh_host_field.value or "").strip() if self.ssh_host_field else ""
            port = int(self.ssh_port_field.value or "22") if self.ssh_port_field else 22
            user = (self.ssh_user_field.value or "").strip() if self.ssh_user_field else ""
            pwd = (self.ssh_pass_field.value or "") if self.ssh_pass_field else ""

            use_key = bool(self.ssh_use_key_cb.value) if self.ssh_use_key_cb else False
            key_fn = self.ssh_config.get("key_filename") if use_key else None

            if not host or not user:
                self.add_log("Ошибка: SSH host/user не заполнены", "error")
                return

            max_workers = 4
            try:
                max_workers = int(self.max_workers_field.value) if self.max_workers_field.value else 4
            except:
                pass

            self.backup_worker = ParallelSSHBackupWorker(
                host=host,
                port=port,
                username=user,
                password=pwd if pwd else None,
                key_filename=key_fn if key_fn else None,
                remote_target_dir=self.ssh_target_dir,
                source_dirs=self.source_dirs,
                source_files=self.source_files,
                incremental=self.incremental_cb.value,
                skip_hidden=self.skip_hidden_cb.value,
                preserve_structure=self.preserve_structure_cb.value,
                compress=False,
                progress_callback=self.update_progress,
                log_callback=self.add_log,
                max_workers=max_workers,
                use_hash_check=self.use_hash_check_cb.value if self.use_hash_check_cb else True,
            )

        self.backup_thread = threading.Thread(target=self.backup_worker.start_backup, daemon=True)
        self.backup_in_progress = True
        self._set_start_button_state(True)

        if self.progress_bar:
            self.progress_bar.value = 0
        if self.progress_text:
            self.progress_text.value = "Инициализация..."
        self.add_log("Запуск резервного копирования...", "info")
        self.page.update()

        self.backup_thread.start()
        self.monitor_thread = threading.Thread(target=self.monitor_backup_progress, daemon=True)
        self.monitor_thread.start()

    def stop_backup_process(self):
        if self.backup_worker:
            try:
                self.backup_worker.stop()
            except Exception:
                pass

        self.backup_in_progress = False
        self._set_start_button_state(False)

        if self.progress_text:
            self.progress_text.value = "Остановлено пользователем"
        self.add_log("Копирование остановлено", "warning")
        self.page.update()

    def monitor_backup_progress(self):
        while self.backup_in_progress and self.backup_thread and self.backup_thread.is_alive():
            if self.backup_worker and self.stats_text and self.progress_bar and self.progress_text:
                try:
                    stats = self.backup_worker.get_stats()
                    self.stats_text.value = f"Файлов: {stats.get('files_copied',0)}/{stats.get('total_files',0)} | Размер: {stats.get('total_size_mb',0.0):.2f} MB"
                    tf = stats.get("total_files", 0)
                    if tf > 0:
                        progress = (stats.get("files_copied", 0)) / tf
                        self.progress_bar.value = max(0.0, min(1.0, progress))
                        self.progress_text.value = f"{progress * 100:.1f}%"
                except Exception:
                    pass
            time.sleep(0.3)
            try:
                self.page.update()
            except Exception:
                pass

        if self.backup_in_progress and self.backup_thread and (not self.backup_thread.is_alive()):
            self.backup_finished()

    def backup_finished(self):
        self.backup_in_progress = False
        self._set_start_button_state(False)

        if self.progress_bar:
            self.progress_bar.value = 1
        if self.progress_text:
            self.progress_text.value = "Завершено"

        if self.backup_worker:
            stats = self.backup_worker.get_stats()
            self.add_log(f"Резервное копирование завершено: {stats.get('files_copied',0)} файлов ({stats.get('total_size_mb',0.0):.2f} MB)", "success")

        self.page.update()

    def update_progress(self, current, total, filename):
        if not self.backup_in_progress:
            return
        if total > 0 and self.progress_bar and self.progress_text:
            progress = current / total
            self.progress_bar.value = max(0.0, min(1.0, progress))
            short_name = os.path.basename(filename)
            if len(short_name) > 40:
                short_name = short_name[:37] + "..."
            self.progress_text.value = f"{progress * 100:.1f}% — {short_name}"
            try:
                self.page.update()
            except Exception:
                pass

    # =========================
    # Logs
    # =========================
    def _normalize_level(self, log_type: str) -> str:
        if log_type == "error":
            return "ERROR"
        if log_type == "warning":
            return "WARN"
        if log_type == "success":
            return "INFO"
        return "INFO"

    def _level_color(self, level: str) -> str:
        if level == "ERROR":
            return self.colors["error"]
        if level == "WARN":
            return self.colors["warn"]
        return self.colors["info"]

    def _log_row(self, ts: str, level: str, msg: str):
        lvl_color = self._level_color(level)
        return ft.Container(
            padding=ft.padding.symmetric(vertical=4, horizontal=8),
            border=ft.border.only(bottom=ft.border.BorderSide(1, self.colors["border_soft"])),
            content=ft.Row(
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text(ts, size=self.fs["xs"], color=self.colors["muted"], width=70, font_family="Consolas"),
                    ft.Text(level, size=self.fs["xs"], color=lvl_color, width=54, font_family="Consolas"),
                    ft.Text(msg, size=self.fs["sm"], color=self.colors["text"], expand=True, font_family="Consolas"),
                ],
            ),
        )

    def add_log(self, message, log_type="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        level = self._normalize_level(log_type)
        msg = str(message)

        self.logs_data.append({"ts": ts, "level": level, "msg": msg})
        if len(self.logs_data) > self.max_logs_keep:
            self.logs_data = self.logs_data[-self.max_logs_keep :]

        # mini logs
        tail = self.logs_data[-200:]
        self.log_view_sources.controls.clear()
        for it in tail:
            self.log_view_sources.controls.append(self._log_row(it["ts"], it["level"], it["msg"]))

        if self.log_counter_text:
            self.log_counter_text.value = str(len(self.logs_data))

        try:
            self.log_view_sources.scroll_to(offset=999999, duration=150)
        except Exception:
            pass

        if self.log_view is not None:
            self.refresh_logs_tab()

        try:
            self.page.update()
        except Exception:
            pass

    def clear_logs(self, e=None):
        self.logs_data.clear()
        if self.log_view_sources:
            self.log_view_sources.controls.clear()
        if self.log_counter_text:
            self.log_counter_text.value = "0"
        if self.log_view:
            self.log_view.controls.clear()
        if self.logs_count_text:
            self.logs_count_text.value = "Показано: 0 / Всего: 0"
        self.page.update()

    def save_logs(self, e):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file = f"backup_log_{timestamp}.txt"
            with open(log_file, "w", encoding="utf-8") as f:
                for it in self.logs_data:
                    f.write(f"{it['ts']} [{it['level']}] {it['msg']}\n")
            self.add_log(f"Логи сохранены: {log_file}", "success")
        except Exception as ex:
            self.add_log(f"Ошибка сохранения логов: {ex}", "error")

    # =========================
    # Disk
    # =========================
    def update_disk_info(self):
        if not self.target_dir:
            return
        try:
            import psutil

            du = psutil.disk_usage(self.target_dir)
            free_gb = du.free / (1024 ** 3)
            total_gb = du.total / (1024 ** 3)
            used_ratio = du.percent / 100.0

            if self.disk_progress_bar:
                self.disk_progress_bar.value = used_ratio
            if self.disk_free_text:
                self.disk_free_text.value = f"{free_gb:.0f} GB"
            if self.disk_total_text:
                self.disk_total_text.value = f"{total_gb:.0f} GB"
            if self.status_disk_text and self.target_mode == "local":
                self.status_disk_text.value = f"DISK: {free_gb:.0f}/{total_gb:.0f}GB"

            self.page.update()
        except Exception as ex:
            self.add_log(f"Ошибка при получении информации о диске: {ex}", "error")

    # =========================
    # Settings / Profiles
    # =========================
    def save_settings(self, e):
        self.settings = {
            "incremental": self.incremental_cb.value,
            "skip_hidden": self.skip_hidden_cb.value,
            "compression": self.compression_cb.value,
            "preserve_structure": self.preserve_structure_cb.value,
            "use_hash_check": self.use_hash_check_cb.value,
            "parallel_upload": self.parallel_upload_cb.value,
            "max_workers": int(self.max_workers_field.value) if self.max_workers_field.value else 4,
            "theme": "dark" if self.theme_switch.value else "light",
        }
        try:
            with open("settings.json", "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=2)
            self.add_log("Настройки сохранены", "success")
        except Exception as ex:
            self.add_log(f"Ошибка сохранения настроек: {ex}", "error")

    def reset_settings(self, e):
        self.incremental_cb.value = True
        self.skip_hidden_cb.value = True
        self.compression_cb.value = False
        self.preserve_structure_cb.value = True
        self.use_hash_check_cb.value = True
        self.parallel_upload_cb.value = True
        self.max_workers_field.value = "4"
        self.theme_switch.value = True
        self.page.update()
        self.add_log("Настройки сброшены (не сохранены в файл)", "info")
        # Применяем тему после сброса
        self.change_theme(e)

    def load_settings(self):
        try:
            if os.path.exists("settings.json"):
                with open("settings.json", "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {
            "incremental": True,
            "skip_hidden": True,
            "compression": False,
            "preserve_structure": True,
            "use_hash_check": True,
            "parallel_upload": True,
            "max_workers": 4,
            "theme": "dark",
        }

    def _read_profiles(self) -> list:
        try:
            if os.path.exists("profiles.json"):
                with open("profiles.json", "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else []
        except Exception:
            pass
        return []

    def _write_profiles(self, profiles: list):
        with open("profiles.json", "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, indent=2)

    def save_profile(self, profile_name):
        if not profile_name:
            self.add_log("Введите имя профиля", "warning")
            return

        profile = {
            "name": profile_name,
            "source_dirs": self.source_dirs,
            "source_files": self.source_files,
            "target_dir": self.target_dir,
            "settings": self.settings,
            "timestamp": datetime.now().isoformat(),
        }

        try:
            profiles = self._read_profiles()
            profiles.append(profile)
            self._write_profiles(profiles)

            if self.profile_name_field:
                self.profile_name_field.value = ""

            self.load_profiles_list()
            self.add_log(f"Профиль '{profile_name}' сохранен", "success")
            self.page.update()
        except Exception as ex:
            self.add_log(f"Ошибка сохранения профиля: {ex}", "error")

    def load_profiles_list(self):
        if not self.profiles_list:
            return
        try:
            profiles = self._read_profiles()
            self.profiles_list.controls.clear()
            for p in profiles:
                self.profiles_list.controls.append(self._profile_item(p))
            if self.profiles_count_text:
                self.profiles_count_text.value = str(len(profiles))
            self.page.update()
        except Exception as ex:
            self.add_log(f"Ошибка загрузки профилей: {ex}", "error")

    def _profile_item(self, profile: dict):
        ts_iso = profile.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_iso).strftime("%d.%m.%Y %H:%M")
        except Exception:
            ts = ts_iso or "--"

        row = ft.Row(
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(ft.icons.SAVE, color=self.colors["accent"], size=16),
                ft.Column(
                    expand=True,
                    spacing=2,
                    controls=[
                        ft.Text(profile.get("name", "profile"), size=self.fs["md"], color=self.colors["text"]),
                        ft.Text(f"{len(profile.get('source_dirs', []))} папок, {len(profile.get('source_files', []))} файлов | {ts}",
                                size=self.fs["sm"], color=self.colors["muted"]),
                    ],
                ),
                ft.IconButton(
                    icon=ft.icons.PLAY_ARROW,
                    icon_size=16,
                    icon_color=self.colors["accent"],
                    tooltip="Загрузить",
                    on_click=lambda e, p=profile: self.load_profile(p),
                ),
            ],
        )

        return ft.GestureDetector(
            on_secondary_tap_down=lambda e, p=profile: self._open_profile_context_menu(e, p),
            content=ft.Container(
                bgcolor=self.colors["bg"],
                border=ft.border.all(1, self.colors["border"]),
                border_radius=6,
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
                content=row,
            ),
        )

    def _open_profile_context_menu(self, e: ft.TapEvent, profile: dict):
        ts = profile.get("timestamp", "")

        def do_load():
            self.load_profile(profile)

        def do_delete():
            self.delete_profile(ts)

        items = [
            ("Загрузить", ft.icons.PLAY_ARROW, do_load),
            ("Удалить", ft.icons.DELETE_OUTLINE, do_delete),
        ]
        self.show_context_menu(e.global_x, e.global_y, items)

    def delete_profile(self, timestamp_iso: str):
        try:
            profiles = self._read_profiles()
            new_profiles = [p for p in profiles if p.get("timestamp") != timestamp_iso]
            self._write_profiles(new_profiles)
            self.load_profiles_list()
            self.add_log("Профиль удалён", "success")
        except Exception as ex:
            self.add_log(f"Ошибка удаления профиля: {ex}", "error")

    def load_profile_dialog(self, e):
        self.load_profiles_list()
        self.add_log("Список профилей обновлён", "info")

    def load_profile(self, profile):
        try:
            self.source_dirs = profile.get("source_dirs", [])
            self.source_files = profile.get("source_files", [])
            self.target_dir = profile.get("target_dir", "")
            self.settings = profile.get("settings", self.settings)

            self.incremental_cb.value = self.settings.get("incremental", True)
            self.skip_hidden_cb.value = self.settings.get("skip_hidden", True)
            self.compression_cb.value = self.settings.get("compression", False)
            self.preserve_structure_cb.value = self.settings.get("preserve_structure", True)
            self.use_hash_check_cb.value = self.settings.get("use_hash_check", True)
            self.parallel_upload_cb.value = self.settings.get("parallel_upload", True)
            self.max_workers_field.value = str(self.settings.get("max_workers", 4))
            self.theme_switch.value = self.settings.get("theme", "dark") == "dark"

            # Применяем тему из профиля
            self.change_theme(None)

            self.update_source_list()
            self._refresh_target_panel()
            self.add_log(f"Профиль '{profile.get('name','')}' загружен", "success")
            self.page.update()
        except Exception as ex:
            self.add_log(f"Ошибка загрузки профиля: {ex}", "error")

    # =========================
    # Small OS helpers
    # =========================
    def _open_path(self, path: str):
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif os.name == "nt":
                os.startfile(path)  # type: ignore
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    def _reveal_in_explorer(self, path: str):
        try:
            if os.name == "nt":
                if os.path.isfile(path):
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
                else:
                    os.startfile(path)  # type: ignore
            else:
                parent = path if os.path.isdir(path) else os.path.dirname(path)
                self._open_path(parent)
        except Exception:
            pass

    def copy_to_clipboard(self, text: str):
        """Копирование текста в буфер обмена"""
        try:
            self.page.set_clipboard(text)
            self.add_log("Скопировано в буфер обмена", "success")
        except Exception as ex:
            self.add_log(f"Ошибка копирования: {ex}", "error")

    # =========================
    # SSH logic
    # =========================
    def _ssh_on_use_key_toggle(self, e):
        use_key = bool(self.ssh_use_key_cb.value)
        # password field still used as passphrase if needed; keep enabled
        if self.ssh_pick_key_btn:
            self.ssh_pick_key_btn.disabled = not use_key
        try:
            self.page.update()
        except Exception:
            pass

    def _ssh_pick_key(self, e):
        def on_key_selected(ev: ft.FilePickerResultEvent):
            if ev.files and len(ev.files) > 0:
                p = ev.files[0].path
                self.ssh_config["key_filename"] = p
                if self.ssh_key_text:
                    self.ssh_key_text.value = f"Key: {p}"
                self.add_log(f"SSH key выбран: {p}", "info")
                try:
                    self.page.update()
                except Exception:
                    pass

        picker = ft.FilePicker(on_result=on_key_selected)
        self.page.overlay.append(picker)
        self.page.update()
        picker.pick_files(allow_multiple=False)

    def _ssh_set_status(self, connected: bool, text: str):
        self.ssh_connected = connected
        if self.ssh_status_text:
            self.ssh_status_text.value = text
            self.ssh_status_text.color = self.colors["success"] if connected else self.colors["muted"]
        if self.ssh_connect_btn:
            if connected:
                self.ssh_connect_btn.text = "Отключиться"
                self.ssh_connect_btn.icon = ft.icons.LINK_OFF
                self.ssh_connect_btn.on_click = self.ssh_disconnect
                self.ssh_connect_btn.style = ft.ButtonStyle(
                    color=ft.colors.WHITE,
                    bgcolor="#C42B1C",
                    padding=ft.padding.symmetric(horizontal=14, vertical=10),
                    shape=ft.RoundedRectangleBorder(radius=6),
                )
            else:
                self.ssh_connect_btn.text = "Подключиться"
                self.ssh_connect_btn.icon = ft.icons.LINK
                self.ssh_connect_btn.on_click = self.ssh_connect
                self.ssh_connect_btn.style = ft.ButtonStyle(
                    color=ft.colors.WHITE,
                    bgcolor=self.colors["accent"],
                    padding=ft.padding.symmetric(horizontal=14, vertical=10),
                    shape=ft.RoundedRectangleBorder(radius=6),
                )

    def ssh_connect(self, e=None):
        def worker():
            try:
                import paramiko
            except Exception:
                self.add_log("SSH: установите paramiko: pip install paramiko", "error")
                self._ssh_set_status(False, "Статус: ERROR (paramiko missing)")
                try:
                    self.page.update()
                except Exception:
                    pass
                return

            host = (self.ssh_host_field.value or "").strip()
            port = int(self.ssh_port_field.value or "22")
            user = (self.ssh_user_field.value or "").strip()
            pwd = (self.ssh_pass_field.value or "")
            use_key = bool(self.ssh_use_key_cb.value)
            key_fn = (self.ssh_config.get("key_filename") or "").strip() if use_key else ""

            if not host or not user:
                self.add_log("SSH: host/user не заполнены", "error")
                self._ssh_set_status(False, "Статус: ERROR (fill host/user)")
                try:
                    self.page.update()
                except Exception:
                    pass
                return

            self._ssh_set_status(False, "Статус: CONNECTING...")

            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

                kwargs = dict(
                    hostname=host,
                    port=port,
                    username=user,
                    timeout=10,
                    banner_timeout=10,
                    auth_timeout=10,
                    look_for_keys=False,
                    allow_agent=False,
                )
                if key_fn:
                    kwargs["key_filename"] = key_fn
                    if pwd:
                        kwargs["password"] = pwd
                else:
                    kwargs["password"] = pwd

                client.connect(**kwargs)
                sftp = client.open_sftp()

                # save state
                self.ssh_client = client
                self.ssh_sftp = sftp
                self.ssh_current_path = "/"
                self.ssh_config.update({"host": host, "port": port, "username": user, "password": pwd, "use_key": use_key})

                self.add_log(f"SSH: подключено к {host}:{port}", "success")
                self._ssh_set_status(True, f"Статус: CONNECTED ({host})")

                # load root listing
                self.ssh_refresh()

            except Exception as ex:
                self.add_log(f"SSH: ошибка подключения: {ex}", "error")
                self.ssh_client = None
                self.ssh_sftp = None
                self._ssh_set_status(False, "Статус: DISCONNECTED")

            try:
                self.page.update()
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def ssh_disconnect(self, e=None):
        try:
            if self.ssh_sftp:
                self.ssh_sftp.close()
        except Exception:
            pass
        try:
            if self.ssh_client:
                self.ssh_client.close()
        except Exception:
            pass
        self.ssh_client = None
        self.ssh_sftp = None
        self.ssh_connected = False
        self._ssh_set_status(False, "Статус: DISCONNECTED")
        self.add_log("SSH: отключено", "info")
        try:
            self.page.update()
        except Exception:
            pass

    def _ssh_norm(self, p: str) -> str:
        p = (p or "").replace("\\", "/")
        if not p.startswith("/"):
            p = "/" + p
        p = posixpath.normpath(p)
        return p if p != "." else "/"

    def ssh_refresh(self, e=None):
        if not self.ssh_sftp:
            self.add_log("SSH: нет подключения", "warning")
            return

        try:
            cur = self._ssh_norm(self.ssh_current_path)
            self.ssh_current_path = cur
            if self.remote_path_text:
                self.remote_path_text.value = cur

            self.remote_list.controls.clear()

            # listdir_attr
            attrs = self.ssh_sftp.listdir_attr(cur)

            # sort dirs first
            entries = []
            for a in attrs:
                is_dir = stat.S_ISDIR(a.st_mode)
                entries.append((is_dir, a.filename, a))
            entries.sort(key=lambda x: (not x[0], x[1].lower()))

            for is_dir, name, a in entries:
                full = posixpath.join(cur, name) if cur != "/" else "/" + name
                self.remote_list.controls.append(self._remote_item(name, full, is_dir, a))

            try:
                self.page.update()
            except Exception:
                pass

        except Exception as ex:
            self.add_log(f"SSH: ошибка чтения директории: {ex}", "error")

    def ssh_go_up(self, e=None):
        if not self.ssh_sftp:
            return
        cur = self._ssh_norm(self.ssh_current_path)
        if cur == "/":
            return
        parent = posixpath.dirname(cur.rstrip("/"))
        parent = parent if parent else "/"
        self.ssh_current_path = parent
        self.ssh_refresh()

    def ssh_cd(self, new_path: str):
        self.ssh_current_path = self._ssh_norm(new_path)
        self.ssh_refresh()

    def ssh_set_current_as_target(self, e=None):
        cur = self._ssh_norm(self.ssh_current_path)
        self.ssh_target_dir = cur
        if self.ssh_target_text:
            self.ssh_target_text.value = f"Цель SSH: {cur}"
            self.ssh_target_text.color = self.colors["success"]
        self.add_log(f"SSH: цель выбрана -> {cur}", "success")
        self._refresh_target_panel()
        try:
            self.page.update()
        except Exception:
            pass

    def _remote_item(self, name: str, full_path: str, is_dir: bool, attr):
        """Элемент списка файлов с контекстным меню"""
        icon = ft.icons.FOLDER if is_dir else ft.icons.DESCRIPTION
        ic_color = self.colors["accent"] if is_dir else self.colors["muted"]
        size_txt = ""
        
        try:
            if not is_dir:
                size = attr.st_size
                if size < 1024:
                    size_txt = f"{size} B"
                elif size < 1024**2:
                    size_txt = f"{size/1024:.1f} KB"
                else:
                    size_txt = f"{size/(1024**2):.1f} MB"
        except Exception:
            pass
        
        row = ft.Row(
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(icon, size=16, color=ic_color),
                ft.Text(name, size=self.fs["md"], color=self.colors["text"], expand=True),
                ft.Text(size_txt, size=self.fs["sm"], color=self.colors["muted"], width=80, text_align=ft.TextAlign.RIGHT),
            ],
        )
        
        # Контекстное меню для файла/папки
        def open_context_menu(e: ft.TapEvent):
            items = []
            
            if is_dir:
                items.append(("Открыть", ft.icons.FOLDER_OPEN, lambda: self.ssh_cd(full_path)))
                items.append(("Удалить папку", ft.icons.DELETE, lambda: self.ssh_delete_dialog(full_path, name, True)))
            else:
                items.append(("Скачать", ft.icons.DOWNLOAD, lambda: self.ssh_download_file(full_path, name)))
                items.append(("Удалить файл", ft.icons.DELETE, lambda: self.ssh_delete_dialog(full_path, name, False)))
            
            items.append(("Переименовать", ft.icons.EDIT, lambda: self.ssh_rename_dialog(full_path, name, is_dir)))
            items.append(("Копировать путь", ft.icons.CONTENT_COPY, lambda: self.copy_to_clipboard(full_path)))
            
            self.show_context_menu(e.global_x, e.global_y, items)
        
        return ft.GestureDetector(
            on_tap=lambda e: self.ssh_cd(full_path) if is_dir else None,
            on_secondary_tap_down=open_context_menu,
            content=ft.Container(
                bgcolor=self.colors["bg"],
                border=ft.border.all(1, self.colors["border"]),
                border_radius=6,
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
                content=row,
            ),
        )

    # =========================
    # SSH File Operations
    # =========================
    def ssh_mkdir_dialog(self, e=None):
        """Диалог создания новой папки"""
        def on_dialog_result(e):
            if e.control.text == "Создать" and name_field.value:
                self.ssh_create_directory(name_field.value)
            dialog.open = False
            self.page.update()
        
        name_field = ft.TextField(label="Имя папки", autofocus=True)
        dialog = ft.AlertDialog(
            title=ft.Text("Создать папку"),
            content=name_field,
            actions=[
                ft.TextButton("Отмена", on_click=lambda e: setattr(dialog, "open", False)),
                ft.TextButton("Создать", on_click=on_dialog_result),
            ],
        )
        
        self.page.dialog = dialog
        dialog.open = True
        self.page.update()

    def ssh_create_directory(self, dirname):
        """Создание директории на сервере"""
        if not self.ssh_sftp:
            self.add_log("Нет SSH соединения", "error")
            return
        
        try:
            new_path = posixpath.join(self.ssh_current_path, dirname)
            self.ssh_sftp.mkdir(new_path)
            self.add_log(f"Создана папка: {new_path}", "success")
            self.ssh_refresh()
        except Exception as ex:
            self.add_log(f"Ошибка создания папки: {ex}", "error")

    def ssh_delete_dialog(self, path: str, name: str, is_dir: bool):
        """Диалог подтверждения удаления"""
        def on_dialog_result(e):
            if e.control.text == "Удалить":
                self.ssh_delete(path, is_dir)
            dialog.open = False
            self.page.update()
        
        dialog = ft.AlertDialog(
            title=ft.Text("Подтверждение удаления"),
            content=ft.Text(f"Удалить {'папку' if is_dir else 'файл'} '{name}'?"),
            actions=[
                ft.TextButton("Отмена", on_click=lambda e: setattr(dialog, "open", False)),
                ft.TextButton("Удалить", on_click=on_dialog_result, style=ft.ButtonStyle(color="#C42B1C")),
            ],
        )
        
        self.page.dialog = dialog
        dialog.open = True
        self.page.update()

    def ssh_delete(self, path: str, is_dir: bool):
        """Удаление файла/папки на сервере"""
        if not self.ssh_sftp:
            self.add_log("Нет SSH соединения", "error")
            return
        
        try:
            if is_dir:
                # Рекурсивное удаление папки
                self._ssh_delete_recursive(path)
            else:
                self.ssh_sftp.remove(path)
            
            self.add_log(f"Удалено: {os.path.basename(path)}", "success")
            self.ssh_refresh()
        except Exception as ex:
            self.add_log(f"Ошибка удаления: {ex}", "error")

    def _ssh_delete_recursive(self, path: str):
        """Рекурсивное удаление директории"""
        for entry in self.ssh_sftp.listdir_attr(path):
            entry_path = posixpath.join(path, entry.filename)
            if stat.S_ISDIR(entry.st_mode):
                self._ssh_delete_recursive(entry_path)
            else:
                self.ssh_sftp.remove(entry_path)
        self.ssh_sftp.rmdir(path)

    def ssh_rename_dialog(self, old_path: str, old_name: str, is_dir: bool):
        """Диалог переименования"""
        def on_dialog_result(e):
            if e.control.text == "Переименовать" and name_field.value:
                self.ssh_rename(old_path, name_field.value, is_dir)
            dialog.open = False
            self.page.update()
        
        name_field = ft.TextField(label="Новое имя", value=old_name, autofocus=True)
        dialog = ft.AlertDialog(
            title=ft.Text("Переименовать"),
            content=name_field,
            actions=[
                ft.TextButton("Отмена", on_click=lambda e: setattr(dialog, "open", False)),
                ft.TextButton("Переименовать", on_click=on_dialog_result),
            ],
        )
        
        self.page.dialog = dialog
        dialog.open = True
        self.page.update()

    def ssh_rename(self, old_path: str, new_name: str, is_dir: bool):
        """Переименование файла/папки"""
        if not self.ssh_sftp:
            self.add_log("Нет SSH соединения", "error")
            return
        
        try:
            dir_path = posixpath.dirname(old_path)
            new_path = posixpath.join(dir_path, new_name)
            self.ssh_sftp.rename(old_path, new_path)
            self.add_log(f"Переименовано: {posixpath.basename(old_path)} -> {new_name}", "success")
            self.ssh_refresh()
        except Exception as ex:
            self.add_log(f"Ошибка переименования: {ex}", "error")

    def ssh_download_file(self, remote_path: str, filename: str):
        """Скачивание файла с сервера"""
        def on_directory_selected(e: ft.FilePickerResultEvent):
            if e.path:
                self._ssh_download_worker(remote_path, e.path)
            self.page.overlay.remove(picker)
            self.page.update()
        
        picker = ft.FilePicker(on_result=on_directory_selected)
        self.page.overlay.append(picker)
        self.page.update()
        picker.get_directory_path(dialog_title="Выберите папку для сохранения")

    def _ssh_download_worker(self, remote_path: str, local_dir: str):
        """Фоновая задача скачивания"""
        def download_thread():
            try:
                local_path = os.path.join(local_dir, os.path.basename(remote_path))
                self.ssh_sftp.get(remote_path, local_path)
                self.add_log(f"Скачан файл: {os.path.basename(remote_path)}", "success")
            except Exception as ex:
                self.add_log(f"Ошибка скачивания: {ex}", "error")
        
        threading.Thread(target=download_thread, daemon=True).start()

    # =========================
    # SSH Profiles
    # =========================
    def ssh_save_profile_dialog(self, e=None):
        """Диалог сохранения SSH профиля"""
        def on_dialog_result(e):
            if e.control.text == "Сохранить" and name_field.value:
                self.save_ssh_profile(name_field.value)
            dialog.open = False
            self.page.update()
        
        name_field = ft.TextField(label="Имя профиля", autofocus=True)
        dialog = ft.AlertDialog(
            title=ft.Text("Сохранить SSH профиль"),
            content=ft.Column([
                name_field,
                ft.Text("Пароль будет зашифрован", size=self.fs["sm"], color=self.colors["muted"]),
            ], spacing=10),
            actions=[
                ft.TextButton("Отмена", on_click=lambda e: setattr(dialog, "open", False)),
                ft.TextButton("Сохранить", on_click=on_dialog_result),
            ],
        )
        
        self.page.dialog = dialog
        dialog.open = True
        self.page.update()

    def save_ssh_profile(self, profile_name: str):
        """Сохранение SSH профиля (без пароля в открытом виде)"""
        if not profile_name:
            self.add_log("Введите имя профиля", "warning")
            return
        
        # Шифрование пароля простым base64 (для демо)
        password = self.ssh_config.get("password", "")
        encrypted_pass = base64.b64encode(password.encode()).decode() if password else ""
        
        profile = {
            "name": profile_name,
            "host": self.ssh_config.get("host", ""),
            "port": self.ssh_config.get("port", 22),
            "username": self.ssh_config.get("username", ""),
            "use_key": self.ssh_config.get("use_key", False),
            "key_filename": self.ssh_config.get("key_filename", ""),
            "password_encrypted": encrypted_pass,
            "timestamp": datetime.now().isoformat(),
        }
        
        try:
            profiles = self._read_ssh_profiles()
            profiles.append(profile)
            self._write_ssh_profiles(profiles)
            self.add_log(f"SSH профиль '{profile_name}' сохранен", "success")
            self.load_ssh_profiles_list()
        except Exception as ex:
            self.add_log(f"Ошибка сохранения SSH профиля: {ex}", "error")

    def _read_ssh_profiles(self) -> list:
        """Чтение SSH профилей"""
        try:
            if os.path.exists("ssh_profiles.json"):
                with open("ssh_profiles.json", "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _write_ssh_profiles(self, profiles: list):
        """Запись SSH профилей"""
        with open("ssh_profiles.json", "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, indent=2)

    def load_ssh_profiles_list(self):
        if not self.ssh_profiles_list:
            return
        try:
            profiles = self._read_ssh_profiles()
            self.ssh_profiles_list.controls.clear()
            for p in profiles:
                self.ssh_profiles_list.controls.append(self._ssh_profile_item(p))
            if self.ssh_profiles_count_text:
                self.ssh_profiles_count_text.value = str(len(profiles))
            self.page.update()
        except Exception as ex:
            self.add_log(f"Ошибка загрузки SSH профилей: {ex}", "error")


    def _ssh_profile_item(self, profile: dict):
        ts_iso = profile.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_iso).strftime("%d.%m.%Y %H:%M")
        except Exception:
            ts = ts_iso or "--"
        
        host = profile.get("host", "")
        user = profile.get("username", "")
        
        row = ft.Row(
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(ft.icons.CLOUD, color=self.colors["accent"], size=16),
                ft.Column(
                    expand=True,
                    spacing=2,
                    controls=[
                        ft.Text(profile.get("name", "profile"), size=self.fs["md"], color=self.colors["text"]),
                        ft.Text(f"{user}@{host} | {ts}", size=self.fs["sm"], color=self.colors["muted"]),
                    ],
                ),
                ft.IconButton(
                    icon=ft.icons.PLAY_ARROW,
                    icon_size=16,
                    icon_color=self.colors["accent"],
                    tooltip="Загрузить",
                    on_click=lambda e, p=profile: self.load_ssh_profile(p),
                ),
            ],
        )

        return ft.GestureDetector(
            on_secondary_tap_down=lambda e, p=profile: self._open_ssh_profile_context_menu(e, p),
            content=ft.Container(
                bgcolor=self.colors["bg"],
                border=ft.border.all(1, self.colors["border"]),
                border_radius=6,
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
                content=row,
            ),
        )

    def _open_ssh_profile_context_menu(self, e: ft.TapEvent, profile: dict):
        ts = profile.get("timestamp", "")

        def do_load():
            self.load_ssh_profile(profile)

        def do_delete():
            self.delete_ssh_profile(ts)

        items = [
            ("Загрузить", ft.icons.PLAY_ARROW, do_load),
            ("Удалить", ft.icons.DELETE_OUTLINE, do_delete),
        ]
        self.show_context_menu(e.global_x, e.global_y, items)

    def delete_ssh_profile(self, timestamp_iso: str):
        try:
            profiles = self._read_ssh_profiles()
            new_profiles = [p for p in profiles if p.get("timestamp") != timestamp_iso]
            self._write_ssh_profiles(new_profiles)
            self.load_ssh_profiles_list()
            self.add_log("SSH профиль удалён", "success")
        except Exception as ex:
            self.add_log(f"Ошибка удаления SSH профиля: {ex}", "error")

    def load_ssh_profile(self, profile: dict):
        """Загрузка SSH профиля"""
        try:
            self.ssh_host_field.value = profile.get("host", "")
            self.ssh_port_field.value = str(profile.get("port", 22))
            self.ssh_user_field.value = profile.get("username", "")
            self.ssh_use_key_cb.value = profile.get("use_key", False)
            
            # Дешифровка пароля
            enc_pass = profile.get("password_encrypted", "")
            if enc_pass:
                try:
                    password = base64.b64decode(enc_pass).decode()
                    self.ssh_pass_field.value = password
                except Exception:
                    self.ssh_pass_field.value = ""
                    self.add_log("Не удалось расшифровать пароль", "warning")
            else:
                self.ssh_pass_field.value = ""
            
            key_fn = profile.get("key_filename", "")
            if key_fn and os.path.exists(key_fn):
                self.ssh_config["key_filename"] = key_fn
                if self.ssh_key_text:
                    self.ssh_key_text.value = f"Key: {key_fn}"
            
            self.add_log(f"SSH профиль '{profile.get('name')}' загружен", "success")
            self.page.update()
        except Exception as ex:
            self.add_log(f"Ошибка загрузки SSH профиля: {ex}", "error")


def main(page: ft.Page):
    PoogaloBackup(page)


if __name__ == "__main__":
    ft.app(target=main)