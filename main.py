#!/usr/bin/env python3
"""
OCR Data Labeling & Cropping Tool V2
=====================================
Desktop GUI for efficient *vertical* image cropping and text labeling.

Workflow:  open boundary starts at X=0 → move cursor with Arrow keys →
           press O to close crop → label → repeat.
"""
import warnings
# Suppress the SIP deprecation warning
warnings.filterwarnings("ignore", category=DeprecationWarning, module=".*sip.*")



# ── IME support (must be set BEFORE importing Qt) ──
import os
os.environ["QT_IM_MODULE"] = "fcitx"
os.environ["XMODIFIERS"] = "@im=fcitx"
os.environ["GTK_IM_MODULE"] = "fcitx"

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QPushButton, QLabel, QLineEdit, QTextEdit, QListWidget,
    QScrollArea, QFrame, QFileDialog, QStatusBar, QGraphicsView,
    QGraphicsScene, QProgressBar,
)
from PyQt6.QtCore import Qt, QRectF, QTimer, pyqtSignal, QEvent, QThread
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QColor, QPixmap, QFont, QKeySequence,
    QPalette, QWheelEvent, QShortcut,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

C = {
    "bg_darkest":   "#0d0d1a",
    "bg_dark":      "#131325",
    "bg_main":      "#1a1a2e",
    "bg_surface":   "#242440",
    "bg_elevated":  "#2d2d4a",
    "border":       "#3d3d60",
    "border_hover": "#555580",
    "accent":       "#7c3aed",
    "accent_light": "#a78bfa",
    "accent_hover": "#9333ea",
    "green":        "#10b981",
    "red":          "#ef4444",
    "orange":       "#f59e0b",
    "gold":         "#fbbf24",
    "text":         "#e2e8f0",
    "text_muted":   "#94a3b8",
    "text_dim":     "#64748b",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}
BATCH_SIZE = 10
MIN_CROP_WIDTH = 3   # minimum pixels between open and cursor to allow a crop


# ═══════════════════════════════════════════════════════════════════════════════
#  DATA MODEL
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class CropSegment:
    """One cropped vertical strip from the original image."""
    index: int
    x_start: int          # left pixel column  (was the open boundary)
    x_end: int            # right pixel column  (where O was pressed)
    cropped_pixmap: QPixmap
    label_text: str = ""
    is_saved: bool = False


class CropSession:
    """Manages all crop segments for the currently loaded original image."""

    def __init__(self, image_path: str, pixmap: QPixmap):
        self.image_path = image_path
        self.original_pixmap = pixmap
        self.segments: List[CropSegment] = []
        self.crop_counter: int = 0

    def add_segment(self, x_a: int, x_b: int) -> CropSegment:
        self.crop_counter += 1
        x_left = max(0, min(x_a, x_b))
        x_right = min(self.original_pixmap.width(), max(x_a, x_b))
        w = max(1, x_right - x_left)
        h = self.original_pixmap.height()
        cropped = self.original_pixmap.copy(x_left, 0, w, h)
        seg = CropSegment(
            index=self.crop_counter,
            x_start=x_left,
            x_end=x_right,
            cropped_pixmap=cropped,
        )
        self.segments.append(seg)
        return seg

    def get_segment(self, index: int) -> Optional[CropSegment]:
        return next((s for s in self.segments if s.index == index), None)

    def remove_last(self) -> Optional[CropSegment]:
        if self.segments:
            seg = self.segments.pop()
            self.crop_counter = max(0, self.crop_counter - 1)
            return seg
        return None

    @property
    def saved_count(self) -> int:
        return sum(1 for s in self.segments if s.is_saved)

    @property
    def total_count(self) -> int:
        return len(self.segments)


# ═══════════════════════════════════════════════════════════════════════════════
#  WORKER THREAD — DIRECTORY SCANNER
# ═══════════════════════════════════════════════════════════════════════════════

class DirectoryScanner(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(list)

    def __init__(self, folder: str, parent=None):
        super().__init__(parent)
        self._folder = folder

    def run(self):
        files: List[str] = []
        count = 0
        try:
            for entry in sorted(Path(self._folder).iterdir()):
                if entry.is_file() and entry.suffix.lower() in IMAGE_EXTS:
                    files.append(str(entry))
                    count += 1
                    if count % 200 == 0:
                        self.progress.emit(count)
        except OSError:
            pass
        self.finished.emit(files)


# ═══════════════════════════════════════════════════════════════════════════════
#  CANVAS WIDGET — VERTICAL CROPPING  (O-only workflow)
# ═══════════════════════════════════════════════════════════════════════════════

class ImageCanvasWidget(QGraphicsView):
    """
    Open-boundary starts at X=0.  User positions the cursor (Arrow keys or
    mouse click) then presses **O** to close the crop.  The O-position
    becomes the next open-boundary.

    Visual elements:
        • Green solid line   = open boundary
        • Gold dashed line   = interactive cursor  (Arrow / click)
        • Green tint         = region that will be cropped on next O
        • Gray overlay       = already-saved crops
    """

    crop_created = pyqtSignal(object)        # CropSegment
    cursor_moved = pyqtSignal(int, int, int)  # open_x, cursor_x, width

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        # Graphics items
        self._pixmap_item = None
        self._open_line = None
        self._open_label = None
        self._cursor_line = None
        self._cursor_label = None
        self._highlight = None
        self._overlay_pairs: list = []

        # State
        self._open_x: float = 0.0
        self._cursor_x: float = 0.0
        self._user_zoomed: bool = False
        self._session: Optional[CropSession] = None

        # Config
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setBackgroundBrush(QBrush(QColor(C["bg_darkest"])))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ── public API ──

    def load_image(self, pixmap: QPixmap, session: CropSession):
        self._scene.clear()
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self._session = session
        self._open_line = self._open_label = None
        self._cursor_line = self._cursor_label = None
        self._highlight = None
        self._overlay_pairs.clear()
        self._user_zoomed = False
        self._open_x = 0.0
        self._cursor_x = 0.0
        self.resetTransform()
        self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        self._draw_open_line()
        self._draw_cursor()
        self._refresh_highlight()

    def move_cursor(self, delta: int):
        """Move the cursor by *delta* pixels (negative = left)."""
        if not self._pixmap_item:
            return
        self._cursor_x = self._clamp_x(self._cursor_x + delta)
        self._draw_cursor()
        self._refresh_highlight()
        self._emit_cursor_info()
        self.ensureVisible(QRectF(self._cursor_x - 10, 0, 20, 20),
                           xMargin=60, yMargin=0)

    def set_cursor_pos(self, x: float):
        """Set the cursor to an exact X (used by mouse click)."""
        if not self._pixmap_item:
            return
        self._cursor_x = self._clamp_x(x)
        self._draw_cursor()
        self._refresh_highlight()
        self._emit_cursor_info()

    def commit_crop(self):
        """Press O: crop from open_x → cursor_x, advance open_x."""
        if not self._pixmap_item or not self._session:
            return False
        w = abs(self._cursor_x - self._open_x)
        if w < MIN_CROP_WIDTH:
            return False
        left = min(self._open_x, self._cursor_x)
        right = max(self._open_x, self._cursor_x)
        seg = self._session.add_segment(int(left), int(right))
        # Advance open boundary
        self._open_x = right
        self._draw_open_line()
        self._draw_cursor()
        self._refresh_highlight()
        self._emit_cursor_info()
        self.crop_created.emit(seg)
        return True

    def set_open_x(self, x: float):
        """Restore the open boundary (used by undo)."""
        self._open_x = x
        self._draw_open_line()
        self._refresh_highlight()
        self._emit_cursor_info()

    def restore_boundaries(self, open_x: float, cursor_x: float):
        """Restore both the open boundary and cursor position (used by undo)."""
        self._open_x = open_x
        self._cursor_x = cursor_x
        self._draw_open_line()
        self._draw_cursor()
        self._refresh_highlight()
        self._emit_cursor_info()

    def set_open_to_cursor(self):
        """Set the open boundary to the current cursor position (I-key)."""
        if not self._pixmap_item:
            return
        self._open_x = self._cursor_x
        self._draw_open_line()
        self._refresh_highlight()
        self._emit_cursor_info()

    def add_overlay(self, seg: CropSegment):
        if not self._pixmap_item:
            return
        h = self._img_h()
        r = QRectF(seg.x_start, 0, seg.x_end - seg.x_start, h)
        brush = QBrush(QColor(100, 100, 160, 50))
        pen = QPen(QColor(150, 150, 190, 110), 1.5, Qt.PenStyle.DotLine)
        ri = self._scene.addRect(r, pen, brush)
        ri.setZValue(5)
        font = QFont("Inter", 12, QFont.Weight.Bold)
        ti = self._scene.addSimpleText(f"#{seg.index}", font)
        ti.setBrush(QBrush(QColor(210, 210, 240, 190)))
        ti.setPos(seg.x_start + 4, 8)
        ti.setZValue(6)
        self._overlay_pairs.append((ri, ti))

    def remove_last_overlay(self):
        if self._overlay_pairs:
            ri, ti = self._overlay_pairs.pop()
            self._rm(ri); self._rm(ti)

    def clear_marks(self):
        """Clear cursor highlight (after save, before returning focus)."""
        self._rm(self._highlight); self._highlight = None

    # ── Qt events ──

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pixmap_item:
            pos = self.mapToScene(event.pos())
            self.set_cursor_pos(pos.x())
        else:
            super().mousePressEvent(event)

    def wheelEvent(self, event: QWheelEvent):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            f = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
            self.scale(f, f)
            self._user_zoomed = True
        else:
            super().wheelEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._pixmap_item and not self._user_zoomed:
            self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_F and self._pixmap_item:
            self.resetTransform()
            self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
            self._user_zoomed = False
        else:
            super().keyPressEvent(event)

    # ── drawing helpers ──

    def _draw_open_line(self):
        self._rm(self._open_line)
        self._rm(self._open_label)
        if not self._pixmap_item:
            return
        h = self._img_h()
        x = self._open_x
        pen = QPen(QColor(C["green"]), 2.5, Qt.PenStyle.SolidLine)
        self._open_line = self._scene.addLine(x, 0, x, h, pen)
        self._open_line.setZValue(12)

    def _draw_cursor(self):
        self._rm(self._cursor_line)
        self._rm(self._cursor_label)
        if not self._pixmap_item:
            return
        h = self._img_h()
        x = self._cursor_x
        pen = QPen(QColor(C["gold"]), 2, Qt.PenStyle.DashLine)
        self._cursor_line = self._scene.addLine(x, 0, x, h, pen)
        self._cursor_line.setZValue(15)

    def _refresh_highlight(self):
        self._rm(self._highlight); self._highlight = None
        if not self._pixmap_item:
            return
        xl, xr = sorted((self._open_x, self._cursor_x))
        if xr - xl < 1:
            return
        r = QRectF(xl, 0, xr - xl, self._img_h())
        self._highlight = self._scene.addRect(
            r, QPen(Qt.PenStyle.NoPen), QBrush(QColor(16, 185, 129, 30))
        )
        self._highlight.setZValue(8)

    # ── util ──

    def _img_w(self) -> int:
        return self._pixmap_item.pixmap().width() if self._pixmap_item else 0

    def _img_h(self) -> int:
        return self._pixmap_item.pixmap().height() if self._pixmap_item else 0

    def _clamp_x(self, x: float) -> float:
        return max(0.0, min(x, float(self._img_w()))) if self._pixmap_item else x

    def _emit_cursor_info(self):
        self.cursor_moved.emit(
            int(self._open_x), int(self._cursor_x),
            int(abs(self._cursor_x - self._open_x))
        )

    @staticmethod
    def _rm(item):
        if item is not None and item.scene() is not None:
            item.scene().removeItem(item)


# ═══════════════════════════════════════════════════════════════════════════════
#  CROP THUMBNAIL CARD
# ═══════════════════════════════════════════════════════════════════════════════

class CropThumbnailCard(QFrame):
    clicked = pyqtSignal(int)

    def __init__(self, segment: CropSegment, parent=None):
        super().__init__(parent)
        self.idx = segment.index
        self.setFixedSize(130, 95)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._active = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(5, 5, 5, 4)
        lay.setSpacing(3)

        thumb = segment.cropped_pixmap.scaled(
            120, 52,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._thumb = QLabel()
        self._thumb.setPixmap(thumb)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setFixedHeight(55)
        self._thumb.setStyleSheet("background:transparent;border:none;")
        lay.addWidget(self._thumb)

        row = QHBoxLayout(); row.setSpacing(4)
        self._idx_lbl = QLabel(f"#{segment.index}")
        self._idx_lbl.setStyleSheet(
            f"color:{C['text_muted']};font-size:11px;font-weight:600;"
            "background:transparent;border:none;"
        )
        row.addWidget(self._idx_lbl); row.addStretch()
        self._st = QLabel()
        self._st.setStyleSheet("background:transparent;border:none;")
        self.update_status(segment.is_saved)
        row.addWidget(self._st)
        lay.addLayout(row)
        self._restyle()

    def update_status(self, saved: bool):
        col = C["green"] if saved else C["orange"]
        txt = "✓ OK" if saved else "● Edit"
        self._st.setText(txt)
        self._st.setStyleSheet(
            f"color:{col};font-size:11px;font-weight:bold;"
            "background:transparent;border:none;"
        )

    def set_active(self, v: bool):
        self._active = v; self._restyle()

    def _restyle(self):
        bc = C["accent"] if self._active else C["border"]
        bw = 2 if self._active else 1
        bg = C["bg_elevated"] if self._active else C["bg_surface"]
        self.setStyleSheet(
            f"CropThumbnailCard{{background:{bg};border:{bw}px solid {bc};"
            f"border-radius:8px;}}"
            f"CropThumbnailCard:hover{{border-color:{C['accent_light']};}}"
        )

    def mousePressEvent(self, event):
        self.clicked.emit(self.idx)


# ═══════════════════════════════════════════════════════════════════════════════
#  CROP HISTORY GRID
# ═══════════════════════════════════════════════════════════════════════════════

class CropHistoryGrid(QWidget):
    card_clicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(132)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        hdr = QLabel("  ✂  CROPPED HISTORY")
        hdr.setStyleSheet(
            f"color:{C['text_dim']};font-size:11px;font-weight:bold;"
            f"letter-spacing:1.5px;padding:2px 0;"
        )
        outer.addWidget(hdr)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFixedHeight(107)
        self._scroll.setStyleSheet(
            f"QScrollArea{{border:1px solid {C['border']};border-radius:8px;"
            f"background:{C['bg_dark']};}}"
            f"QScrollBar:horizontal{{height:6px;background:transparent;}}"
            f"QScrollBar::handle:horizontal{{background:{C['border']};"
            f"border-radius:3px;min-width:30px;}}"
            f"QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal"
            f"{{width:0;height:0;}}"
        )
        self._box = QWidget()
        self._box.setStyleSheet("background:transparent;")
        self._hlay = QHBoxLayout(self._box)
        self._hlay.setContentsMargins(6, 6, 6, 6)
        self._hlay.setSpacing(8)
        self._hlay.addStretch()
        self._scroll.setWidget(self._box)
        outer.addWidget(self._scroll)
        self._cards: dict[int, CropThumbnailCard] = {}

    def add_card(self, seg: CropSegment):
        card = CropThumbnailCard(seg)
        card.clicked.connect(self._on_click)
        self._hlay.insertWidget(self._hlay.count() - 1, card)
        self._cards[seg.index] = card
        QTimer.singleShot(50, self._scroll_end)

    def update_card_status(self, idx: int, saved: bool):
        if idx in self._cards:
            self._cards[idx].update_status(saved)

    def set_active_card(self, idx: int):
        for k, c in self._cards.items():
            c.set_active(k == idx)

    def remove_last_card(self):
        if self._cards:
            last = max(self._cards)
            card = self._cards.pop(last)
            self._hlay.removeWidget(card); card.deleteLater()

    def clear_all(self):
        for c in self._cards.values():
            self._hlay.removeWidget(c); c.deleteLater()
        self._cards.clear()

    def _on_click(self, idx):
        self.set_active_card(idx); self.card_clicked.emit(idx)

    def _scroll_end(self):
        sb = self._scroll.horizontalScrollBar(); sb.setValue(sb.maximum())


# ═══════════════════════════════════════════════════════════════════════════════
#  LABEL ZONE WIDGET
# ═══════════════════════════════════════════════════════════════════════════════

class LabelZoneWidget(QWidget):
    label_committed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        hdr = QLabel("  🏷  ACTIVE LABEL ZONE")
        hdr.setStyleSheet(
            f"color:{C['text_dim']};font-size:11px;font-weight:bold;"
            f"letter-spacing:1.5px;padding:2px 0;"
        )
        lay.addWidget(hdr)

        box = QFrame()
        box.setStyleSheet(
            f"QFrame{{background:{C['bg_dark']};border:1px solid {C['border']};"
            f"border-radius:8px;}}"
        )
        blay = QVBoxLayout(box)
        blay.setContentsMargins(10, 8, 10, 8)
        blay.setSpacing(6)

        self._preview = QLabel("No crop selected")
        self._preview.setFixedHeight(60)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet(
            f"color:{C['text_dim']};background:{C['bg_darkest']};"
            f"border:1px dashed {C['border']};border-radius:6px;font-size:12px;"
        )
        blay.addWidget(self._preview)

        irow = QHBoxLayout(); irow.setSpacing(8)
        lbl = QLabel("Label:")
        lbl.setStyleSheet(
            f"color:{C['text_muted']};font-weight:bold;font-size:13px;border:none;"
        )
        irow.addWidget(lbl)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Type text for current crop…  (Enter to save)")
        self._input.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self._input.setStyleSheet(
            f"QLineEdit{{background:{C['bg_main']};border:2px solid {C['border']};"
            f"border-radius:6px;color:{C['text']};padding:6px 10px;font-size:14px;"
            f"selection-background-color:{C['accent']};}}"
            f"QLineEdit:focus{{border-color:{C['accent']};}}"
        )
        self._input.returnPressed.connect(lambda: self.label_committed.emit(self.text))
        irow.addWidget(self._input, 1)

        self._MAX_CHARS = 25                           # Configurable parameter
        self._counter = QLabel(f"0 / {self._MAX_CHARS}")
        self._counter.setStyleSheet(
            f"color:{C['text_muted']};font-size:12px;font-weight:600;"
            f"background:transparent;border:none;padding-right:4px;"
        )
        self._input.textChanged.connect(self._update_counter)
        irow.addWidget(self._counter)

        blay.addLayout(irow)
        lay.addWidget(box)
        self._active_idx: Optional[int] = None

    def load_segment(self, seg: CropSegment):
        self._active_idx = seg.index
        pw = max(self._preview.width() - 20, 200)
        scaled = seg.cropped_pixmap.scaled(
            pw, 50,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._preview.setPixmap(scaled)
        self._input.setText(seg.label_text)
        self._input.setFocus()
        if not seg.label_text:
            self._input.selectAll()

    def clear(self):
        self._active_idx = None
        self._preview.clear()
        self._preview.setText("No crop selected")
        self._input.clear()

    def _update_counter(self, text: str):
        self._counter.setText(f"{len(text)} / {self._MAX_CHARS}")

    @property
    def text(self) -> str:
        return self._input.text().strip()

    @property
    def active_index(self) -> Optional[int]:
        return self._active_idx

    def focus_input(self):
        self._input.setFocus()


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN WINDOW
# ═══════════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("OCR Labeling Tool")
        self.setMinimumSize(1280, 820)
        self.resize(1440, 920)

        self._src_folder = ""
        self._out_folder = ""
        self._session: Optional[CropSession] = None

        self._all_files: List[str] = []
        self._unprocessed_files: List[str] = []
        self._processed_stems: set[str] = set()
        self._batch_start: int = 0
        self._batch_files: List[str] = []
        self._scanner: Optional[DirectoryScanner] = None

        self._build_ui()
        self._bind_shortcuts()
        QApplication.instance().installEventFilter(self)

    # ──────────────────────────────────────────────────────────────────────────
    #  UI CONSTRUCTION
    # ──────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 6)
        root.setSpacing(8)
        root.addLayout(self._make_toolbar())

        sp = QSplitter(Qt.Orientation.Horizontal)
        sp.setHandleWidth(3)
        sp.setStyleSheet(
            f"QSplitter::handle{{background:{C['border']};"
            f"margin:2px;border-radius:1px;}}"
        )

        # Left
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)

        self._canvas = ImageCanvasWidget()
        self._canvas.crop_created.connect(self._on_crop_created)
        self._canvas.cursor_moved.connect(self._on_cursor_moved)
        ll.addWidget(self._canvas, 5)

        self._history = CropHistoryGrid()
        self._history.card_clicked.connect(self._on_history_click)
        ll.addWidget(self._history)

        self._label_zone = LabelZoneWidget()
        self._label_zone.label_committed.connect(self._on_label_commit)
        ll.addWidget(self._label_zone)
        sp.addWidget(left)

        # Right
        sp.addWidget(self._make_right_panel())
        sp.setStretchFactor(0, 4)
        sp.setStretchFactor(1, 1)
        sp.setSizes([1050, 350])
        root.addWidget(sp, 1)

        # Status bar
        self._status = QStatusBar()
        self._status.setStyleSheet(
            f"QStatusBar{{background:{C['bg_dark']};color:{C['text_muted']};"
            f"border-top:1px solid {C['border']};font-size:12px;padding:2px 8px;}}"
        )
        self.setStatusBar(self._status)
        self._status.showMessage(
            "Ready  —  Select a source folder to begin  |  "
            "Arrow keys move cursor · O = crop · Enter = save label"
        )

    def _make_toolbar(self) -> QHBoxLayout:
        tb = QHBoxLayout(); tb.setSpacing(10)
        bss = (
            f"QPushButton{{background:{C['bg_elevated']};color:{C['text']};"
            f"border:1px solid {C['border']};border-radius:6px;"
            f"padding:7px 14px;font-size:13px;font-weight:600;}}"
            f"QPushButton:hover{{background:{C['accent']};border-color:{C['accent']};}}"
            f"QPushButton:pressed{{background:{C['accent_hover']};}}"
        )
        lss = (
            f"color:{C['text_dim']};background:{C['bg_surface']};"
            f"border:1px solid {C['border']};border-radius:6px;"
            f"padding:6px 10px;font-size:12px;"
        )
        self._src_btn = QPushButton("📁  Source Folder")
        self._src_btn.setStyleSheet(bss)
        self._src_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._src_btn.clicked.connect(self._pick_source)
        tb.addWidget(self._src_btn)
        self._src_lbl = QLabel("No folder selected")
        self._src_lbl.setStyleSheet(lss)
        tb.addWidget(self._src_lbl, 1)
        self._out_btn = QPushButton("📂  Output Folder")
        self._out_btn.setStyleSheet(bss)
        self._out_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._out_btn.clicked.connect(self._pick_output)
        tb.addWidget(self._out_btn)
        self._out_lbl = QLabel("No folder selected")
        self._out_lbl.setStyleSheet(lss)
        tb.addWidget(self._out_lbl, 1)
        return tb

    def _make_right_panel(self) -> QWidget:
        w = QWidget()
        w.setMinimumWidth(280); w.setMaximumWidth(420)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.setSpacing(6)
        hdr_ss = (
            f"color:{C['text_dim']};font-size:11px;font-weight:bold;"
            f"letter-spacing:1.5px;"
        )

        # Queue header
        lay.addWidget(self._hdr("📋  IMAGE QUEUE", hdr_ss))

        # Batch bar
        bb = QHBoxLayout(); bb.setSpacing(6)
        nav_ss = (
            f"QPushButton{{background:{C['bg_surface']};color:{C['text_muted']};"
            f"border:1px solid {C['border']};border-radius:4px;"
            f"padding:4px 10px;font-size:12px;font-weight:600;}}"
            f"QPushButton:hover{{background:{C['accent']};color:white;"
            f"border-color:{C['accent']};}}"
            f"QPushButton:disabled{{background:{C['bg_dark']};"
            f"color:{C['text_dim']};border-color:{C['bg_dark']};}}"
        )
        self._prev_bb = QPushButton("◀ Prev")
        self._prev_bb.setStyleSheet(nav_ss)
        self._prev_bb.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_bb.clicked.connect(self._prev_batch)
        self._prev_bb.setEnabled(False)
        bb.addWidget(self._prev_bb)
        self._batch_info = QLabel("No files loaded")
        self._batch_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._batch_info.setStyleSheet(
            f"color:{C['text_muted']};font-size:11px;font-weight:bold;"
            f"background:{C['bg_dark']};border:1px solid {C['border']};"
            f"border-radius:4px;padding:4px 6px;"
        )
        bb.addWidget(self._batch_info, 1)
        self._next_bb = QPushButton("Next ▶")
        self._next_bb.setStyleSheet(nav_ss)
        self._next_bb.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_bb.clicked.connect(self._next_batch)
        self._next_bb.setEnabled(False)
        bb.addWidget(self._next_bb)
        lay.addLayout(bb)

        # Progress bar
        self._scan_bar = QProgressBar()
        self._scan_bar.setRange(0, 0)
        self._scan_bar.setFixedHeight(18)
        self._scan_bar.setFormat("Scanning…  %v files found")
        self._scan_bar.setStyleSheet(
            f"QProgressBar{{background:{C['bg_dark']};border:1px solid {C['border']};"
            f"border-radius:4px;color:{C['text_muted']};font-size:11px;text-align:center;}}"
            f"QProgressBar::chunk{{background:{C['accent']};border-radius:3px;}}"
        )
        self._scan_bar.hide()
        lay.addWidget(self._scan_bar)

        # Queue list
        self._queue = QListWidget()
        self._queue.setStyleSheet(
            f"QListWidget{{background:{C['bg_dark']};border:1px solid {C['border']};"
            f"border-radius:8px;color:{C['text']};font-size:13px;"
            f"padding:4px;outline:none;}}"
            f"QListWidget::item{{padding:6px 10px;border-radius:4px;margin:1px 2px;}}"
            f"QListWidget::item:selected{{background:{C['accent']};color:white;}}"
            f"QListWidget::item:hover:!selected{{background:{C['bg_elevated']};}}"
        )
        self._queue.currentRowChanged.connect(self._on_queue_row)
        lay.addWidget(self._queue, 3)

        # Text content
        lay.addWidget(self._hdr("📝  ORIGINAL TEXT CONTENT", hdr_ss))
        self._txt_view = QTextEdit()
        self._txt_view.setReadOnly(True)
        self._txt_view.setPlaceholderText("No sidecar .txt file found")
        self._txt_view.setStyleSheet(
            f"QTextEdit{{background:{C['bg_dark']};border:1px solid {C['border']};"
            f"border-radius:8px;color:{C['text_muted']};font-size:13px;padding:8px;}}"
        )
        lay.addWidget(self._txt_view, 2)

        # Buttons
        pri = (
            f"QPushButton{{background:{C['accent']};color:white;border:none;"
            f"border-radius:8px;padding:10px;font-size:14px;font-weight:bold;}}"
            f"QPushButton:hover{{background:{C['accent_hover']};}}"
            f"QPushButton:pressed{{background:#6d28d9;}}"
            f"QPushButton:disabled{{background:{C['bg_elevated']};color:{C['text_dim']};}}"
        )
        sec = (
            f"QPushButton{{background:{C['bg_elevated']};color:{C['text']};"
            f"border:1px solid {C['border']};border-radius:8px;padding:10px;"
            f"font-size:13px;font-weight:600;}}"
            f"QPushButton:hover{{background:{C['border']};border-color:{C['accent_light']};}}"
        )
        self._save_btn = QPushButton("💾  Save Crop  (Ctrl+S)")
        self._save_btn.setStyleSheet(pri)
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.clicked.connect(self._do_save)
        lay.addWidget(self._save_btn)
        self._prev_btn = QPushButton("⏮  Prev Image  (Ctrl+P)")
        self._prev_btn.setStyleSheet(sec)
        self._prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_btn.clicked.connect(self._go_prev)
        lay.addWidget(self._prev_btn)
        self._next_btn = QPushButton("⏭  Next Image  (Ctrl+N)")
        self._next_btn.setStyleSheet(sec)
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.clicked.connect(self._go_next)
        lay.addWidget(self._next_btn)
        self._undo_btn = QPushButton("↩  Undo Last Crop  (Ctrl+Z)")
        self._undo_btn.setStyleSheet(sec)
        self._undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._undo_btn.clicked.connect(self._do_undo)
        lay.addWidget(self._undo_btn)
        return w

    @staticmethod
    def _hdr(text: str, ss: str) -> QLabel:
        h = QLabel(f"  {text}"); h.setStyleSheet(ss); return h

    # ──────────────────────────────────────────────────────────────────────────
    #  FOLDER & BATCH
    # ──────────────────────────────────────────────────────────────────────────

    def _pick_source(self):
        d = QFileDialog.getExistingDirectory(self, "Select Source Folder")
        if not d:
            return
        self._src_folder = d
        self._src_lbl.setText(d)
        self._all_files.clear(); self._unprocessed_files.clear(); self._batch_start = 0
        self._batch_files.clear(); self._queue.clear()
        self._batch_info.setText("Scanning…")
        self._scan_bar.setValue(0); self._scan_bar.show()
        self._status.showMessage("Scanning folder for images…")
        self._scanner = DirectoryScanner(d, self)
        self._scanner.progress.connect(self._on_scan_progress)
        self._scanner.finished.connect(self._on_scan_done)
        self._scanner.start()

    def _on_scan_progress(self, c):
        self._scan_bar.setFormat(f"Scanning…  {c} files found")
        self._scan_bar.setValue(c)

    def _on_scan_done(self, files):
        self._scan_bar.hide()
        self._all_files = files
        self._batch_start = 0
        self._filter_unprocessed_files()
        n = len(files)
        unprocessed_n = len(self._unprocessed_files)
        self._status.showMessage(
            f"Found {n:,} images ({unprocessed_n:,} unprocessed)  —  "
            f"showing batch of {min(BATCH_SIZE, unprocessed_n)}"
        )
        self._show_batch()
        self._scanner = None

    def _pick_output(self):
        d = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if d:
            self._out_folder = d
            self._out_lbl.setText(d)
            self._status.showMessage(f"Output → {d}")
            if self._all_files:
                self._filter_unprocessed_files()
                self._batch_start = 0
                self._show_batch()

    def _scan_output_folder(self):
        self._processed_stems = set()
        if not self._out_folder or not os.path.isdir(self._out_folder):
            return
        try:
            with os.scandir(self._out_folder) as it:
                for entry in it:
                    if entry.is_file():
                        name = entry.name
                        if name.endswith("_crop.txt"):
                            self._processed_stems.add(name[:-9])
                        elif "_crop_" in name:
                            self._processed_stems.add(name.rsplit("_crop_", 1)[0])
        except Exception:
            pass

    def _filter_unprocessed_files(self):
        self._scan_output_folder()
        self._unprocessed_files = []
        for fp in self._all_files:
            stem = Path(fp).stem
            if stem not in self._processed_stems:
                self._unprocessed_files.append(fp)

    def _show_batch(self):
        total = len(self._unprocessed_files)
        if total == 0:
            self._batch_info.setText("No images found")
            self._prev_bb.setEnabled(False); self._next_bb.setEnabled(False)
            self._queue.blockSignals(True); self._queue.clear(); self._queue.blockSignals(False)
            return
        s = self._batch_start
        e = min(s + BATCH_SIZE, total)
        self._batch_files = self._unprocessed_files[s:e]
        self._queue.blockSignals(True); self._queue.clear()
        for fp in self._batch_files:
            self._queue.addItem(Path(fp).name)
        self._queue.blockSignals(False)
        bn = s // BATCH_SIZE + 1
        tb = (total + BATCH_SIZE - 1) // BATCH_SIZE
        self._batch_info.setText(f"Batch {bn}/{tb}  ({s+1}–{e} of {total:,})")
        self._prev_bb.setEnabled(s > 0)
        self._next_bb.setEnabled(e < total)
        if self._batch_files:
            self._queue.setCurrentRow(0)

    def _next_batch(self):
        if self._batch_start + BATCH_SIZE < len(self._unprocessed_files):
            self._batch_start += BATCH_SIZE; self._show_batch()

    def _prev_batch(self):
        if self._batch_start >= BATCH_SIZE:
            self._batch_start -= BATCH_SIZE; self._show_batch()

    # ──────────────────────────────────────────────────────────────────────────
    #  IMAGE LOADING
    # ──────────────────────────────────────────────────────────────────────────

    def _on_queue_row(self, row: int):
        if row < 0 or row >= len(self._batch_files):
            return
        path = self._batch_files[row]
        pix = QPixmap(path)
        if pix.isNull():
            self._status.showMessage(f"⚠ Failed to load: {Path(path).name}"); return
        self._session = CropSession(path, pix)
        self._canvas.load_image(pix, self._session)
        self._history.clear_all()
        self._label_zone.clear()
        self._canvas.setFocus()
        # Sidecar
        txt = Path(path).with_suffix(".txt")
        if txt.exists():
            try:
                self._txt_view.setPlainText(
                    txt.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                self._txt_view.clear()
        else:
            self._txt_view.clear()
        self._status.showMessage(
            f"📷 {Path(path).name}  ({pix.width()}×{pix.height()})  —  "
            f"Use ← → to position cursor, O to crop"
        )
        self._refresh_title()

    # ──────────────────────────────────────────────────────────────────────────
    #  CROP WORKFLOW
    # ──────────────────────────────────────────────────────────────────────────

    def _on_crop_created(self, seg: CropSegment):
        self._history.add_card(seg)
        self._history.set_active_card(seg.index)
        self._label_zone.load_segment(seg)
        self._refresh_title()
        self._status.showMessage(
            f"✂ Crop #{seg.index}  (x {seg.x_start}→{seg.x_end})  —  "
            f"Type label and press Enter"
        )

    def _on_history_click(self, idx):
        if self._session:
            seg = self._session.get_segment(idx)
            if seg:
                self._label_zone.load_segment(seg)

    def _on_cursor_moved(self, open_x, cursor_x, width):
        if self._session:
            self._status.showMessage(
                f"Open: {open_x}  |  Cursor: {cursor_x}  |  Width: {width}px  —  "
                f"Press O to crop"
            )

    def _on_label_commit(self, _):
        self._do_save()

    def _do_save(self):
        if not self._session:
            self._status.showMessage("⚠ No image loaded"); return
        if not self._out_folder:
            self._status.showMessage("⚠ Select an output folder first!"); return
        idx = self._label_zone.active_index
        if idx is None:
            self._status.showMessage("⚠ No crop selected to save"); return
        seg = self._session.get_segment(idx)
        if seg is None:
            return

        seg.label_text = self._label_zone.text
        seg.is_saved = True

        stem = Path(self._session.image_path).stem
        tag = f"{stem}_crop_{seg.index:03d}"
        out = Path(self._out_folder)
        out.mkdir(parents=True, exist_ok=True)

        # Save crop image
        seg.cropped_pixmap.save(str(out / f"{tag}.jpg"), "JPEG", 95)

        # Write consolidated label file
        self._write_label_file()

        self._history.update_card_status(idx, True)
        self._canvas.add_overlay(seg)
        self._canvas.clear_marks()
        self._label_zone.clear()
        self._canvas.setFocus()
        self._refresh_title()
        self._status.showMessage(f"✅ Saved  {tag}.jpg  →  Use ← → then O for next crop")

    def _write_label_file(self):
        """Write / overwrite the consolidated label file for current image.

        Format per line:  {output_folder_name}/{crop_filename} {label_text}
        File name:        {original_stem}_crop.txt
        """
        if not self._session or not self._out_folder:
            return
        stem = Path(self._session.image_path).stem
        label_path = Path(self._out_folder) / f"{stem}_crop.txt"
        lines = []
        out_name = Path(self._out_folder).name
        for seg in self._session.segments:
            if seg.is_saved:
                crop_path = Path(out_name) / f"{stem}_crop_{seg.index:03d}.jpg"
                lines.append(f"{crop_path.as_posix()} {seg.label_text}")
        label_path.write_text(
            "\n".join(lines) + ("\n" if lines else ""),
            encoding="utf-8",
        )

    def _go_next(self):
        cur = self._queue.currentRow()
        if cur < self._queue.count() - 1:
            self._queue.setCurrentRow(cur + 1)
        elif self._batch_start + BATCH_SIZE < len(self._unprocessed_files):
            self._next_batch()
        else:
            self._status.showMessage("ℹ Already at the last image")

    def _go_prev(self):
        cur = self._queue.currentRow()
        if cur > 0:
            self._queue.setCurrentRow(cur - 1)
        elif self._batch_start >= BATCH_SIZE:
            self._batch_start -= BATCH_SIZE
            self._show_batch()
            self._queue.setCurrentRow(self._queue.count() - 1)
        else:
            self._status.showMessage("ℹ Already at the first image")

    def _do_undo(self):
        w = QApplication.focusWidget()
        if isinstance(w, QLineEdit) or (isinstance(w, QTextEdit) and not w.isReadOnly()):
            w.undo()
            return
        if not self._session:
            return
        seg = self._session.remove_last()
        if not seg:
            self._status.showMessage("ℹ Nothing to undo"); return
        self._history.remove_last_card()
        if seg.is_saved:
            self._canvas.remove_last_overlay()
            stem = Path(self._session.image_path).stem
            tag = f"{stem}_crop_{seg.index:03d}"
            out = Path(self._out_folder)
            (out / f"{tag}.jpg").unlink(missing_ok=True)
            # Re-write label file without the removed segment
            self._write_label_file()
        # Restore boundaries to what they were before this crop
        self._canvas.restore_boundaries(float(seg.x_start), float(seg.x_end))
        self._label_zone.clear()
        self._canvas.setFocus()
        self._refresh_title()
        self._status.showMessage(f"↩ Undid crop #{seg.index}  —  boundaries restored")

    def _refresh_title(self):
        base = "OCR Labeling Tool V2"
        if self._session:
            name = Path(self._session.image_path).name
            s, t = self._session.saved_count, self._session.total_count
            total_imgs = len(self._unprocessed_files)
            gi = self._batch_start + max(0, self._queue.currentRow()) + 1
            self.setWindowTitle(
                f"{base}  —  {name}  [{s}/{t} crops]  (image {gi}/{total_imgs:,})"
            )
        else:
            self.setWindowTitle(base)

    # ──────────────────────────────────────────────────────────────────────────
    #  SHORTCUTS & EVENT FILTER
    # ──────────────────────────────────────────────────────────────────────────

    def _bind_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self._do_save)
        QShortcut(QKeySequence("Ctrl+N"), self).activated.connect(self._go_next)
        QShortcut(QKeySequence("Ctrl+P"), self).activated.connect(self._go_prev)
        QShortcut(QKeySequence("Ctrl+Z"), self).activated.connect(self._do_undo)

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(obj, event)

        w = QApplication.focusWidget()
        is_editing = isinstance(w, QLineEdit) or (
            isinstance(w, QTextEdit) and not w.isReadOnly()
        )
        if is_editing:
            return super().eventFilter(obj, event)

        k = event.key()
        mods = event.modifiers()

        # I → set open boundary to cursor position
        if k == Qt.Key.Key_I:
            self._canvas.set_open_to_cursor()
            return True

        # O → commit crop
        if k == Qt.Key.Key_O:
            self._canvas.commit_crop()
            return True

        # Arrow keys → move cursor
        if k in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            if mods & Qt.KeyboardModifier.ControlModifier:
                step = 100
            elif mods & Qt.KeyboardModifier.ShiftModifier:
                step = 20
            else:
                step = 5
            if k == Qt.Key.Key_Left:
                step = -step
            self._canvas.move_cursor(step)
            return True

        return super().eventFilter(obj, event)


# ═══════════════════════════════════════════════════════════════════════════════
#  DARK THEME
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_theme(app: QApplication):
    pal = QPalette(); q = QColor
    pal.setColor(QPalette.ColorRole.Window,          q(C["bg_main"]))
    pal.setColor(QPalette.ColorRole.WindowText,      q(C["text"]))
    pal.setColor(QPalette.ColorRole.Base,            q(C["bg_dark"]))
    pal.setColor(QPalette.ColorRole.AlternateBase,   q(C["bg_surface"]))
    pal.setColor(QPalette.ColorRole.ToolTipBase,     q(C["bg_elevated"]))
    pal.setColor(QPalette.ColorRole.ToolTipText,     q(C["text"]))
    pal.setColor(QPalette.ColorRole.Text,            q(C["text"]))
    pal.setColor(QPalette.ColorRole.Button,          q(C["bg_surface"]))
    pal.setColor(QPalette.ColorRole.ButtonText,      q(C["text"]))
    pal.setColor(QPalette.ColorRole.BrightText,      q("#ffffff"))
    pal.setColor(QPalette.ColorRole.Link,            q(C["accent_light"]))
    pal.setColor(QPalette.ColorRole.Highlight,       q(C["accent"]))
    pal.setColor(QPalette.ColorRole.HighlightedText, q("#ffffff"))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text,       q(C["text_dim"]))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, q(C["text_dim"]))
    app.setPalette(pal)
    font = QFont("Inter", 10)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("OCR Labeling Tool V2")
    app.setOrganizationName("OCRTools")
    _apply_theme(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
