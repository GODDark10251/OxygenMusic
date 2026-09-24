import os
import sys
import re
import glob
import math
import signal
import sqlite3
import subprocess
import warnings
from pathlib import Path
from io import BytesIO

# Suppress stderr to keep terminal clean
try:
    null_fd = os.open(os.devnull, os.O_RDWR)
    os.dup2(null_fd, 2)
except Exception:
    pass

warnings.filterwarnings("ignore")

import numpy as np
from scipy.io import wavfile

from PyQt6.QtCore import (
    Qt, QUrl, QTimer, QThread, pyqtSignal, QRectF, QPointF, QPropertyAnimation, QEasingCurve
)
from PyQt6.QtGui import (
    QColor, QPainter, QBrush, QPen, QLinearGradient, QRadialGradient, 
    QFont, QFontMetrics, QPainterPath, QIcon
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSlider, QListWidget, QListWidgetItem,
    QLineEdit, QFileDialog, QSplitter, QMessageBox, QFrame,
    QStackedWidget, QAbstractButton, QSystemTrayIcon, QMenu, QDialog
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput

# MPRIS D-Bus Linux Desktop Integration
try:
    from PyQt6.QtDBus import QDBusConnection, QDBusAbstractAdaptor, pyqtSlot
    HAS_DBUS = True
except ImportError:
    HAS_DBUS = False

# -------------------------------------------------------------------------
# STORAGE & DATABASE INITIALIZATION
# -------------------------------------------------------------------------
APP_DIR = Path.home() / ".local" / "share" / "oxygenmusic"
APP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = APP_DIR / "library.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            filepath TEXT UNIQUE
        )
    """)
    conn.commit()
    conn.close()

init_db()

# -------------------------------------------------------------------------
# EXACT iOS 27 SPATIAL GLASS PAINTER
# -------------------------------------------------------------------------
def draw_ios27_glass(painter: QPainter, rect: QRectF, radius: float = 24.0, hover_progress: float = 0.0):
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    
    grad_bg = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    r_boost = int(hover_progress * 18)
    grad_bg.setColorAt(0.0, QColor(26 + r_boost, 32, 48 + r_boost, 185))
    grad_bg.setColorAt(0.5, QColor(16, 20, 30, 210))
    grad_bg.setColorAt(1.0, QColor(8, 11, 18, 235))
    
    painter.setBrush(QBrush(grad_bg))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(rect, radius, radius)

    specular_rect = QRectF(rect.x() + 1, rect.y() + 1, rect.width() - 2, rect.height() * 0.45)
    spec_grad = QLinearGradient(specular_rect.topLeft(), specular_rect.bottomLeft())
    spec_grad.setColorAt(0.0, QColor(255, 255, 255, int(45 + hover_progress * 25)))
    spec_grad.setColorAt(1.0, QColor(255, 255, 255, 2))
    
    painter.setBrush(QBrush(spec_grad))
    painter.drawRoundedRect(specular_rect, radius, radius)

    border_alpha = int(55 + hover_progress * 40)
    grad_border = QLinearGradient(rect.topLeft(), rect.bottomRight())
    grad_border.setColorAt(0.0, QColor(255, 255, 255, border_alpha + 50))
    grad_border.setColorAt(0.5, QColor(255, 255, 255, border_alpha))
    grad_border.setColorAt(1.0, QColor(255, 255, 255, 15))
    
    painter.setPen(QPen(QBrush(grad_border), 1.25))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect.adjusted(0.6, 0.6, -0.6, -0.6), radius, radius)
    painter.restore()

# -------------------------------------------------------------------------
# WORKERS (Search, Download, and New Online Stream Worker)
# -------------------------------------------------------------------------
class SearchWorker(QThread):
    results_signal = pyqtSignal(bool, list, str)

    def __init__(self, query: str):
        super().__init__()
        self.query = query

    def run(self):
        try:
            import yt_dlp
            search_query = f"ytsearch8:{self.query}"
            ydl_opts = {'quiet': True, 'extract_flat': True, 'skip_download': True, 'no_warnings': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_query, download=False)
                entries = info.get('entries', []) or []
                results = []
                for item in entries:
                    if not item:
                        continue
                    title = item.get('title', 'Unknown Title')
                    url = item.get('url') or item.get('webpage_url')
                    if not url and item.get('id'):
                        url = f"https://www.youtube.com/watch?v={item.get('id')}"
                    dur = item.get('duration')
                    dur_str = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else "--:--"
                    results.append({'title': title, 'url': url, 'duration': dur_str})
                self.results_signal.emit(True, results, "")
        except Exception as e:
            self.results_signal.emit(False, [], str(e))

class StreamWorker(QThread):
    stream_ready = pyqtSignal(bool, str, str, str)  # success, stream_url, title, error

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            import yt_dlp
            ydl_opts = {'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
                stream_url = info.get('url')
                title = info.get('title', 'Online Stream')
                if stream_url:
                    self.stream_ready.emit(True, stream_url, title, "")
                else:
                    self.stream_ready.emit(False, "", "", "Could not extract direct stream URL.")
        except Exception as e:
            self.stream_ready.emit(False, "", "", str(e))

class DownloadWorker(QThread):
    finished_signal = pyqtSignal(bool, str, str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        import yt_dlp
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))

        local_ffmpeg = os.path.join(base_dir, "ffmpeg.exe")
        ffmpeg_loc = local_ffmpeg if os.path.exists(local_ffmpeg) else None

        output_template = str(APP_DIR / "%(title)s.%(ext)s")
        try:
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': output_template,
                'quiet': True,
                'no_warnings': True,
                'ffmpeg_location': ffmpeg_loc,
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=True)
                filename = ydl.prepare_filename(info)
                audio_path = os.path.splitext(filename)[0] + ".mp3"
                if not os.path.exists(audio_path):
                    audio_path = filename
                title = info.get('title', 'Unknown Title')
                self.finished_signal.emit(True, audio_path, title)
        except Exception as e:
            self.finished_signal.emit(False, str(e), "")

# -------------------------------------------------------------------------
# AUDIO EQUALIZER PREAMP DIALOG
# -------------------------------------------------------------------------
class EqualizerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Audio Equalizer & Preamp")
        self.resize(380, 300)
        self.setStyleSheet("""
            QDialog { background-color: #06080d; color: #f0f4ff; font-family: 'sans-serif'; }
            QLabel { color: #b8c7ff; font-size: 9pt; font-weight: bold; }
            QSlider::groove:vertical { width: 6px; background: rgba(255,255,255,0.08); border-radius: 3px; }
            QSlider::sub-page:vertical { background: #5865f2; border-radius: 3px; }
            QSlider::add-page:vertical { background: rgba(255,255,255,0.03); border-radius: 3px; }
            QSlider::handle:vertical { background: #ffffff; height: 16px; margin: 0 -5px; border-radius: 8px; }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)

        bands = ["Preamp", "60 Hz", "250 Hz", "1 kHz", "4 kHz", "16 kHz"]
        for band in bands:
            col = QVBoxLayout()
            col.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.setSpacing(10)

            lbl_val = QLabel("0 dB")
            slider = QSlider(Qt.Orientation.Vertical)
            slider.setRange(-12, 12)
            slider.setValue(0)
            slider.setFixedWidth(28)
            lbl_name = QLabel(band)

            slider.valueChanged.connect(lambda val, l=lbl_val: l.setText(f"{val:+d} dB"))

            col.addWidget(lbl_val)
            col.addWidget(slider, 1, Qt.AlignmentFlag.AlignCenter)
            col.addWidget(lbl_name)
            layout.addLayout(col)

# -------------------------------------------------------------------------
# PURE VISUALIZER STAGE
# -------------------------------------------------------------------------
class PureVisualizerStage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.num_bars = 96
        self.current_heights = np.zeros(self.num_bars, dtype=np.float32)
        self.target_heights = np.zeros(self.num_bars, dtype=np.float32)
        self.smooth_targets = np.zeros(self.num_bars, dtype=np.float32)
        self.spectrum_data = None
        self.spatial_phase = 0.0

        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.physics_tick)
        self.anim_timer.start(8)

    def set_spectrum(self, spectrum):
        self.spectrum_data = spectrum
        if spectrum is None:
            self.target_heights.fill(0.0)

    def sync_to_timestamp(self, pos_ms: int):
        if self.spectrum_data is not None:
            chunk_idx = int(pos_ms / 40)
            if chunk_idx < len(self.spectrum_data):
                raw = self.spectrum_data[chunk_idx]
                xp = np.linspace(0, 1, len(raw))
                x = np.linspace(0, 1, self.num_bars)
                interpolated = np.interp(x, xp, raw)
                smoothed = np.copy(interpolated)
                kernel = np.array([0.22, 0.56, 0.22])
                smoothed[1:-1] = np.convolve(interpolated, kernel, mode='valid')
                self.target_heights = np.maximum(smoothed, 0.035)
            else:
                self.target_heights *= 0.88
        else:
            self.target_heights = np.maximum(self.target_heights * 0.88, 0.02)

    def physics_tick(self):
        self.spatial_phase += 0.018
        self.smooth_targets += (self.target_heights - self.smooth_targets) * 0.22
        for i in range(self.num_bars):
            target = self.smooth_targets[i]
            curr = self.current_heights[i]
            if target > curr:
                self.current_heights[i] += (target - curr) * 0.32
            else:
                self.current_heights[i] -= (curr - target) * 0.095
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        painter.fillRect(self.rect(), QColor("#030406"))

        avg_energy = float(np.mean(self.current_heights))
        aura_alpha = int(60 + avg_energy * 140)
        aura = QRadialGradient(w * 0.5 + math.sin(self.spatial_phase) * 90, h * 0.45, w * 0.65)
        aura.setColorAt(0.0, QColor(95, 125, 255, aura_alpha))
        aura.setColorAt(1.0, QColor(3, 4, 6, 0))
        painter.fillRect(self.rect(), aura)

        glass_margin = 16.0
        stage_rect = QRectF(glass_margin, glass_margin, w - glass_margin * 2, h - glass_margin * 2)
        draw_ios27_glass(painter, stage_rect, radius=26.0, hover_progress=0.0)

        baseline_y = h - glass_margin - 40
        inner_w = w - (glass_margin * 2) - 40
        start_x = glass_margin + 20
        spacing = 2.4
        bar_w = max(2.0, (inner_w - ((self.num_bars - 1) * spacing)) / float(self.num_bars))
        max_vis_height = h * 0.62

        for i in range(self.num_bars):
            val = float(self.current_heights[i])
            up_h = max(6.0, val * max_vis_height)
            x = start_x + i * (bar_w + spacing)
            y_up = baseline_y - up_h

            grad_up = QLinearGradient(x, baseline_y, x, y_up)
            grad_up.setColorAt(0.0, QColor(80, 115, 235, 190))
            grad_up.setColorAt(0.6, QColor(140, 190, 255, 240))
            grad_up.setColorAt(1.0, QColor(255, 255, 255, 255))

            painter.setBrush(QBrush(grad_up))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(x, y_up, bar_w, up_h), 4.0, 4.0)

# -------------------------------------------------------------------------
# PROGRESS BAR & CONTROLS
# -------------------------------------------------------------------------
class Fluid120HzProgressBar(QWidget):
    position_seek = pyqtSignal(int)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(22)
        self.setMouseTracking(True)
        self.duration_ms, self.target_pos_ms, self.render_pos_ms = 1, 0.0, 0.0
        self.is_hovered, self.is_dragging = False, False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.frame_tick)
        self.timer.start(8)

    def set_duration(self, dur_ms: int):
        self.duration_ms = max(1, dur_ms)
        self.update()

    def sync_hardware_pos(self, pos_ms: int):
        if not self.is_dragging:
            self.target_pos_ms = float(pos_ms)

    def frame_tick(self):
        if not self.is_dragging:
            diff = self.target_pos_ms - self.render_pos_ms
            self.render_pos_ms += diff * (0.18 if abs(diff) > 0.5 else 1.0)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        track_h = 5.0 if not self.is_hovered and not self.is_dragging else 8.0
        track_y = (h - track_h) / 2.0
        progress_w = max(0.0, min(1.0, self.render_pos_ms / float(self.duration_ms))) * w

        painter.setBrush(QColor(20, 25, 38, 220))
        painter.setPen(QPen(QColor(255, 255, 255, 35), 1.0))
        painter.drawRoundedRect(QRectF(0, track_y, w, track_h), track_h / 2.0, track_h / 2.0)

        if progress_w > 0:
            grad = QLinearGradient(0, 0, progress_w, 0)
            grad.setColorAt(0.0, QColor("#5865f2"))
            grad.setColorAt(0.7, QColor("#8fa4ff"))
            grad.setColorAt(1.0, QColor("#ffffff"))
            painter.setBrush(grad)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(0, track_y, progress_w, track_h), track_h / 2.0, track_h / 2.0)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_dragging = True
            self.seek(event.position().x())

    def mouseMoveEvent(self, event):
        if self.is_dragging:
            self.seek(event.position().x())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_dragging = False
            self.seek(event.position().x())

    def seek(self, x):
        pos = int(max(0.0, min(1.0, x / float(self.width()))) * self.duration_ms)
        self.target_pos_ms, self.render_pos_ms = float(pos), float(pos)
        self.position_seek.emit(pos)
        self.update()

class AnimatedPlayPauseButton(QAbstractButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(54, 54)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.is_playing, self.morph_ratio, self.hover_scale = False, 0.0, 1.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.animate_tick)
        self.timer.start(8)

    def set_playing(self, p: bool):
        self.is_playing = p

    def animate_tick(self):
        self.morph_ratio += ((1.0 if self.is_playing else 0.0) - self.morph_ratio) * 0.25
        self.hover_scale += ((1.12 if self.underMouse() else 1.00) - self.hover_scale) * 0.28
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        center = QPointF(w / 2.0, h / 2.0)
        radius = 23.0 * self.hover_scale

        grad = QLinearGradient(0, 0, w, h)
        grad.setColorAt(0.0, QColor(95, 125, 255, 245))
        grad.setColorAt(1.0, QColor(50, 65, 210, 250))
        painter.setBrush(grad)
        painter.setPen(QPen(QColor(255, 255, 255, 130), 1.5))
        painter.drawEllipse(center, radius, radius)

        m = self.morph_ratio
        painter.setBrush(QColor("#ffffff"))
        p1_x = center.x() - (7.0 - 2.0 * m)
        p1_w, p1_h = 4.0 + (1.0 * (1.0 - m)), 16.0 - (4.0 * (1.0 - m))
        painter.drawRoundedRect(QRectF(p1_x, center.y() - p1_h / 2.0, p1_w, p1_h), 2.0, 2.0)
        if m > 0.05:
            painter.setOpacity(float(m))
            painter.drawRoundedRect(QRectF(center.x() + (3.0 * m), center.y() - p1_h / 2.0, p1_w, p1_h), 2.0, 2.0)
            painter.setOpacity(1.0)
        if m < 0.95:
            painter.setOpacity(float(1.0 - m))
            play_tip = QPainterPath()
            play_tip.moveTo(center.x() - 4.0, center.y() - 9.0)
            play_tip.lineTo(center.x() + 9.5, center.y())
            play_tip.lineTo(center.x() - 4.0, center.y() + 9.0)
            painter.drawPath(play_tip)
            painter.setOpacity(1.0)

class AnimatedVolumeIcon(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(28, 28)
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.update())
        self.timer.start(16)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cy = self.height() / 2.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(210, 230, 255, 250))
        spk = QPainterPath()
        spk.moveTo(4, cy - 3)
        spk.lineTo(8, cy - 3)
        spk.lineTo(13, cy - 8)
        spk.lineTo(13, cy + 8)
        spk.lineTo(8, cy + 3)
        spk.lineTo(4, cy + 3)
        painter.drawPath(spk)

class FrostedGlassFrame(QFrame):
    def __init__(self, radius=24.0, parent=None):
        super().__init__(parent)
        self.radius = radius
    def paintEvent(self, event):
        draw_ios27_glass(QPainter(self), QRectF(self.rect()), radius=self.radius, hover_progress=0.0)

# -------------------------------------------------------------------------
# OXYGENMUSIC MAIN WINDOW
# -------------------------------------------------------------------------
class OxygenMusic(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OxygenMusic - iOS 27 Edition")
        self.resize(1260, 840)
        self.setMinimumSize(980, 660)

        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.85)

        self.current_filepath = ""
        self.current_title = "No track loaded"
        self.audio_worker = None
        self.search_worker = None
        self.download_worker = None
        self.stream_worker = None

        self.init_ui()
        self.init_tray()
        self.connect_signals()
        self.load_library_from_db()

    def init_ui(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #030406; }
            QWidget#rootCanvas { background-color: #030406; }
            QWidget { color: #f0f4ff; font-family: 'sans-serif'; }
            QLineEdit { background: rgba(255, 255, 255, 0.06); border: 1px solid rgba(255, 255, 255, 0.16); border-radius: 12px; padding: 10px 15px; color: #ffffff; }
            QLineEdit:focus { border: 1px solid rgba(138, 172, 255, 0.8); background: rgba(255, 255, 255, 0.09); }
            QPushButton { background: rgba(255, 255, 255, 0.07); border: 1px solid rgba(255, 255, 255, 0.16); border-radius: 12px; padding: 9px 18px; font-weight: bold; }
            QPushButton:hover { background: rgba(255, 255, 255, 0.14); border: 1px solid rgba(255, 255, 255, 0.35); }
            QPushButton#primaryBtn { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 rgba(88,101,242,0.95), stop:1 rgba(45,55,175,0.99)); color: #fff; border: 1px solid rgba(255, 255, 255, 0.35); }
            QPushButton#streamBtn { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 rgba(52,199,89,0.95), stop:1 rgba(38,140,65,0.99)); color: #fff; border: 1px solid rgba(255, 255, 255, 0.35); }
            QListWidget { background: rgba(10, 14, 22, 0.65); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 14px; font-size: 10pt; }
            QListWidget::item { padding: 11px 14px; border-radius: 10px; margin-bottom: 5px; }
            QListWidget::item:selected { background: rgba(110, 140, 255, 0.32); color: #d6e2ff; border: 1px solid rgba(255, 255, 255, 0.25); }
        """)

        central = QWidget()
        central.setObjectName("rootCanvas")
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(14, 14, 14, 14)
        root_layout.setSpacing(14)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        sidebar = FrostedGlassFrame(radius=24.0)
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(18, 22, 18, 18)
        sb_layout.setSpacing(14)

        logo = QLabel("OxygenMusic")
        logo.setStyleSheet("font-size: 19pt; font-weight: 900; color: #8fa4ff; letter-spacing: 0.8px;")
        sb_layout.addWidget(logo)

        lib_title = QLabel("Offline Library")
        lib_title.setStyleSheet("font-size: 10pt; font-weight: bold; color: rgba(175, 195, 235, 0.7);")
        sb_layout.addWidget(lib_title)

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter stored tracks...")
        self.filter_input.textChanged.connect(self.filter_library)
        sb_layout.addWidget(self.filter_input)

        self.track_list = QListWidget()
        sb_layout.addWidget(self.track_list, 1)

        self.btn_import = QPushButton("Import Local Audio")
        self.btn_import.clicked.connect(self.import_files)
        sb_layout.addWidget(self.btn_import)
        
        self.btn_export = QPushButton("Export Playlist (.m3u)")
        self.btn_export.clicked.connect(self.export_playlist)
        sb_layout.addWidget(self.btn_export)

        self.btn_eq = QPushButton("🎛 Audio Equalizer")
        self.btn_eq.clicked.connect(lambda: EqualizerDialog(self).exec())
        sb_layout.addWidget(self.btn_eq)

        sidebar.setMinimumWidth(320)
        splitter.addWidget(sidebar)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(14)

        tab_header = QHBoxLayout()
        self.tab_buttons = []
        for idx, name in enumerate(["Visualizer Stage", "Online Search & Stream"]):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, i=idx: self.switch_tab_spring(i))
            tab_header.addWidget(btn)
            self.tab_buttons.append(btn)
        self.tab_buttons[0].setChecked(True)
        tab_header.addStretch()
        center_layout.addLayout(tab_header)

        self.stack = QStackedWidget()
        self.stage = PureVisualizerStage()
        self.stack.addWidget(self.stage)

        search_tab = FrostedGlassFrame(radius=24.0)
        st_layout = QVBoxLayout(search_tab)
        st_layout.setContentsMargins(24, 24, 24, 24)
        
        search_bar = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search online songs to stream or download...")
        self.search_input.returnPressed.connect(self.start_online_search)
        self.btn_search = QPushButton("Search")
        self.btn_search.clicked.connect(self.start_online_search)
        search_bar.addWidget(self.search_input, 1)
        search_bar.addWidget(self.btn_search)
        st_layout.addLayout(search_bar)

        self.search_status = QLabel("Search online music to stream instantly or download.")
        st_layout.addWidget(self.search_status)
        
        self.search_results_list = QListWidget()
        st_layout.addWidget(self.search_results_list, 1)

        btn_layout = QHBoxLayout()
        self.btn_stream_selected = QPushButton("▶ Stream Online Now")
        self.btn_stream_selected.setObjectName("streamBtn")
        self.btn_stream_selected.clicked.connect(self.stream_selected_search_result)
        
        self.btn_dl_selected = QPushButton("⬇ Download to Library")
        self.btn_dl_selected.setObjectName("primaryBtn")
        self.btn_dl_selected.clicked.connect(self.download_selected_search_result)
        
        btn_layout.addWidget(self.btn_stream_selected)
        btn_layout.addWidget(self.btn_dl_selected)
        st_layout.addLayout(btn_layout)

        self.stack.addWidget(search_tab)
        center_layout.addWidget(self.stack, 1)
        splitter.addWidget(center)
        splitter.setStretchFactor(1, 4)
        root_layout.addWidget(splitter, 1)

        playbar = FrostedGlassFrame(radius=24.0)
        pb_layout = QVBoxLayout(playbar)
        pb_layout.setContentsMargins(26, 14, 26, 16)
        pb_layout.setSpacing(10)

        prog_layout = QHBoxLayout()
        self.lbl_time_curr, self.lbl_time_total = QLabel("0:00"), QLabel("0:00")
        self.slider_progress = Fluid120HzProgressBar()
        prog_layout.addWidget(self.lbl_time_curr)
        prog_layout.addWidget(self.slider_progress, 1)
        prog_layout.addWidget(self.lbl_time_total)
        pb_layout.addLayout(prog_layout)

        ctrl_layout = QHBoxLayout()
        self.track_info_label = QLabel("No track loaded")
        self.track_info_label.setFixedWidth(300)
        ctrl_layout.addWidget(self.track_info_label)
        ctrl_layout.addStretch(1)

        self.btn_prev, self.btn_play, self.btn_next = QPushButton("⏮"), AnimatedPlayPauseButton(), QPushButton("⏭")
        self.btn_prev.clicked.connect(self.play_previous)
        self.btn_play.clicked.connect(self.toggle_playback)
        self.btn_next.clicked.connect(self.play_next)
        ctrl_layout.addWidget(self.btn_prev)
        ctrl_layout.addWidget(self.btn_play)
        ctrl_layout.addWidget(self.btn_next)
        ctrl_layout.addStretch(1)

        self.vol_icon, self.vol_slider = AnimatedVolumeIcon(), QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(85)
        self.vol_slider.setFixedWidth(95)
        self.vol_slider.valueChanged.connect(lambda v: self.audio_output.setVolume(v / 100.0))
        ctrl_layout.addWidget(self.vol_icon)
        ctrl_layout.addWidget(self.vol_slider)
        pb_layout.addLayout(ctrl_layout)
        root_layout.addWidget(playbar)

    def switch_tab_spring(self, index: int):
        for i, btn in enumerate(self.tab_buttons):
            btn.setChecked(i == index)
        anim = QPropertyAnimation(self.stack, b"windowOpacity")
        anim.setDuration(300)
        anim.setStartValue(0.3)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutBack)
        anim.start()
        self.stack.setCurrentIndex(index)

    def init_tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu()
        menu.addAction("Show / Hide", lambda: self.setVisible(not self.isVisible()))
        menu.addAction("Quit", QApplication.instance().quit)
        self.tray.setContextMenu(menu)
        self.tray.show()

    def set_display_title(self, text: str):
        self.current_title = text
        self.track_info_label.setText(QFontMetrics(self.track_info_label.font()).elidedText(text, Qt.TextElideMode.ElideRight, self.track_info_label.width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.set_display_title(self.current_title)

    def connect_signals(self):
        self.player.positionChanged.connect(lambda pos: (self.lbl_time_curr.setText(self.fmt(pos)), self.slider_progress.sync_hardware_pos(pos), self.stage.sync_to_timestamp(pos)))
        self.player.durationChanged.connect(lambda dur: (self.slider_progress.set_duration(dur), self.lbl_time_total.setText(self.fmt(dur))))
        self.player.playbackStateChanged.connect(lambda st: self.btn_play.set_playing(st == QMediaPlayer.PlaybackState.PlayingState))
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)
        self.slider_progress.position_seek.connect(self.player.setPosition)
        self.track_list.itemDoubleClicked.connect(lambda item: self.play_track_at_row(self.track_list.row(item)))
        self.search_results_list.itemDoubleClicked.connect(lambda item: self.stream_selected_search_result())

    def on_media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.play_next()

    def fmt(self, ms):
        s = ms // 1000
        m, s = divmod(s, 60)
        return f"{m}:{s:02d}"

    def play_track_at_row(self, row: int):
        if 0 <= row < self.track_list.count():
            self.track_list.setCurrentRow(row)
            item = self.track_list.item(row)
            path = item.data(Qt.ItemDataRole.UserRole)
            title = item.data(Qt.ItemDataRole.ToolTipRole) or item.text().replace("♫  ", "")
            if os.path.exists(path):
                self.current_filepath = path
                self.player.setSource(QUrl.fromLocalFile(path))
                self.player.play()
                self.set_display_title(title)

                if self.audio_worker and self.audio_worker.isRunning():
                    self.audio_worker.stop()
                    self.audio_worker.wait(50)
                # Note: real-time FFT analyzer works best on local files

    def toggle_playback(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if not self.player.source().isEmpty():
                self.player.play()
            elif self.track_list.count() > 0:
                self.play_track_at_row(0)

    def play_next(self):
        c = self.track_list.currentRow()
        if c + 1 < self.track_list.count():
            self.play_track_at_row(c + 1)
        elif self.track_list.count() > 0:
            self.play_track_at_row(0)

    def play_previous(self):
        c = self.track_list.currentRow()
        if c - 1 >= 0:
            self.play_track_at_row(c - 1)

    def start_online_search(self):
        q = self.search_input.text().strip()
        if not q:
            return
        self.search_results_list.clear()
        self.search_status.setText("Searching online...")
        self.search_worker = SearchWorker(q)
        self.search_worker.results_signal.connect(self.handle_search_results)
        self.search_worker.start()

    def handle_search_results(self, success, results, err):
        if not success:
            self.search_status.setText(f"Search failed: {err}")
            return
        self.search_status.setText(f"Found {len(results)} tracks. Double-click to stream.")
        for r in results:
            dur = r.get('duration', '--:--')
            title = r.get('title', 'Unknown Title')
            item = QListWidgetItem(f"[{dur}] {title}")
            item.setData(Qt.ItemDataRole.UserRole, r['url'])
            item.setData(Qt.ItemDataRole.ToolTipRole, title)
            self.search_results_list.addItem(item)

    def stream_selected_search_result(self):
        item = self.search_results_list.currentItem()
        if not item:
            QMessageBox.warning(self, "Selection Error", "Please select a track to stream first!")
            return
        url = item.data(Qt.ItemDataRole.UserRole)
        title = item.data(Qt.ItemDataRole.ToolTipRole) or item.text()
        
        self.btn_stream_selected.setEnabled(False)
        self.btn_stream_selected.setText("Resolving Stream...")
        self.search_status.setText(f"Extracting live stream for: {title}...")

        self.stream_worker = StreamWorker(url)
        self.stream_worker.stream_ready.connect(lambda success, stream_url, t, err: self.handle_stream_ready(success, stream_url, title, err))
        self.stream_worker.start()

    def handle_stream_ready(self, success, stream_url, title, err):
        self.btn_stream_selected.setEnabled(True)
        self.btn_stream_selected.setText("▶ Stream Online Now")
        if success:
            self.player.setSource(QUrl(stream_url))
            self.player.play()
            self.set_display_title(f"⚡ Streaming: {title}")
            self.search_status.setText(f"Now streaming: {title}")
        else:
            QMessageBox.critical(self, "Streaming Failed", f"Could not stream track:\n{err}")
            self.search_status.setText("Streaming failed.")

    def download_selected_search_result(self):
        item = self.search_results_list.currentItem()
        if not item:
            QMessageBox.warning(self, "Selection Error", "Please select a track to download first!")
            return
        url = item.data(Qt.ItemDataRole.UserRole)
        self.btn_dl_selected.setEnabled(False)
        self.btn_dl_selected.setText("Downloading...")
        
        self.download_worker = DownloadWorker(url)
        self.download_worker.finished_signal.connect(self.handle_download_finished)
        self.download_worker.start()

    def handle_download_finished(self, success, path_or_err, title):
        self.btn_dl_selected.setEnabled(True)
        self.btn_dl_selected.setText("⬇ Download to Library")
        if success:
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.cursor().execute("INSERT OR REPLACE INTO songs (title, filepath) VALUES (?, ?)", (title, path_or_err))
                conn.commit()
                conn.close()
                self.load_library_from_db()
                QMessageBox.information(self, "Success", f"Successfully downloaded and added to library:\n{title}")
            except Exception as e:
                QMessageBox.critical(self, "Database Error", f"Failed to save to library: {e}")
        else:
            QMessageBox.critical(self, "Download Failed", f"Error:\n{path_or_err}")

    def load_library_from_db(self):
        self.track_list.clear()
        conn = sqlite3.connect(DB_PATH)
        for title, path in conn.cursor().execute("SELECT title, filepath FROM songs ORDER BY id DESC").fetchall():
            item = QListWidgetItem(f"♫  {title}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setData(Qt.ItemDataRole.ToolTipRole, title)
            self.track_list.addItem(item)
        conn.close()

    def filter_library(self, text: str):
        for i in range(self.track_list.count()):
            item = self.track_list.item(i)
            item.setHidden(text.lower() not in (item.data(Qt.ItemDataRole.ToolTipRole) or item.text()).lower())

    def import_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Import Audio", str(Path.home()), "Audio (*.mp3 *.wav *.flac *.ogg *.m4a)")
        if files:
            conn = sqlite3.connect(DB_PATH)
            for f in files:
                try:
                    conn.cursor().execute("INSERT INTO songs (title, filepath) VALUES (?, ?)", (Path(f).stem, f))
                except sqlite3.IntegrityError:
                    pass
            conn.commit()
            conn.close()
            self.load_library_from_db()

    def export_playlist(self):
        file_path, _ = QFileDialog.getSaveFileName(self, "Export Playlist", str(Path.home() / "oxygen_playlist.m3u"), "Playlist (*.m3u)")
        if not file_path:
            return
        try:
            conn = sqlite3.connect(DB_PATH)
            rows = conn.cursor().execute("SELECT title, filepath FROM songs ORDER BY id DESC").fetchall()
            conn.close()
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write("#EXTM3U\n")
                for title, filepath in rows:
                    if os.path.exists(filepath):
                        f.write(f"#EXTINF:-1,{title}\n{filepath}\n")
            QMessageBox.information(self, "Success", f"Playlist exported to:\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to export playlist:\n{e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = OxygenMusic()
    window.show()
    sys.exit(app.exec())
