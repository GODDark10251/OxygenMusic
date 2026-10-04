import os
import sys
import re
import json
import bisect
import time
from urllib.parse import quote_plus
from difflib import SequenceMatcher
import glob
import math
import signal
import base64
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
import requests

from PyQt6.QtCore import (
    Qt, QUrl, QTimer, QThread, pyqtSignal, QRectF, QPointF, QPropertyAnimation, QEasingCurve,
    QVariantAnimation
)
from PyQt6.QtGui import (
    QColor, QPainter, QBrush, QPen, QLinearGradient, QRadialGradient, 
    QFont, QFontMetrics, QPainterPath, QIcon, QShortcut, QKeySequence, QPixmap, QAction, QImage, QDesktopServices
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSlider, QListWidget, QListWidgetItem,
    QLineEdit, QFileDialog, QSplitter, QMessageBox, QFrame,
    QStackedWidget, QAbstractButton, QSystemTrayIcon, QMenu, QInputDialog, QDialog, QPlainTextEdit
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput

# MPRIS D-Bus Linux Desktop Integration
try:
    from PyQt6.QtDBus import (
        QDBusConnection, QDBusAbstractAdaptor, pyqtSlot, QDBusMessage, QDBusVariant
    )
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
    cur.execute("CREATE TABLE IF NOT EXISTS lyrics_cache (key TEXT PRIMARY KEY, data TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS imported_lyrics (key TEXT PRIMARY KEY, data TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS lyric_offsets (key TEXT PRIMARY KEY, offset_ms INTEGER)")
    cur.execute("DELETE FROM lyrics_cache WHERE key NOT LIKE 'v4|%'")   # drop results from the old loose matcher
    conn.commit()
    conn.close()

init_db()

# -------------------------------------------------------------------------
# GENERATE BLACK THEMED APP LOGO ICON
# -------------------------------------------------------------------------
def create_app_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    grad = QLinearGradient(0, 0, 64, 64)
    grad.setColorAt(0.0, QColor(22, 24, 30))
    grad.setColorAt(1.0, QColor(10, 11, 14))
    painter.setBrush(QBrush(grad))
    
    painter.setPen(QPen(QColor("#eb0029"), 2.0))
    painter.drawRoundedRect(QRectF(4, 4, 56, 56), 16, 16)

    painter.setPen(QPen(QColor("#eb0029"), 4.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QRectF(20, 24, 24, 24))
    painter.drawLine(QPointF(44, 36), QPointF(44, 16))

    painter.end()
    return QIcon(pixmap)

# -------------------------------------------------------------------------
# FULL MPRIS V2 D-BUS ADAPTOR
# -------------------------------------------------------------------------
if HAS_DBUS:
    class MprisRootAdaptor(QDBusAbstractAdaptor):
        def __init__(self, parent):
            super().__init__(parent)
            self.setAutoRelaySignals(True)
            self.win = parent

        @pyqtSlot(result=bool)
        def CanQuit(self):
            return True

        @pyqtSlot(result=bool)
        def CanRaise(self):
            return True

        @pyqtSlot(result=str)
        def Identity(self):
            return "OxygenMusic"

        @pyqtSlot()
        def Quit(self):
            QApplication.instance().quit()

        @pyqtSlot()
        def Raise(self):
            self.win.showNormal()
            self.win.activateWindow()

    class MprisPlayerAdaptor(QDBusAbstractAdaptor):
        def __init__(self, parent):
            super().__init__(parent)
            self.setAutoRelaySignals(True)
            self.win = parent

        @pyqtSlot(result=str)
        def PlaybackStatus(self):
            if self.win.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                return "Playing"
            elif self.win.player.playbackState() == QMediaPlayer.PlaybackState.PausedState:
                return "Paused"
            return "Stopped"

        @pyqtSlot(result=dict)
        def Metadata(self):
            return {
                "mpris:trackid": "/org/oxygenmusic/current_track",
                "xesam:title": self.win.current_title,
                "xesam:artist": ["OxygenMusic"],
                "xesam:album": "OxygenOS Spatial UI"
            }

        @pyqtSlot()
        def PlayPause(self):
            self.win.toggle_playback()

        @pyqtSlot()
        def Play(self):
            if self.win.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                self.win.toggle_playback()

        @pyqtSlot()
        def Pause(self):
            if self.win.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.win.toggle_playback()

        @pyqtSlot()
        def Next(self):
            self.win.play_next()

        @pyqtSlot()
        def Previous(self):
            self.win.play_previous()

# -------------------------------------------------------------------------
# APPLE REALISTIC LIQUID GLASS PAINTER
# -------------------------------------------------------------------------
GLASS_PHASE = 0.0          # advanced by a timer in the main window -> animates the light
_NOISE_PIXMAP = None

def _glass_noise():
    """Fine monochrome grain used as the frosted-glass texture."""
    global _NOISE_PIXMAP
    if _NOISE_PIXMAP is None:
        size = 96
        rng = np.random.default_rng(7)
        v = rng.integers(0, 256, (size, size), dtype=np.uint8)
        arr = np.zeros((size, size, 4), dtype=np.uint8)
        arr[..., 0] = v
        arr[..., 1] = v
        arr[..., 2] = v
        arr[..., 3] = 16
        data = arr.tobytes()
        img = QImage(data, size, size, size * 4, QImage.Format.Format_ARGB32)
        _NOISE_PIXMAP = QPixmap.fromImage(img)
    return _NOISE_PIXMAP

def draw_apple_liquid_glass(painter: QPainter, rect: QRectF, radius: float = 24.0, is_dark: bool = True):
    """Layered 'liquid glass': translucent body, grain, drifting light sweep,
    top specular, red caustic at the bottom and a two-tone refractive rim."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)

    # 1. translucent body (lets the animated backdrop glow through)
    body = QLinearGradient(rect.topLeft(), rect.bottomRight())
    if is_dark:
        body.setColorAt(0.0, QColor(34, 37, 46, 150))
        body.setColorAt(0.5, QColor(18, 19, 24, 172))
        body.setColorAt(1.0, QColor(10, 11, 14, 205))
    else:
        body.setColorAt(0.0, QColor(246, 248, 251, 112))
        body.setColorAt(0.5, QColor(235, 239, 244, 104))
        body.setColorAt(1.0, QColor(224, 229, 236, 118))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(body))
    painter.drawPath(path)

    painter.setClipPath(path)

    # 2. frosted grain texture
    painter.setOpacity(0.9 if is_dark else 0.22)
    painter.fillRect(rect, QBrush(_glass_noise()))
    painter.setOpacity(1.0)

    # 3. slow drifting light sweep
    pos = 0.5 + 0.5 * math.sin(GLASS_PHASE * 0.5)
    c = 0.25 + 0.5 * pos
    sweep = QLinearGradient(rect.topLeft(), rect.bottomRight())
    sweep.setColorAt(0.0, QColor(255, 255, 255, 0))
    sweep.setColorAt(1.0, QColor(255, 255, 255, 0))
    sweep.setColorAt(c - 0.2, QColor(255, 255, 255, 0))
    sweep.setColorAt(c, QColor(255, 255, 255, 26 if is_dark else 28))
    sweep.setColorAt(c + 0.2, QColor(255, 255, 255, 0))
    painter.fillRect(rect, QBrush(sweep))

    # 4. top specular highlight
    top_h = min(rect.height() * 0.38, 110.0)
    spec = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.top() + top_h)
    spec.setColorAt(0.0, QColor(255, 255, 255, 46 if is_dark else 52))
    spec.setColorAt(0.45, QColor(255, 255, 255, 12 if is_dark else 18))
    spec.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.fillRect(QRectF(rect.left(), rect.top(), rect.width(), top_h), QBrush(spec))

    # 5. warm caustic glow collecting at the bottom edge
    bot_h = min(rect.height() * 0.25, 90.0)
    caustic = QLinearGradient(rect.left(), rect.bottom(), rect.left(), rect.bottom() - bot_h)
    caustic.setColorAt(0.0, QColor(235, 0, 41, 34 if is_dark else 12))
    caustic.setColorAt(1.0, QColor(235, 0, 41, 0))
    painter.fillRect(QRectF(rect.left(), rect.bottom() - bot_h, rect.width(), bot_h), QBrush(caustic))

    painter.setClipping(False)

    # 6. refractive rim: lit from top-left and bounce-lit from bottom-right
    rim = QLinearGradient(rect.topLeft(), rect.bottomRight())
    if is_dark:
        rim.setColorAt(0.0, QColor(255, 255, 255, 150))
        rim.setColorAt(0.25, QColor(255, 255, 255, 40))
        rim.setColorAt(0.7, QColor(255, 255, 255, 14))
        rim.setColorAt(1.0, QColor(255, 255, 255, 85))
    else:
        rim.setColorAt(0.0, QColor(255, 255, 255, 145))
        rim.setColorAt(0.5, QColor(255, 255, 255, 48))
        rim.setColorAt(1.0, QColor(0, 0, 0, 34))
    painter.setPen(QPen(QBrush(rim), 1.3))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect.adjusted(0.65, 0.65, -0.65, -0.65), radius, radius)

    # 7. soft inner rim gives the glass thickness
    painter.setPen(QPen(QColor(255, 255, 255, 255 if is_dark else 38), 1.0))
    inner_r = max(0.0, radius - 2.0)
    painter.drawRoundedRect(rect.adjusted(2.0, 2.0, -2.0, -2.0), inner_r, inner_r)
    painter.restore()

# -------------------------------------------------------------------------
# AUDIO FFT ANALYSIS & WORKERS
# -------------------------------------------------------------------------
class AudioAnalysisWorker(QThread):
    analysis_ready = pyqtSignal(object)

    def __init__(self, source_uri: str, num_bars: int = 48):
        super().__init__()
        self.source_uri = source_uri
        self.num_bars = num_bars
        self._is_stopped = False
        self.proc = None

    def stop(self):
        self._is_stopped = True
        if self.proc:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                pass

    def run(self):
        try:
            cmd = [
                'ffmpeg', '-nostats', '-loglevel', 'quiet', '-i', self.source_uri,
                '-t', '900', '-f', 'wav', '-ac', '1', '-ar', '16000', '-'
            ]
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True
            )
            raw_audio, _ = self.proc.communicate()

            if self._is_stopped or not raw_audio or len(raw_audio) < 1000:
                self.analysis_ready.emit(None)
                return

            sr, samples = wavfile.read(BytesIO(raw_audio))
            if samples.dtype != np.float32:
                samples = samples.astype(np.float32) / (np.max(np.abs(samples)) + 1e-6)

            chunk_size = int(sr * 0.040)
            total_chunks = len(samples) // chunk_size
            if total_chunks == 0 or self._is_stopped:
                self.analysis_ready.emit(None)
                return

            spectrogram = np.zeros((total_chunks, self.num_bars), dtype=np.float32)
            freqs = np.fft.rfftfreq(chunk_size, 1.0 / sr)
            edges = np.logspace(np.log10(30), np.log10(7500), self.num_bars + 1)
            bin_indices = np.digitize(freqs, edges)

            for i in range(total_chunks):
                if self._is_stopped:
                    return
                segment = samples[i * chunk_size:(i + 1) * chunk_size] * np.hanning(chunk_size)
                fft_vals = np.abs(np.fft.rfft(segment))
                for b in range(1, self.num_bars + 1):
                    mask = (bin_indices == b)
                    if np.any(mask):
                        spectrogram[i, b - 1] = np.mean(fft_vals[mask])

            for i in range(total_chunks):
                row = spectrogram[i]
                smoothed = np.convolve(row, np.ones(3)/3.0, mode='same')
                spectrogram[i] = smoothed

            max_val = np.percentile(spectrogram, 98)
            if max_val > 0:
                spectrogram = np.clip(spectrogram / (max_val * 0.65), 0.0, 1.0)

            if not self._is_stopped:
                self.analysis_ready.emit(spectrogram)
        except Exception:
            if not self._is_stopped:
                self.analysis_ready.emit(None)

# -------------------------------------------------------------------------
# LYRICS MATCHING HELPERS (strict song match; preserve original lyric language)
# -------------------------------------------------------------------------
_EN_WORDS = set("""the and you i to a me my it in is that of for on with be we your love just don't dont i'm im
its it's like all but so know can got baby oh yeah no not what this are when if up do go say never now one get let see
way how down back they he she her him will was have had been would could out there from as at or an by tonight time
feel heart night day want need take make come them our us who why where too more only still than then here over""".split())

_OTHER_WORDS = set("""hai hain tera tere teri mera mere meri tu tum tujhe mujhe mein main nahi nahin kya ke ki ka ko se ho hoon
bhi aur dil pyaar pyar ishq yaar zindagi jaan kaise kyun raat aaj phir sabse wala wali hum humein woh vo ye yeh jo toh
na naa nee naan enna ennai oka raa chala el la los las que de y mi te amor corazon corazón para con por una un es en
pero yo vida je le les des est pas et mon ma ton du um uma não você eu ele nós ich du und nicht der die das ist ein
sa ne wa ni ga wo ga da yo""".split()) - _EN_WORDS

def _norm_text(s: str) -> str:
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", (s or "").lower())
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def looks_english(synced_text: str) -> bool:
    """Heuristic language check: English function words dominate, no non-Latin
    script and no romanised Hindi/Spanish/French/etc. markers."""
    text = re.sub(r"\[[^\]]*\]", " ", synced_text or "")
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 40:
        return False
    foreign = sum(1 for c in letters if ord(c) > 0xBF)          # non-ASCII letters (scripts / accents)
    if foreign / len(letters) > 0.03:
        return False
    words = re.findall(r"[a-z]+(?:'[a-z]+)?", text.lower())
    if len(words) < 20:
        return False
    en = sum(1 for w in words if w in _EN_WORDS) / len(words)
    other = sum(1 for w in words if w in _OTHER_WORDS) / len(words)
    return en >= 0.22 and other <= 0.10 and en >= other * 2.5

def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    r = SequenceMatcher(None, a, b).ratio()
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        r = max(r, 0.9)
    return r

def score_lyrics_candidate(artist: str, track: str, d: dict, duration_s: float):
    """Return a match score (higher = better) or None if this is not the same song."""
    name = _norm_text(d.get("trackName") or "")
    art = _norm_text(d.get("artistName") or "")
    if not name:
        return None
    nt, na = _norm_text(track), _norm_text(artist)

    t1 = _sim(nt, name)
    a1 = _sim(na, art) if na else None
    if na:                                   # title may be written "Song - Artist"
        t2, a2 = _sim(na, name), _sim(nt, art)
        if t2 > t1:
            t1, a1 = t2, a2
    if t1 < 0.75:
        return None
    if a1 is not None and a1 < 0.5:
        return None

    score = t1 if a1 is None else 0.65 * t1 + 0.35 * a1

    dur = d.get("duration") or 0
    if duration_s > 0 and dur:
        diff = abs(dur - duration_s)
        if diff > 8:                         # different cut -> timing would be wrong
            return None
        if a1 is None and diff > 5:
            return None
        score -= 0.01 * diff
    elif a1 is None and t1 < 0.9:
        return None
    return score

class LyricsWorker(QThread):
    """Fetches time-synced lyrics from the lrclib.net community database and preserves the song's original language.
    Nothing is bundled in the app: lyrics are looked up for the playing song and cached locally."""
    lyrics_ready = pyqtSignal(list, int)

    HEADERS = {"User-Agent": "OxygenMusic/1.0"}

    def __init__(self, query: str, duration_s: float = 0.0, token: int = 0):
        super().__init__()
        self.query = query
        self.duration_s = duration_s
        self.token = token
        self.cache_key = f"v4|{query}"

    @staticmethod
    def split_title(title: str):
        t = re.sub(r'\(.*?\)|\[.*?\]|【.*?】', ' ', title)
        t = re.split(r'\s*[|•]\s*', t)[0]
        t = re.sub(r'(?i)\b(official\s+(music\s+)?(video|audio|lyric\s+video)|lyric\s+video|lyrics?|visuali[sz]er|full\s+song|hd|hq|4k)\b', ' ', t)
        t = re.sub(r'\s+', ' ', t).strip(' -–—')
        artist, track = "", t
        parts = re.split(r'\s[-–—]\s', t, maxsplit=1)
        if len(parts) == 2:
            artist, track = parts[0].strip(), parts[1].strip()
        track = re.sub(r'(?i)\s+(ft\.?|feat\.?|featuring)\s+.*$', '', track).strip()
        artist = re.sub(r'(?i)\s*-?\s*(topic|vevo)$', '', artist).strip()
        return artist, track

    @staticmethod
    def parse_synced(raw: str):
        parsed = []
        for line in raw.split('\n'):
            stamps = re.findall(r'\[(\d+):(\d{2})(?:[.:](\d{1,3}))?\]', line)
            if not stamps:
                continue
            text = re.sub(r'\[[^\]]*\]', '', line).strip() or "♪"
            for m, s, frac in stamps:
                frac = frac or "0"
                ms = int(frac) * (100 if len(frac) == 1 else 10 if len(frac) == 2 else 1)
                parsed.append((int(m) * 60000 + int(s) * 1000 + ms, text))
        parsed.sort(key=lambda x: x[0])
        return parsed

    def cache_get(self):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5)
            row = conn.cursor().execute("SELECT data FROM lyrics_cache WHERE key = ?", (self.cache_key,)).fetchone()
            conn.close()
            if row:
                return [(int(t), str(x)) for t, x in json.loads(row[0])]
        except Exception:
            pass
        return []

    def cache_put(self, lyrics):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5)
            conn.cursor().execute("INSERT OR REPLACE INTO lyrics_cache (key, data) VALUES (?, ?)", (self.cache_key, json.dumps(lyrics)))
            conn.commit()
            conn.close()
        except Exception:
            pass

    def fetch_online(self):
        artist, track = self.split_title(self.query)
        attempts = []
        if artist and track:
            attempts.append({"track_name": track, "artist_name": artist})
            attempts.append({"q": f"{artist} {track}"})
        attempts.append({"q": track or self.query})

        # gather candidates from every search, then keep only the one that really is this song
        seen, cands = set(), []
        for params in attempts:
            try:
                res = requests.get("https://lrclib.net/api/search", params=params, headers=self.HEADERS, timeout=8)
                data = res.json() if res.status_code == 200 else []
            except Exception:
                continue
            for d in data or []:
                key = d.get("id") or (d.get("trackName"), d.get("artistName"), d.get("duration"))
                if key in seen or not d.get("syncedLyrics") or d.get("instrumental"):
                    continue
                seen.add(key)
                cands.append(d)

        best, best_score = None, 0.0
        for d in cands:
            score = score_lyrics_candidate(artist, track, d, self.duration_s)
            if score is None:
                continue
            if score > best_score:
                best, best_score = d, score
        return self.parse_synced(best["syncedLyrics"]) if best else []

    def run(self):
        try:
            lyrics = self.cache_get()
            if not lyrics:
                lyrics = self.fetch_online()
                if lyrics:
                    self.cache_put(lyrics)
            self.lyrics_ready.emit(lyrics or [], self.token)
        except Exception:
            self.lyrics_ready.emit([], self.token)

# -------------------------------------------------------------------------
# AUTOMATIC LYRIC SYNC (uses the song's own audio - no manual steps)
# -------------------------------------------------------------------------
FRAME_MS = 40   # spectrogram frame length (matches AudioAnalysisWorker)

def onset_flux(spec) -> np.ndarray:
    """How strongly new sound starts in the vocal range (~200 Hz - 3.5 kHz), per 40 ms frame."""
    S = np.asarray(spec, dtype=np.float32)
    band = S[:, 16:42] if S.shape[1] >= 42 else S
    d = np.diff(band, axis=0, prepend=band[:1])
    flux = np.maximum(d, 0.0).sum(axis=1)
    k = np.array([1, 2, 3, 2, 1], dtype=np.float32)
    flux = np.convolve(flux, k / k.sum(), mode="same")
    p95 = float(np.percentile(flux, 95)) if len(flux) else 0.0
    return np.clip(flux / p95, 0.0, 1.5) if p95 > 0 else flux

def auto_calibrate_offset(times_ms, flux, max_shift_ms: int = 5000) -> int:
    """Find how far the lyric timestamps are from the real vocal entries by testing every shift
    (+-5 s) and keeping the one where lyric line starts land on the strongest onsets.
    Returns the offset in ms (>0 = show lyrics later); 0 when the timing already fits or the evidence is weak."""
    starts = np.array([t for t in times_ms if t >= 0], dtype=np.int64)
    if len(starts) < 8 or len(flux) < 100:
        return 0
    max_f = max_shift_ms // FRAME_MS
    deltas = np.arange(-max_f, max_f + 1)
    idx = (starts // FRAME_MS)[:, None] + deltas[None, :]
    valid = (idx >= 0) & (idx < len(flux))
    vals = np.where(valid, flux[np.clip(idx, 0, len(flux) - 1)], 0.0)
    cnt = valid.sum(axis=0)
    score = np.where(cnt >= max(6, int(0.8 * len(starts))), vals.sum(axis=0) / np.maximum(cnt, 1), np.nan)
    if np.all(np.isnan(score)):
        return 0
    zero = max_f
    best = int(np.nanargmax(score))
    sd = float(np.nanstd(score))
    if sd < 1e-6 or not np.isfinite(score[zero]):
        return 0
    z = (float(score[best]) - float(np.nanmean(score))) / sd
    if abs(best - zero) * FRAME_MS < 120:            # already in sync
        return 0
    if z < 3.0 or score[best] < 1.25 * score[zero]:   # not convincingly better than the original timing
        return 0
    return int(deltas[best] * FRAME_MS)


def refine_synced_timing(lines, flux, search_ms=650):
    """Refine existing LRC line starts against this exact audio.

    The online LRC remains the authoritative text/order.  Only timestamps are
    moved when the audio contains a clear vocal/onset peak nearby.  This makes
    a downloaded LRC survive different intros/outros/cuts much better than a
    single global offset while avoiding large speculative shifts.
    """
    if not lines or flux is None or len(flux) < 100:
        return lines

    arr = np.asarray(flux, dtype=np.float32)
    frame_ms = FRAME_MS
    out = []
    prev = -1

    for t, lyric in lines:
        base = int(t)
        center = max(0, min(len(arr) - 1, base // frame_ms))
        radius = max(1, int(search_ms / frame_ms))
        lo = max(0, center - radius)
        hi = min(len(arr), center + radius + 1)

        local = arr[lo:hi]
        if len(local) == 0:
            out.append((base, lyric))
            prev = base
            continue

        # Prefer a local maximum rather than the loudest frame of a sustained
        # vowel.  A confidence gate prevents chorus/background beats from
        # rewriting otherwise-good LRC timestamps.
        candidates = []
        for j in range(1, len(local) - 1):
            if local[j] >= local[j - 1] and local[j] > local[j + 1]:
                candidates.append((float(local[j]), lo + j))
        if not candidates:
            candidates = [(float(np.max(local)), lo + int(np.argmax(local)))]

        peak_val, peak_i = max(candidates, key=lambda x: x[0])
        baseline = float(np.median(local)) + 1e-6
        confidence = peak_val / baseline

        candidate_ms = int(peak_i * frame_ms)
        delta = candidate_ms - base

        # Only trust strong evidence and keep correction deliberately small.
        if confidence >= 1.45 and abs(delta) <= search_ms:
            corrected = candidate_ms
        else:
            corrected = base

        # Preserve lyric order and avoid collapsing adjacent lines.
        if prev >= 0:
            corrected = max(corrected, prev + 80)
        out.append((corrected, lyric))
        prev = corrected

    return out


def auto_align_plain(lines, duration_ms: int, flux):
    """Time un-timestamped lines automatically: snap each line to a real vocal onset in the audio,
    in order, while keeping gaps roughly proportional to line length (dynamic programming).
    Returns [(ms, line)] or None if the audio gives too little to go on."""
    n = len(lines)
    if n < 4 or len(flux) < 100:
        return None
    thr = max(0.25, float(np.percentile(flux, 70)))
    cand = [i for i in range(1, len(flux) - 1) if flux[i] >= thr and flux[i] >= flux[i - 1] and flux[i] > flux[i + 1]]
    cand.sort(key=lambda i: -flux[i])                 # strongest first, keep them >= 0.4 s apart
    kept = []
    for i in cand:
        if all(abs(i - j) >= 10 for j in kept):
            kept.append(i)
        if len(kept) >= 700:
            break
    kept.sort()
    if len(kept) < n:
        return None

    pt = np.array(kept, dtype=np.float64) * FRAME_MS / 1000.0     # seconds
    pv = flux[kept].astype(np.float64)
    dur = (duration_ms if duration_ms and duration_ms > 0 else len(flux) * FRAME_MS) / 1000.0
    w = np.array([max(len(l), 8) for l in lines], dtype=np.float64)
    gaps = w[:-1] / w.sum() * (0.86 * dur)                       # expected seconds from line i to i+1
    lam, lam0, NEG = 0.012, 0.003, -1e9

    G = pt[None, :] - pt[:, None]                                  # G[q, p] = t_p - t_q
    allowed = G >= 0.6
    dp = pv - lam0 * (pt - 0.07 * dur) ** 2
    back = np.zeros((n, len(pt)), dtype=np.int64)
    for i in range(1, n):
        cand_m = np.where(allowed, dp[:, None] - lam * (G - gaps[i - 1]) ** 2, NEG)
        b = np.argmax(cand_m, axis=0)
        dp = cand_m[b, np.arange(len(pt))] + pv
        back[i] = b
    p = int(np.argmax(dp))
    if dp[p] < NEG / 2:
        return None
    order = [p]
    for i in range(n - 1, 0, -1):
        p = int(back[i][p])
        order.append(p)
    order.reverse()
    return [(int(pt[j] * 1000), lines[i]) for i, j in enumerate(order)]

# -------------------------------------------------------------------------
# USER-IMPORTED LYRICS (.lrc / .txt / pasted text) - saved per song, used before any online lookup
# -------------------------------------------------------------------------
def parse_user_lyrics(text: str):
    """('synced', [(ms, line)]) for LRC-style text, ('plain', [line]) otherwise, (None, []) if empty."""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    synced = LyricsWorker.parse_synced(raw)
    non_empty = [l for l in raw.split("\n") if l.strip()]
    if len(synced) >= 3 and len(synced) >= 0.3 * len(non_empty):
        return "synced", synced
    plain = [l.strip() for l in non_empty if not re.fullmatch(r"\[[^\]]*\]", l.strip())]   # drop [Chorus], [ar:..]
    plain = [re.sub(r"\[\d+:\d{2}(?:[.:]\d{1,3})?\]", "", l).strip() for l in plain]
    plain = [l for l in plain if l]
    return ("plain", plain) if plain else (None, [])

def spread_plain_lines(lines, duration_ms: int):
    """Approximate timing for un-timestamped text: spread lines over the song, weighted by line length."""
    dur = duration_ms if duration_ms and duration_ms > 0 else 180000
    start, end = dur * 0.06, dur * 0.94
    weights = [max(len(l), 8) for l in lines]
    total = float(sum(weights)) or 1.0
    out, acc = [], 0.0
    for line, wt in zip(lines, weights):
        out.append((int(start + (end - start) * acc / total), line))
        acc += wt
    return out

def save_imported_lyrics(key: str, kind: str, lines):
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.cursor().execute("INSERT OR REPLACE INTO imported_lyrics (key, data) VALUES (?, ?)",
                          (key, json.dumps({"kind": kind, "lines": lines})))
    conn.commit()
    conn.close()

def load_imported_lyrics(key: str):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        row = conn.cursor().execute("SELECT data FROM imported_lyrics WHERE key = ?", (key,)).fetchone()
        conn.close()
        if row:
            obj = json.loads(row[0])
            return obj["kind"], obj["lines"]
    except Exception:
        pass
    return None

def load_lyric_offset(key: str):
    """Manually saved timing fix for a song, or None -> timing is automatic."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        row = conn.cursor().execute("SELECT offset_ms FROM lyric_offsets WHERE key = ?", (key,)).fetchone()
        conn.close()
        return int(row[0]) if row else None
    except Exception:
        return None

def delete_lyric_offset(key: str):
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.cursor().execute("DELETE FROM lyric_offsets WHERE key = ?", (key,))
    conn.commit()
    conn.close()

def save_lyric_offset(key: str, ms: int):
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.cursor().execute("INSERT OR REPLACE INTO lyric_offsets (key, offset_ms) VALUES (?, ?)", (key, int(ms)))
    conn.commit()
    conn.close()

def delete_imported_lyrics(key: str):
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.cursor().execute("DELETE FROM imported_lyrics WHERE key = ?", (key,))
    conn.commit()
    conn.close()

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

class StreamUrlWorker(QThread):
    stream_ready = pyqtSignal(bool, str, str, str)

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
        output_template = str(APP_DIR / "%(title)s.%(ext)s")
        try:
            ydl_opts = {
                'format': 'bestaudio[ext=m4a]/bestaudio/best',
                'outtmpl': output_template,
                'quiet': True,
                'no_warnings': True,
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'm4a'}],
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=True)
                filename = ydl.prepare_filename(info)
                audio_path = os.path.splitext(filename)[0] + ".m4a"
                if not os.path.exists(audio_path):
                    audio_path = filename
                title = info.get('title', 'Unknown Title')
                self.finished_signal.emit(True, audio_path, title)
        except Exception as e:
            self.finished_signal.emit(False, str(e), "")

# -------------------------------------------------------------------------
# CUSTOM DRAWN CIRCLED DOWNLOAD BUTTON
# -------------------------------------------------------------------------
class CircledDownloadButton(QAbstractButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(34, 34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hover_scale = 1.0
        self.setMouseTracking(True)

    def enterEvent(self, event):
        self.hover_scale = 1.12
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hover_scale = 1.0
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        center = QPointF(w / 2.0, h / 2.0)
        radius = 15.0 * self.hover_scale

        is_hovered = self.underMouse()
        bg_color = QColor(235, 0, 41) if is_hovered else QColor(255, 255, 255, 18)
        border_color = QColor(235, 0, 41) if is_hovered else QColor(255, 255, 255, 50)

        painter.setBrush(bg_color)
        painter.setPen(QPen(border_color, 1.4))
        painter.drawEllipse(center, radius, radius)

        arrow_color = QColor(255, 255, 255) if is_hovered else QColor(220, 225, 235)
        painter.setPen(QPen(arrow_color, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        
        cx, cy = center.x(), center.y()
        painter.drawLine(QPointF(cx, cy - 5.5), QPointF(cx, cy + 3.5))
        painter.drawLine(QPointF(cx - 3.5, cy + 0.5), QPointF(cx, cy + 4.5))
        painter.drawLine(QPointF(cx + 3.5, cy + 0.5), QPointF(cx, cy + 4.5))

class SearchResultItemWidget(QWidget):
    download_clicked = pyqtSignal(str)

    def __init__(self, title: str, duration: str, url: str, is_dark: bool = True, parent=None):
        super().__init__(parent)
        self.url = url
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(16)
        
        text_color = "#e2e8f0" if is_dark else "#1d1d1f"
        self.lbl_title = QLabel(f"{title}  ({duration})")
        self.lbl_title.setWordWrap(True)
        self.lbl_title.setStyleSheet(f"color: {text_color}; font-size: 10.5pt; font-weight: 500; background: transparent;")
        
        self.btn_download = CircledDownloadButton()
        self.btn_download.setToolTip("Download to Library")
        self.btn_download.clicked.connect(lambda: self.download_clicked.emit(self.url))

        layout.addWidget(self.lbl_title, 1)
        layout.addWidget(self.btn_download, 0)

# -------------------------------------------------------------------------
# PURE VISUALIZER STAGE WITH WIDER GAP & HIGH-DENSITY MIRRORED BARS
# -------------------------------------------------------------------------
class PureVisualizerStage(QWidget):
    def __init__(self, is_dark=True, parent=None):
        super().__init__(parent)
        self.is_dark = is_dark
        self.num_bars_half = 48
        self.current_heights = np.zeros(self.num_bars_half, dtype=np.float32)
        self.target_heights = np.zeros(self.num_bars_half, dtype=np.float32)
        self.smooth_targets = np.zeros(self.num_bars_half, dtype=np.float32)
        self.spectrum_data = None
        self.spatial_phase = 0.0
        self.lyrics = []
        self.lyric_times = []
        self.lyric_idx = -2
        self.lyric_t = 1.0
        self.lyrics_state = "idle"   # idle / loading / none / ok
        self.lyric_offset_ms = 0     # >0 shows lyrics later, <0 earlier (saved per song)
        self.is_playing = False
        self._pos_base = 0.0         # last position reported by the player (ms)
        self._pos_stamp = time.monotonic()
        self.current_lyric_text = ""

        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.physics_tick)
        self.anim_timer.start(8)  # ~120 Hz UI interpolation for fluid Liquid Glass motion

    def set_spectrum(self, spectrum):
        self.spectrum_data = spectrum
        if spectrum is None:
            self.target_heights.fill(0.0)

    def set_lyrics(self, lyrics_list):
        self.lyrics = list(lyrics_list)
        self.lyric_times = [t for t, _ in self.lyrics]
        self.lyrics_state = "ok" if self.lyrics else "none"
        self.lyric_idx = -2
        self.lyric_t = 1.0

    def set_lyrics_state(self, state: str):
        self.lyrics = []
        self.lyric_times = []
        self.lyrics_state = state
        self.lyric_idx = -2
        self.lyric_t = 1.0

    def gap_width(self, w: float) -> float:
        return max(160.0, min(340.0, (w - 60.0) * 0.34))

    def set_playing(self, playing: bool):
        self._pos_base = self._estimated_pos()
        self._pos_stamp = time.monotonic()
        self.is_playing = playing

    def _estimated_pos(self) -> float:
        """High-resolution playback clock between QMediaPlayer positionChanged signals."""
        if not self.is_playing:
            return self._pos_base
        # Keep interpolation bounded so a stalled/late multimedia callback
        # cannot jump the lyric state by several seconds.
        return self._pos_base + min(1000.0, (time.monotonic() - self._pos_stamp) * 1000.0)

    def update_lyric_index(self, pos_ms: float):
        if not self.lyric_times:
            return
        # LRC timestamps describe the instant the line begins. Do not use the
        # old 150 ms look-ahead: it made every line visibly early.  A small
        # 20 ms display lead compensates for paint/audio scheduling without
        # changing the actual lyric timestamp.
        effective_pos = pos_ms - self.lyric_offset_ms + 20.0
        idx = bisect.bisect_right(self.lyric_times, effective_pos) - 1
        if idx != self.lyric_idx:
            step_forward = (idx == self.lyric_idx + 1)
            self.lyric_idx = idx
            self.lyric_t = 0.0 if step_forward else 1.0   # animate normal progress, snap on seeks

    def set_lyric_offset(self, ms: int):
        self.lyric_offset_ms = int(ms)
        self.update_lyric_index(self._estimated_pos())

    def sync_to_timestamp(self, pos_ms: int):
        if self.spectrum_data is not None:
            chunk_idx = int(pos_ms / 40)
            if chunk_idx < len(self.spectrum_data):
                raw = self.spectrum_data[chunk_idx]
                xp = np.linspace(0, 1, len(raw))
                x = np.linspace(0, 1, self.num_bars_half)
                interpolated = np.interp(x, xp, raw)
                smoothed = np.copy(interpolated)
                kernel = np.array([0.2, 0.6, 0.2])
                smoothed = np.convolve(interpolated, kernel, mode='same')
                self.target_heights = np.maximum(smoothed, 0.04)
            else:
                self.target_heights *= 0.88
        else:
            self.target_heights = np.maximum(self.target_heights * 0.88, 0.02)

        self._pos_base = float(pos_ms)
        self._pos_stamp = time.monotonic()
        self.update_lyric_index(pos_ms)

    def physics_tick(self):
        self.spatial_phase += 0.010
        if self.lyric_t < 1.0:
            # Critically-damped-feeling ease: quick response, no mechanical snap.
            self.lyric_t = min(1.0, self.lyric_t + 0.095)
        if self.is_playing and self.lyric_times:
            self.update_lyric_index(self._estimated_pos())   # line changes land on time, not on the next player tick
        self.smooth_targets += (self.target_heights - self.smooth_targets) * 0.22
        for i in range(self.num_bars_half):
            target = self.smooth_targets[i]
            curr = self.current_heights[i]
            if target > curr:
                self.current_heights[i] += (target - curr) * 0.30
            else:
                self.current_heights[i] -= (curr - target) * 0.10
        self.update()

    def draw_lyrics(self, painter, w, h):
        """Synced lyrics in the empty space between the two visualizer halves:
        previous / current / next lines glide upward as the song moves on."""
        gap_w = self.gap_width(w)
        text_w = gap_w - 20.0
        cx = w / 2.0
        x0 = cx - text_w / 2.0
        fg = QColor(255, 255, 255) if self.is_dark else QColor(29, 29, 31)
        flags = (Qt.AlignmentFlag.AlignHCenter.value | Qt.AlignmentFlag.AlignTop.value
                 | Qt.TextFlag.TextWordWrap.value)

        painter.save()
        painter.setClipRect(QRectF(cx - gap_w / 2.0, 78.0, gap_w, max(0.0, h - 78.0 - 36.0)))

        if not self.lyrics:
            msg = {"loading": "Searching lyrics…", "none": "Lyrics unavailable"}.get(self.lyrics_state, "")
            if msg:
                f = QFont("-apple-system")
                f.setPointSizeF(10.0)
                painter.setFont(f)
                painter.setOpacity(0.5)
                painter.setPen(fg)
                if self.lyrics_state == "none":
                    box = QRectF(x0, h * 0.5 - 30.0, text_w, 60.0)
                    align = Qt.AlignmentFlag.AlignHCenter.value | Qt.AlignmentFlag.AlignBottom.value | Qt.TextFlag.TextWordWrap.value
                else:
                    box = QRectF(x0, 78.0, text_w, h - 78.0 - 36.0)
                    align = Qt.AlignmentFlag.AlignCenter.value
                painter.drawText(box, align, msg)
            painter.restore()
            return

        ease = 1.0 - (1.0 - self.lyric_t) ** 3

        def base_op(o):
            a = abs(o)
            return 1.0 if a == 0 else 0.42 if a == 1 else 0.2 if a == 2 else 0.0

        def base_size(o):
            a = abs(o)
            return 15.5 if a == 0 else 11.5 if a == 1 else 10.0

        n = len(self.lyrics)
        items = []
        for o in range(-2, 3):
            li = self.lyric_idx + o
            if not (0 <= li < n):
                continue
            # a line at offset o used to sit at offset o+1 -> blend from there
            op = base_op(o + 1) * (1.0 - ease) + base_op(o) * ease
            size = base_size(o + 1) * (1.0 - ease) + base_size(o) * ease
            text = self.lyrics[li][1]
            font = QFont("-apple-system")
            font.setPointSizeF(size)
            font.setWeight(QFont.Weight.Bold if o == 0 else QFont.Weight.DemiBold)
            fh = QFontMetrics(font).boundingRect(0, 0, int(text_w), 4000, flags, text).height()
            items.append((o, text, font, fh, op))

        if items:
            spacing = 16.0
            ys, y = {}, 0.0
            for o, _, _, fh, _ in items:
                ys[o] = y
                y += fh + spacing
            cur = next((it for it in items if it[0] == 0), None)
            anchor = (ys[0] + cur[3] / 2.0) if cur else (y - spacing) / 2.0
            top = h * 0.5 + 14.0 - anchor + (1.0 - ease) * 36.0
            painter.setPen(fg)
            for o, text, font, fh, op in items:
                painter.setOpacity(max(0.0, min(1.0, op)))
                painter.setFont(font)
                painter.drawText(QRectF(x0, top + ys[o], text_w, fh), flags, text)
        painter.restore()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        bg_color = QColor("#060709") if self.is_dark else QColor("#e9edf2")
        painter.fillRect(self.rect(), bg_color)

        avg_energy = float(np.mean(self.current_heights))
        aura_alpha = int(25 + avg_energy * 80)
        aura = QRadialGradient(w * 0.5 + math.sin(self.spatial_phase) * 70, h * 0.5, w * 0.55)
        if self.is_dark:
            aura.setColorAt(0.0, QColor(235, 0, 41, aura_alpha))
            aura.setColorAt(1.0, QColor(6, 7, 9, 0))
        else:
            aura.setColorAt(0.0, QColor(235, 0, 41, int(aura_alpha * 0.35)))
            aura.setColorAt(1.0, QColor(245, 245, 247, 0))
        painter.fillRect(self.rect(), aura)

        stage_rect = QRectF(0, 0, w, h)
        draw_apple_liquid_glass(painter, stage_rect, radius=0.0, is_dark=self.is_dark)

        self.draw_lyrics(painter, w, h)

        baseline_y = h - 28
        side_margin = 30
        available_w = w - (side_margin * 2)
        center_gap = self.gap_width(w)
        half_w = (available_w - center_gap) / 2.0
        
        spacing = 3.0
        bar_w = max(2.0, (half_w - ((self.num_bars_half - 1) * spacing)) / float(self.num_bars_half))
        max_vis_height = h * 0.55

        for i in range(self.num_bars_half):
            val = float(self.current_heights[i])
            up_h = max(6.0, val * max_vis_height)

            x_left = side_margin + i * (bar_w + spacing)
            y_up_left = baseline_y - up_h

            rev_i = (self.num_bars_half - 1) - i
            val_rev = float(self.current_heights[rev_i])
            up_h_rev = max(6.0, val_rev * max_vis_height)
            x_right = side_margin + half_w + center_gap + i * (bar_w + spacing)
            y_up_right = baseline_y - up_h_rev

            grad_left = QLinearGradient(x_left, baseline_y, x_left, y_up_left)
            if self.is_dark:
                grad_left.setColorAt(0.0, QColor(235, 0, 41, 190))
                grad_left.setColorAt(0.7, QColor(255, 80, 120, 220))
                grad_left.setColorAt(1.0, QColor(255, 255, 255, 240))
            else:
                grad_left.setColorAt(0.0, QColor(235, 0, 41, 210))
                grad_left.setColorAt(0.75, QColor(190, 15, 45, 230))
                grad_left.setColorAt(1.0, QColor(28, 30, 38, 245))

            painter.setBrush(QBrush(grad_left))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(x_left, y_up_left, bar_w, up_h), 2.0, 2.0)

            grad_right = QLinearGradient(x_right, baseline_y, x_right, y_up_right)
            if self.is_dark:
                grad_right.setColorAt(0.0, QColor(235, 0, 41, 190))
                grad_right.setColorAt(0.7, QColor(255, 80, 120, 220))
                grad_right.setColorAt(1.0, QColor(255, 255, 255, 240))
            else:
                grad_right.setColorAt(0.0, QColor(235, 0, 41, 210))
                grad_right.setColorAt(0.75, QColor(190, 15, 45, 230))
                grad_right.setColorAt(1.0, QColor(28, 30, 38, 245))

            painter.setBrush(QBrush(grad_right))
            painter.drawRoundedRect(QRectF(x_right, y_up_right, bar_w, up_h_rev), 2.0, 2.0)

class Fluid120HzProgressBar(QWidget):
    position_seek = pyqtSignal(int)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(20)
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
            self.render_pos_ms += diff * (0.15 if abs(diff) > 0.5 else 1.0)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        track_h = 5.0
        track_y = (h - track_h) / 2.0
        progress_w = max(0.0, min(1.0, self.render_pos_ms / float(self.duration_ms))) * w

        painter.setBrush(QColor(150, 150, 150, 40))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(0, track_y, w, track_h), track_h / 2.0, track_h / 2.0)

        if progress_w > 0:
            grad = QLinearGradient(0, 0, progress_w, 0)
            grad.setColorAt(0.0, QColor("#eb0029"))
            grad.setColorAt(0.55, QColor("#ff496d"))
            grad.setColorAt(1.0, QColor("#ffffff"))
            painter.setBrush(grad)
            painter.drawRoundedRect(QRectF(0, track_y, progress_w, track_h), track_h / 2.0, track_h / 2.0)

            # Tiny luminous glass thumb; it tracks the high-resolution render
            # position instead of the coarse multimedia callback.
            r = 4.2
            painter.setBrush(QColor(255, 255, 255, 235))
            painter.setPen(QPen(QColor(255, 255, 255, 120), 1.0))
            painter.drawEllipse(QPointF(progress_w, h / 2.0), r, r)

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
        self.setFixedSize(52, 52)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.is_playing, self.morph_ratio, self.hover_scale = False, 0.0, 1.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.animate_tick)
        self.timer.start(8)
        self.press_t = 0.0

    def set_playing(self, p: bool):
        self.is_playing = p

    def animate_tick(self):
        self.morph_ratio += ((1.0 if self.is_playing else 0.0) - self.morph_ratio) * 0.18
        self.hover_scale += ((1.055 if self.underMouse() else 1.00) - self.hover_scale) * 0.18
        target_press = 1.0 if self.isDown() else 0.0
        self.press_t += (target_press - self.press_t) * 0.28
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        center = QPointF(w / 2.0, h / 2.0)
        radius = 23.0 * self.hover_scale * (1.0 - 0.055 * self.press_t)

        glow_a = int(max(0, min(255,
            28 + 48 * self.morph_ratio + (self.hover_scale - 1.0) * 360 + 30 * self.press_t
        )))
        glow = QRadialGradient(center, 26.0)
        glow.setColorAt(0.55, QColor(235, 0, 41, glow_a))
        glow.setColorAt(1.0, QColor(235, 0, 41, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(glow))
        painter.drawEllipse(center, 26.0, 26.0)

        grad = QLinearGradient(0, 0, w, h)
        grad.setColorAt(0.0, QColor(235, 0, 41, 245))
        grad.setColorAt(1.0, QColor(195, 0, 30, 255))
        painter.setBrush(grad)
        painter.setPen(QPen(QColor(255, 255, 255, 180), 1.2))
        painter.drawEllipse(center, radius, radius)

        # Liquid-Glass rim: two passes create the thin refractive edge used by
        # modern Apple-style controls.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 165), 1.0))
        painter.drawEllipse(center, radius - 0.7, radius - 0.7)
        painter.setPen(QPen(QColor(255, 255, 255, 55), 2.0))
        painter.drawArc(QRectF(center.x() - radius + 1, center.y() - radius + 1,
                               2 * radius - 2, 2 * radius - 2), 35 * 16, 115 * 16)

        # glass gloss on the upper half of the button
        painter.save()
        clip = QPainterPath()
        clip.addEllipse(center, radius, radius)
        painter.setClipPath(clip)
        gloss = QLinearGradient(center.x(), center.y() - radius, center.x(), center.y())
        gloss.setColorAt(0.0, QColor(255, 255, 255, 110))
        gloss.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(QRectF(0, center.y() - radius, w, radius), QBrush(gloss))
        painter.restore()

        m = self.morph_ratio
        painter.setBrush(QColor("#ffffff"))
        p1_x = center.x() - (6.5 - 2.0 * m)
        p1_w, p1_h = 3.8 + (1.0 * (1.0 - m)), 15.0 - (4.0 * (1.0 - m))
        painter.drawRoundedRect(QRectF(p1_x, center.y() - p1_h / 2.0, p1_w, p1_h), 1.8, 1.8)
        if m > 0.05:
            painter.setOpacity(float(m))
            painter.drawRoundedRect(QRectF(center.x() + (2.8 * m), center.y() - p1_h / 2.0, p1_w, p1_h), 1.8, 1.8)
            painter.setOpacity(1.0)
        if m < 0.95:
            painter.setOpacity(float(1.0 - m))
            play_tip = QPainterPath()
            play_tip.moveTo(center.x() - 3.8, center.y() - 7.5)
            play_tip.lineTo(center.x() + 8.2, center.y())
            play_tip.lineTo(center.x() - 3.8, center.y() + 7.5)
            painter.drawPath(play_tip)
            painter.setOpacity(1.0)

class AnimatedVolumeIcon(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(32, 32)
        self.current_volume = 85
        self.pulse_val = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    def set_volume_level(self, vol: int):
        self.current_volume = vol
        self.pulse_val = 1.0
        self.update()

    def tick(self):
        if self.pulse_val > 0.0:
            self.pulse_val -= 0.06
            if self.pulse_val < 0:
                self.pulse_val = 0.0
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cy = h / 2.0
        
        scale_factor = 1.0 + (self.pulse_val * 0.2)
        painter.translate(6, cy)
        painter.scale(scale_factor, scale_factor)
        painter.translate(-6, -cy)

        painter.setPen(Qt.PenStyle.NoPen)
        color_val = QColor(235, 0, 41) if self.pulse_val > 0 else QColor(150, 155, 165, 210)
        painter.setBrush(color_val)

        spk = QPainterPath()
        spk.moveTo(2, cy - 3)
        spk.lineTo(6, cy - 3)
        spk.lineTo(11, cy - 8)
        spk.lineTo(11, cy + 8)
        spk.lineTo(6, cy + 3)
        spk.lineTo(2, cy + 3)
        painter.drawPath(spk)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen_color = color_val if self.pulse_val > 0 else QColor(150, 155, 165, 180)
        painter.setPen(QPen(pen_color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))

        vol = self.current_volume
        if vol > 0:
            painter.drawArc(QRectF(13, cy - 4, 5, 8), -50 * 16, 100 * 16)
        if vol > 35:
            painter.drawArc(QRectF(13, cy - 7, 8, 14), -50 * 16, 100 * 16)
        if vol > 70:
            painter.drawArc(QRectF(13, cy - 10, 11, 20), -50 * 16, 100 * 16)

class SlidingPanel(QWidget):
    """Library panel whose width follows the mouse (drag the edge handle).

    Width is continuous from 0 to the right edge of the window - no snapping, no auto-close.
    Below MIN_CONTENT the content keeps its size and simply slides out of
    view to the left, so nothing re-wraps or jumps while you drag.
    """
    MIN_CONTENT = 220
    HANDLE_W = 16

    def __init__(self, content: QWidget, full_width: int = 280, parent=None):
        super().__init__(parent)
        self.last_width = full_width
        self.content = content
        content.setParent(self)
        self.setFixedWidth(full_width)

        self.anim = QVariantAnimation(self)
        self.anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.anim.valueChanged.connect(lambda v: self.set_width(float(v)))

    def max_width(self):
        """The panel may be dragged all the way to the right edge of the window."""
        return max(0, self.window().width() - self.HANDLE_W)

    def set_width(self, w):
        w = int(max(0, min(self.max_width(), round(w))))
        if w != self.width():
            self.setFixedWidth(w)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cw = max(self.width(), self.MIN_CONTENT)
        self.content.setGeometry(self.width() - cw, 0, cw, self.height())

    def toggle(self):
        """Animated open/close (button, Ctrl+B or double-click on the handle)."""
        self.anim.stop()
        start = self.width()
        if start > 0:
            self.last_width = start if start >= 160 else self.last_width
            end = 0
        else:
            end = self.last_width
        self.anim.setDuration(max(120, int(340 * abs(end - start) / 280.0)))
        self.anim.setStartValue(float(start))
        self.anim.setEndValue(float(end))
        self.anim.start()

class SidebarResizeHandle(QWidget):
    """Glass grab-pill on the panel's right edge: drag with the mouse to resize.
    It grows on hover, stretches + glows while dragging and springs back on release."""
    def __init__(self, panel: SlidingPanel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.setFixedWidth(SlidingPanel.HANDLE_W)
        self.setCursor(Qt.CursorShape.SplitHCursor)
        self.setMouseTracking(True)
        self.setToolTip("Drag to resize  •  double-click to show / hide")
        self.dragging = False
        self.hover_t = 0.0
        self.press_t = 0.0
        self.start_x = 0.0
        self.start_w = 0

        self.hover_anim = QVariantAnimation(self)
        self.hover_anim.valueChanged.connect(self._set_hover)
        self.press_anim = QVariantAnimation(self)
        self.press_anim.valueChanged.connect(self._set_press)

    def _set_hover(self, v):
        self.hover_t = float(v)
        self.update()

    def _set_press(self, v):
        self.press_t = float(v)
        self.update()

    def _run(self, anim, start, end, ms, curve):
        anim.stop()
        anim.setStartValue(float(start))
        anim.setEndValue(float(end))
        anim.setDuration(ms)
        anim.setEasingCurve(curve)
        anim.start()

    def enterEvent(self, event):
        self._run(self.hover_anim, self.hover_t, 1.0, 180, QEasingCurve.Type.OutCubic)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._run(self.hover_anim, self.hover_t, 0.0, 260, QEasingCurve.Type.OutCubic)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.panel.anim.stop()
            self.dragging = True
            self.start_x = event.globalPosition().x()
            self.start_w = self.panel.width()
            self._run(self.press_anim, self.press_t, 1.0, 140, QEasingCurve.Type.OutCubic)

    def mouseMoveEvent(self, event):
        if self.dragging:
            self.panel.set_width(self.start_w + (event.globalPosition().x() - self.start_x))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = False
            if self.panel.width() >= 160:
                self.panel.last_width = self.panel.width()
            self._run(self.press_anim, self.press_t, 0.0, 520, QEasingCurve.Type.OutBack)

    def mouseDoubleClickEvent(self, event):
        self.panel.toggle()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        hv, pr = self.hover_t, self.press_t
        glow = max(hv * 0.45, pr)

        def clamp(v):
            return int(max(0, min(255, v)))

        # hairline along the whole edge
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, clamp(14 + 34 * glow)))
        painter.drawRect(QRectF(cx - 0.5, 0, 1.0, h))

        # red glow behind the pill
        if glow > 0.02:
            g = QRadialGradient(cx, cy, 90.0)
            g.setColorAt(0.0, QColor(235, 0, 41, clamp(95 * glow)))
            g.setColorAt(1.0, QColor(235, 0, 41, 0))
            painter.setBrush(QBrush(g))
            painter.drawRect(self.rect())

        pw = max(4.0, 6.0 + 3.0 * hv + 2.5 * pr)
        ph = max(30.0, 56.0 + 14.0 * hv + 28.0 * pr)
        rect = QRectF(cx - pw / 2.0, cy - ph / 2.0, pw, ph)

        body = QLinearGradient(rect.left(), 0, rect.right(), 0)
        tint_g = clamp(255 - 175 * pr)
        tint_b = clamp(255 - 150 * pr)
        body.setColorAt(0.0, QColor(255, tint_g, tint_b, clamp(95 + 70 * glow)))
        body.setColorAt(1.0, QColor(255, tint_g, tint_b, clamp(45 + 60 * glow)))
        painter.setBrush(QBrush(body))
        painter.setPen(QPen(QColor(255, 255, 255, clamp(110 + 90 * glow)), 1.0))
        painter.drawRoundedRect(rect, pw / 2.0, pw / 2.0)

        # specular dot near the top
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 120))
        painter.drawRoundedRect(QRectF(rect.left() + pw * 0.28, rect.top() + 4.0, pw * 0.44, ph * 0.2), pw * 0.22, pw * 0.22)

class SkipButton(QAbstractButton):
    """Previous / next icon drawn by hand: perfectly centred, glass hover + press animation."""
    def __init__(self, forward: bool, parent=None):
        super().__init__(parent)
        self.forward = forward
        self.setFixedSize(44, 44)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._t = 0.0
        self.anim = QVariantAnimation(self)
        self.anim.valueChanged.connect(self._set_t)

    def _set_t(self, v):
        self._t = float(v)
        self.update()

    def _go(self, end, ms):
        self.anim.stop()
        self.anim.setStartValue(float(self._t))
        self.anim.setEndValue(float(end))
        self.anim.setDuration(ms)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim.start()

    def enterEvent(self, event):
        self._go(1.0, 170)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._go(0.0, 240)
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        t = max(0.0, min(1.0, self._t))
        scale = 0.88 if self.isDown() else 1.0 + 0.06 * t

        if t > 0.01:
            r = 20.0 * scale
            circle = QPainterPath()
            circle.addEllipse(QPointF(cx, cy), r, r)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(235, 0, 41, int(55 * t)))
            painter.drawPath(circle)
            painter.save()
            painter.setClipPath(circle)
            gloss = QLinearGradient(cx, cy - r, cx, cy)
            gloss.setColorAt(0.0, QColor(255, 255, 255, int(85 * t)))
            gloss.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.fillRect(QRectF(cx - r, cy - r, r * 2, r), QBrush(gloss))
            painter.restore()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(255, 255, 255, int(110 * t)), 1.1))
            painter.drawEllipse(QPointF(cx, cy), r, r)

        # icon (mirrored horizontally for "next")
        painter.translate(cx, cy)
        painter.scale(-scale if self.forward else scale, scale)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255) if t > 0.5 else QColor(226, 232, 240))
        painter.drawRoundedRect(QRectF(-7.5, -7.5, 2.8, 15.0), 1.2, 1.2)
        tri = QPainterPath()
        tri.moveTo(7.5, -7.5)
        tri.lineTo(-4.0, 0.0)
        tri.lineTo(7.5, 7.5)
        tri.closeSubpath()
        painter.drawPath(tri)


class LyricsImportDialog(QDialog):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import lyrics")
        self.resize(560, 480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)

        head = QLabel(f"Lyrics for: {title}")
        head.setWordWrap(True)
        head.setStyleSheet("font-size: 11pt; font-weight: 600;")
        hint = QLabel("Paste the lyrics below, or open a .lrc / .txt file.\n"
                      "Lines with [mm:ss.xx] timestamps play exactly in sync. Plain lines are spread "
                      "across the song (approximate), so .lrc gives the best timing.")
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 9pt; color: rgba(150, 155, 165, 0.95);")

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("Paste lyrics here…")
        self.editor.setStyleSheet("QPlainTextEdit { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.14);"
                                  " border-radius: 12px; padding: 8px; }")

        row = QHBoxLayout()
        btn_open = QPushButton("Open file…")
        btn_cancel = QPushButton("Cancel")
        btn_save = QPushButton("Save lyrics")
        btn_save.setDefault(True)
        btn_open.clicked.connect(self.open_file)
        btn_cancel.clicked.connect(self.reject)
        btn_save.clicked.connect(self.accept)
        row.addWidget(btn_open)
        row.addStretch(1)
        row.addWidget(btn_cancel)
        row.addWidget(btn_save)

        lay.addWidget(head)
        lay.addWidget(hint)
        lay.addWidget(self.editor, 1)
        lay.addLayout(row)

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open lyrics file", "", "Lyrics (*.lrc *.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                self.editor.setPlainText(fh.read())
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not read file:\n{e}")

    def text(self) -> str:
        return self.editor.toPlainText()

class LyricsSyncControl(QWidget):
    """Glass pill:  [ − ]  Timing +0.25s  [ + ]   - nudge lyrics earlier/later (hold to repeat)."""
    adjust = pyqtSignal(int)
    reset = pyqtSignal()
    STEP = 250

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(200, 32)
        self.setMouseTracking(True)
        self.offset_ms = 0
        self.auto = False
        self.hover = None
        self.hold_dir = 0
        self.hold_timer = QTimer(self)
        self.hold_timer.timeout.connect(self._repeat)
        self.setToolTip("Lyrics timing\n−  shows lyrics earlier   +  shows them later\n"
                        "Click the middle to reset  (Alt+← / Alt+→ also work)")

    def set_offset(self, ms: int, auto: bool = False):
        self.offset_ms = int(ms)
        self.auto = auto
        self.update()

    def _zone(self, x):
        return "minus" if x < 36 else "plus" if x > self.width() - 36 else "mid"

    def _repeat(self):
        self.hold_timer.setInterval(110)
        self.adjust.emit(self.hold_dir * self.STEP)

    def mouseMoveEvent(self, event):
        z = self._zone(event.position().x())
        if z != self.hover:
            self.hover = z
            self.update()

    def leaveEvent(self, event):
        self.hover = None
        self.hold_timer.stop()
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        z = self._zone(event.position().x())
        if z == "mid":
            self.reset.emit()
            return
        self.hold_dir = -1 if z == "minus" else 1
        self.adjust.emit(self.hold_dir * self.STEP)
        self.hold_timer.start(380)

    def mouseReleaseEvent(self, event):
        self.hold_timer.stop()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        rect = QRectF(1.0, 1.0, w - 2.0, h - 2.0)
        path = QPainterPath()
        path.addRoundedRect(rect, h / 2.0, h / 2.0)
        painter.setPen(QPen(QColor(255, 255, 255, 60), 1.0))
        painter.setBrush(QColor(255, 255, 255, 20))
        painter.drawPath(path)

        for zone, cx in (("minus", 17.0), ("plus", w - 17.0)):
            if self.hover == zone:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(235, 0, 41, 110))
                painter.drawEllipse(QPointF(cx, h / 2.0), 13.0, 13.0)
            painter.setPen(QPen(QColor(235, 240, 250), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(QPointF(cx - 4.5, h / 2.0), QPointF(cx + 4.5, h / 2.0))
            if zone == "plus":
                painter.drawLine(QPointF(cx, h / 2.0 - 4.5), QPointF(cx, h / 2.0 + 4.5))

        font = QFont("-apple-system")
        font.setPointSizeF(8.5)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255, 235 if self.hover == "mid" else 175))
        painter.drawText(QRectF(36, 0, w - 72, h), Qt.AlignmentFlag.AlignCenter.value,
                         f"{'Auto' if self.auto else 'Timing'}  {self.offset_ms / 1000.0:+.2f}s")

class TapSyncDialog(QDialog):
    """Play the song and press SPACE as each line starts: gives exact timestamps for imported text."""
    def __init__(self, win, title: str, lines, parent=None):
        super().__init__(parent or win)
        self.win = win
        self.lines = list(lines)
        self.stamps = []
        self.setWindowTitle("Tap-sync lyrics")
        self.resize(540, 580)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        head = QLabel(f"Tap-sync: {title}")
        head.setWordWrap(True)
        head.setStyleSheet("font-size: 11pt; font-weight: 600;")
        hint = QLabel("The song is playing. Press SPACE (or Tap) at the exact moment each highlighted line "
                      "starts. BACKSPACE undoes the last tap.")
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 9pt; color: rgba(150, 155, 165, 0.95);")

        self.list = QListWidget()
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for line in self.lines:
            self.list.addItem(QListWidgetItem(line))

        row = QHBoxLayout()
        self.btn_tap = QPushButton("Tap  (Space)")
        self.btn_undo = QPushButton("Undo")
        self.btn_restart = QPushButton("Restart song")
        self.btn_cancel = QPushButton("Cancel")
        self.btn_save = QPushButton("Save timing")
        for b in (self.btn_tap, self.btn_undo, self.btn_restart, self.btn_cancel, self.btn_save):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setAutoDefault(False)
        self.btn_tap.clicked.connect(self.tap)
        self.btn_undo.clicked.connect(self.undo)
        self.btn_restart.clicked.connect(self.restart)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_save.clicked.connect(self.accept)
        row.addWidget(self.btn_tap)
        row.addWidget(self.btn_undo)
        row.addWidget(self.btn_restart)
        row.addStretch(1)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_save)

        lay.addWidget(head)
        lay.addWidget(hint)
        lay.addWidget(self.list, 1)
        lay.addLayout(row)
        self.refresh()

    @staticmethod
    def fmt(ms: int) -> str:
        m, s = divmod(int(ms) // 1000, 60)
        return f"{m}:{s:02d}.{(int(ms) % 1000) // 10:02d}"

    def refresh(self):
        for i, line in enumerate(self.lines):
            stamp = f"[{self.fmt(self.stamps[i])}]" if i < len(self.stamps) else "  ·  "
            self.list.item(i).setText(f"{stamp}   {line}")
        self.list.setCurrentRow(min(len(self.stamps), len(self.lines) - 1))
        self.btn_save.setEnabled(len(self.stamps) == len(self.lines))

    def tap(self):
        if len(self.stamps) >= len(self.lines):
            return
        ts = max(0, int(self.win.player.position()) - 250)      # ~reaction time
        if self.stamps and ts < self.stamps[-1] + 100:
            ts = self.stamps[-1] + 100
        self.stamps.append(ts)
        self.refresh()

    def undo(self):
        if self.stamps:
            self.stamps.pop()
            self.refresh()

    def restart(self):
        self.stamps.clear()
        self.win.player.setPosition(0)
        self.win.player.play()
        self.refresh()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space:
            self.tap()
        elif event.key() == Qt.Key.Key_Backspace:
            self.undo()
        else:
            super().keyPressEvent(event)

    def result_lines(self):
        return [(ts, line) for ts, line in zip(self.stamps, self.lines)]

class AmbientBackdrop(QWidget):
    """Window backdrop with slowly drifting colour blobs so the glass has something to refract."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.is_dark = True

    def paintEvent(self, event):
        painter = QPainter(self)
        w, h = self.width(), self.height()
        painter.fillRect(self.rect(), QColor("#060709") if self.is_dark else QColor("#e9edf2"))
        ph = GLASS_PHASE
        radius = max(w, h) * 0.38
        blobs = (
            (0.10, 0.30, 0.0, QColor(235, 0, 41)),
            (0.45, 0.95, 2.1, QColor(255, 80, 120)),
            (0.02, 0.85, 4.2, QColor(110, 60, 230)),
            (0.90, 0.90, 1.2, QColor(235, 0, 41)),
        )
        for bx, by, k, col in blobs:
            cx = w * (bx + 0.12 * math.sin(ph * 0.6 + k))
            cy = h * (by - 0.30 * 0.5 + 0.15 * math.cos(ph * 0.45 + k * 2.0))
            g = QRadialGradient(cx, cy, radius)
            col.setAlpha(80 if self.is_dark else 45)
            g.setColorAt(0.0, col)
            col.setAlpha(0)
            g.setColorAt(1.0, col)
            painter.fillRect(self.rect(), QBrush(g))

class FrostedGlassFrame(QFrame):
    def __init__(self, radius=0.0, is_dark=True, parent=None):
        super().__init__(parent)
        self.radius = radius
        self.is_dark = is_dark
    def paintEvent(self, event):
        draw_apple_liquid_glass(QPainter(self), QRectF(self.rect()), radius=self.radius, is_dark=self.is_dark)

# -------------------------------------------------------------------------
# FLOATING LIQUID GLASS OVERLAY SEARCH WIDGET
# -------------------------------------------------------------------------
class AttachedSearchWidget(QWidget):
    def __init__(self, is_dark=True, parent=None):
        super().__init__(parent)
        self.is_dark = is_dark
        self.setFixedWidth(720)
        
        self.outer_frame = FrostedGlassFrame(radius=26.0, is_dark=self.is_dark)
        outer_layout = QVBoxLayout(self.outer_frame)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText('⌘ Type to search online music...')
        self.search_input.setFixedHeight(58)
        
        self.results_list = QListWidget()
        self.results_list.setFixedHeight(0)
        self.results_list.hide()
        
        outer_layout.addWidget(self.search_input)
        outer_layout.addWidget(self.results_list)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self.outer_frame)
        self.update_style()

    def animate_dropdown(self, show: bool, target_height: int = 240):
        if show:
            self.results_list.show()
            self.anim = QPropertyAnimation(self.results_list, b"maximumHeight")
            self.anim.setDuration(240)
            self.anim.setStartValue(0)
            self.anim.setEndValue(target_height)
            self.anim.setEasingCurve(QEasingCurve.Type.OutBack)
            self.anim.start()
        else:
            self.anim = QPropertyAnimation(self.results_list, b"maximumHeight")
            self.anim.setDuration(180)
            self.anim.setStartValue(self.results_list.height())
            self.anim.setEndValue(0)
            self.anim.setEasingCurve(QEasingCurve.Type.InCubic)
            self.anim.finished.connect(self.results_list.hide)
            self.anim.start()

    def update_style(self):
        fg = "#e2e8f0" if self.is_dark else "#1d1d1f"
        border_col = "rgba(255, 255, 255, 0.12)" if self.is_dark else "rgba(0, 0, 0, 0.1)"
        self.search_input.setStyleSheet(f"""
            QLineEdit {{
                background: transparent;
                border: none;
                border-bottom: 1px solid {border_col};
                border-top-left-radius: 26px;
                border-top-right-radius: 26px;
                padding: 6px 28px 0 28px;
                color: {fg};
                font-size: 11.5pt;
                font-family: '-apple-system', 'SF Pro Text', sans-serif;
            }}
        """)
        self.results_list.setStyleSheet(f"""
            QListWidget {{
                background: transparent;
                border: none;
                border-bottom-left-radius: 26px;
                border-bottom-right-radius: 26px;
                outline: none;
                padding: 10px;
            }}
            QListWidget::item {{
                padding: 12px 16px;
                border-radius: 10px;
                color: {fg};
            }}
            QListWidget::item:selected {{
                background: rgba(235, 0, 41, 0.45);
                color: #ffffff;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 6px;
                margin: 4px;
                border-radius: 3px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(150, 150, 150, 0.3);
                border-radius: 3px;
            }}
        """)

# -------------------------------------------------------------------------
# OXYGENMUSIC MAIN WINDOW
# -------------------------------------------------------------------------
class OxygenMusic(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OxygenMusic — OxygenOS Spatial UI")
        self.resize(1240, 820)
        self.setMinimumSize(960, 640)

        self.app_icon = create_app_icon()
        self.setWindowIcon(self.app_icon)

        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.85)

        self.current_filepath = ""
        self.current_title = "No track loaded"
        self.audio_worker = None
        self.stream_worker = None
        self.lyrics_worker = None
        self.lyrics_token = 0
        self.lyrics_title = ""
        self.audio_token = 0
        self.current_spectrum = None
        self.lyrics_kind = None      # "synced" / "plain" once lyrics are loaded
        self.lyrics_base = None
        self.auto_done = False
        self.manual_offset = False
        self._lyrics_workers = []
        self.is_dark_mode = True

        self.init_ui()
        self.init_tray()
        self.init_mpris()
        self.connect_signals()
        self.load_library_from_db()

        self.theme_shortcut = QShortcut(QKeySequence("Ctrl+T"), self)
        self.theme_shortcut.activated.connect(self.toggle_theme)
        self.sidebar_shortcut = QShortcut(QKeySequence("Ctrl+B"), self)
        self.sidebar_shortcut.activated.connect(self.toggle_sidebar)
        self._offset_shortcuts = []
        for keys, delta in (("Alt+Left", -250), ("Alt+Right", 250), ("Alt+0", None)):
            sc = QShortcut(QKeySequence(keys), self)
            sc.activated.connect(lambda d=delta: self.adjust_lyrics_offset(d))
            self._offset_shortcuts.append(sc)

        # drives the animated light on every glass surface + the drifting backdrop
        self.glass_timer = QTimer(self)
        self.glass_timer.timeout.connect(self.tick_glass)
        self.glass_timer.start(16)

    def init_ui(self):
        self.update_main_stylesheet()

        central = AmbientBackdrop()
        central.setObjectName("rootCanvas")
        self.backdrop = central
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Main horizontal layout container extending edge-to-edge
        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # Left Sidebar Container Wrapper with fixed initial width
        self.sidebar = FrostedGlassFrame(radius=0.0, is_dark=self.is_dark_mode)
        sb_layout = QVBoxLayout(self.sidebar)
        sb_layout.setContentsMargins(18, 20, 18, 18)
        sb_layout.setSpacing(12)

        self.logo_label = QLabel("OxygenMusic")
        self.logo_label.setStyleSheet("font-size: 16pt; font-weight: 700; color: #eb0029; letter-spacing: -0.3px;")
        sb_layout.addWidget(self.logo_label)

        lib_title = QLabel("Offline Library")
        lib_title.setStyleSheet("font-size: 8pt; font-weight: 600; color: rgba(226, 232, 240, 0.6); text-transform: uppercase;")
        sb_layout.addWidget(lib_title)

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter stored tracks...")
        self.filter_input.textChanged.connect(self.filter_library)
        sb_layout.addWidget(self.filter_input)

        self.track_list = QListWidget()
        self.track_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.track_list.customContextMenuRequested.connect(self.show_library_context_menu)
        self.update_library_list_style()
        sb_layout.addWidget(self.track_list, 1)

        self.sidebar_container = SlidingPanel(self.sidebar, 280)
        content_layout.addWidget(self.sidebar_container)
        self.sidebar_handle = SidebarResizeHandle(self.sidebar_container)
        content_layout.addWidget(self.sidebar_handle)

        # Center Area with Visualizer Stage
        center_wrapper = QWidget()
        cw_layout = QVBoxLayout(center_wrapper)
        cw_layout.setContentsMargins(0, 0, 0, 0)
        cw_layout.setSpacing(0)

        self.center_stack = QStackedWidget(center_wrapper)
        self.stage = PureVisualizerStage(is_dark=self.is_dark_mode)
        self.center_stack.addWidget(self.stage)

        overlay_container = QWidget()
        overlay_container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        overlay_layout = QVBoxLayout(overlay_container)
        overlay_layout.setContentsMargins(20, 22, 20, 20)
        overlay_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self.attached_search = AttachedSearchWidget(is_dark=self.is_dark_mode)
        self.attached_search.search_input.returnPressed.connect(self.start_online_search)
        self.attached_search.results_list.itemDoubleClicked.connect(self.stream_selected_online_track)
        
        overlay_layout.addWidget(self.attached_search)
        overlay_layout.addStretch(1)


        overlay_container.setParent(self.center_stack)
        overlay_container.setGeometry(self.center_stack.rect())
        self.sync_ctrl = LyricsSyncControl(overlay_container)
        self.sync_ctrl.adjust.connect(self.adjust_lyrics_offset)
        self.sync_ctrl.reset.connect(lambda: self.adjust_lyrics_offset(None))
        self.sync_ctrl.hide()

        def on_stack_resize(ev):
            overlay_container.resize(ev.size())
        self.center_stack.resizeEvent = on_stack_resize

        cw_layout.addWidget(self.center_stack, 1)
        content_layout.addWidget(center_wrapper, 1)
        root_layout.addLayout(content_layout, 1)

        # Bottom Playbar stretching edge-to-edge with strictly centered controls
        self.playbar = FrostedGlassFrame(radius=0.0, is_dark=self.is_dark_mode)
        pb_layout = QVBoxLayout(self.playbar)
        pb_layout.setContentsMargins(24, 12, 24, 14)
        pb_layout.setSpacing(8)

        prog_layout = QHBoxLayout()
        self.lbl_time_curr, self.lbl_time_total = QLabel("0:00"), QLabel("0:00")
        self.lbl_time_curr.setStyleSheet("color: rgba(150, 150, 150, 0.8); font-size: 8.5pt;")
        self.lbl_time_total.setStyleSheet("color: rgba(150, 150, 150, 0.8); font-size: 8.5pt;")
        self.slider_progress = Fluid120HzProgressBar()
        prog_layout.addWidget(self.lbl_time_curr)
        prog_layout.addWidget(self.slider_progress, 1)
        prog_layout.addWidget(self.lbl_time_total)
        pb_layout.addLayout(prog_layout)

        SIDE_W = 300  # left and right zones share the same width -> middle is dead-centre
        ctrl_layout = QHBoxLayout()
        ctrl_layout.setContentsMargins(0, 0, 0, 0)
        ctrl_layout.setSpacing(0)

        self.track_info_label = QLabel("No track loaded")
        self.track_info_label.setFixedWidth(SIDE_W)
        ctrl_layout.addWidget(self.track_info_label, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        ctrl_layout.addStretch(1)
        center_controls_widget = QWidget()
        cc_layout = QHBoxLayout(center_controls_widget)
        cc_layout.setContentsMargins(0, 0, 0, 0)
        cc_layout.setSpacing(14)
        cc_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.btn_prev, self.btn_play, self.btn_next = SkipButton(False), AnimatedPlayPauseButton(), SkipButton(True)
        self.btn_prev.clicked.connect(self.play_previous)
        self.btn_play.clicked.connect(self.toggle_playback)
        self.btn_next.clicked.connect(self.play_next)
        cc_layout.addWidget(self.btn_prev, 0, Qt.AlignmentFlag.AlignCenter)
        cc_layout.addWidget(self.btn_play, 0, Qt.AlignmentFlag.AlignCenter)
        cc_layout.addWidget(self.btn_next, 0, Qt.AlignmentFlag.AlignCenter)
        ctrl_layout.addWidget(center_controls_widget, 0, Qt.AlignmentFlag.AlignCenter)
        ctrl_layout.addStretch(1)

        vol_widget = QWidget()
        vol_widget.setFixedWidth(SIDE_W)
        vw_layout = QHBoxLayout(vol_widget)
        vw_layout.setContentsMargins(0, 0, 0, 0)
        vw_layout.setSpacing(8)
        self.vol_icon = AnimatedVolumeIcon()
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(85)
        self.vol_slider.setFixedWidth(90)
        self.vol_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.vol_slider.valueChanged.connect(lambda v: (self.audio_output.setVolume(v / 100.0), self.vol_icon.set_volume_level(v)))
        vw_layout.addStretch(1)
        vw_layout.addWidget(self.vol_icon)
        vw_layout.addWidget(self.vol_slider)

        ctrl_layout.addWidget(vol_widget, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        pb_layout.addLayout(ctrl_layout)
        root_layout.addWidget(self.playbar)

    def toggle_sidebar(self):
        self.sidebar_container.toggle()

    def tick_glass(self):
        global GLASS_PHASE
        GLASS_PHASE += 0.02
        self.backdrop.update()

    def update_library_list_style(self):
        if self.is_dark_mode:
            bg_list, border_list = "rgba(0, 0, 0, 0.22)", "rgba(255, 255, 255, 0.10)"
            hover_bg, hover_bd = "rgba(255, 255, 255, 0.07)", "rgba(255, 255, 255, 0.12)"
            sel_fg = "#ffffff"
        else:
            bg_list, border_list = "rgba(255, 255, 255, 0.34)", "rgba(0, 0, 0, 0.06)"
            hover_bg, hover_bd = "rgba(255, 255, 255, 0.26)", "rgba(0, 0, 0, 0.06)"
            sel_fg = "#ffffff"
        self.track_list.setStyleSheet(f"""
            QListWidget {{
                background: {bg_list};
                border: 1px solid {border_list};
                border-radius: 16px;
                font-size: 10pt;
                outline: none;
                padding: 4px;
            }}
            QListWidget::item {{
                padding: 10px 12px;
                border-radius: 12px;
                margin: 1px 2px;
                border: 1px solid transparent;
            }}
            QListWidget::item:hover {{
                background: {hover_bg};
                border: 1px solid {hover_bd};
            }}
            QListWidget::item:selected {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(255, 85, 120, 0.62), stop:1 rgba(235, 0, 41, 0.38));
                border: 1px solid rgba(255, 255, 255, 0.30);
                color: {sel_fg};
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 6px;
                margin: 4px;
                border-radius: 3px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(150, 150, 150, 0.3);
                border-radius: 3px;
            }}
        """)

    def update_main_stylesheet(self):
        if self.is_dark_mode:
            self.setStyleSheet("""
                QMainWindow { background-color: #060709; }
                QWidget#rootCanvas { background-color: #060709; }
                QWidget { color: #e2e8f0; font-family: '-apple-system', 'SF Pro Text', sans-serif; }
                QLineEdit { background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 12px; padding: 8px 12px; color: #ffffff; }
                QLineEdit:focus { border: 1.5px solid rgba(235, 0, 41, 0.6); }
                QPushButton { background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 10px; padding: 7px 14px; font-weight: 500; }
                QPushButton:hover { background: rgba(235, 0, 41, 0.2); border: 1px solid rgba(235, 0, 41, 0.6); }
            """)
        else:
            self.setStyleSheet("""
                QMainWindow { background-color: #e9edf2; }
                QWidget#rootCanvas { background-color: #e9edf2; }
                QWidget { color: #1d1d1f; font-family: '-apple-system', 'SF Pro Text', sans-serif; }
                QLineEdit { background: rgba(255, 255, 255, 0.52); border: 1px solid rgba(0, 0, 0, 0.08); border-radius: 12px; padding: 8px 12px; color: #1d1d1f; }
                QLineEdit:focus { border: 1.5px solid #eb0029; }
                QPushButton { background: rgba(255, 255, 255, 0.48); border: 1px solid rgba(0, 0, 0, 0.07); border-radius: 10px; padding: 7px 14px; font-weight: 500; }
                QPushButton:hover { background: rgba(255, 255, 255, 0.68); border: 1px solid rgba(235, 0, 41, 0.35); color: #9d1730; }
            """)

    def toggle_theme(self):
        self.is_dark_mode = not self.is_dark_mode
        self.update_main_stylesheet()
        self.sidebar.is_dark = self.is_dark_mode
        self.sidebar.update()
        self.playbar.is_dark = self.is_dark_mode
        self.playbar.update()
        self.attached_search.is_dark = self.is_dark_mode
        self.attached_search.outer_frame.is_dark = self.is_dark_mode
        self.attached_search.outer_frame.update()
        self.attached_search.update_style()
        self.stage.is_dark = self.is_dark_mode
        self.stage.update()
        self.backdrop.is_dark = self.is_dark_mode
        self.backdrop.update()
        self.update_library_list_style()

    def init_mpris(self):
        if not HAS_DBUS:
            return
        try:
            bus = QDBusConnection.sessionBus()
            bus.registerService("org.mpris.MediaPlayer2.oxygenmusic")
            bus.registerObject("/org/mpris/MediaPlayer2", self)
            self.mpris_root = MprisRootAdaptor(self)
            self.mpris_player = MprisPlayerAdaptor(self)
        except Exception:
            pass

    def init_tray(self):
        self.tray = QSystemTrayIcon(self.app_icon, self)
        menu = QMenu()
        menu.addAction("Toggle Dark / Light (Ctrl+T)", self.toggle_theme)
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
        if hasattr(self, "sidebar_container"):
            self.sidebar_container.set_width(self.sidebar_container.width())  # re-clamp to the new window width

    def connect_signals(self):
        self.player.positionChanged.connect(lambda pos: (self.lbl_time_curr.setText(self.fmt(pos)), self.slider_progress.sync_hardware_pos(pos), self.stage.sync_to_timestamp(pos)))
        self.player.durationChanged.connect(lambda dur: (self.slider_progress.set_duration(dur), self.lbl_time_total.setText(self.fmt(dur))))
        self.player.playbackStateChanged.connect(lambda st: (
            self.btn_play.set_playing(st == QMediaPlayer.PlaybackState.PlayingState),
            self.stage.set_playing(st == QMediaPlayer.PlaybackState.PlayingState)))
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)
        self.slider_progress.position_seek.connect(self.player.setPosition)
        self.track_list.itemDoubleClicked.connect(lambda item: self.play_track_at_row(self.track_list.row(item)))

    def show_library_context_menu(self, pos):
        item = self.track_list.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        
        info_action = QAction("ℹ️ View Track Details", self)
        info_action.triggered.connect(lambda: self.show_track_details(item))
        
        delete_action = QAction("🗑 Delete Track from Library", self)
        delete_action.triggered.connect(lambda: self.delete_selected_track(item))
        
        song_title = item.data(Qt.ItemDataRole.ToolTipRole) or item.text().replace("♫  ", "")
        menu.addAction(info_action)
        if load_imported_lyrics(song_title):
            tap_action = QAction("⏱ Tap-Sync Lyrics…", self)
            tap_action.triggered.connect(lambda: self.tap_sync_lyrics_for(song_title))
            menu.addAction(tap_action)
            remove_action = QAction("✖ Remove Imported Lyrics", self)
            remove_action.triggered.connect(lambda: self.remove_imported_lyrics_for(song_title))
            menu.addAction(remove_action)
        menu.addSeparator()
        menu.addAction(delete_action)
        menu.exec(self.track_list.mapToGlobal(pos))

    def show_track_details(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        title = item.data(Qt.ItemDataRole.ToolTipRole) or item.text().replace("♫  ", "")
        
        file_size = "Unknown"
        file_ext = Path(path).suffix.upper().replace(".", "")
        if os.path.exists(path):
            size_bytes = os.path.getsize(path)
            if size_bytes > 1024 * 1024:
                file_size = f"{size_bytes / (1024 * 1024):.2f} MB"
            else:
                file_size = f"{size_bytes / 1024:.2f} KB"

        details_msg = (
            f"<b>Title:</b> {title}<br><br>"
            f"<b>Format:</b> {file_ext} Audio<br>"
            f"<b>File Size:</b> {file_size}<br><br>"
            f"<b>File Path:</b><br>{path}"
        )
        
        QMessageBox.information(self, "Track Details", details_msg)

    def delete_selected_track(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        title = item.data(Qt.ItemDataRole.ToolTipRole) or item.text().replace("♫  ", "")
        
        reply = QMessageBox.question(
            self, "Delete Track",
            f"Are you sure you want to delete '{title}' from your library and disk?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            if self.current_filepath == path:
                self.player.stop()
                self.player.setSource(QUrl())
                self.set_display_title("No track loaded")
            try:
                if os.path.exists(path):
                    os.remove(path)
                conn = sqlite3.connect(DB_PATH)
                conn.cursor().execute("DELETE FROM songs WHERE filepath = ?", (path,))
                conn.commit()
                conn.close()
                self.load_library_from_db()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Could not delete track:\n{e}")

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
                self.start_audio_analysis(path)

                self.fetch_lyrics(title)
    def refresh_translate_button(self):
        # No translation/import controls are displayed on the visualizer.
        synced = self.stage.lyrics_state == "ok"
        self.sync_ctrl.setVisible(synced)
        if synced:
            w, h = self.center_stack.width(), self.center_stack.height()
            self.sync_ctrl.move(int((w - self.sync_ctrl.width()) / 2), int(h - 28 - 34 - 8))
            self.sync_ctrl.raise_()
            self.sync_ctrl.set_offset(self.stage.lyric_offset_ms, auto=self.auto_done and not self.manual_offset)



    def start_audio_analysis(self, source: str):
        self.audio_token += 1
        token = self.audio_token
        self.current_spectrum = None
        worker = AudioAnalysisWorker(source)
        worker.analysis_ready.connect(lambda spec, t=token: self.on_spectrum_ready(spec, t))
        self.audio_worker = worker
        worker.start()

    def on_spectrum_ready(self, spec, token: int):
        if token != self.audio_token:
            return
        self.stage.set_spectrum(spec)
        self.current_spectrum = spec
        self.auto_align_lyrics()

    def auto_align_lyrics(self):
        """Once both the lyrics and the song's audio analysis are ready, sync them automatically."""
        if self.auto_done or self.lyrics_kind is None or self.current_spectrum is None:
            return
        try:
            flux = onset_flux(self.current_spectrum)
            if self.lyrics_kind == "plain":
                aligned = auto_align_plain(self.lyrics_base, self.player.duration(), flux)
                if aligned:
                    self.stage.set_lyrics(aligned)
            elif not self.manual_offset:
                # First correct the LRC against the actual audio, then apply the
                # remaining global drift.  This is substantially tighter than
                # applying one offset to the whole song.
                refined = refine_synced_timing(self.lyrics_base, flux)
                self.lyrics_base = refined
                self.stage.set_lyrics(refined)
                residual = auto_calibrate_offset([t for t, _ in refined], flux)
                if residual:
                    shifted = [(max(0, int(t + residual)), line) for t, line in refined]
                    self.lyrics_base = shifted
                    self.stage.set_lyrics(shifted)
                    self.stage.set_lyric_offset(0)
                else:
                    self.stage.set_lyric_offset(0)
            self.auto_done = True
        except Exception:
            self.auto_done = True      # never let auto-sync break playback
        self.refresh_translate_button()

    def adjust_lyrics_offset(self, delta):
        """Manual timing nudge for the current song (saved). Reset (None) hands control back to automatic sync."""
        if not self.lyrics_title or not self.stage.lyrics:
            return
        if delta is None:
            try:
                delete_lyric_offset(self.lyrics_title)
            except Exception:
                pass
            self.manual_offset = False
            self.stage.set_lyric_offset(0)
            self.auto_done = False
            self.auto_align_lyrics()
            self.refresh_translate_button()
            return
        new = max(-15000, min(15000, self.stage.lyric_offset_ms + int(delta)))
        self.manual_offset = True
        self.stage.set_lyric_offset(new)
        self.sync_ctrl.set_offset(new, auto=False)
        try:
            save_lyric_offset(self.lyrics_title, new)
        except Exception:
            pass

    def tap_sync_lyrics_for(self, title: str):
        got = load_imported_lyrics(title)
        if not got:
            QMessageBox.information(self, "Tap-sync", "Import lyrics for this song first.")
            return
        if title != self.lyrics_title or self.player.source().isEmpty():
            QMessageBox.information(self, "Tap-sync", "Play this song first, then start Tap-sync.")
            return
        kind, lines = got
        texts = [str(x[1]) if kind == "synced" else str(x) for x in lines]
        self.player.setPosition(0)
        self.player.play()
        dlg = TapSyncDialog(self, title, texts)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            try:
                save_imported_lyrics(title, "synced", [[ms, t] for ms, t in dlg.result_lines()])
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Could not save timing:\n{e}")
                return
            self.lyrics_token += 1
            self.try_load_imported(title)

    def try_load_imported(self, title: str) -> bool:
        """Use lyrics the user imported for this song (they win over any online lookup)."""
        got = load_imported_lyrics(title)
        if not got:
            return False
        kind, lines = got
        if kind == "synced":
            timed = [(int(t), str(x)) for t, x in lines]
        else:
            timed = spread_plain_lines([str(x) for x in lines], self.player.duration())
        self.stage.set_lyrics(timed)
        self.lyrics_kind = kind
        self.lyrics_base = timed if kind == "synced" else [str(x) for x in lines]
        self.auto_done = False
        self.refresh_translate_button()
        self.auto_align_lyrics()
        return True

    def import_lyrics_for(self, title: str):
        if not title:
            return
        dlg = LyricsImportDialog(title, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        kind, lines = parse_user_lyrics(dlg.text())
        if not kind:
            QMessageBox.warning(self, "Import lyrics", "No lyrics found in that text.")
            return
        try:
            save_imported_lyrics(title, kind, lines)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not save lyrics:\n{e}")
            return
        if title == self.lyrics_title:
            self.lyrics_token += 1          # cancel any lookup still in flight
            self.try_load_imported(title)
        if kind == "plain":
            if title == self.lyrics_title:
                ask = QMessageBox.question(
                    self, "Lyrics saved",
                    "Saved. These lyrics have no timestamps, so they are only roughly spread across the song.\n\n"
                    "Tap-sync now to match them exactly to the music?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes)
                if ask == QMessageBox.StandardButton.Yes:
                    self.tap_sync_lyrics_for(title)
            else:
                QMessageBox.information(self, "Lyrics saved",
                                        "Saved. These lyrics have no timestamps, so they are only roughly spread across "
                                        "the song. Play the song, then right-click it in the library and choose "
                                        "Tap-Sync Lyrics for exact timing.")

    def remove_imported_lyrics_for(self, title: str):
        try:
            delete_imported_lyrics(title)
        except Exception:
            return
        if title == self.lyrics_title:
            self.fetch_lyrics(title)

    def open_translation_search(self):
        """Open a web search for an English translation (nothing is fetched or stored by the app)."""
        artist, track = LyricsWorker.split_title(self.lyrics_title)
        query = f"{artist} {track} english translation lyrics".strip()
        QDesktopServices.openUrl(QUrl("https://www.google.com/search?q=" + quote_plus(query)))

    def fetch_lyrics(self, title: str):
        """Look up the real synced lyrics for the song that just started."""
        self.lyrics_title = title
        self.sync_ctrl.hide()
        saved = load_lyric_offset(title)
        self.manual_offset = saved is not None          # a fix you saved yourself beats the automatic one
        self.stage.lyric_offset_ms = saved or 0
        self.lyrics_kind, self.lyrics_base, self.auto_done = None, None, False
        self.lyrics_token += 1
        token = self.lyrics_token
        self.stage.set_lyrics_state("loading")
        # short delay so the player knows the track duration (used to pick the right version)
        QTimer.singleShot(800, lambda: self._launch_lyrics(token, title, 0))

    def _launch_lyrics(self, token: int, title: str, tries: int = 0):
        if token != self.lyrics_token:
            return  # a newer song already started
        dur = self.player.duration()
        if dur <= 0 and tries < 4:   # duration not known yet -> wait a little
            QTimer.singleShot(700, lambda: self._launch_lyrics(token, title, tries + 1))
            return
        if self.try_load_imported(title):
            return  # the user's own lyrics for this song
        worker = LyricsWorker(title, dur / 1000.0 if dur > 0 else 0.0, token)
        worker.lyrics_ready.connect(self.on_lyrics_ready)
        self._lyrics_workers = [w for w in self._lyrics_workers if w.isRunning()] + [worker]
        self.lyrics_worker = worker
        worker.start()

    def on_lyrics_ready(self, lyrics, token):
        if token == self.lyrics_token:
            self.stage.set_lyrics(lyrics)
            self.lyrics_kind = "synced" if lyrics else None
            self.lyrics_base = list(lyrics)
            self.auto_done = False
            self.refresh_translate_button()
            self.auto_align_lyrics()

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
        q = self.attached_search.search_input.text().strip()
        if not q:
            self.attached_search.animate_dropdown(False)
            return
        
        self.attached_search.results_list.clear()
        self.attached_search.results_list.addItem(QListWidgetItem("Searching online..."))
        self.attached_search.animate_dropdown(True, target_height=60)

        self.search_worker = SearchWorker(q)
        self.search_worker.results_signal.connect(self.handle_search_results_finished)
        self.search_worker.start()

    def handle_search_results_finished(self, success, res, err):
        self.attached_search.results_list.clear()
        if not success or not res:
            self.attached_search.results_list.addItem(QListWidgetItem("No results found."))
            self.attached_search.animate_dropdown(True, target_height=60)
            return

        for r in res:
            list_item = QListWidgetItem(self.attached_search.results_list)
            widget = SearchResultItemWidget(r['title'], r['duration'], r['url'], is_dark=self.is_dark_mode)
            widget.download_clicked.connect(self.download_track_from_url)
            
            list_item.setSizeHint(widget.sizeHint())
            self.attached_search.results_list.setItemWidget(list_item, widget)
            list_item.setData(Qt.ItemDataRole.UserRole, r['url'])
            list_item.setData(Qt.ItemDataRole.ToolTipRole, r['title'])

        calc_height = min(320, len(res) * 58 + 16)
        self.attached_search.animate_dropdown(True, target_height=calc_height)

    def stream_selected_online_track(self, item):
        url = item.data(Qt.ItemDataRole.UserRole)
        title = item.data(Qt.ItemDataRole.ToolTipRole) or "Online Stream"
        if not url:
            return
        self.attached_search.animate_dropdown(False)
        self.attached_search.search_input.setText("Loading stream...")

        if self.stream_worker and self.stream_worker.isRunning():
            self.stream_worker.wait(50)
        self.stream_worker = StreamUrlWorker(url)
        self.stream_worker.stream_ready.connect(lambda s, su, t, e: self.handle_stream_ready(s, su, title if title != "Online Stream" else t, e))
        self.stream_worker.start()

    def handle_stream_ready(self, success, stream_url, title, err):
        if success:
            self.player.setSource(QUrl(stream_url))
            self.player.play()
            self.set_display_title(f"⚡ Streaming: {title}")
            self.attached_search.search_input.clear()
            self.attached_search.search_input.setPlaceholderText('⌘ Type to search online music...')

            if self.audio_worker and self.audio_worker.isRunning():
                self.audio_worker.stop()
                self.audio_worker.wait(50)
            self.start_audio_analysis(stream_url)

            self.fetch_lyrics(title)
        else:
            QMessageBox.critical(self, "Streaming Failed", f"Could not stream track:\n{err}")
            self.attached_search.search_input.clear()

    def download_track_from_url(self, url):
        self.attached_search.animate_dropdown(False)
        self.attached_search.search_input.setText("Downloading to library...")

        self.download_worker = DownloadWorker(url)
        self.download_worker.finished_signal.connect(self.handle_download_finished)
        self.download_worker.start()

    def handle_download_finished(self, success, path, title):
        if success:
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.cursor().execute("INSERT OR REPLACE INTO songs (title, filepath) VALUES (?, ?)", (title, path))
                conn.commit()
                conn.close()
                self.load_library_from_db()
                self.attached_search.search_input.clear()
                self.attached_search.search_input.setPlaceholderText('⌘ Type to search online music...')
                QMessageBox.information(self, "Download Complete", f"Added to library:\n{title}")
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))
        else:
            QMessageBox.critical(self, "Download Failed", f"Could not download track:\n{path}")
            self.attached_search.search_input.clear()

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

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = OxygenMusic()
    window.show()
    sys.exit(app.exec())
