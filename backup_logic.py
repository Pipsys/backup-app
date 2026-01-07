import os
import shutil
import hashlib
import zipfile
import threading
import time
from datetime import datetime
import json
from typing import List, Dict, Optional, Callable
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import psutil


class BackupManager:
    """Менеджер резервного копирования"""

    def __init__(
        self,
        source_dirs: List[str],
        source_files: List[str],
        target_dir: str,
        incremental: bool = True,
        skip_hidden: bool = True,
        compress: bool = False,
        preserve_structure: bool = True,
        progress_callback: Optional[Callable] = None,
        log_callback: Optional[Callable] = None,
    ):
        self.source_dirs = source_dirs
        self.source_files = source_files
        self.target_dir = target_dir
        self.incremental = incremental
        self.skip_hidden = skip_hidden
        self.compress = compress
        self.preserve_structure = preserve_structure
        self.progress_callback = progress_callback
        self.log_callback = log_callback

        # Статистика
        self.stats = {
            "total_files": 0,
            "files_copied": 0,
            "files_skipped": 0,
            "total_size": 0,
            "start_time": None,
            "end_time": None,
        }

        self._stop_flag = threading.Event()

        self.last_backup_info = self.load_last_backup_info()

        self.observer = None
        self.file_monitor = None

    def start_backup(self):
        """Запуск резервного копирования"""
        try:
            self.stats["start_time"] = datetime.now()

            self._log("Начинаю резервное копирование...", "info")
            self._log(f"Источники: {len(self.source_dirs)} папок, {len(self.source_files)} файлов", "info")
            self._log(f"Целевая директория: {self.target_dir}", "info")

            backup_name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            self.backup_path = os.path.join(self.target_dir, backup_name)
            os.makedirs(self.backup_path, exist_ok=True)

            metadata = {
                "backup_name": backup_name,
                "timestamp": datetime.now().isoformat(),
                "settings": {
                    "incremental": self.incremental,
                    "compress": self.compress,
                    "preserve_structure": self.preserve_structure,
                },
                "sources": {
                    "directories": self.source_dirs,
                    "files": self.source_files,
                },
            }

            all_files = self._collect_files()
            self.stats["total_files"] = len(all_files)

            if self.stats["total_files"] == 0:
                self._log("Нет файлов для копирования", "warning")
                return

            self._log(f"Найдено {self.stats['total_files']} файлов для обработки", "info")

            if self.compress:
                self._create_compressed_backup(all_files, metadata)
            else:
                self._copy_files(all_files, metadata)

            # ✅ исправлено сохранение last_backup.json (datetime -> isoformat)
            self.save_backup_info(metadata)

            self.stats["end_time"] = datetime.now()
            duration = (self.stats["end_time"] - self.stats["start_time"]).total_seconds()

            self._log("Резервное копирование завершено успешно!", "success")
            self._log(
                f"Статистика: {self.stats['files_copied']} скопировано, {self.stats['files_skipped']} пропущено",
                "info",
            )
            self._log(f"Время выполнения: {duration:.2f} секунд", "info")

        except Exception as e:
            self._log(f"Ошибка при резервном копировании: {str(e)}", "error")
            raise

    def _collect_files(self) -> List[Dict]:
        """Сбор всех файлов для копирования"""
        all_files = []

        for source_dir in self.source_dirs:
            if self._stop_flag.is_set():
                break

            if not os.path.exists(source_dir):
                self._log(f"Директория не найдена: {source_dir}", "warning")
                continue

            for root, dirs, files in os.walk(source_dir):
                if self.skip_hidden:
                    dirs[:] = [d for d in dirs if not d.startswith(".")]

                for file in files:
                    if self._stop_flag.is_set():
                        return all_files

                    file_path = os.path.join(root, file)

                    if self.skip_hidden and file.startswith("."):
                        continue

                    try:
                        file_size = os.path.getsize(file_path)
                        self.stats["total_size"] += file_size

                        relative_path = os.path.relpath(file_path, source_dir)

                        all_files.append(
                            {
                                "source_path": file_path,
                                "relative_path": relative_path,
                                "size": file_size,
                                "source_type": "directory",
                                "source_root": source_dir,
                            }
                        )

                    except (OSError, PermissionError) as e:
                        self._log(f"Не удалось получить информацию о файле {file_path}: {str(e)}", "warning")

        for file_path in self.source_files:
            if self._stop_flag.is_set():
                break

            if not os.path.exists(file_path):
                self._log(f"Файл не найден: {file_path}", "warning")
                continue

            try:
                file_size = os.path.getsize(file_path)
                self.stats["total_size"] += file_size

                all_files.append(
                    {
                        "source_path": file_path,
                        "relative_path": os.path.basename(file_path),
                        "size": file_size,
                        "source_type": "file",
                    }
                )

            except (OSError, PermissionError) as e:
                self._log(f"Не удалось получить информацию о файле {file_path}: {str(e)}", "warning")

        return all_files

    def _copy_files(self, files: List[Dict], metadata: Dict):
        """Копирование файлов"""
        metadata_file = os.path.join(self.backup_path, "backup_metadata.json")
        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        log_file = os.path.join(self.backup_path, "backup_log.txt")

        copied_count = 0
        with open(log_file, "w", encoding="utf-8") as log:
            log.write(f"Резервное копирование начато: {datetime.now()}\n")
            log.write(f"Всего файлов: {len(files)}\n")
            log.write("-" * 80 + "\n")

            for i, file_info in enumerate(files):
                if self._stop_flag.is_set():
                    self._log("Копирование остановлено пользователем", "warning")
                    break

                try:
                    source_path = file_info["source_path"]
                    relative_path = file_info["relative_path"]

                    if self.preserve_structure and file_info["source_type"] == "directory":
                        target_path = os.path.join(
                            self.backup_path,
                            file_info["source_root"].replace(":", "_").lstrip("/"),
                            relative_path,
                        )
                    else:
                        target_path = os.path.join(self.backup_path, os.path.basename(source_path))

                    os.makedirs(os.path.dirname(target_path), exist_ok=True)

                    if self.incremental and self._should_skip_file(source_path, target_path):
                        self.stats["files_skipped"] += 1
                        log.write(f"ПРОПУЩЕНО: {source_path}\n")
                        continue

                    shutil.copy2(source_path, target_path)

                    self.stats["files_copied"] += 1
                    copied_count += 1

                    log.write(f"СКОПИРОВАНО: {source_path} -> {target_path}\n")

                    if self.progress_callback:
                        self.progress_callback(i + 1, len(files), source_path)

                    if copied_count % 10 == 0:
                        self._log(f"Скопировано {copied_count} из {len(files)} файлов", "info")

                except PermissionError as e:
                    self._log(f"Нет прав доступа к файлу {file_info['source_path']}: {str(e)}", "warning")
                    self.stats["files_skipped"] += 1

                except Exception as e:
                    self._log(f"Ошибка при копировании файла {file_info['source_path']}: {str(e)}", "warning")
                    self.stats["files_skipped"] += 1

            log.write("-" * 80 + "\n")
            log.write(f"Резервное копирование завершено: {datetime.now()}\n")
            log.write(f"Итого скопировано: {self.stats['files_copied']} файлов\n")
            log.write(f"Пропущено: {self.stats['files_skipped']} файлов\n")

    def _create_compressed_backup(self, files: List[Dict], metadata: Dict):
        """Создание сжатого архива"""
        archive_path = os.path.join(self.backup_path, "backup.zip")

        self._log(f"Создание архива: {archive_path}", "info")

        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            metadata_str = json.dumps(metadata, indent=2, ensure_ascii=False)
            zipf.writestr("backup_metadata.json", metadata_str)

            for i, file_info in enumerate(files):
                if self._stop_flag.is_set():
                    break

                try:
                    source_path = file_info["source_path"]

                    if self.preserve_structure and file_info["source_type"] == "directory":
                        arcname = os.path.join(
                            file_info["source_root"].replace(":", "_").lstrip("/"),
                            file_info["relative_path"],
                        )
                    else:
                        arcname = os.path.basename(source_path)

                    zipf.write(source_path, arcname)

                    self.stats["files_copied"] += 1

                    if self.progress_callback:
                        self.progress_callback(i + 1, len(files), source_path)

                except Exception as e:
                    self._log(f"Ошибка при добавлении файла в архив {file_info['source_path']}: {str(e)}", "warning")
                    self.stats["files_skipped"] += 1

        self._log(f"Архив создан: {archive_path}", "success")

    def _should_skip_file(self, source_path: str, target_path: str) -> bool:
        """Проверка, нужно ли пропускать файл при инкрементальном бэкапе"""
        if not os.path.exists(target_path):
            return False

        try:
            source_mtime = os.path.getmtime(source_path)
            target_mtime = os.path.getmtime(target_path)
            return target_mtime >= source_mtime
        except OSError:
            return False

    def _get_file_hash(self, filepath: str) -> str:
        """Получение хэша файла"""
        hash_md5 = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def _log(self, message: str, log_type: str = "info"):
        if self.log_callback:
            self.log_callback(message, log_type)

    def stop(self):
        self._stop_flag.set()
        self._log("Получена команда на остановку...", "warning")

    def get_stats(self) -> Dict:
        stats = self.stats.copy()
        stats["total_size_mb"] = stats["total_size"] / (1024 * 1024)
        return stats

    # ✅ FIX: datetime -> isoformat
    def _to_json_safe(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {k: self._to_json_safe(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._to_json_safe(v) for v in obj]
        return obj

    def save_backup_info(self, metadata: Dict):
        """Сохранение информации о последнем бэкапе (без ошибок JSON)."""
        backup_info = {
            "last_backup": metadata["timestamp"],
            "backup_path": self.backup_path,
            "stats": self.stats,
        }

        backup_info = self._to_json_safe(backup_info)

        info_file = os.path.join(self.target_dir, "last_backup.json")
        with open(info_file, "w", encoding="utf-8") as f:
            json.dump(backup_info, f, indent=2, ensure_ascii=False)

    def load_last_backup_info(self) -> Optional[Dict]:
        info_file = os.path.join(self.target_dir, "last_backup.json")
        if os.path.exists(info_file):
            try:
                with open(info_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                pass
        return None

    def get_disk_space(self) -> Optional[Dict]:
        try:
            disk_usage = psutil.disk_usage(self.target_dir)
            return {
                "total": disk_usage.total,
                "used": disk_usage.used,
                "free": disk_usage.free,
                "percent": disk_usage.percent,
            }
        except:
            return None


class FileMonitor(FileSystemEventHandler):
    """Мониторинг изменений файлов"""

    def __init__(self, callback: Callable):
        self.callback = callback
        self.observer = Observer()

    def on_modified(self, event):
        if not event.is_directory:
            self.callback(event.src_path, "modified")

    def on_created(self, event):
        if not event.is_directory:
            self.callback(event.src_path, "created")

    def on_deleted(self, event):
        if not event.is_directory:
            self.callback(event.src_path, "deleted")

    def start_monitoring(self, path: str):
        self.observer.schedule(self, path, recursive=True)
        self.observer.start()

    def stop_monitoring(self):
        self.observer.stop()
        self.observer.join()


class BackupScheduler:
    """Планировщик резервного копирования"""

    def __init__(self, backup_manager: BackupManager):
        self.backup_manager = backup_manager
        self.schedule_thread = None
        self._stop_flag = threading.Event()

    def schedule_daily(self, hour: int = 0, minute: int = 0):
        self._stop_flag.clear()
        self.schedule_thread = threading.Thread(target=self._daily_schedule, args=(hour, minute))
        self.schedule_thread.daemon = True
        self.schedule_thread.start()

    def _daily_schedule(self, hour: int, minute: int):
        while not self._stop_flag.is_set():
            now = datetime.now()
            if now.hour == hour and now.minute == minute:
                self.backup_manager.start_backup()
                time.sleep(3600)
            time.sleep(60)

    def stop_schedule(self):
        self._stop_flag.set()
        if self.schedule_thread:
            self.schedule_thread.join(timeout=5)


def validate_paths(paths: List[str]) -> Dict[str, List[str]]:
    result = {"valid": [], "invalid": []}
    for path in paths:
        if os.path.exists(path):
            result["valid"].append(path)
        else:
            result["invalid"].append(path)
    return result


def calculate_backup_size(paths: List[str]) -> int:
    total_size = 0
    for path in paths:
        if os.path.isfile(path):
            try:
                total_size += os.path.getsize(path)
            except OSError:
                pass
        elif os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                for file in files:
                    try:
                        file_path = os.path.join(root, file)
                        total_size += os.path.getsize(file_path)
                    except OSError:
                        pass
    return total_size


def format_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"
