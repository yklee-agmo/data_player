#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_player.py — 이미지 폴더 / 동영상 플레이어 (PyQt6)

* 이미지 폴더를 열면 정렬된 이미지들을 프레임처럼 이어서 재생합니다.
* 동영상 파일을 열면 ffmpeg(imageio-ffmpeg)로 디코딩해 재생합니다.
* 두 경우 모두 0.1x ~ 10x 배속을 조절할 수 있습니다.
* 원하는 번호(순번 또는 파일명 번호)를 입력하면 그 화면으로 바로 이동합니다.

실행:  python3 data_player.py [경로]
"""
from __future__ import annotations

import math
import os
import re
import sys
from collections import OrderedDict

import numpy as np
from PyQt6.QtCore import Qt, QTimer, QElapsedTimer, QRect, QSize
from PyQt6.QtGui import (QAction, QColor, QIcon, QImage, QImageReader,
                         QKeySequence, QPainter)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QSizePolicy,
    QSlider, QSpinBox, QToolBar, QVBoxLayout, QWidget,
)

# --------------------------------------------------------------------------
# 파일 종류
# --------------------------------------------------------------------------

def _supported_image_exts() -> set[str]:
    exts = {"." + bytes(f).decode("ascii").lower()
            for f in QImageReader.supportedImageFormats()}
    exts |= {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".ppm", ".pgm"}
    return exts


IMAGE_EXTS = _supported_image_exts()
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg",
              ".wmv", ".flv", ".ts", ".m2ts", ".mts", ".3gp", ".ogv"}

_NUM_RE = re.compile(r"(\d+)")


def natural_key(name: str):
    """frame_2.png 가 frame_10.png 보다 앞에 오도록 정렬."""
    return [int(t) if t.isdigit() else t.lower() for t in _NUM_RE.split(name)]


def trailing_number(stem: str):
    """파일명에서 마지막 숫자 덩어리를 파일 번호로 사용."""
    nums = _NUM_RE.findall(stem)
    return int(nums[-1]) if nums else None


def qimage_from_rgb(arr: np.ndarray) -> QImage:
    h, w, _ = arr.shape
    arr = np.ascontiguousarray(arr)
    return QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


def fmt_time(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:  # 음수/NaN
        seconds = 0.0
    m, s = divmod(seconds, 60.0)
    h, m = divmod(int(m), 60)
    if h:
        return f"{h:d}:{m:02d}:{s:04.1f}"
    return f"{m:02d}:{s:04.1f}"


# --------------------------------------------------------------------------
# 프레임 소스
# --------------------------------------------------------------------------

class FrameSource:
    """이미지 폴더와 동영상을 같은 방식(프레임 인덱스)으로 다루기 위한 공통 인터페이스."""

    kind = ""

    def __init__(self, path: str):
        self.path = path

    def __len__(self) -> int:
        raise NotImplementedError

    def frame(self, index: int):
        """index 번째 프레임을 QImage 로 반환. 실패하면 None."""
        raise NotImplementedError

    @property
    def fps(self) -> float:
        raise NotImplementedError

    def label(self, index: int) -> str:
        return ""

    def numbers_available(self) -> bool:
        return False

    def close(self) -> None:
        pass


class ImageFolderSource(FrameSource):
    """폴더 안의 이미지들을 번호순으로 늘어놓은 프레임 소스."""

    kind = "folder"

    def __init__(self, path: str, fps: float = 30.0):
        super().__init__(path)
        try:
            entries = os.listdir(path)
        except OSError as exc:
            raise ValueError(f"폴더를 읽을 수 없습니다: {exc}") from exc

        names = [n for n in entries
                 if os.path.splitext(n)[1].lower() in IMAGE_EXTS
                 and os.path.isfile(os.path.join(path, n))]
        if not names:
            raise ValueError("폴더 안에 이미지 파일이 없습니다.")
        names.sort(key=natural_key)

        self.names = names
        self._fps = float(fps)
        self.numbers = [trailing_number(os.path.splitext(n)[0]) for n in names]

        # 모든 파일에 번호가 있을 때만 '파일 번호로 이동'을 지원한다.
        self.number_map: dict[int, int] = {}
        if all(n is not None for n in self.numbers):
            for idx, num in enumerate(self.numbers):
                self.number_map.setdefault(num, idx)

    def __len__(self) -> int:
        return len(self.names)

    @property
    def fps(self) -> float:
        return self._fps

    @fps.setter
    def fps(self, value: float) -> None:
        self._fps = max(0.1, float(value))

    def frame(self, index: int):
        full = os.path.join(self.path, self.names[index])
        img = QImage(full)
        if not img.isNull():
            return img
        # Qt 가 못 읽는 포맷은 Pillow 로 한 번 더 시도한다.
        try:
            from PIL import Image
            with Image.open(full) as im:
                arr = np.asarray(im.convert("RGB"))
            return qimage_from_rgb(arr)
        except Exception:
            return None

    def label(self, index: int) -> str:
        return self.names[index]

    def numbers_available(self) -> bool:
        return bool(self.number_map)


class VideoSource(FrameSource):
    """ffmpeg 로 디코딩하는 동영상 프레임 소스.

    순차 재생은 하나의 ffmpeg 파이프를 계속 읽어서 빠르게 처리하고,
    뒤로 가거나 멀리 건너뛸 때만 -ss 로 파이프를 다시 연다(프레임 단위 정확).
    """

    kind = "video"
    #: 앞으로 이만큼 이내면 파이프를 다시 열지 않고 프레임을 흘려보낸다.
    SKIP_LIMIT = 60

    def __init__(self, path: str):
        super().__init__(path)
        try:
            import imageio_ffmpeg as iff
        except ImportError as exc:  # pragma: no cover
            raise ValueError(
                "동영상 재생에는 imageio-ffmpeg 가 필요합니다.\n"
                "  pip install imageio-ffmpeg"
            ) from exc
        self._iff = iff

        probe = iff.read_frames(path)
        try:
            info = next(probe)
        except Exception as exc:
            raise ValueError(f"동영상을 열 수 없습니다: {exc}") from exc
        finally:
            probe.close()

        self._size = info["size"]                      # (w, h)
        self._fps = float(info.get("fps") or 0.0) or 30.0
        duration = float(info.get("duration") or 0.0)
        nframes = info.get("nframes")
        if isinstance(nframes, int) and 0 < nframes < 1 << 40:
            self._count = nframes
        else:
            self._count = max(1, int(round(duration * self._fps)))

        self._gen = None        # 현재 열려 있는 ffmpeg 파이프
        self._next_index = 0    # 그 파이프에서 다음에 나올 프레임 번호

    def __len__(self) -> int:
        return self._count

    @property
    def fps(self) -> float:
        return self._fps

    def label(self, index: int) -> str:
        return os.path.basename(self.path)

    def _open_at(self, index: int) -> None:
        self.close()
        params = []
        if index > 0:
            params = ["-ss", f"{index / self._fps:.6f}"]
        self._gen = self._iff.read_frames(
            self.path, input_params=params, bits_per_pixel=24)
        next(self._gen)  # 첫 yield 는 메타데이터
        self._next_index = index

    def frame(self, index: int):
        if index < 0:
            return None
        if (self._gen is None
                or index < self._next_index
                or index > self._next_index + self.SKIP_LIMIT):
            try:
                self._open_at(index)
            except Exception:
                return None

        w, h = self._size
        try:
            while self._next_index < index:      # 조금 앞이면 흘려보낸다
                next(self._gen)
                self._next_index += 1
            raw = next(self._gen)
            self._next_index += 1
        except StopIteration:
            # 추정한 프레임 수가 실제보다 길었다. 실제 길이로 줄인다.
            self._count = max(1, min(self._count, index))
            self.close()
            return None
        except Exception:
            self.close()
            return None

        return qimage_from_rgb(np.frombuffer(raw, np.uint8).reshape(h, w, 3))

    def close(self) -> None:
        if self._gen is not None:
            try:
                self._gen.close()
            except Exception:
                pass
            self._gen = None


# --------------------------------------------------------------------------
# 화면 표시 위젯
# --------------------------------------------------------------------------

class ImageCanvas(QWidget):
    """QImage 를 비율을 유지한 채 창에 맞춰 그려 주는 위젯."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image: QImage | None = None
        self._placeholder = "이미지 폴더나 동영상을 열어 주세요\n(창에 끌어다 놓아도 됩니다)"
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), QColor(24, 24, 26))
        self.setPalette(pal)

    def set_image(self, image: QImage | None) -> None:
        self._image = image
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(960, 600)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(24, 24, 26))

        if self._image is None or self._image.isNull():
            painter.setPen(QColor(150, 150, 155))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._placeholder)
            return

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        target = self._image.size().scaled(
            self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x = (self.width() - target.width()) // 2
        y = (self.height() - target.height()) // 2
        painter.drawImage(QRect(x, y, target.width(), target.height()), self._image)


# --------------------------------------------------------------------------
# 메인 윈도우
# --------------------------------------------------------------------------

SPEED_MIN, SPEED_MAX = 0.1, 10.0
CACHE_FRAMES = 48


class PlayerWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Data Player")
        self.resize(1000, 720)
        self.setAcceptDrops(True)

        self.source: FrameSource | None = None
        self.index = 0
        self.speed = 1.0
        self.playing = False
        self._folder_fps = 30.0   # 이미지 폴더용 기준 fps (동영상 fps 와 섞이지 않게 따로 둔다)
        self._cache: OrderedDict[int, QImage] = OrderedDict()
        self._updating_ui = False

        # 재생 시계: '지금 몇 번째 프레임이어야 하는가'를 경과 시간으로 계산해
        # 디코딩이 느려도 배속이 흐트러지지 않게 한다(늦으면 프레임을 건너뜀).
        self._clock = QElapsedTimer()
        self._anchor = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)

        self._build_ui()
        self._update_enabled()

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        tb = QToolBar("파일")
        tb.setMovable(False)
        self.addToolBar(tb)

        self.act_open_folder = QAction("이미지 폴더 열기", self)
        self.act_open_folder.setShortcut(QKeySequence("Ctrl+O"))
        self.act_open_folder.triggered.connect(self.open_folder_dialog)
        tb.addAction(self.act_open_folder)

        self.act_open_video = QAction("동영상 열기", self)
        self.act_open_video.setShortcut(QKeySequence("Ctrl+Shift+O"))
        self.act_open_video.triggered.connect(self.open_video_dialog)
        tb.addAction(self.act_open_video)

        self.canvas = ImageCanvas()

        # --- 재생 컨트롤 (1행) -------------------------------------------
        self.btn_first = QPushButton("|◁")
        self.btn_prev = QPushButton("◁")
        self.btn_play = QPushButton("▶")
        self.btn_next = QPushButton("▷")
        self.btn_last = QPushButton("▷|")
        for b, tip in ((self.btn_first, "처음으로 (Home)"),
                       (self.btn_prev, "이전 프레임 (←)"),
                       (self.btn_play, "재생 / 일시정지 (Space)"),
                       (self.btn_next, "다음 프레임 (→)"),
                       (self.btn_last, "끝으로 (End)")):
            b.setToolTip(tip)
            b.setFixedWidth(46)
        self.btn_play.setFixedWidth(60)
        self.btn_first.clicked.connect(lambda: self.goto(0))
        self.btn_prev.clicked.connect(lambda: self.step(-1))
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_next.clicked.connect(lambda: self.step(+1))
        self.btn_last.clicked.connect(lambda: self.goto(self.count - 1))

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setTracking(True)
        self.slider.valueChanged.connect(self._on_slider)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["순번", "파일 번호"])
        self.mode_combo.setToolTip("숫자 입력칸이 무엇을 뜻하는지 고릅니다.")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self.num_spin = QSpinBox()
        self.num_spin.setKeyboardTracking(False)   # 다 입력한 뒤에만 이동
        self.num_spin.setRange(0, 0)
        self.num_spin.setFixedWidth(90)
        self.num_spin.setToolTip("번호를 입력하고 Enter 를 누르면 그 화면으로 이동합니다.")
        self.num_spin.valueChanged.connect(self._on_number_entered)

        self.btn_goto = QPushButton("이동")
        self.btn_goto.setFixedWidth(50)
        self.btn_goto.clicked.connect(
            lambda: self._on_number_entered(self.num_spin.value()))

        self.total_label = QLabel("/ 0")
        self.time_label = QLabel("00:0.0 / 00:0.0")
        self.time_label.setMinimumWidth(140)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight
                                     | Qt.AlignmentFlag.AlignVCenter)

        row1 = QHBoxLayout()
        for w in (self.btn_first, self.btn_prev, self.btn_play,
                  self.btn_next, self.btn_last):
            row1.addWidget(w)
        row1.addSpacing(8)
        row1.addWidget(self.slider, 1)
        row1.addSpacing(8)
        row1.addWidget(self.mode_combo)
        row1.addWidget(self.num_spin)
        row1.addWidget(self.total_label)
        row1.addWidget(self.btn_goto)
        row1.addWidget(self.time_label)

        # --- 배속 컨트롤 (2행) -------------------------------------------
        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setRange(0.1, 240.0)
        self.fps_spin.setDecimals(2)
        self.fps_spin.setValue(30.0)
        self.fps_spin.setSuffix(" fps")
        self.fps_spin.setFixedWidth(100)
        self.fps_spin.setToolTip("이미지 폴더를 재생할 때 쓰는 기준 프레임 속도입니다.\n"
                                 "동영상은 파일에 기록된 값을 그대로 씁니다.")
        self.fps_spin.valueChanged.connect(self._on_base_fps_changed)

        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(0, 1000)
        self.speed_slider.setValue(self._speed_to_slider(1.0))
        self.speed_slider.setMinimumWidth(160)
        self.speed_slider.valueChanged.connect(self._on_speed_slider)

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(SPEED_MIN, SPEED_MAX)
        self.speed_spin.setDecimals(2)
        self.speed_spin.setSingleStep(0.1)
        self.speed_spin.setValue(1.0)
        self.speed_spin.setSuffix(" x")
        self.speed_spin.setFixedWidth(90)
        self.speed_spin.setKeyboardTracking(False)
        self.speed_spin.valueChanged.connect(self.set_speed)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("기준 속도:"))
        row2.addWidget(self.fps_spin)
        row2.addSpacing(16)
        row2.addWidget(QLabel("배속:"))
        row2.addWidget(self.speed_slider, 1)
        row2.addWidget(self.speed_spin)
        for preset in (0.25, 0.5, 1.0, 2.0, 4.0):
            b = QPushButton(f"{preset:g}x")
            b.setFixedWidth(42)
            b.clicked.connect(lambda _=False, s=preset: self.set_speed(s))
            row2.addWidget(b)
        row2.addSpacing(16)
        self.loop_check = QCheckBox("반복")
        self.loop_check.setChecked(True)
        row2.addWidget(self.loop_check)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addWidget(self.canvas, 1)
        lay.addWidget(line)
        lay.addLayout(row1)
        lay.addLayout(row2)
        self.setCentralWidget(central)

        self.statusBar().showMessage("이미지 폴더나 동영상을 열어 주세요.")

    # ------------------------------------------------------------ helpers
    @property
    def count(self) -> int:
        return len(self.source) if self.source else 0

    @property
    def base_fps(self) -> float:
        if self.source is None:
            return 30.0
        return max(0.1, self.source.fps)

    @property
    def rate(self) -> float:
        """실제 초당 프레임 수 = 기준 fps x 배속."""
        return max(0.01, self.base_fps * self.speed)

    @staticmethod
    def _speed_to_slider(speed: float) -> int:
        # 1.0 이 한가운데 오도록 로그 눈금을 쓴다.
        t = (math.log10(speed) - math.log10(SPEED_MIN)) / (
            math.log10(SPEED_MAX) - math.log10(SPEED_MIN))
        return int(round(t * 1000))

    @staticmethod
    def _slider_to_speed(value: int) -> float:
        t = value / 1000.0
        return 10 ** (math.log10(SPEED_MIN)
                      + t * (math.log10(SPEED_MAX) - math.log10(SPEED_MIN)))

    def _update_enabled(self) -> None:
        on = self.source is not None
        for w in (self.btn_first, self.btn_prev, self.btn_play, self.btn_next,
                  self.btn_last, self.slider, self.num_spin, self.btn_goto,
                  self.speed_slider, self.speed_spin):
            w.setEnabled(on)
        folder = on and self.source.kind == "folder"
        # 동영상은 파일에 적힌 fps 를 그대로 쓰므로 그때만 잠근다.
        self.fps_spin.setEnabled(not on or folder)
        self.mode_combo.setEnabled(bool(folder and self.source.numbers_available()))

    # --------------------------------------------------------- 파일 열기
    def open_folder_dialog(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "이미지 폴더 선택")
        if path:
            self.load(path)

    def open_video_dialog(self) -> None:
        pattern = " ".join("*" + e for e in sorted(VIDEO_EXTS))
        path, _ = QFileDialog.getOpenFileName(
            self, "동영상 선택", "", f"동영상 ({pattern});;모든 파일 (*)")
        if path:
            self.load(path)

    def load(self, path: str) -> None:
        path = os.path.abspath(path)
        try:
            if os.path.isdir(path):
                source: FrameSource = ImageFolderSource(path, self._folder_fps)
            elif os.path.isfile(path):
                ext = os.path.splitext(path)[1].lower()
                if ext in IMAGE_EXTS:      # 이미지를 떨어뜨리면 그 폴더를 연다
                    folder = os.path.dirname(path)
                    source = ImageFolderSource(folder, self._folder_fps)
                    self._install(source, path)
                    return
                source = VideoSource(path)
            else:
                raise ValueError("파일이나 폴더를 찾을 수 없습니다.")
        except Exception as exc:
            QMessageBox.critical(self, "열기 실패", str(exc))
            return
        self._install(source)

    def _install(self, source: FrameSource, select_file: str | None = None) -> None:
        self.pause()
        if self.source is not None:
            self.source.close()
        self.source = source
        self._cache.clear()

        self._updating_ui = True
        self.slider.setRange(0, max(0, self.count - 1))
        self.mode_combo.setCurrentIndex(0)
        self.fps_spin.setValue(source.fps if source.kind == "video" else self._folder_fps)
        self._updating_ui = False

        self._update_enabled()
        self._sync_number_range()

        start = 0
        if select_file is not None and isinstance(source, ImageFolderSource):
            name = os.path.basename(select_file)
            if name in source.names:
                start = source.names.index(name)

        self.setWindowTitle(f"Data Player — {os.path.basename(source.path)}")
        self.goto(start)

    # ------------------------------------------------------------- 이동
    def goto(self, index: int) -> None:
        if not self.source or self.count == 0:
            return
        index = max(0, min(self.count - 1, int(index)))
        image = self._image_at(index)
        if image is None and self.source.kind == "video":
            # 길이 추정이 실제보다 길었던 경우. _image_at 이 길이를 줄여 놨으므로
            # 새 끝 프레임으로 한 번만 다시 시도한다.
            retry = max(0, min(self.count - 1, index))
            if retry != index:
                index = retry
                image = self._image_at(index)
        self.index = index
        self.canvas.set_image(image)
        self._sync_ui()

    def step(self, delta: int) -> None:
        self.pause()
        self.goto(self.index + delta)

    def _image_at(self, index: int):
        img = self._cache.get(index)
        if img is not None:
            self._cache.move_to_end(index)
            return img
        img = self.source.frame(index)
        if img is None:
            if self.source.kind == "video":
                self._refresh_length()
            return None
        self._cache[index] = img
        while len(self._cache) > CACHE_FRAMES:
            self._cache.popitem(last=False)
        return img

    def _refresh_length(self) -> None:
        self._updating_ui = True
        self.slider.setRange(0, max(0, self.count - 1))
        self._updating_ui = False
        self._sync_number_range()

    # --------------------------------------------------------- 번호 입력
    def _folder_numbers(self) -> bool:
        """'파일 번호' 모드로 동작 중인가."""
        return (self.source is not None
                and self.source.kind == "folder"
                and self.source.numbers_available()
                and self.mode_combo.currentIndex() == 1)

    def _sync_number_range(self) -> None:
        self._updating_ui = True
        if self._folder_numbers():
            nums = self.source.number_map.keys()
            self.num_spin.setRange(min(nums), max(nums))
            self.total_label.setText(f"/ {max(nums)}")
        else:
            self.num_spin.setRange(1, max(1, self.count))
            self.total_label.setText(f"/ {self.count}")
        self._updating_ui = False

    def _on_mode_changed(self, _index: int) -> None:
        if self._updating_ui:
            return
        self._sync_number_range()
        self._sync_ui()

    def _on_number_entered(self, value: int) -> None:
        if self._updating_ui or self.source is None:
            return
        if self._folder_numbers():
            mapping = self.source.number_map
            if value in mapping:
                self.goto(mapping[value])
            else:
                nearest = min(mapping, key=lambda n: abs(n - value))
                self.goto(mapping[nearest])
                self.statusBar().showMessage(
                    f"{value} 번 파일이 없어 가장 가까운 {nearest} 번으로 이동했습니다.", 4000)
        else:
            self.goto(value - 1)      # 화면에는 1부터 보여 준다

    def _on_slider(self, value: int) -> None:
        if self._updating_ui:
            return
        if self.playing:              # 재생 중 스크럽하면 시계를 새 위치로 다시 맞춘다
            self._reanchor(value)
        self.goto(value)

    # --------------------------------------------------------- 배속 / 재생
    def set_speed(self, speed: float) -> None:
        speed = max(SPEED_MIN, min(SPEED_MAX, round(float(speed), 2)))
        self.speed = speed
        self._updating_ui = True
        self.speed_spin.setValue(speed)
        self.speed_slider.setValue(self._speed_to_slider(speed))
        self._updating_ui = False
        if self.playing:
            self._reanchor(self.index)
            self._timer.setInterval(self._interval())
        self._sync_ui()

    def _on_speed_slider(self, value: int) -> None:
        if self._updating_ui:
            return
        self.set_speed(self._slider_to_speed(value))

    def _on_base_fps_changed(self, value: float) -> None:
        if self._updating_ui:
            return
        if self.source is None or self.source.kind == "folder":
            self._folder_fps = value      # 폴더를 열기 전에 미리 정해 둘 수도 있다
        if isinstance(self.source, ImageFolderSource):
            self.source.fps = value
            if self.playing:
                self._reanchor(self.index)
                self._timer.setInterval(self._interval())
            self._sync_ui()

    def _interval(self) -> int:
        """타이머 주기 — 너무 촘촘하거나 너무 성기지 않게 묶어 둔다."""
        return max(4, min(100, int(1000.0 / self.rate)))

    def _reanchor(self, index: float) -> None:
        self._anchor = float(index)
        self._clock.restart()

    def toggle_play(self) -> None:
        self.pause() if self.playing else self.play()

    def play(self) -> None:
        if not self.source or self.count == 0 or self.playing:
            return
        if self.index >= self.count - 1:
            self.goto(0)
        self.playing = True
        self.btn_play.setText("❚❚")
        self._reanchor(self.index)
        self._timer.start(self._interval())

    def pause(self) -> None:
        if not self.playing:
            return
        self.playing = False
        self._timer.stop()
        self.btn_play.setText("▶")

    def _on_tick(self) -> None:
        if not self.playing or not self.source:
            return
        elapsed = self._clock.elapsed() / 1000.0
        target = self._anchor + elapsed * self.rate
        index = int(target)

        if index >= self.count:
            if self.loop_check.isChecked():
                self._reanchor(0)
                index = 0
            else:
                self.goto(self.count - 1)
                self.pause()
                return
        if index != self.index:
            self.goto(index)

    # -------------------------------------------------------------- 표시
    def _sync_ui(self) -> None:
        if not self.source:
            return
        self._updating_ui = True
        self.slider.setValue(self.index)
        if self._folder_numbers():
            num = self.source.numbers[self.index]
            if num is not None:
                self.num_spin.setValue(num)
        else:
            self.num_spin.setValue(self.index + 1)
        self._updating_ui = False

        cur = self.index / self.rate if self.rate else 0.0
        total = self.count / self.rate if self.rate else 0.0
        self.time_label.setText(f"{fmt_time(cur)} / {fmt_time(total)}")

        img = self.canvas._image
        dims = f"{img.width()}x{img.height()}" if img and not img.isNull() else "-"
        kind = "동영상" if self.source.kind == "video" else "이미지 폴더"
        self.statusBar().showMessage(
            f"[{kind}] {self.source.label(self.index)}   "
            f"{self.index + 1} / {self.count} 프레임   {dims}   "
            f"{self.base_fps:g} fps x {self.speed:g} = {self.rate:.2f} fps")

    # ------------------------------------------------------- 입력 이벤트
    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.toggle_play()
        elif key == Qt.Key.Key_Left:
            self.step(-1)
        elif key == Qt.Key.Key_Right:
            self.step(+1)
        elif key == Qt.Key.Key_Home:
            self.pause(); self.goto(0)
        elif key == Qt.Key.Key_End:
            self.pause(); self.goto(self.count - 1)
        elif key == Qt.Key.Key_Up:
            self.set_speed(self.speed * 1.25)
        elif key == Qt.Key.Key_Down:
            self.set_speed(self.speed / 1.25)
        else:
            super().keyPressEvent(event)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if urls:
            self.load(urls[0].toLocalFile())
            event.acceptProposedAction()

    def closeEvent(self, event) -> None:
        self.pause()
        if self.source is not None:
            self.source.close()
        super().closeEvent(event)


def main(argv: list[str]) -> int:
    app = QApplication(argv)
    # 창을 .desktop 항목과 이어 줘야 GNOME 이 독/작업표시줄에 올바른 아이콘을 쓴다.
    app.setApplicationName("Data Player")
    app.setApplicationDisplayName("Data Player")
    app.setDesktopFileName("data-player")
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "data-player.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    win = PlayerWindow()
    win.show()
    if len(argv) > 1:
        win.load(argv[1])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
