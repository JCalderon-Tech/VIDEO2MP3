#!/usr/bin/env python3
"""
Video → MP3 Downloader
======================
Descarga videos desde una cola de URLs y los convierte a MP3 en alta calidad,
con metadatos ID3 y carátula embebida.

Arquitectura (single-launcher orchestration):
    - QueueManager  -> ÚNICO punto de coordinación. Administra la cola,
                        el worker thread y el estado de cada item.
                        Toda la lógica de descarga/conversión vive aquí.
    - DownloadItem  -> Modelo de datos simple. Identidad estable por `uid`
                        (nunca por posición en la lista).
    - MainWindow (PySide6/Qt6) -> Capa de presentación. NO contiene lógica de
                        negocio, solo invoca métodos de QueueManager y refleja
                        su estado vía callbacks + log_queue (patrón QTimer poll).

Requisitos del sistema:
    - Python 3.11+ (yt-dlp recomienda 3.11; 3.9/3.10 están en EOL)
    - ffmpeg instalado y en el PATH (necesario para extraer audio en alta calidad)
    - pip install -r requirements.txt   (yt-dlp, PySide6)

Estado de un item: En cola | Descargando | Convirtiendo | Completado |
                  Error | Cancelado | (Eliminado se purga de la lista)
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, fields
from pathlib import Path
from string import Template
from typing import cast

from PySide6.QtCore import QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


# --------------------------------------------------------------------------- #
# Constantes de estado (strings visibles al usuario, en español)
# --------------------------------------------------------------------------- #

STATUS_QUEUED = "En cola"
STATUS_DOWNLOADING = "Descargando"
STATUS_CONVERTING = "Convirtiendo"
STATUS_COMPLETED = "Completado"
STATUS_ERROR = "Error"
STATUS_CANCELLED = "Cancelado"


class DownloadCancelled(Exception):
    """Se lanza desde el progress_hook para abortar la descarga en curso."""


class DuplicateURLError(ValueError):
    """URL ya presente en la cola."""


# --------------------------------------------------------------------------- #
# Persistencia de configuración (JSON en el home del usuario)
# --------------------------------------------------------------------------- #

@dataclass
class Settings:
    output_dir: str = ""
    bitrate: str = "320"
    ffmpeg_location: str = ""
    theme: str = "light"
    geometry: str = ""


def _settings_path() -> Path:
    return Path.home() / ".video2mp3.json"


def _asset_path(filename: str) -> Path:
    """Devuelve la ruta a un recurso estático (soporta modo bundle PyInstaller)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "assets" / filename
    return Path(__file__).resolve().parent / "assets" / filename


def load_settings() -> Settings:
    path = _settings_path()
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Settings()
    result = Settings()
    for f in fields(Settings):
        if f.name in data:
            setattr(result, f.name, data[f.name])
    return result


def save_settings(s: Settings) -> None:
    try:
        _settings_path().write_text(
            json.dumps(s.__dict__, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass  # config opcional: no debe romper la app


# --------------------------------------------------------------------------- #
# Modelo de datos
# --------------------------------------------------------------------------- #

@dataclass
class DownloadItem:
    uid: int = 0
    url: str = ""
    status: str = STATUS_QUEUED
    progress: float = 0.0        # 0.0 - 100.0
    speed_str: str = ""
    eta_str: str = ""
    error_msg: str = ""
    title: str = ""
    cancel_requested: bool = False
    shown: bool = False          # ya se mostró el detalle de error en el log


# --------------------------------------------------------------------------- #
# Orquestador único (LAUNCHER)
# --------------------------------------------------------------------------- #

class QueueManager:
    """
    Único punto de coordinación del sistema. Administra:
      - la cola de items a descargar
      - el ciclo de vida del worker thread
      - la extracción de audio vía yt-dlp/ffmpeg
      - el reporte de progreso hacia la UI (por callback, sin acoplar lógica)

    La GUI nunca llama a yt-dlp directamente: todo pasa por aquí.
    """

    def __init__(self, output_dir: Path, bitrate_kbps: str = "320",
                 ffmpeg_location: str | None = None,
                 on_update=None, on_log=None):
        self.output_dir = output_dir
        self.bitrate_kbps = bitrate_kbps
        # Ruta explícita a la carpeta que contiene ffmpeg.exe/ffprobe.exe.
        # Se usa cuando el PATH del sistema no expone el binario (común en
        # Windows tras instalar con winget sin reiniciar sesión).
        self.ffmpeg_location = ffmpeg_location
        self.items: list[DownloadItem] = []
        # Identidad estable: los uid nunca se reutilizan, aunque se eliminen
        # items de la lista (evita los bugs de indexar por posición).
        self._next_uid = 0
        self._task_queue: "queue.Queue[int]" = queue.Queue()
        self._worker_thread: threading.Thread | None = None
        self._running = False
        self._stop_requested = False
        self._updating_ytdlp = False

        # Buffer thread-safe de mensajes (cola) en lugar de lista compartida.
        self.log_queue: queue.Queue[str] = queue.Queue()
        # on_log puede ser un callable externo; si no se da, se usa la cola.
        self.on_log = on_log or self.log_queue.put_nowait

        # Callbacks hacia la capa de presentación (inyección de dependencia,
        # así QueueManager no conoce nada de la GUI)
        self.on_update = on_update or (lambda uid: None)

    # ---- API pública usada por la GUI --------------------------------- #

    def _item_by_uid(self, uid: int) -> DownloadItem | None:
        for it in self.items:
            if it.uid == uid:
                return it
        return None

    def add_url(self, url: str) -> int:
        url = url.strip()
        if not url:
            raise ValueError("URL vacía")
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url  # acepta URLs sin esquema
        for it in self.items:
            if it.url == url:
                raise DuplicateURLError(url)
        uid = self._next_uid
        self._next_uid += 1
        item = DownloadItem(url=url, uid=uid)
        self.items.append(item)
        self._task_queue.put(uid)
        self.on_update(uid)
        self.on_log(f"[+] Añadido a la cola: {url}")
        return uid

    def remove_item(self, uid: int):
        """Elimina un ítem de la lista (purga). No aplica si está descargando."""
        item = self._item_by_uid(uid)
        if item is None:
            return
        if item.status in (STATUS_DOWNLOADING, STATUS_CONVERTING):
            return  # para eso está cancel_item
        self.items.remove(item)
        self.on_log(f"[-] Eliminado de la cola: {item.url}")
        self.on_update(uid)

    def retry_item(self, uid: int):
        """Reintenta un ítem en Error o Cancelado."""
        item = self._item_by_uid(uid)
        if item is None or item.status not in (STATUS_ERROR, STATUS_CANCELLED):
            return
        item.status = STATUS_QUEUED
        item.progress = 0.0
        item.error_msg = ""
        item.speed_str = ""
        item.eta_str = ""
        item.cancel_requested = False
        item.shown = False
        self._task_queue.put(uid)
        self.on_log(f"[↻] Reintentando: {item.url}")
        self.on_update(uid)

    def cancel_item(self, uid: int):
        """Cancela un ítem en cola o aborta la descarga en curso."""
        item = self._item_by_uid(uid)
        if item is None:
            return
        if item.status in (STATUS_DOWNLOADING, STATUS_CONVERTING):
            item.cancel_requested = True
            self.on_log(f"[x] Cancelando descarga en curso: {item.url}")
        elif item.status == STATUS_QUEUED:
            item.status = STATUS_CANCELLED
            self.on_log(f"[x] Cancelado: {item.url}")
        else:
            return
        self.on_update(uid)

    def start(self):
        if self._running:
            return
        if yt_dlp is None:
            self.on_log("[!] ERROR: falta la librería 'yt-dlp'. Ejecuta: "
                         "pip install yt-dlp")
            return
        try:
            from yt_dlp.version import __version__ as _ytver
            self.on_log(f"[i] Motor yt-dlp {_ytver}")
        except Exception:
            pass
        self._running = True
        self._stop_requested = False
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        self.on_log("[>] Worker iniciado.")

    def stop(self):
        """Frena el worker de forma cooperativa (termina el ítem en curso)."""
        self._stop_requested = True
        self.on_log("[>] Deteniendo el worker...")

    def is_running(self) -> bool:
        return self._running

    def set_bitrate(self, bitrate: str):
        """Permite cambiar la calidad de codificación en tiempo real."""
        self.bitrate_kbps = bitrate

    def update_ytdlp(self):
        """Actualiza yt-dlp a la última versión estable (en un hilo propio)."""
        if self._updating_ytdlp:
            return
        self._updating_ytdlp = True
        threading.Thread(target=self._run_ytdlp_update, daemon=True).start()

    # ---- Lógica interna -------------------------------------------------- #

    def _run_ytdlp_update(self):
        try:
            self.on_log("[>] Actualizando yt-dlp...")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            proc = subprocess.run(
                [sys.executable, "-m", "yt_dlp", "-U"],
                capture_output=True, text=True, timeout=180,
                creationflags=flags,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            for line in output.splitlines():
                if line.strip():
                    self.on_log(f"    {line.strip()}")
            if proc.returncode != 0:
                self.on_log("[!] No se completó la actualización. "
                            "Probá: pip install -U yt-dlp")
        except Exception as exc:  # noqa: BLE001 - reportar y no colgar la app
            self.on_log(f"[!] No se pudo actualizar yt-dlp: {exc}")
        finally:
            self._updating_ytdlp = False

    def _worker_loop(self):
        # El worker se mantiene activo mientras no se solicite la parada.
        # Cuando la cola está vacía, espera nuevos ítems (no se detiene).
        while not self._stop_requested:
            try:
                uid = self._task_queue.get(timeout=0.5)
            except queue.Empty:
                continue  # espera nuevos ítems en lugar de terminar

            item = self._item_by_uid(uid)
            if item is None or item.status != STATUS_QUEUED:
                continue  # fue eliminado o cancelado antes de procesarse

            try:
                self._download_and_convert(uid)
            except DownloadCancelled:
                item = self._item_by_uid(uid)
                if item is not None:
                    item.status = STATUS_CANCELLED
                    self.on_log(f"[x] Cancelado: {item.url}")
                    self.on_update(uid)
            except Exception as exc:  # noqa: BLE001 - reportar cualquier fallo
                item = self._item_by_uid(uid)
                if item is not None:
                    item.status = STATUS_ERROR
                    item.error_msg = str(exc)
                    self.on_log(f"[x] Error con {item.url}: {exc}")
                    self.on_update(uid)

        self._running = False
        self.on_log("[>] Worker detenido.")

    def _download_and_convert(self, uid: int):
        item = self._item_by_uid(uid)
        if item is None:
            return
        item.status = STATUS_DOWNLOADING
        item.progress = 0.0
        self.on_update(uid)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        def progress_hook(d):
            if item.cancel_requested:
                raise DownloadCancelled()
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes")
                if total:
                    item.progress = min(100.0, downloaded / total * 100.0)
                else:
                    pct_str = d.get("_percent_str", "0%").strip().replace("%", "")
                    try:
                        item.progress = float(pct_str)
                    except ValueError:
                        pass
                item.speed_str = d.get("_speed_str", "") or ""
                item.eta_str = d.get("_eta_str", "") or ""
                item.status = STATUS_DOWNLOADING
                self.on_update(uid)
            elif d.get("status") == "finished":
                item.status = STATUS_CONVERTING
                item.progress = 100.0
                self.on_update(uid)

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(self.output_dir / "%(title)s.%(ext)s"),
            # Política de colisión: no sobrescribir archivos ya existentes.
            "overwrites": False,
            # Metadatos ID3 + carátula embebida en el MP3 final.
            "writethumbnail": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": self.bitrate_kbps,  # ej. "320" kbps
                },
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ],
            "progress_hooks": [progress_hook],
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "noplaylist": False,  # si pegan una playlist, la baja completa
            "retries": 5,
            "fragment_retries": 5,
            # Evita que una conexión colgada bloquee el worker indefinidamente.
            "socket_timeout": 30,
            # Mitiga bloqueos 403 causados por firmas/cliente desactualizados:
            # se prueba primero con el cliente "android", que suele evitar
            # el desafío de firma que rompe al cliente "web".
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "tv", "ios", "web"],
                }
            },
            "verbose": False,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            },
        }

        if self.ffmpeg_location:
            ydl_opts["ffmpeg_location"] = self.ffmpeg_location

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(item.url, download=True)
            item.title = info.get("title", item.url)

        item.status = STATUS_COMPLETED
        item.progress = 100.0
        item.speed_str = ""
        item.eta_str = ""
        self.on_log(f"[✓] Completado: {item.title}")
        self.on_update(uid)


# --------------------------------------------------------------------------- #
# Sistema de temas claro/oscuro — paleta de tokens universal + QSS dinámico
# --------------------------------------------------------------------------- #
#
# Todos los colores de la interfaz viven en THEMES. Ningún widget define
# colores por su cuenta: get_stylesheet() traduce esos tokens a un QSS global
# (app.setStyleSheet) que cubre TODA la jerarquía de widgets: superficies,
# campos, botones, tablas, listas, menús, dialogs, tooltips, popups,
# scrollbars, sliders, progress bars, checkboxes, radios, spinners y más.
#
# Jerarquía de superficies (identidad compartida por ambos temas):
#     background     -> superficie principal de la ventana
#     surface        -> paneles / áreas elevadas
#     surface_alt    -> hover de superficies, filas alternas
#     card           -> tarjetas / agrupaciones
#     input          -> campos de edición (rehundidos)
#     button         -> botones (base / hover / pressed)
#     header         -> encabezados de tablas
#     menu           -> menús y popups
#     tooltip        -> tooltips
#     border / scrollbar -> bordes y desplazadores
#
# Los recursos gráficos (flechas de combo/spin, checks, radios) se generan
# por tema con QPainter y se referencian desde el QSS, de modo que ningún
# icono queda "negro sobre oscuro" ni "blanco sobre claro".

THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "background": "#17181b",
        "surface": "#212327",
        "surface_alt": "#2a2d33",
        "card": "#26292f",
        "input": "#1c1e22",
        "border": "#3a3f47",
        "text": "#e8eaed",
        "text_secondary": "#a8adb4",
        "text_disabled": "#6a7078",
        "primary": "#2b72c9",
        "primary_hover": "#3780dc",
        "primary_pressed": "#2567b6",
        "success": "#238636",
        "success_hover": "#2b9440",
        "success_pressed": "#1e7a2f",
        "warning": "#d29922",
        "warning_hover": "#dfa62e",
        "warning_pressed": "#b98a1e",
        "error": "#c73b30",
        "error_hover": "#d4483d",
        "error_pressed": "#ab3228",
        "error_text": "#ff8a80",
        "error_bg": "#3a2226",
        "info": "#2f8ff0",
        "selection": "#2b72c9",
        "selection_text": "#ffffff",
        "on_colored": "#ffffff",
        "scrollbar": "#3a3f47",
        "scrollbar_hover": "#52585f",
        "tooltip_bg": "#2b2e34",
        "tooltip_border": "#3f444b",
        "menu": "#212327",
        "menu_hover": "#2d3138",
        "menu_separator": "#3a3f47",
        "header": "#2a2e34",
        "header_text": "#d7dadd",
        "alt_row": "#1f2125",
        "button": "#2a2d33",
        "button_hover": "#34383f",
        "button_pressed": "#1f2125",
    },
    "light": {
        "background": "#f2f3f5",
        "surface": "#ffffff",
        "surface_alt": "#e9ebee",
        "card": "#fafbfc",
        "input": "#ffffff",
        "border": "#c8cdd3",
        "text": "#1c1e21",
        "text_secondary": "#5b6168",
        "text_disabled": "#7f868d",
        "primary": "#1668dc",
        "primary_hover": "#0f5cc7",
        "primary_pressed": "#0b4fae",
        "success": "#1a7f37",
        "success_hover": "#14682e",
        "success_pressed": "#115827",
        "warning": "#9a6b1b",
        "warning_hover": "#8a5e16",
        "warning_pressed": "#7a5213",
        "error": "#c42a1f",
        "error_hover": "#b0261c",
        "error_pressed": "#971f16",
        "error_text": "#b42318",
        "error_bg": "#fdecec",
        "info": "#1668dc",
        "selection": "#1668dc",
        "selection_text": "#ffffff",
        "on_colored": "#ffffff",
        "scrollbar": "#c8cdd3",
        "scrollbar_hover": "#a7adb5",
        "tooltip_bg": "#ffffff",
        "tooltip_border": "#c8cdd3",
        "menu": "#ffffff",
        "menu_hover": "#eef1f4",
        "menu_separator": "#e2e5e9",
        "header": "#eef0f2",
        "header_text": "#2b2f34",
        "alt_row": "#f7f8f9",
        "button": "#e9ebee",
        "button_hover": "#dde0e4",
        "button_pressed": "#d0d4da",
    },
}


_ASSET_CACHE: dict[tuple[str, str], str] = {}


def _draw_asset(kind: str, color: str) -> QPixmap:
    """Dibuja un recurso 16x16 (flechas, checks, radios) en un color dado."""
    base = kind.removesuffix("_dim")
    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color), 1.8))
    path = QPainterPath()
    if base in ("arrow", "spin_down"):
        path.moveTo(4.0, 6.0)
        path.lineTo(8.0, 10.0)
        path.lineTo(12.0, 6.0)
    elif base == "spin_up":
        path.moveTo(4.0, 10.0)
        path.lineTo(8.0, 6.0)
        path.lineTo(12.0, 10.0)
    elif base == "check":
        path.moveTo(3.5, 8.0)
        path.lineTo(6.5, 11.0)
        path.lineTo(12.5, 4.5)
    painter.drawPath(path)
    if base == "dot":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(color)))
        painter.drawEllipse(QRectF(4.0, 4.0, 8.0, 8.0))
    painter.end()
    return pixmap


def _asset(theme: str, kind: str, color: str) -> str:
    """Devuelve la ruta en disco de un recurso PNG cacheado por (tema, tipo)."""
    key = (theme, kind)
    cached = _ASSET_CACHE.get(key)
    if cached is not None:
        return cached
    if QApplication.instance() is None:
        _ASSET_CACHE[key] = ""
        return ""
    dest = Path(tempfile.gettempdir()) / f"video2mp3_{theme}_{kind}.png"
    path = ""
    if _draw_asset(kind, color).save(str(dest), "PNG"):
        path = str(dest).replace("\\", "/")
    _ASSET_CACHE[key] = path
    return path


def _assets_for(theme: str, t: dict[str, str]) -> dict[str, str]:
    """Reúne las rutas de los recursos gráficos generados para el tema."""
    return {
        "arrow": _asset(theme, "arrow", t["text_secondary"]),
        "spin_up": _asset(theme, "spin_up", t["text_secondary"]),
        "spin_down": _asset(theme, "spin_down", t["text_secondary"]),
        "check": _asset(theme, "check", t["on_colored"]),
        "check_dim": _asset(theme, "check_dim", t["text_disabled"]),
        "dot": _asset(theme, "dot", t["on_colored"]),
        "dot_dim": _asset(theme, "dot_dim", t["text_disabled"]),
    }


def _combo_arrow(theme: str) -> str:
    """Compatibilidad: ruta de la flecha del combo para el tema dado."""
    return _asset(theme, "arrow", THEMES[theme]["text_secondary"])


_QSS_TEMPLATE = Template(
    """
/* Superficies raíz */
QMainWindow, QDialog, QMessageBox, QFileDialog {
    background-color: $background;
    color: $text;
}
/* Los contenedores internos de QScrollArea heredan el palette nativo de Qt
   (light) si no se les asigna bg; con transparencia dejan ver el fondo del
   ancestro temado y evitan fugas de color en el tema oscuro. */
QScrollArea QWidget {
    background-color: transparent;
}
QWidget {
    color: $text;
}
QWidget {
    color: $text;
}

/* Etiquetas */
QLabel {
    background-color: transparent;
    color: $text;
}
QLabel[error="true"] {
    color: $error_text;
    font-weight: 600;
}

/* Campos de texto y edición */
QLineEdit, QSpinBox, QDoubleSpinBox {
    background-color: $input;
    color: $text;
    border: 1px solid $border;
    border-radius: 4px;
    padding: 6px 8px;
    selection-background-color: $selection;
    selection-color: $selection_text;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid $primary;
}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    color: $text_disabled;
    background-color: $background;
    border-color: $border;
}

/* Botones */
QPushButton, QToolButton {
    background-color: $button;
    color: $text;
    border: 1px solid $border;
    border-radius: 4px;
    padding: 5px 10px;
    min-height: 18px;
    font-weight: 500;
}
QPushButton:hover, QToolButton:hover {
    background-color: $button_hover;
    border-color: $text_disabled;
}
QPushButton:pressed, QToolButton:pressed {
    background-color: $button_pressed;
    border-color: $text_disabled;
}
QPushButton:focus, QToolButton:focus {
    border: 1px solid $primary;
}
QPushButton:disabled, QToolButton:disabled {
    color: $text_disabled;
    background-color: $surface;
    border-color: $border;
}

/* Acciones principales (acento de marca) */
QPushButton#startButton {
    background-color: $success;
    color: $on_colored;
    border: 1px solid $success;
}
QPushButton#startButton:hover {
    background-color: $success_hover;
}
QPushButton#startButton:pressed {
    background-color: $success_pressed;
}
QPushButton#startButton:disabled {
    background-color: $button;
    color: $text_disabled;
    border-color: $border;
}
QPushButton#stopButton {
    background-color: $error;
    color: $on_colored;
    border: 1px solid $error;
}
QPushButton#stopButton:hover {
    background-color: $error_hover;
}
QPushButton#stopButton:pressed {
    background-color: $error_pressed;
}
QPushButton#stopButton:disabled {
    background-color: $button;
    color: $text_disabled;
    border-color: $border;
}

/* Combobox */
QComboBox {
    background-color: $input;
    color: $text;
    border: 1px solid $border;
    border-radius: 4px;
    padding: 5px 8px;
    min-height: 18px;
}
QComboBox:hover {
    border-color: $text_disabled;
}
QComboBox:focus {
    border: 1px solid $primary;
}
QComboBox:disabled {
    color: $text_disabled;
    background-color: $background;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background-color: $menu;
    color: $text;
    border: 1px solid $border;
    selection-background-color: $selection;
    selection-color: $selection_text;
    outline: 0;
}
QComboBox QAbstractItemView::item:hover {
    background-color: $menu_hover;
}
QComboBox QAbstractItemView::item:selected {
    background-color: $selection;
    color: $selection_text;
}

/* SpinBox (botones de incremento/decremento) */
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
    height: 10px;
    border-left: 1px solid $border;
    background-color: $button;
    border-top-right-radius: 4px;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    height: 10px;
    border-left: 1px solid $border;
    background-color: $button;
    border-bottom-right-radius: 4px;
}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
    background-color: $button_hover;
}
QSpinBox::up-button:disabled, QDoubleSpinBox::up-button:disabled,
QSpinBox::down-button:disabled, QDoubleSpinBox::down-button:disabled {
    background-color: $surface;
}

/* Checkbox y radio */
QCheckBox, QRadioButton {
    color: $text;
    spacing: 6px;
}
QCheckBox:disabled, QRadioButton:disabled {
    color: $text_disabled;
}
QCheckBox::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid $border;
    background-color: $input;
    border-radius: 3px;
}
QCheckBox::indicator:hover {
    border-color: $text_disabled;
}
QCheckBox::indicator:checked {
    background-color: $primary;
    border-color: $primary;
}
QCheckBox::indicator:disabled {
    border-color: $border;
    background-color: $surface;
}
QCheckBox::indicator:checked:disabled {
    background-color: $text_disabled;
    border-color: $text_disabled;
}
QRadioButton::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid $border;
    background-color: $input;
    border-radius: 7px;
}
QRadioButton::indicator:hover {
    border-color: $text_disabled;
}
QRadioButton::indicator:checked {
    background-color: $primary;
    border-color: $primary;
}
QRadioButton::indicator:disabled {
    border-color: $border;
    background-color: $surface;
}
QRadioButton::indicator:checked:disabled {
    background-color: $text_disabled;
    border-color: $text_disabled;
}

/* Slider */
QSlider::groove:horizontal {
    height: 4px;
    background-color: $border;
    border-radius: 2px;
}
QSlider::groove:vertical {
    width: 4px;
    background-color: $border;
    border-radius: 2px;
}
QSlider::sub-page:horizontal, QSlider::add-page:vertical {
    background-color: $primary;
    border-radius: 2px;
}
QSlider::add-page:horizontal, QSlider::sub-page:vertical {
    background-color: $border;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
    background-color: $primary;
}
QSlider::handle:vertical {
    width: 14px;
    height: 14px;
    margin: 0 -5px;
    border-radius: 7px;
    background-color: $primary;
}
QSlider::handle:hover {
    background-color: $primary_hover;
}
QSlider:disabled::handle {
    background-color: $text_disabled;
}
QSlider:disabled::sub-page:horizontal, QSlider:disabled::add-page:vertical {
    background-color: $border;
}

/* Progress bar */
QProgressBar {
    background-color: $surface_alt;
    color: $text;
    border: 1px solid $border;
    border-radius: 4px;
    text-align: center;
    padding: 1px;
}
QProgressBar::chunk {
    background-color: $primary;
    border-radius: 3px;
}

/* Vistas de datos (tablas, listas, árboles) */
QTableView, QTableWidget, QListWidget, QTreeWidget, QListView, QTreeView {
    background-color: $surface;
    color: $text;
    alternate-background-color: $alt_row;
    gridline-color: $border;
    border: 1px solid $border;
    border-radius: 4px;
    selection-background-color: $selection;
    selection-color: $selection_text;
    outline: 0;
}
QTableView::item, QTableWidget::item, QListWidget::item, QTreeWidget::item,
QListView::item, QTreeView::item {
    color: $text;
}
QTableView::item:hover, QTableWidget::item:hover, QListWidget::item:hover,
QTreeWidget::item:hover, QListView::item:hover, QTreeView::item:hover {
    background-color: $surface_alt;
}
QTableView::item:selected, QTableWidget::item:selected, QListWidget::item:selected,
QTreeWidget::item:selected, QListView::item:selected, QTreeView::item:selected {
    background-color: $selection;
    color: $selection_text;
}
QTableView::item:disabled, QTableWidget::item:disabled, QListWidget::item:disabled,
QTreeWidget::item:disabled, QListView::item:disabled, QTreeView::item:disabled {
    color: $text_disabled;
}
QTableWidget::item:focus, QTableView::item:focus {
    outline: none;
}

/* Encabezados de columnas */
QHeaderView::section {
    background-color: $header;
    color: $header_text;
    border: none;
    border-right: 1px solid $border;
    border-bottom: 1px solid $border;
    padding: 6px 8px;
    font-weight: 600;
}
QHeaderView::section:hover {
    background-color: $surface_alt;
}
QTableCornerButton::section {
    background-color: $header;
    border: none;
}

/* Log / texto plano */
QPlainTextEdit, QTextEdit {
    background-color: $surface;
    color: $text;
    border: 1px solid $border;
    border-radius: 4px;
    padding: 4px 6px;
    selection-background-color: $selection;
    selection-color: $selection_text;
}
QPlainTextEdit:focus, QTextEdit:focus {
    border-color: $primary;
}
QPlainTextEdit:disabled, QTextEdit:disabled {
    color: $text_disabled;
    background-color: $background;
}

/* Scrollbars */
QScrollBar:vertical {
    background: transparent;
    width: 12px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: $scrollbar;
    border-radius: 6px;
    min-height: 24px;
    margin: 1px;
}
QScrollBar::handle:vertical:hover {
    background: $scrollbar_hover;
}
QScrollBar::handle:vertical:pressed {
    background: $text_disabled;
}
QScrollBar:horizontal {
    background: transparent;
    height: 12px;
    margin: 0;
}
QScrollBar::handle:horizontal {
    background: $scrollbar;
    border-radius: 6px;
    min-width: 24px;
    margin: 1px;
}
QScrollBar::handle:horizontal:hover {
    background: $scrollbar_hover;
}
QScrollBar::handle:horizontal:pressed {
    background: $text_disabled;
}
QScrollBar::add-line, QScrollBar::sub-line {
    background: transparent;
    border: none;
    width: 0;
    height: 0;
}
QScrollBar::add-page, QScrollBar::sub-page {
    background: transparent;
}
QScrollBar::corner {
    background: transparent;
}

/* Barra de estado */
QStatusBar {
    background-color: $surface;
    color: $text;
    border-top: 1px solid $border;
}
QStatusBar::item {
    border: none;
}
#devLabel {
    color: $text_secondary;
    font-size: 11px;
    font-weight: 600;
    padding-right: 4px;
}

/* Menús */
QMenuBar {
    background-color: $background;
    color: $text;
    border-bottom: 1px solid $border;
}
QMenuBar::item {
    background-color: transparent;
    color: $text;
    padding: 4px 10px;
    border-radius: 3px;
}
QMenuBar::item:selected {
    background-color: $menu_hover;
}
QMenuBar::item:pressed {
    background-color: $menu;
}
QMenu {
    background-color: $menu;
    color: $text;
    border: 1px solid $border;
    border-radius: 4px;
    padding: 4px;
}
QMenu::item {
    background-color: transparent;
    color: $text;
    padding: 6px 28px 6px 12px;
    border-radius: 3px;
}
QMenu::item:selected {
    background-color: $menu_hover;
}
QMenu::item:disabled {
    color: $text_disabled;
    background-color: transparent;
}
QMenu::separator {
    height: 1px;
    background-color: $menu_separator;
    margin: 4px 8px;
}
QMenu::indicator {
    width: 16px;
    height: 16px;
}

/* Tooltips */
QToolTip {
    background-color: $tooltip_bg;
    color: $text;
    border: 1px solid $tooltip_border;
    padding: 4px 8px;
}

/* GroupBox */
QGroupBox {
    background-color: transparent;
    color: $text;
    border: 1px solid $border;
    border-radius: 4px;
    margin-top: 14px;
    padding-top: 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 8px;
    padding: 0 4px;
}

/* Pestañas */
QTabWidget::pane {
    border: 1px solid $border;
    border-radius: 4px;
}
QTabBar::tab {
    background-color: $button;
    color: $text;
    border: 1px solid $border;
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    padding: 6px 14px;
}
QTabBar::tab:selected {
    background-color: $surface;
    border-bottom-color: $surface;
}
QTabBar::tab:hover:!selected {
    background-color: $button_hover;
}
QTabBar::tab:disabled {
    color: $text_disabled;
}

/* Splitter */
QSplitter::handle {
    background-color: $border;
}
QSplitter::handle:hover {
    background-color: $scrollbar_hover;
}
QSplitter::handle:vertical {
    height: 6px;
}
QSplitter::handle:horizontal {
    width: 6px;
}

/* Áreas desplazables */
QScrollArea {
    background: transparent;
    border: none;
}
"""
)


def get_stylesheet(theme: str) -> str:
    """Construye el QSS global del tema a partir de los tokens de THEMES.

    Los recursos gráficos (flechas, checks, radios) se inyectan como reglas
    adicionales; si no hay instancia de QApplication se omite el url() para
    no generar estilos inválidos.
    """
    t = THEMES[theme]
    assets = _assets_for(theme, t)
    values = dict(t)
    values.update(assets)
    base = _QSS_TEMPLATE.substitute(values)

    img_rules: list[str] = []
    if assets["arrow"]:
        img_rules.append(
            "QComboBox::down-arrow {\n"
            f"    image: url({assets['arrow']});\n"
            "    width: 10px;\n"
            "    height: 10px;\n"
            "}\n"
        )
    if assets["spin_up"]:
        img_rules.append(
            "QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {\n"
            f"    image: url({assets['spin_up']});\n"
            "    width: 10px;\n"
            "    height: 10px;\n"
            "}\n"
        )
    if assets["spin_down"]:
        img_rules.append(
            "QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {\n"
            f"    image: url({assets['spin_down']});\n"
            "    width: 10px;\n"
            "    height: 10px;\n"
            "}\n"
        )
    if assets["check"]:
        img_rules.append(
            "QCheckBox::indicator:checked {\n"
            f"    image: url({assets['check']});\n"
            "}\n"
            "QCheckBox::indicator:checked:disabled {\n"
            f"    image: url({assets['check_dim']});\n"
            "}\n"
        )
    if assets["dot"]:
        img_rules.append(
            "QRadioButton::indicator:checked {\n"
            f"    image: url({assets['dot']});\n"
            "}\n"
            "QRadioButton::indicator:checked:disabled {\n"
            f"    image: url({assets['dot_dim']});\n"
            "}\n"
        )
    return base + "\n/* Recursos gráficos por tema */\n" + "\n".join(img_rules)





# --------------------------------------------------------------------------- #
# Capa de presentación (PySide6/Qt6) — sin lógica de negocio
# --------------------------------------------------------------------------- #

class MainWindow(QMainWindow):
    """
    Capa de presentación (PySide6/Qt6) — sin lógica de negocio.

    Jerarquía de capas (de arriba a abajo, en el layout vertical central):
        Fila 0: entrada de URL + botón "Añadir a la cola" (Alt+A)
        Fila 1: carpeta destino + "Cambiar..." (Alt+B)
        Fila 2: estado de ffmpeg + "Ubicar ffmpeg.exe..." (Alt+F)
        Fila 3: QSplitter vertical con tabla de la cola (peso 3) y log (peso 1)
        Fila 4: controles principales + calidad (scroll horizontal solo si falta ancho)
        Fila 5: controles secundarios (scroll horizontal solo si falta ancho)
        Barra de estado (QStatusBar) -> siempre visible abajo

    Gestor de geometría: QVBoxLayout/QHBoxLayout/QSplitter (sin coordenadas
    absolutas). La tabla y el log reparten el espacio sobrante en proporción
    3:1; el resto de filas conserva altura natural. El piso es
    `setMinimumSize(620, 480)`. La barra de estado jamás queda tapada.

    Comunicación worker→UI: `_on_item_update` es deliberadamente un no-op;
    ningún widget se toca desde el hilo worker. El refresco real ocurre en
    `_poll_status`, disparado por un QTimer de 400ms que drena `log_queue`
    (cola thread-safe) y actualiza tabla, log, botones y barra de estado.

    Atajos: Delete (quitar), Ctrl+R/K/O/I/U + mnemonics Alt+letra (12 únicos,
    vía carácter `&` en el texto de los botones):
        Alt+A añadir   Alt+B cambiar carpeta   Alt+F ubicar ffmpeg
        Alt+G iniciar  Alt+P parar             Alt+Q quitar
        Alt+R reintentar Alt+C cancelar        Alt+E abrir carpeta
        Alt+I importar Alt+U actualizar yt-dlp Alt+T cambiar tema
    """

    def __init__(self, settings: Settings | None = None):
        super().__init__()
        app = QApplication.instance()
        if app is not None:
            app = cast(QApplication, app)
            if str(app.style().objectName()).lower() != "fusion":
                app.setStyle("Fusion")

        self.settings = settings or Settings()
        self.theme = self.settings.theme or "light"
        self._closed = False

        self.output_dir = Path(
            self.settings.output_dir or self._default_output_dir())
        self.manager = QueueManager(
            output_dir=self.output_dir,
            bitrate_kbps=self.settings.bitrate or "320",
            ffmpeg_location=self.settings.ffmpeg_location or self._detect_ffmpeg(),
            on_update=self._on_item_update,
        )

        self.setWindowTitle("Video → MP3 Downloader — by NEXUS_CALDERON")
        icon_file = _asset_path("icon.ico")
        if icon_file.exists():
            self.setWindowIcon(QIcon(str(icon_file)))
        self.setMinimumSize(620, 480)
        self._build_ui()
        self._apply_theme()
        self._restore_geometry()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(400)
        self._poll_timer.timeout.connect(self._poll_status)
        self._poll_timer.start()
        self._poll_status()

    # ---- utilidades ----------------------------------------------------- #

    @staticmethod
    def _default_output_dir() -> Path:
        """Devuelve la carpeta de descargas del sistema + 'Video2MP3'.
        Funciona en Windows, Linux y macOS."""
        downloads = Path.home() / "Downloads"
        if downloads.exists():
            return downloads / "Video2MP3"
        return Path.home() / "Video2MP3"

    def _persist(self):
        geo = self.geometry()
        save_settings(Settings(
            output_dir=str(self.output_dir),
            bitrate=self.manager.bitrate_kbps,
            ffmpeg_location=self.manager.ffmpeg_location or "",
            theme=self.theme,
            geometry=f"{geo.width()}x{geo.height()}+{geo.x()}+{geo.y()}",
        ))

    def _restore_geometry(self):
        m = re.match(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$", self.settings.geometry)
        if m:
            w = max(int(m.group(1)), self.minimumWidth())
            h = max(int(m.group(2)), self.minimumHeight())
            self.setGeometry(QRect(int(m.group(3)), int(m.group(4)), w, h))
        else:
            self.resize(720, 540)

    @staticmethod
    def _detect_ffmpeg() -> str | None:
        """Busca ffmpeg en el PATH y en carpetas comunes (Windows/Linux/macOS).
        Devuelve la carpeta que lo contiene, o None si no se encuentra."""
        found = shutil.which("ffmpeg")
        if found:
            return str(Path(found).parent)
        common_paths = [
            # Windows
            r"C:\ffmpeg\bin",
            r"C:\Program Files\ffmpeg\bin",
            r"C:\Program Files (x86)\ffmpeg\bin",
            # macOS (Homebrew)
            "/opt/homebrew/bin",
            "/usr/local/bin",
            # Linux (snap, flatpak, manual)
            "/snap/bin",
            "/usr/bin",
        ]
        binary = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        for p in common_paths:
            if (Path(p) / binary).exists():
                return p
        return None

    def _update_dir_label(self):
        self.dir_label.setText(f"Carpeta destino: {self.output_dir}")

    def _update_ffmpeg_label(self):
        if self.manager.ffmpeg_location:
            self.ffmpeg_label.setText(f"ffmpeg: {self.manager.ffmpeg_location}")
            self.ffmpeg_label.setProperty("error", False)
        else:
            self.ffmpeg_label.setText(
                "ffmpeg: NO detectado en el PATH — seleccioná la carpeta "
                "manualmente")
            self.ffmpeg_label.setProperty("error", True)
        self.ffmpeg_label.style().unpolish(self.ffmpeg_label)
        self.ffmpeg_label.style().polish(self.ffmpeg_label)

    # ---- construcción de UI ---- #

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(6)

        outer.addLayout(self._build_header_row())
        outer.addLayout(self._build_dir_row())
        outer.addLayout(self._build_ffmpeg_row())

        # Tabla (peso 3) y log (peso 1) en un splitter vertical elástico.
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setObjectName("mainSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(6)
        self.splitter.addWidget(self._build_table())
        self.splitter.addWidget(self._build_log())
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([360, 120])
        outer.addWidget(self.splitter, 1)

        outer.addWidget(self._build_controls_scroll())
        outer.addWidget(self._build_secondary_scroll())

        self._build_statusbar()
        self._bind_shortcuts()

    def _build_header_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("URL del video:"))
        self.url_entry = QLineEdit()
        self.url_entry.setPlaceholderText("https://...")
        self.url_entry.returnPressed.connect(self._add_url)
        row.addWidget(self.url_entry, 1)
        self.add_btn = QPushButton("&Añadir a la cola")
        self.add_btn.clicked.connect(self._add_url)
        row.addWidget(self.add_btn)
        return row

    def _build_dir_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        self.dir_label = QLabel()
        self.dir_label.setObjectName("dirLabel")
        self.dir_label.setWordWrap(True)
        self.dir_label.setMinimumWidth(0)
        self.dir_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._update_dir_label()
        row.addWidget(self.dir_label, 1)
        self.dir_btn = QPushButton("Cam&biar...")
        self.dir_btn.clicked.connect(self._choose_dir)
        row.addWidget(self.dir_btn)
        return row

    def _build_ffmpeg_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        self.ffmpeg_label = QLabel()
        self.ffmpeg_label.setObjectName("ffmpegLabel")
        self.ffmpeg_label.setWordWrap(True)
        self.ffmpeg_label.setMinimumWidth(0)
        self.ffmpeg_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.ffmpeg_label, 1)
        self.ffmpeg_btn = QPushButton("Ubicar &ffmpeg.exe...")
        self.ffmpeg_btn.clicked.connect(self._choose_ffmpeg)
        row.addWidget(self.ffmpeg_btn)
        return row

    def _build_table(self) -> QTableWidget:
        self.table = QTableWidget(0, 3)
        self.table.setObjectName("queueTable")
        self.table.setHorizontalHeaderLabels(["URL", "Estado", "Progreso"])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setMinimumHeight(100)
        self.table.setMinimumWidth(0)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._uid_row: dict[int, int] = {}
        return self.table

    def _build_log(self) -> QWidget:
        container = QWidget()
        box = QVBoxLayout(container)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)
        box.addWidget(QLabel("Registro:"))
        self.log_box = QPlainTextEdit()
        self.log_box.setObjectName("logBox")
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(2000)
        self.log_box.setMinimumHeight(60)
        box.addWidget(self.log_box, 1)
        return container

    def _build_controls_scroll(self) -> QScrollArea:
        content = QWidget()
        row = QHBoxLayout(content)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(3)
        self.start_btn = QPushButton("▶ Iniciar descar&ga")
        self.start_btn.setObjectName("startButton")
        self.start_btn.clicked.connect(self._start)
        row.addWidget(self.start_btn)
        self.stop_btn = QPushButton("⏹ &Parar")
        self.stop_btn.setObjectName("stopButton")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        row.addWidget(self.stop_btn)
        self.remove_btn = QPushButton("🗑 &Quitar seleccionado")
        self.remove_btn.clicked.connect(self._remove_selected)
        row.addWidget(self.remove_btn)
        row.addStretch(1)
        row.addWidget(QLabel("Calidad:"))
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.setObjectName("bitrateCombo")
        self.bitrate_combo.addItems(["128", "192", "256", "320"])
        self.bitrate_combo.setCurrentText(self.manager.bitrate_kbps)
        self.bitrate_combo.currentIndexChanged.connect(self._on_bitrate_changed)
        row.addWidget(self.bitrate_combo)
        self.controls_scroll = self._wrap_scroll(content)
        return self.controls_scroll

    def _build_secondary_scroll(self) -> QScrollArea:
        content = QWidget()
        row = QHBoxLayout(content)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(3)
        self.retry_btn = QPushButton("↻ &Reintentar")
        self.retry_btn.clicked.connect(self._retry_selected)
        row.addWidget(self.retry_btn)
        self.cancel_btn = QPushButton("✕ &Cancelar")
        self.cancel_btn.clicked.connect(self._cancel_selected)
        row.addWidget(self.cancel_btn)
        self.open_btn = QPushButton("Abrir carp&eta")
        self.open_btn.clicked.connect(self._open_output_dir)
        row.addWidget(self.open_btn)
        self.import_btn = QPushButton("&Importar URLs...")
        self.import_btn.clicked.connect(self._import_from_file)
        row.addWidget(self.import_btn)
        self.update_btn = QPushButton("Act&ualizar yt-dlp")
        self.update_btn.clicked.connect(self.manager.update_ytdlp)
        row.addWidget(self.update_btn)
        self.theme_btn = QPushButton("Cambiar &tema")
        self.theme_btn.clicked.connect(self._toggle_theme)
        row.addWidget(self.theme_btn)
        row.addStretch(1)
        self.secondary_scroll = self._wrap_scroll(content)
        return self.secondary_scroll

    @staticmethod
    def _wrap_scroll(content: QWidget) -> QScrollArea:
        """Envuelve una fila de controles en un scroll horizontal as-needed:
        nunca corta ni superpone botones cuando la ventana es angosta."""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(content)
        area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return area

    def _build_statusbar(self):
        self.status_label = QLabel("")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.statusBar().addWidget(self.status_label, 1)
        self.dev_label = QLabel("NEXUS_CALDERON")
        self.dev_label.setObjectName("devLabel")
        self.statusBar().addPermanentWidget(self.dev_label)

    def _bind_shortcuts(self):
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self, self._remove_selected)
        QShortcut(QKeySequence("Ctrl+R"), self, self._retry_selected)
        QShortcut(QKeySequence("Ctrl+K"), self, self._cancel_selected)
        QShortcut(QKeySequence("Ctrl+O"), self, self._open_output_dir)
        QShortcut(QKeySequence("Ctrl+I"), self, self._import_from_file)
        QShortcut(QKeySequence("Ctrl+U"), self, self.manager.update_ytdlp)

    # ---- tema claro/oscuro ---- #

    def _apply_theme(self):
        # QSS a nivel de aplicación: cubre también dialogs sin padre, menús,
        # popups, tooltips y cualquier widget secundario. Al cambiar de tema,
        # Qt repinta toda la jerarquía con el nuevo stylesheet.
        app = QApplication.instance()
        if app is not None:
            cast(QApplication, app).setStyleSheet(get_stylesheet(self.theme))
        else:
            self.setStyleSheet(get_stylesheet(self.theme))
        self._update_ffmpeg_label()
        self._table_refresh()

    def _toggle_theme(self):
        self.theme = "dark" if self.theme == "light" else "light"
        self._apply_theme()
        self._persist()

    # ---- acciones UI ---- #

    def _add_url(self):
        url = self.url_entry.text().strip()
        try:
            self.manager.add_url(url)
        except DuplicateURLError:
            QMessageBox.warning(self, "URL duplicada", "Esa URL ya está en la cola.")
            return
        except ValueError:
            QMessageBox.warning(self, "URL inválida", "Escribe una URL antes de añadir.")
            return
        self.url_entry.clear()
        self._table_refresh()

    def _choose_dir(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "Seleccioná carpeta de destino", str(self.output_dir))
        if chosen:
            self.output_dir = Path(chosen)
            self.manager.output_dir = self.output_dir
            self._update_dir_label()
            self._persist()

    def _choose_ffmpeg(self):
        if sys.platform == "win32":
            filter_str = "ffmpeg.exe (ffmpeg.exe);;Ejecutables (*.exe)"
        else:
            filter_str = "ffmpeg (ffmpeg);;Todos los archivos (*)"
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Seleccioná ffmpeg", "", filter_str)
        if chosen:
            folder = str(Path(chosen).parent)
            self.manager.ffmpeg_location = folder
            self._update_ffmpeg_label()
            self._persist()

    def _start(self):
        if not self.manager.items:
            QMessageBox.information(self, "Cola vacía",
                                    "Añade al menos una URL primero.")
            return
        if not self.manager.ffmpeg_location and not shutil.which("ffmpeg"):
            QMessageBox.warning(
                self,
                "ffmpeg no encontrado",
                "No se detectó ffmpeg. Usá el botón 'Ubicar ffmpeg.exe...' "
                "para indicar dónde lo instalaste, o instalalo primero.",
            )
            return
        self.manager.set_bitrate(self.bitrate_combo.currentText())
        self.manager.start()
        self.start_btn.setEnabled(False)
        self.start_btn.setText("Descargando...")
        self.stop_btn.setEnabled(True)

    def _stop(self):
        self.manager.stop()
        self.stop_btn.setEnabled(False)

    def _remove_selected(self):
        uid = self._selected_uid()
        if uid is None:
            return
        self.manager.remove_item(uid)
        row = self._uid_row.get(uid)
        if row is not None:
            self.table.removeRow(row)
            self._rebuild_row_index()

    def _retry_selected(self):
        uid = self._selected_uid()
        if uid is not None:
            self.manager.retry_item(uid)

    def _cancel_selected(self):
        uid = self._selected_uid()
        if uid is not None:
            self.manager.cancel_item(uid)

    def _open_output_dir(self):
        path = Path(self.output_dir)
        path.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                startfile = getattr(os, "startfile", None)
                if startfile is not None:
                    startfile(str(path))
                else:
                    subprocess.run(["explorer", str(path)], check=False)
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except OSError as exc:
            QMessageBox.critical(self, "No se pudo abrir la carpeta", str(exc))

    def _import_from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Seleccionar archivo con URLs (una por línea)", "",
            "Texto (*.txt);;Todos los archivos (*.*)")
        if not path:
            return
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            QMessageBox.critical(self, "Error",
                                 f"No se pudo leer el archivo: {exc}")
            return
        added = 0
        for raw in lines:
            url = raw.strip()
            if not url or url.startswith("#"):
                continue
            try:
                self.manager.add_url(url)
            except (DuplicateURLError, ValueError):
                continue
            added += 1
        self._table_refresh()
        if added == 0:
            QMessageBox.information(
                self, "Importación", "No se añadió ninguna URL del archivo.")
        else:
            QMessageBox.information(
                self, "Importación", f"Se añadieron {added} URL(s) a la cola.")

    def _on_bitrate_changed(self, index: int):
        self.manager.set_bitrate(self.bitrate_combo.itemText(index))
        self._persist()

    # ---- callbacks desde QueueManager (hilo worker) ---- #

    def _on_item_update(self, uid: int):
        # Solo se invoca desde el worker thread; nunca tocamos widgets aquí.
        # El refresco real ocurre en _poll_status (QTimer, thread-safe).
        pass

    def _poll_status(self):
        if self._closed:
            return
        self._table_refresh()
        self._drain_log()
        self._show_error_details()
        self._update_action_buttons()
        self._update_statusbar()

    # ---- refresco de widgets (siempre desde el hilo de la UI) ---- #

    def _table_refresh(self):
        valid = {it.uid: it for it in self.manager.items}
        for uid in [u for u in self._uid_row if u not in valid]:
            self.table.removeRow(self._uid_row[uid])
        self._rebuild_row_index()
        for item in self.manager.items:
            row = self._uid_row.get(item.uid)
            if row is None:
                row = self.table.rowCount()
                self.table.insertRow(row)
                self._uid_row[item.uid] = row
            self._set_table_row(item, row)

    def _rebuild_row_index(self):
        self._uid_row = {}
        for row in range(self.table.rowCount()):
            cell = self.table.item(row, 0)
            if cell is not None:
                self._uid_row[int(cell.data(Qt.ItemDataRole.UserRole))] = row

    def _set_table_row(self, item: DownloadItem, row: int):
        values = (item.url, item.status, self._progress_text(item))
        is_error = item.status == STATUS_ERROR
        fg, bg = self._error_colors() if is_error else (None, None)
        for col, text in enumerate(values):
            cell = self.table.item(row, col)
            if cell is None:
                cell = QTableWidgetItem()
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, col, cell)
            cell.setText(text)
            if col == 0:
                cell.setData(Qt.ItemDataRole.UserRole, item.uid)
            cell.setForeground(QBrush(QColor(fg)) if fg else QBrush())
            cell.setBackground(QBrush(QColor(bg)) if bg else QBrush())

    @staticmethod
    def _progress_text(item: DownloadItem) -> str:
        if item.status == STATUS_DOWNLOADING and (item.speed_str or item.eta_str):
            return (
                f"{item.progress:.0f}%  {item.speed_str}  {item.eta_str}"
            ).strip()
        return f"{item.progress:.0f}%"

    def _error_colors(self) -> tuple[str, str]:
        """Colores de texto/fondo para filas en error (desde THEMES)."""
        t = THEMES[self.theme]
        return t["error_text"], t["error_bg"]

    def _selected_uid(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        cell = self.table.item(rows[0].row(), 0)
        if cell is None:
            return None
        return int(cell.data(Qt.ItemDataRole.UserRole))

    def _drain_log(self):
        while True:
            try:
                msg = self.manager.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_box.appendPlainText(msg)
        bar = self.log_box.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _show_error_details(self):
        for item in self.manager.items:
            if item.status == STATUS_ERROR and item.error_msg and not item.shown:
                self.log_box.appendPlainText(f"    detalle: {item.error_msg}")
                item.shown = True

    def _update_action_buttons(self):
        if not self.manager.is_running():
            if not self.start_btn.isEnabled():
                self.start_btn.setEnabled(True)
                self.start_btn.setText("▶ Iniciar descarga")
            if self.stop_btn.isEnabled():
                self.stop_btn.setEnabled(False)

    def _update_statusbar(self):
        if self.manager.is_running():
            self.status_label.setText("Descargando...")
            return
        queued = sum(1 for i in self.manager.items if i.status == STATUS_QUEUED)
        finished = sum(
            1 for i in self.manager.items
            if i.status in (STATUS_COMPLETED, STATUS_ERROR, STATUS_CANCELLED)
        )
        self.status_label.setText(
            f"{len(self.manager.items)} ítems · {queued} en cola · "
            f"{finished} finalizados"
        )

    # ---- cierre ---- #

    def closeEvent(self, event):
        if self.manager.is_running():
            answer = QMessageBox.question(
                self,
                "Descarga en curso",
                "Hay descargas activas. ¿Cerrar la aplicación de todos modos?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.manager.stop()  # frena el worker de forma cooperativa
        self._closed = True
        self._poll_timer.stop()
        self._persist()
        event.accept()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main():
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "video2mp3.downloader.desktop.1.0"
            )
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName("Video → MP3 Downloader")

    icon_file = _asset_path("icon.ico")
    if icon_file.exists():
        app.setWindowIcon(QIcon(str(icon_file)))

    window = MainWindow(settings=load_settings())
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
