#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_player.py — 이미지 폴더 / 동영상 플레이어 (PyQt6)

* 이미지 폴더를 열면 정렬된 이미지들을 프레임처럼 이어서 재생합니다.
* 동영상 파일을 열면 ffmpeg(imageio-ffmpeg)로 디코딩해 재생합니다.
* 두 경우 모두 0.1x ~ 10x 배속을 조절할 수 있습니다.
* 원하는 번호(순번 또는 파일명 번호)를 입력하면 그 화면으로 바로 이동합니다.
* 멀티 뷰로 화면을 나눠 여러 동영상/폴더를 나란히 비교할 수 있습니다.
  나뉜 화면에서 우클릭하면 그 칸에 파일을 열 수 있고,
  선택한 화면들만 함께 재생하거나 한 화면만 따로 재생할 수 있습니다.

실행:  python3 data_player.py [경로]
"""
from __future__ import annotations

import math
import os
import re
import sys
from collections import OrderedDict

import numpy as np
from PyQt6.QtCore import (Qt, QElapsedTimer, QRect, QSize, QThread, QTimer,
                          pyqtSignal)
from PyQt6.QtGui import (QAction, QActionGroup, QColor, QIcon, QImage,
                         QImageReader, QKeySequence, QPainter)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QProgressDialog, QPushButton, QRadioButton, QSizePolicy, QSlider, QSpinBox,
    QStyle, QStyleOptionSlider, QToolBar, QToolButton, QVBoxLayout, QWidget,
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
                "지금 실행 중인 파이썬에 설치해 주세요:\n"
                f"  {sys.executable} -m pip install imageio-ffmpeg"
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
        self.setMinimumSize(160, 120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), QColor(24, 24, 26))
        self.setPalette(pal)

    def set_image(self, image: QImage | None) -> None:
        self._image = image
        self.update()

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        if self._image is None:
            self.update()

    def sizeHint(self) -> QSize:
        return QSize(960, 600)

    # 마우스/컨텍스트 메뉴는 이 위젯이 삼키지 않고 부모(뷰 칸)가 처리한다.
    def mousePressEvent(self, event) -> None:
        event.ignore()

    def mouseDoubleClickEvent(self, event) -> None:
        event.ignore()

    def contextMenuEvent(self, event) -> None:
        event.ignore()

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


class SeekSlider(QSlider):
    """홈(groove)을 클릭하면 그 위치로 바로 이동하는 슬라이더.

    기본 QSlider 는 홈을 클릭하면 한 페이지씩만 움직이므로,
    누른 지점의 값으로 먼저 옮긴 뒤 평소처럼 드래그를 이어 간다.
    편집 구간이 정해져 있으면 그 범위를 띠로 표시한다.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mark_lo: int | None = None
        self._mark_hi: int | None = None

    def set_marks(self, lo: int | None, hi: int | None) -> None:
        if (lo, hi) == (self._mark_lo, self._mark_hi):
            return
        self._mark_lo, self._mark_hi = lo, hi
        self.update()

    def _value_at(self, pos) -> int:
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        style = self.style()
        groove = style.subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                      QStyle.SubControl.SC_SliderGroove, self)
        handle = style.subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                      QStyle.SubControl.SC_SliderHandle, self)
        if self.orientation() == Qt.Orientation.Horizontal:
            span = groove.width() - handle.width()
            offset = pos.x() - groove.x() - handle.width() / 2.0
        else:
            span = groove.height() - handle.height()
            offset = pos.y() - groove.y() - handle.height() / 2.0
        if span <= 0:
            return self.minimum()
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), int(round(offset)), int(span),
            opt.upsideDown)

    def _handle_rect(self) -> QRect:
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt,
            QStyle.SubControl.SC_SliderHandle, self)

    def mousePressEvent(self, event) -> None:
        if (event.button() == Qt.MouseButton.LeftButton
                and self.maximum() > self.minimum()):
            pos = event.position().toPoint()
            if not self._handle_rect().contains(pos):
                # 값을 먼저 옮기면 핸들이 커서 아래로 오므로
                # 이어지는 기본 처리가 그대로 드래그로 연결된다.
                self.setValue(self._value_at(pos))
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._mark_lo is None and self._mark_hi is None:
            return
        if self.maximum() <= self.minimum():
            return

        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        style = self.style()
        groove = style.subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                      QStyle.SubControl.SC_SliderGroove, self)
        handle = style.subControlRect(QStyle.ComplexControl.CC_Slider, opt,
                                      QStyle.SubControl.SC_SliderHandle, self)
        span = groove.width() - handle.width()
        if span <= 0:
            return

        def x_of(value: int) -> int:
            pos = QStyle.sliderPositionFromValue(
                self.minimum(), self.maximum(), value, span, opt.upsideDown)
            return groove.x() + handle.width() // 2 + pos

        lo = self.minimum() if self._mark_lo is None else self._mark_lo
        hi = self.maximum() if self._mark_hi is None else self._mark_hi
        x1, x2 = x_of(max(self.minimum(), lo)), x_of(min(self.maximum(), hi))

        painter = QPainter(self)
        mid = groove.center().y()
        painter.fillRect(QRect(x1, mid - 3, max(2, x2 - x1), 7),
                         QColor(76, 141, 255, 130))
        painter.setPen(QColor(76, 141, 255))
        top, bottom = self.rect().top() + 1, self.rect().bottom() - 1
        for x in (x1, x2):
            painter.drawLine(x, top, x, bottom)


# --------------------------------------------------------------------------
# 구간 저장 (편집 · 내보내기)
# --------------------------------------------------------------------------

#: 이미지로 저장할 때 고를 수 있는 형식 — (표시 이름, 확장자, 품질 사용 여부)
IMAGE_SAVE_FORMATS = [
    ("PNG (무손실)", "png", False),
    ("JPEG", "jpg", True),
    ("WebP", "webp", True),
    ("BMP", "bmp", False),
]

#: 동영상으로 저장할 때 고를 수 있는 코덱 — (표시 이름, ffmpeg 코덱 이름)
VIDEO_SAVE_CODECS = [
    ("H.264 (mp4, 권장)", "libx264"),
    ("MPEG-4", "mpeg4"),
    ("FFV1 (무손실, 용량 큼)", "ffv1"),
]


def ffmpeg_available() -> bool:
    try:
        import imageio_ffmpeg  # noqa: F401
    except Exception:
        return False
    return True


def fit_qimage(img: QImage, size: QSize) -> QImage:
    """프레임을 목표 크기에 맞춘다. 비율은 유지하고 남는 자리는 검게 채운다."""
    out = img.convertToFormat(QImage.Format.Format_RGB888)
    if out.size() == size:
        return out
    canvas = QImage(size, QImage.Format.Format_RGB888)
    canvas.fill(QColor(0, 0, 0))
    scaled = out.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
    painter = QPainter(canvas)
    painter.drawImage((size.width() - scaled.width()) // 2,
                      (size.height() - scaled.height()) // 2, scaled)
    painter.end()
    return canvas


def qimage_rgb_bytes(img: QImage) -> bytes:
    """QImage 를 ffmpeg 에 넘길 rgb24 바이트로 바꾼다(행 끝 여백 제거)."""
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    raw = img.constBits().asstring(bpl * h)
    if bpl == 3 * w:
        return raw
    arr = np.frombuffer(raw, np.uint8).reshape(h, bpl)[:, :3 * w]
    return np.ascontiguousarray(arr).tobytes()


class ExportWorker(QThread):
    """구간을 이미지 여러 장 또는 동영상 한 개로 저장하는 작업 스레드.

    창이 쓰고 있는 소스를 건드리지 않도록 스레드 안에서 소스를 새로 연다
    (동영상은 ffmpeg 파이프를 하나만 가지므로 같이 쓰면 서로 방해한다).
    """

    progress = pyqtSignal(int, int)   # (저장한 프레임 수, 전체)
    done = pyqtSignal(bool, str)      # (성공 여부, 알림 문구)

    def __init__(self, spec: dict, parent=None):
        super().__init__(parent)
        self.spec = spec
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def _open_source(self) -> FrameSource:
        if self.spec["kind"] == "folder":
            return ImageFolderSource(self.spec["path"], self.spec["fps"])
        return VideoSource(self.spec["path"])

    def _open_writer(self, size: QSize):
        import imageio_ffmpeg as iff
        writer = iff.write_frames(
            self.spec["out"], (size.width(), size.height()),
            pix_fmt_in="rgb24",
            fps=max(0.1, float(self.spec["out_fps"])),
            codec=self.spec["codec"],
            quality=self.spec["vquality"],
            macro_block_size=2,          # 크기는 짝수까지만 맞춘다
            ffmpeg_log_level="error")
        writer.send(None)                # 제너레이터 시작
        return writer

    def run(self) -> None:
        spec = self.spec
        indices = list(range(spec["start"], spec["end"] + 1, spec["step"]))
        total = len(indices)
        source = writer = None
        saved = skipped = 0
        try:
            if not spec["as_video"]:
                os.makedirs(spec["out"], exist_ok=True)
            source = self._open_source()
            size: QSize | None = None
            for done_count, index in enumerate(indices, 1):
                if self._cancel:
                    break
                img = source.frame(index)
                if img is None or img.isNull():
                    skipped += 1
                    self.progress.emit(done_count, total)
                    continue
                if spec["as_video"]:
                    if writer is None:
                        size = img.size()
                        writer = self._open_writer(size)
                    writer.send(qimage_rgb_bytes(fit_qimage(img, size)))
                else:
                    name = (f"{spec['prefix']}{index + 1:0{spec['digits']}d}"
                            f".{spec['ext']}")
                    out = os.path.join(spec["out"], name)
                    if not img.save(out, spec["ext"].upper(), spec["iquality"]):
                        raise ValueError(f"이미지를 저장하지 못했습니다:\n{out}")
                saved += 1
                self.progress.emit(done_count, total)
        except Exception as exc:
            self._close(source, writer)
            self.done.emit(False, f"저장 실패: {exc}")
            return
        self._close(source, writer)

        where = spec["out"]
        if self._cancel:
            self.done.emit(
                False,
                f"저장을 취소했습니다. 그때까지 저장된 {saved} 프레임은 "
                f"그대로 남아 있습니다:\n{where}")
            return
        if saved == 0:
            self.done.emit(False, "저장할 수 있는 프레임이 없었습니다.")
            return
        tail = f"\n(읽지 못해 건너뛴 프레임 {skipped}개)" if skipped else ""
        if spec["as_video"]:
            self.done.emit(True, f"{saved} 프레임을 동영상으로 저장했습니다:\n{where}{tail}")
        else:
            self.done.emit(True, f"이미지 {saved}장을 저장했습니다:\n{where}{tail}")

    @staticmethod
    def _close(source, writer) -> None:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        if source is not None:
            source.close()


class ExportDialog(QDialog):
    """구간을 어떻게 저장할지 고르는 창."""

    def __init__(self, pane: "PlayerPane", parent=None,
                 prefer_video: bool | None = None):
        super().__init__(parent)
        self.setWindowTitle("구간 저장")
        self.pane = pane
        self.start, self.end = pane.range_bounds()
        source = pane.source
        stem = os.path.splitext(
            os.path.basename(source.path.rstrip(os.sep)))[0] or "clip"
        parent_dir = (os.path.dirname(source.path.rstrip(os.sep))
                      or os.path.expanduser("~"))
        tag = f"{self.start + 1}-{self.end + 1}"
        self._default_dir = os.path.join(parent_dir, f"{stem}_{tag}")
        self._default_video = os.path.join(parent_dir, f"{stem}_{tag}.mp4")

        info = QLabel(
            f"{os.path.basename(source.path.rstrip(os.sep))}\n"
            f"구간 {self.start + 1} ~ {self.end + 1} 프레임 "
            f"(모두 {self.end - self.start + 1} 프레임)")

        # --- 저장 형식 ---------------------------------------------------
        # 이미지 폴더든 동영상이든 두 형식 다 고를 수 있다.
        self.rb_images = QRadioButton("이미지 프레임 폴더로 저장")
        self.rb_video = QRadioButton("동영상 파일로 저장")
        want_video = (source.kind == "video" if prefer_video is None
                      else bool(prefer_video))
        self.rb_images.setChecked(not want_video)
        self.rb_video.setChecked(want_video)

        hint = QLabel("이미지 폴더를 동영상으로, 동영상을 이미지들로 "
                      "서로 바꿔 저장할 수 있습니다.")
        hint.setStyleSheet("color: gray;")
        if not ffmpeg_available():
            self.rb_video.setEnabled(False)
            self.rb_images.setChecked(True)
            hint.setText(
                "동영상으로 저장하려면 imageio-ffmpeg 가 필요합니다. "
                "지금 실행 중인 파이썬에 설치해 주세요:\n"
                f"    {sys.executable} -m pip install imageio-ffmpeg")
            hint.setStyleSheet("color: #b00;")
        self.rb_images.toggled.connect(self._sync_mode)

        kind_box = QVBoxLayout()
        kind_box.addWidget(self.rb_images)
        kind_box.addWidget(self.rb_video)
        kind_box.addWidget(hint)
        self.grp_kind = QGroupBox("저장 형식")
        self.grp_kind.setLayout(kind_box)

        # --- 이미지로 저장 -----------------------------------------------
        self.ed_dir = QLineEdit(self._default_dir)
        btn_dir = QPushButton("찾아보기…")
        btn_dir.clicked.connect(self._pick_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.ed_dir, 1)
        dir_row.addWidget(btn_dir)

        self.ed_prefix = QLineEdit(f"{stem}_")
        self.cb_format = QComboBox()
        for name, ext, _q in IMAGE_SAVE_FORMATS:
            self.cb_format.addItem(name, ext)
        self.cb_format.currentIndexChanged.connect(self._sync_quality)

        self.sp_iquality = QSpinBox()
        self.sp_iquality.setRange(1, 100)
        self.sp_iquality.setValue(95)
        self.sp_iquality.setSuffix(" %")

        img_form = QFormLayout()
        img_form.addRow("저장 폴더:", dir_row)
        img_form.addRow("파일 이름 앞부분:", self.ed_prefix)
        img_form.addRow("이미지 형식:", self.cb_format)
        img_form.addRow("품질:", self.sp_iquality)
        self.grp_images = QGroupBox("이미지로 저장")
        self.grp_images.setLayout(img_form)

        # --- 동영상으로 저장 ---------------------------------------------
        self.ed_file = QLineEdit(self._default_video)
        btn_file = QPushButton("찾아보기…")
        btn_file.clicked.connect(self._pick_file)
        file_row = QHBoxLayout()
        file_row.addWidget(self.ed_file, 1)
        file_row.addWidget(btn_file)

        self.cb_codec = QComboBox()
        for name, codec in VIDEO_SAVE_CODECS:
            self.cb_codec.addItem(name, codec)

        self.sp_fps = QDoubleSpinBox()
        self.sp_fps.setRange(0.1, 240.0)
        self.sp_fps.setDecimals(2)
        self.sp_fps.setValue(pane.base_fps)
        self.sp_fps.setSuffix(" fps")
        self.sp_fps.setToolTip("저장할 동영상의 프레임 속도입니다.\n"
                               "기본값은 원본의 기준 속도입니다.\n"
                               "이미지 폴더는 아래쪽 '기준 속도' 값을 씁니다.")
        self.sp_fps.valueChanged.connect(self._sync_count)

        self.sp_vquality = QSpinBox()
        self.sp_vquality.setRange(1, 10)
        self.sp_vquality.setValue(7)
        self.sp_vquality.setToolTip("10 에 가까울수록 화질이 좋고 용량이 큽니다.")

        vid_form = QFormLayout()
        vid_form.addRow("저장 파일:", file_row)
        vid_form.addRow("코덱:", self.cb_codec)
        vid_form.addRow("프레임 속도:", self.sp_fps)
        vid_form.addRow("화질:", self.sp_vquality)
        self.grp_video = QGroupBox("동영상으로 저장")
        self.grp_video.setLayout(vid_form)

        # --- 공통 --------------------------------------------------------
        self.sp_step = QSpinBox()
        self.sp_step.setRange(1, 1000)
        self.sp_step.setValue(1)
        self.sp_step.setPrefix("매 ")
        self.sp_step.setSuffix(" 프레임")
        self.sp_step.setToolTip("1 이면 구간의 모든 프레임을 저장합니다.\n"
                                "2 로 하면 한 장씩 건너뜁니다.")
        self.sp_step.valueChanged.connect(self._sync_count)
        self.lbl_count = QLabel()

        step_row = QHBoxLayout()
        step_row.addWidget(QLabel("저장 간격:"))
        step_row.addWidget(self.sp_step)
        step_row.addWidget(self.lbl_count, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(info)
        lay.addWidget(self.grp_kind)
        lay.addWidget(self.grp_images)
        lay.addWidget(self.grp_video)
        lay.addLayout(step_row)
        lay.addWidget(buttons)

        self._sync_mode()
        self._sync_quality()
        self._sync_count()
        self.setMinimumWidth(560)

    # ------------------------------------------------------------ 상태 동기
    @property
    def as_video(self) -> bool:
        return self.rb_video.isChecked()

    def _sync_mode(self) -> None:
        self.grp_images.setVisible(not self.as_video)
        self.grp_video.setVisible(self.as_video)
        self._sync_count()
        self.adjustSize()

    def _sync_quality(self) -> None:
        uses_quality = IMAGE_SAVE_FORMATS[self.cb_format.currentIndex()][2]
        self.sp_iquality.setEnabled(uses_quality)

    def _sync_count(self) -> None:
        n = len(range(self.start, self.end + 1, self.sp_step.value()))
        if self.as_video:
            seconds = n / max(0.1, self.sp_fps.value())
            self.lbl_count.setText(f"→ 동영상 1개 · {n} 프레임 · 약 {seconds:.1f}초")
        else:
            self.lbl_count.setText(f"→ 이미지 {n}장")

    def _pick_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "저장할 폴더 선택", self.ed_dir.text() or self._default_dir)
        if path:
            self.ed_dir.setText(path)

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "저장할 동영상 파일", self.ed_file.text() or self._default_video,
            "동영상 (*.mp4 *.mkv *.avi *.mov);;모든 파일 (*)")
        if path:
            self.ed_file.setText(path)

    # ---------------------------------------------------------------- 확인
    def _on_accept(self) -> None:
        if self.as_video:
            path = self.ed_file.text().strip()
            if not path:
                QMessageBox.warning(self, "구간 저장", "저장할 파일 이름을 적어 주세요.")
                return
            folder = os.path.dirname(os.path.abspath(path))
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as exc:
                QMessageBox.warning(self, "구간 저장", f"폴더를 만들 수 없습니다: {exc}")
                return
            if os.path.exists(path):
                answer = QMessageBox.question(
                    self, "구간 저장", f"파일이 이미 있습니다. 덮어쓸까요?\n{path}")
                if answer != QMessageBox.StandardButton.Yes:
                    return
        else:
            path = self.ed_dir.text().strip()
            if not path:
                QMessageBox.warning(self, "구간 저장", "저장할 폴더를 골라 주세요.")
                return
            try:
                os.makedirs(path, exist_ok=True)
            except OSError as exc:
                QMessageBox.warning(self, "구간 저장", f"폴더를 만들 수 없습니다: {exc}")
                return
            if os.listdir(path):
                answer = QMessageBox.question(
                    self, "구간 저장",
                    f"폴더에 이미 파일이 있습니다. 그대로 저장할까요?\n{path}")
                if answer != QMessageBox.StandardButton.Yes:
                    return
        self.accept()

    def spec(self) -> dict:
        """작업 스레드에 넘길 설정."""
        pane = self.pane
        ext = self.cb_format.currentData()
        uses_quality = IMAGE_SAVE_FORMATS[self.cb_format.currentIndex()][2]
        return {
            "kind": pane.source.kind,
            "path": pane.source.path,
            "fps": pane.base_fps,
            "start": self.start,
            "end": self.end,
            "step": self.sp_step.value(),
            "as_video": self.as_video,
            "out": (self.ed_file.text().strip() if self.as_video
                    else self.ed_dir.text().strip()),
            # 이미지
            "prefix": self.ed_prefix.text(),
            "ext": ext,
            "iquality": self.sp_iquality.value() if uses_quality else -1,
            "digits": max(4, len(str(max(1, pane.count)))),
            # 동영상
            "codec": self.cb_codec.currentData(),
            "out_fps": self.sp_fps.value(),
            "vquality": self.sp_vquality.value(),
        }


# --------------------------------------------------------------------------
# 뷰 칸 (멀티 뷰의 한 화면)
# --------------------------------------------------------------------------

SPEED_MIN, SPEED_MAX = 0.1, 10.0
CACHE_FRAMES = 48

#: 멀티 뷰 분할 목록 — (이름, 행, 열)
VIEW_LAYOUTS = [
    ("1 x 1 (단일 화면)", 1, 1),
    ("1 x 2 (좌우)", 1, 2),
    ("2 x 1 (위아래)", 2, 1),
    ("2 x 2 (4분할)", 2, 2),
    ("1 x 3", 1, 3),
    ("2 x 3 (6분할)", 2, 3),
    ("3 x 3 (9분할)", 3, 3),
]

PANE_HINT = ("우클릭 → 동영상 / 이미지 폴더 열기\n"
             "(파일을 이 칸에 끌어다 놓아도 됩니다)")


class PlayerPane(QFrame):
    """한 화면의 상태를 모두 들고 있는 칸.

    소스, 현재 프레임, 프레임 캐시, 배속, 재생 시계를 칸마다 따로 갖는다.
    그래서 여러 칸을 동시에 재생하거나 한 칸만 재생할 수 있다.
    """

    activated = pyqtSignal(object)   # 클릭되어 활성 칸이 됨
    updated = pyqtSignal(object)     # 소스/위치/재생 상태가 바뀜
    #: 이 칸의 구간을 저장해 달라는 요청 — (칸, 동영상으로 저장할지)
    export_requested = pyqtSignal(object, object)

    def __init__(self, folder_fps: float = 30.0, parent=None):
        super().__init__(parent)
        self.setObjectName("pane")
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        self.source: FrameSource | None = None
        self.index = 0
        self.speed = 1.0
        self.playing = False
        self.folder_fps = float(folder_fps)
        self.active = False
        self._multi = False       # 멀티 뷰일 때만 활성 칸을 파랗게 표시한다
        self.mark_in: int | None = None    # 편집 구간 시작 (없으면 처음부터)
        self.mark_out: int | None = None   # 편집 구간 끝 (없으면 끝까지)

        self._cache: OrderedDict[int, QImage] = OrderedDict()
        self._cache_limit = CACHE_FRAMES
        self._anchor = 0.0
        self._clock = QElapsedTimer()

        # --- 머리글 (멀티 뷰에서만 보인다) -----------------------------
        self.chk_select = QCheckBox()
        self.chk_select.setToolTip("선택한 화면들이 함께 재생·이동됩니다.")
        self.chk_select.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.chk_select.toggled.connect(lambda _=False: self.updated.emit(self))

        self.lbl_name = QLabel("비어 있음")
        self.lbl_name.setStyleSheet("color:#d8d8dc;")

        self.btn_play = QToolButton()
        self.btn_play.setText("▶")
        self.btn_play.setToolTip("이 화면만 재생 / 일시정지")
        self.btn_play.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_play.clicked.connect(self.toggle_play)

        self.btn_menu = QToolButton()
        self.btn_menu.setText("⋯")
        self.btn_menu.setToolTip("열기 / 비우기")
        self.btn_menu.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_menu.clicked.connect(self._show_menu_at_button)

        header = QHBoxLayout()
        header.setContentsMargins(4, 2, 4, 2)
        header.setSpacing(4)
        header.addWidget(self.chk_select)
        header.addWidget(self.lbl_name, 1)
        header.addWidget(self.btn_play)
        header.addWidget(self.btn_menu)
        self.header = QWidget()
        self.header.setLayout(header)
        self.header.setVisible(False)

        self.canvas = ImageCanvas()
        self.canvas.set_placeholder(PANE_HINT)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        lay.addWidget(self.header)
        lay.addWidget(self.canvas, 1)
        self._apply_style()

    # ---------------------------------------------------------------- 상태
    @property
    def count(self) -> int:
        return len(self.source) if self.source else 0

    @property
    def base_fps(self) -> float:
        if self.source is None:
            return self.folder_fps
        return max(0.1, self.source.fps)

    @property
    def rate(self) -> float:
        """실제 초당 프레임 수 = 기준 fps x 배속."""
        return max(0.01, self.base_fps * self.speed)

    def range_bounds(self) -> tuple[int, int]:
        """편집 구간의 (시작, 끝) 프레임. 표시가 없으면 전체 구간."""
        last = max(0, self.count - 1)
        lo = 0 if self.mark_in is None else max(0, min(last, self.mark_in))
        hi = last if self.mark_out is None else max(0, min(last, self.mark_out))
        if hi < lo:
            lo, hi = hi, lo
        return lo, hi

    def has_range(self) -> bool:
        return self.mark_in is not None or self.mark_out is not None

    def set_mark_in(self, index: int | None = None) -> None:
        if self.source is None:
            return
        self.mark_in = self.index if index is None else int(index)
        if self.mark_out is not None and self.mark_out < self.mark_in:
            self.mark_in, self.mark_out = self.mark_out, self.mark_in
        self._sync_header()
        self.updated.emit(self)

    def set_mark_out(self, index: int | None = None) -> None:
        if self.source is None:
            return
        self.mark_out = self.index if index is None else int(index)
        if self.mark_in is not None and self.mark_in > self.mark_out:
            self.mark_in, self.mark_out = self.mark_out, self.mark_in
        self._sync_header()
        self.updated.emit(self)

    def clear_marks(self) -> None:
        if not self.has_range():
            return
        self.mark_in = self.mark_out = None
        self._sync_header()
        self.updated.emit(self)

    @property
    def selected(self) -> bool:
        return self.chk_select.isChecked()

    def set_selected(self, on: bool) -> None:
        self.chk_select.setChecked(bool(on))

    def set_active(self, on: bool) -> None:
        if self.active == bool(on):
            return
        self.active = bool(on)
        self._apply_style()

    def set_header_visible(self, on: bool) -> None:
        self.header.setVisible(bool(on))
        self._multi = bool(on)
        self._apply_style()
        self.canvas.set_placeholder(PANE_HINT if on else
                                    "이미지 폴더나 동영상을 열어 주세요\n"
                                    "(창에 끌어다 놓아도 됩니다)")

    def set_cache_limit(self, frames: int) -> None:
        self._cache_limit = max(4, int(frames))
        while len(self._cache) > self._cache_limit:
            self._cache.popitem(last=False)

    def _apply_style(self) -> None:
        color = "#4c8dff" if (self.active and self._multi) else "#33333a"
        self.setStyleSheet(
            f"QFrame#pane {{ border: 2px solid {color}; border-radius: 4px;"
            f" background: #18181a; }}")

    # ------------------------------------------------------------ 파일 열기
    def load(self, path: str) -> bool:
        path = os.path.abspath(path)
        select_file = None
        try:
            if os.path.isdir(path):
                source: FrameSource = ImageFolderSource(path, self.folder_fps)
            elif os.path.isfile(path):
                ext = os.path.splitext(path)[1].lower()
                if ext in IMAGE_EXTS:      # 이미지를 놓으면 그 폴더를 연다
                    select_file = path
                    source = ImageFolderSource(os.path.dirname(path), self.folder_fps)
                else:
                    source = VideoSource(path)
            else:
                raise ValueError("파일이나 폴더를 찾을 수 없습니다.")
        except Exception as exc:
            QMessageBox.critical(self, "열기 실패", str(exc))
            return False

        self.pause()
        if self.source is not None:
            self.source.close()
        self.source = source
        self._cache.clear()
        self.mark_in = self.mark_out = None
        self.set_selected(True)

        start = 0
        if select_file is not None and isinstance(source, ImageFolderSource):
            name = os.path.basename(select_file)
            if name in source.names:
                start = source.names.index(name)
        self.goto(start)
        self.activated.emit(self)
        self.updated.emit(self)
        return True

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

    def clear(self) -> None:
        self.pause()
        if self.source is not None:
            self.source.close()
        self.source = None
        self.index = 0
        self.mark_in = self.mark_out = None
        self._cache.clear()
        self.canvas.set_image(None)
        self.set_selected(False)
        self._sync_header()
        self.updated.emit(self)

    # ---------------------------------------------------------------- 이동
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
        self._sync_header()

    def seek(self, index: int) -> None:
        """사용자가 직접 위치를 옮길 때 — 재생 중이면 시계도 새 위치로 맞춘다."""
        self.goto(index)
        if self.playing:
            self.reanchor()
        self.updated.emit(self)

    def step(self, delta: int) -> None:
        if not self.source:
            return
        self.pause()
        self.goto(self.index + delta)
        self.updated.emit(self)

    def _image_at(self, index: int):
        img = self._cache.get(index)
        if img is not None:
            self._cache.move_to_end(index)
            return img
        img = self.source.frame(index)
        if img is None:
            return None
        self._cache[index] = img
        while len(self._cache) > self._cache_limit:
            self._cache.popitem(last=False)
        return img

    # ------------------------------------------------------------ 재생 제어
    def set_speed(self, speed: float) -> None:
        speed = max(SPEED_MIN, min(SPEED_MAX, round(float(speed), 2)))
        if speed == self.speed:
            return
        self.speed = speed
        if self.playing:
            self.reanchor()

    def set_folder_fps(self, value: float) -> None:
        self.folder_fps = max(0.1, float(value))
        if isinstance(self.source, ImageFolderSource):
            self.source.fps = self.folder_fps
            if self.playing:
                self.reanchor()

    def reanchor(self, index: float | None = None) -> None:
        self._anchor = float(self.index if index is None else index)
        self._clock.restart()

    def play(self) -> None:
        if not self.source or self.count == 0 or self.playing:
            return
        lo, hi = self.range_bounds()
        if not lo <= self.index < hi:
            self.goto(lo)          # 구간 밖이거나 끝이면 구간 처음부터
        self.playing = True
        self.reanchor()
        self._sync_header()
        self.updated.emit(self)

    def pause(self) -> None:
        if not self.playing:
            return
        self.playing = False
        self._sync_header()
        self.updated.emit(self)

    def toggle_play(self) -> None:
        self.pause() if self.playing else self.play()

    def advance(self, loop: bool) -> None:
        """재생 시계를 보고 지금 보여야 할 프레임으로 넘어간다.

        편집 구간이 정해져 있으면 그 안에서만 재생·반복한다.
        """
        if not self.playing or not self.source:
            return
        lo, hi = self.range_bounds()
        target = self._anchor + self._clock.elapsed() / 1000.0 * self.rate
        index = int(target)
        if index > hi:
            if loop:
                self.reanchor(lo)
                index = lo
            else:
                self.goto(hi)
                self.pause()
                return
        if index != self.index:
            self.goto(index)

    # ---------------------------------------------------------------- 표시
    def _sync_header(self) -> None:
        self.btn_play.setText("❚❚" if self.playing else "▶")
        if self.source is None:
            self.lbl_name.setText("비어 있음")
            return
        name = os.path.basename(self.source.path.rstrip(os.sep)) or self.source.path
        mark = ""
        if self.has_range():
            lo, hi = self.range_bounds()
            mark = f"   ✂ {lo + 1}~{hi + 1}"
        self.lbl_name.setText(f"{name}   {self.index + 1}/{self.count}{mark}")

    # ------------------------------------------------------------ 입력 처리
    def _build_menu(self) -> QMenu:
        menu = QMenu(self)
        act = menu.addAction("동영상 열기…")
        act.triggered.connect(self.open_video_dialog)
        act = menu.addAction("이미지 폴더 열기…")
        act.triggered.connect(self.open_folder_dialog)
        if self.source is not None:
            menu.addSeparator()
            act = menu.addAction("일시정지 (이 화면)" if self.playing
                                 else "재생 (이 화면)")
            act.triggered.connect(self.toggle_play)
            act = menu.addAction("처음으로")
            act.triggered.connect(lambda: self.seek(0))
            menu.addSeparator()
            act = menu.addAction("여기를 구간 시작으로 (I)")
            act.triggered.connect(lambda: self.set_mark_in())
            act = menu.addAction("여기를 구간 끝으로 (O)")
            act.triggered.connect(lambda: self.set_mark_out())
            act = menu.addAction("구간 해제")
            act.setEnabled(self.has_range())
            act.triggered.connect(self.clear_marks)
            act = menu.addAction("구간을 이미지 폴더로 저장…")
            act.triggered.connect(
                lambda: self.export_requested.emit(self, False))
            act = menu.addAction("구간을 동영상으로 저장…")
            act.triggered.connect(
                lambda: self.export_requested.emit(self, True))
            menu.addSeparator()
            act = menu.addAction("함께 재생할 화면으로 선택")
            act.setCheckable(True)
            act.setChecked(self.selected)
            act.toggled.connect(self.set_selected)
            act = menu.addAction("비우기")
            act.triggered.connect(self.clear)
        return menu

    def _show_menu_at_button(self) -> None:
        self.activated.emit(self)
        menu = self._build_menu()
        menu.exec(self.btn_menu.mapToGlobal(
            self.btn_menu.rect().bottomLeft()))

    def contextMenuEvent(self, event) -> None:
        self.activated.emit(self)
        self._build_menu().exec(event.globalPos())
        event.accept()

    def mousePressEvent(self, event) -> None:
        self.activated.emit(self)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self.activated.emit(self)
        if self.source is not None:
            self.toggle_play()
        event.accept()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if urls:
            self.load(urls[0].toLocalFile())
            event.acceptProposedAction()

    def close_source(self) -> None:
        self.pause()
        if self.source is not None:
            self.source.close()
            self.source = None
        self._cache.clear()


# --------------------------------------------------------------------------
# 메인 윈도우
# --------------------------------------------------------------------------

class PlayerWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Data Player")
        self.resize(1000, 720)
        self.setAcceptDrops(True)

        self.panes: list[PlayerPane] = []
        self.active: PlayerPane | None = None
        self.rows, self.cols = 1, 1
        self._folder_fps = 30.0   # 이미지 폴더용 기준 fps (동영상 fps 와 섞지 않는다)
        self._updating_ui = False

        # 재생 바를 끌 때는 이동을 조금씩 묶는다. 동영상은 뒤로 갈 때마다
        # ffmpeg 파이프를 다시 열기 때문에 매 픽셀마다 이동하면 버벅인다.
        self._seek_clock = QElapsedTimer()
        self._seek_clock.start()
        self._pending_seek: int | None = None
        self._export_prefer_video: bool | None = None   # 지난번 저장 형식
        self._export_worker: ExportWorker | None = None

        # 재생 시계: '지금 몇 번째 프레임이어야 하는가'를 경과 시간으로 계산해
        # 디코딩이 느려도 배속이 흐트러지지 않게 한다(늦으면 프레임을 건너뜀).
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)

        self._build_ui()
        self.set_view_layout(1, 1, ask=False)

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

        self.act_export = QAction("구간 저장", self)
        self.act_export.setShortcut(QKeySequence("Ctrl+E"))
        self.act_export.setToolTip("표시한 구간을 이미지 프레임 폴더나 "
                                   "동영상 파일로 저장합니다. (Ctrl+E)")
        self.act_export.triggered.connect(self.export_range)
        tb.addAction(self.act_export)

        tb.addSeparator()

        # --- 멀티 뷰 버튼 ------------------------------------------------
        self.btn_multi = QToolButton()
        self.btn_multi.setText("멀티 뷰")
        self.btn_multi.setToolTip("화면을 나눠 여러 영상을 비교합니다.\n"
                                 "버튼을 누르면 단일 화면 ↔ 4분할을 번갈아 바꾸고,\n"
                                 "옆의 화살표로 다른 분할을 고를 수 있습니다.")
        self.btn_multi.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.btn_multi.clicked.connect(self._toggle_multi_view)

        menu = QMenu(self.btn_multi)
        self._layout_group = QActionGroup(self)
        self._layout_group.setExclusive(True)
        self._layout_actions: list[QAction] = []
        for name, rows, cols in VIEW_LAYOUTS:
            act = menu.addAction(name)
            act.setCheckable(True)
            act.setData((rows, cols))
            act.triggered.connect(
                lambda _=False, r=rows, c=cols: self.set_view_layout(r, c))
            self._layout_group.addAction(act)
            self._layout_actions.append(act)
        menu.addSeparator()
        act = menu.addAction("모든 화면 선택")
        act.triggered.connect(lambda: self._select_all(True))
        act = menu.addAction("모든 화면 선택 해제")
        act.triggered.connect(lambda: self._select_all(False))
        act = menu.addAction("모든 화면 비우기")
        act.triggered.connect(self._clear_all)
        self.btn_multi.setMenu(menu)
        tb.addWidget(self.btn_multi)

        # --- 뷰 격자 -----------------------------------------------------
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)

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
            # 버튼이 포커스를 물고 있으면 Space 가 재생 대신 그 버튼을 누른다.
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_play.setFixedWidth(60)
        self.btn_first.clicked.connect(lambda: self.goto_all(0))
        self.btn_prev.clicked.connect(lambda: self.step(-1))
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_next.clicked.connect(lambda: self.step(+1))
        self.btn_last.clicked.connect(self.goto_end)

        self.slider = SeekSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setTracking(True)
        self.slider.setToolTip("원하는 위치를 클릭하면 바로 이동합니다.")
        self.slider.valueChanged.connect(self._on_slider)
        self.slider.sliderReleased.connect(self._flush_seek)

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
        self.btn_goto.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, s=preset: self.set_speed(s))
            row2.addWidget(b)
        row2.addSpacing(16)
        self.loop_check = QCheckBox("반복")
        self.loop_check.setChecked(True)
        row2.addWidget(self.loop_check)

        # --- 구간 편집 (3행) ---------------------------------------------
        self.btn_mark_in = QPushButton("[ 구간 시작")
        self.btn_mark_in.setToolTip("지금 프레임을 구간 시작으로 표시합니다. (I)")
        self.btn_mark_in.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_mark_in.clicked.connect(self.mark_range_in)

        self.btn_mark_out = QPushButton("구간 끝 ]")
        self.btn_mark_out.setToolTip("지금 프레임을 구간 끝으로 표시합니다. (O)")
        self.btn_mark_out.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_mark_out.clicked.connect(self.mark_range_out)

        self.btn_range_play = QPushButton("구간 재생")
        self.btn_range_play.setToolTip("구간 처음으로 가서 재생합니다.\n"
                                       "반복이 켜져 있으면 구간 안에서만 돕니다.")
        self.btn_range_play.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_range_play.clicked.connect(self.play_range)

        self.btn_range_clear = QPushButton("구간 해제")
        self.btn_range_clear.setToolTip("구간 표시를 지우고 전체를 다시 씁니다.")
        self.btn_range_clear.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_range_clear.clicked.connect(self.clear_range)

        self.lbl_range = QLabel("구간: 전체")

        self.btn_export = QToolButton()
        self.btn_export.setText("구간 저장…")
        self.btn_export.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.btn_export.setToolTip("표시한 구간을 저장합니다. (Ctrl+E)\n"
                                   "옆의 화살표로 저장 형식을 바로 고를 수 있습니다.")
        self.btn_export.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_export.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.btn_export.clicked.connect(self.export_range)

        export_menu = QMenu(self.btn_export)
        act = export_menu.addAction("이미지 프레임 폴더로 저장…")
        act.triggered.connect(lambda: self.export_range(False))
        act = export_menu.addAction("동영상 파일로 저장…")
        act.triggered.connect(lambda: self.export_range(True))
        self.btn_export.setMenu(export_menu)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("편집 구간:"))
        row3.addWidget(self.btn_mark_in)
        row3.addWidget(self.btn_mark_out)
        row3.addWidget(self.btn_range_play)
        row3.addWidget(self.btn_range_clear)
        row3.addSpacing(12)
        row3.addWidget(self.lbl_range, 1)
        row3.addWidget(self.btn_export)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addWidget(self.grid_host, 1)
        lay.addWidget(line)
        lay.addLayout(row1)
        lay.addLayout(row2)
        lay.addLayout(row3)
        self.setCentralWidget(central)

        self.statusBar().showMessage("이미지 폴더나 동영상을 열어 주세요.")

    # ------------------------------------------------------------ 멀티 뷰
    def set_view_layout(self, rows: int, cols: int, ask: bool = True) -> None:
        need = max(1, int(rows) * int(cols))
        doomed = [p for p in self.panes[need:] if p.source is not None]
        if ask and doomed:
            answer = QMessageBox.question(
                self, "멀티 뷰",
                f"화면을 {rows} x {cols} 로 바꾸면 열려 있는 화면 "
                f"{len(doomed)}개가 닫힙니다. 계속할까요?")
            if answer != QMessageBox.StandardButton.Yes:
                self._sync_layout_actions()
                return

        self.rows, self.cols = int(rows), int(cols)

        while len(self.panes) < need:
            pane = PlayerPane(self._folder_fps)
            pane.activated.connect(self._on_pane_activated)
            pane.updated.connect(self._on_pane_updated)
            pane.export_requested.connect(
                lambda p, as_video: self._export_pane(p, as_video))
            self.panes.append(pane)

        for pane in self.panes[need:]:
            pane.close_source()
            self.grid.removeWidget(pane)
            pane.setParent(None)
            pane.deleteLater()
        del self.panes[need:]

        for pane in self.panes:
            self.grid.removeWidget(pane)
        for i, pane in enumerate(self.panes):
            self.grid.addWidget(pane, i // self.cols, i % self.cols)
            pane.set_header_visible(need > 1)
            pane.set_cache_limit(CACHE_FRAMES if need == 1
                                 else max(8, CACHE_FRAMES // need))
        for r in range(max(self.grid.rowCount(), self.rows)):
            self.grid.setRowStretch(r, 1 if r < self.rows else 0)
        for c in range(max(self.grid.columnCount(), self.cols)):
            self.grid.setColumnStretch(c, 1 if c < self.cols else 0)

        if self.active not in self.panes:
            self.active = self.panes[0]
        for pane in self.panes:
            pane.set_active(pane is self.active)

        self._sync_layout_actions()
        self._sync_timer()
        self._update_enabled()
        self._sync_controls()
        if need > 1:
            self.statusBar().showMessage(
                f"{self.rows} x {self.cols} 멀티 뷰 — 각 화면에서 우클릭해 "
                "동영상이나 이미지 폴더를 열어 주세요. "
                "체크한 화면들이 함께 재생됩니다.", 8000)

    def _toggle_multi_view(self) -> None:
        """버튼 본체를 누르면 단일 화면 ↔ 4분할."""
        if len(self.panes) > 1:
            self.set_view_layout(1, 1)
        else:
            self.set_view_layout(2, 2)

    def _sync_layout_actions(self) -> None:
        for act in self._layout_actions:
            act.setChecked(act.data() == (self.rows, self.cols))

    def _select_all(self, on: bool) -> None:
        for pane in self.panes:
            if pane.source is not None or not on:
                pane.set_selected(on)
        self._sync_controls()

    def _clear_all(self) -> None:
        for pane in self.panes:
            pane.clear()
        self._sync_timer()
        self._update_enabled()
        self._sync_controls()

    # ------------------------------------------------------------- 칸 신호
    def _on_pane_activated(self, pane: PlayerPane) -> None:
        if pane is self.active:
            return
        self.active = pane
        for p in self.panes:
            p.set_active(p is pane)
        self._update_enabled()
        self._sync_controls()

    def _on_pane_updated(self, pane: PlayerPane) -> None:
        self._sync_timer()
        self._update_enabled()
        self._sync_controls()

    # ------------------------------------------------------------ 대상 화면
    def _targets(self) -> list[PlayerPane]:
        """아래쪽 컨트롤이 적용될 화면들.

        * 단일 화면이면 그 화면.
        * 활성 화면이 선택되어 있지 않으면 활성 화면 하나만 (원하는 영상만 재생).
        * 그렇지 않으면 체크된 화면 전부 (여러 영상 동시 재생).
        """
        loaded = [p for p in self.panes if p.source is not None]
        if len(self.panes) == 1:
            return loaded
        pane = self.active
        if pane is not None and pane.source is not None and not pane.selected:
            return [pane]
        chosen = [p for p in loaded if p.selected]
        if chosen:
            return chosen
        return [pane] if pane is not None and pane.source is not None else []

    # --------------------------------------------------------- 파일 열기
    def open_folder_dialog(self) -> None:
        if self.active is not None:
            self.active.open_folder_dialog()

    def open_video_dialog(self) -> None:
        if self.active is not None:
            self.active.open_video_dialog()

    def load(self, path: str) -> None:
        """활성 화면에 불러온다 (명령줄 인자, 창에 끌어다 놓기)."""
        if self.active is None:
            return
        self.active.load(path)

    # ------------------------------------------------------------- 이동
    def goto_all(self, index: int) -> None:
        for pane in self._targets():
            pane.seek(index)

    def goto_end(self) -> None:
        # 반복 재생 중에 끝으로 가면 곧바로 처음으로 되감기므로 멈춰 준다.
        for pane in self._targets():
            pane.pause()
            pane.seek(pane.count - 1)

    def step(self, delta: int) -> None:
        for pane in self._targets():
            pane.step(delta)

    def _on_slider(self, value: int) -> None:
        if self._updating_ui:
            return
        # 클릭은 (아직 드래그가 아니므로) 언제나 즉시 이동한다.
        if self.slider.isSliderDown() and self._seek_clock.elapsed() < 60:
            self._pending_seek = value
            return
        self._pending_seek = None
        self._seek_clock.restart()
        for pane in self._targets():
            pane.seek(value)

    def _flush_seek(self) -> None:
        """드래그를 놓을 때 미뤄 둔 위치로 마무리 이동."""
        if self._pending_seek is None:
            return
        value, self._pending_seek = self._pending_seek, None
        self._seek_clock.restart()
        for pane in self._targets():
            pane.seek(value)

    # --------------------------------------------------------- 번호 입력
    def _file_number_mode(self) -> bool:
        """'파일 번호' 모드로 동작 중인가 (활성 화면 기준)."""
        pane = self.active
        return (pane is not None
                and pane.source is not None
                and pane.source.kind == "folder"
                and pane.source.numbers_available()
                and self.mode_combo.currentIndex() == 1)

    def _on_mode_changed(self, _index: int) -> None:
        if self._updating_ui:
            return
        self._sync_controls()

    def _on_number_entered(self, value: int) -> None:
        if self._updating_ui:
            return
        by_number = self._file_number_mode()
        for pane in self._targets():
            src = pane.source
            if (by_number and src.kind == "folder" and src.numbers_available()):
                mapping = src.number_map
                if value in mapping:
                    pane.seek(mapping[value])
                else:
                    nearest = min(mapping, key=lambda n: abs(n - value))
                    pane.seek(mapping[nearest])
                    self.statusBar().showMessage(
                        f"{value} 번 파일이 없어 가장 가까운 {nearest} 번으로 "
                        "이동했습니다.", 4000)
            else:
                pane.seek(value - 1)      # 화면에는 1부터 보여 준다

    # --------------------------------------------------------- 구간 편집
    def mark_range_in(self) -> None:
        for pane in self._targets():
            pane.set_mark_in()

    def mark_range_out(self) -> None:
        for pane in self._targets():
            pane.set_mark_out()

    def clear_range(self) -> None:
        for pane in self._targets():
            pane.clear_marks()

    def play_range(self) -> None:
        for pane in self._targets():
            lo, _hi = pane.range_bounds()
            pane.pause()
            pane.goto(lo)
            pane.play()
        self._sync_timer()
        self._sync_controls()

    def export_range(self, as_video: bool | None = None) -> None:
        """활성 화면의 구간을 저장한다.

        as_video 가 None 이면 지난번에 고른 형식(처음에는 소스 종류)을 쓴다.
        """
        if self.active is None or self.active.source is None:
            self.statusBar().showMessage(
                "저장할 화면을 먼저 열어 주세요.", 4000)
            return
        self._export_pane(self.active, as_video)

    def _export_pane(self, pane: PlayerPane,
                     as_video: bool | None = None) -> None:
        if pane.source is None:
            return
        self.pause_all()                 # 저장하는 동안은 디코딩을 저장에 몰아 준다
        self._on_pane_activated(pane)

        prefer = self._export_prefer_video if as_video is None else as_video
        dialog = ExportDialog(pane, self, prefer_video=prefer)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._export_prefer_video = dialog.as_video    # 다음에도 같은 형식으로
        spec = dialog.spec()
        total = max(1, len(range(spec["start"], spec["end"] + 1, spec["step"])))

        progress = QProgressDialog(
            f"{total} 프레임을 저장하고 있습니다…", "취소", 0, total, self)
        progress.setWindowTitle("구간 저장")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)

        worker = ExportWorker(spec, self)
        self._export_worker = worker     # 작업이 끝나기 전에 지워지지 않게
        worker.progress.connect(lambda i, _n: progress.setValue(i))
        progress.canceled.connect(worker.cancel)

        result: list[tuple[bool, str]] = []

        def on_done(ok: bool, message: str) -> None:
            result.append((ok, message))
            try:
                progress.canceled.disconnect()
            except TypeError:
                pass
            progress.close()

        worker.done.connect(on_done)
        worker.start()
        progress.exec()
        worker.cancel()                  # 창을 닫으면 작업도 멈춘다
        worker.wait()

        if result:
            ok, message = result[0]
            self.statusBar().showMessage(message.replace("\n", " "), 12000)
            if ok:
                QMessageBox.information(self, "구간 저장", message)
            else:
                QMessageBox.warning(self, "구간 저장", message)

    # --------------------------------------------------------- 배속 / 재생
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

    def set_speed(self, speed: float) -> None:
        speed = max(SPEED_MIN, min(SPEED_MAX, round(float(speed), 2)))
        for pane in (self._targets() or self.panes):
            pane.set_speed(speed)
        self._updating_ui = True
        self.speed_spin.setValue(speed)
        self.speed_slider.setValue(self._speed_to_slider(speed))
        self._updating_ui = False
        self._sync_timer()
        self._sync_controls()

    def _on_speed_slider(self, value: int) -> None:
        if self._updating_ui:
            return
        self.set_speed(self._slider_to_speed(value))

    def _on_base_fps_changed(self, value: float) -> None:
        if self._updating_ui:
            return
        self._folder_fps = value      # 폴더를 열기 전에 미리 정해 둘 수도 있다
        for pane in (self._targets() or self.panes):
            pane.set_folder_fps(value)
        self._sync_timer()
        self._sync_controls()

    def _interval(self) -> int:
        """타이머 주기 — 재생 중인 화면 중 가장 빠른 것에 맞춘다."""
        rates = [p.rate for p in self.panes if p.playing]
        rate = max(rates) if rates else 30.0
        return max(4, min(100, int(1000.0 / rate)))

    def toggle_play(self) -> None:
        targets = self._targets()
        if not targets:
            return
        if any(p.playing for p in targets):
            for pane in targets:
                pane.pause()
        else:
            for pane in targets:
                pane.play()
        self._sync_timer()
        self._sync_controls()

    def pause_all(self) -> None:
        for pane in self.panes:
            pane.pause()
        self._sync_timer()

    def _sync_timer(self) -> None:
        if any(p.playing for p in self.panes):
            interval = self._interval()
            if self._timer.isActive():
                if self._timer.interval() != interval:
                    self._timer.setInterval(interval)
            else:
                self._timer.start(interval)
        elif self._timer.isActive():
            self._timer.stop()

    def _on_tick(self) -> None:
        loop = self.loop_check.isChecked()
        for pane in list(self.panes):
            if pane.playing:
                pane.advance(loop)
        self._sync_timer()
        self._sync_controls()

    # -------------------------------------------------------------- 표시
    def _update_enabled(self) -> None:
        pane = self.active
        on = pane is not None and pane.source is not None
        for w in (self.btn_first, self.btn_prev, self.btn_play, self.btn_next,
                  self.btn_last, self.slider, self.num_spin, self.btn_goto,
                  self.speed_slider, self.speed_spin):
            w.setEnabled(on or bool(self._targets()))
        targets = self._targets()
        for w in (self.btn_mark_in, self.btn_mark_out, self.btn_range_play):
            w.setEnabled(bool(targets))
        self.btn_range_clear.setEnabled(any(p.has_range() for p in targets))
        self.btn_export.setEnabled(on)
        self.act_export.setEnabled(on)

        folder = on and pane.source.kind == "folder"
        # 동영상은 파일에 적힌 fps 를 그대로 쓰므로 그때만 잠근다.
        self.fps_spin.setEnabled(not on or folder)
        numbers = bool(folder and pane.source.numbers_available())
        self.mode_combo.setEnabled(numbers)
        if not numbers and self.mode_combo.currentIndex() != 0:
            self._updating_ui = True
            self.mode_combo.setCurrentIndex(0)
            self._updating_ui = False

    def _sync_controls(self) -> None:
        pane = self.active
        targets = self._targets()
        self._updating_ui = True
        self.btn_play.setText("❚❚" if any(p.playing for p in targets) else "▶")

        if pane is not None and pane.source is not None:
            count = pane.count
            self.slider.setRange(0, max(0, count - 1))
            if not self.slider.isSliderDown():
                self.slider.setValue(pane.index)
            if self._file_number_mode():
                nums = pane.source.number_map.keys()
                self.num_spin.setRange(min(nums), max(nums))
                self.total_label.setText(f"/ {max(nums)}")
                num = pane.source.numbers[pane.index]
                if num is not None:
                    self.num_spin.setValue(num)
            else:
                self.num_spin.setRange(1, max(1, count))
                self.total_label.setText(f"/ {count}")
                self.num_spin.setValue(pane.index + 1)
            self.fps_spin.setValue(pane.base_fps)
            self.speed_spin.setValue(pane.speed)
            self.speed_slider.setValue(self._speed_to_slider(pane.speed))
            lo, hi = pane.range_bounds()
            if pane.has_range():
                self.slider.set_marks(lo, hi)
                self.lbl_range.setText(
                    f"구간 {lo + 1} ~ {hi + 1} · {hi - lo + 1} 프레임 "
                    f"({fmt_time(lo / pane.rate)} ~ {fmt_time(hi / pane.rate)})")
            else:
                self.slider.set_marks(None, None)
                self.lbl_range.setText("구간: 전체")
        else:
            self.slider.setRange(0, 0)
            self.num_spin.setRange(0, 0)
            self.total_label.setText("/ 0")
            self.slider.set_marks(None, None)
            self.lbl_range.setText("구간: 전체")
        self._updating_ui = False

        if pane is None or pane.source is None:
            self.time_label.setText("00:0.0 / 00:0.0")
            self.setWindowTitle("Data Player")
            if not self.panes or all(p.source is None for p in self.panes):
                self.statusBar().showMessage(
                    "이미지 폴더나 동영상을 열어 주세요."
                    if len(self.panes) == 1 else
                    "각 화면에서 우클릭해 동영상이나 이미지 폴더를 열어 주세요.")
            return

        rate = pane.rate
        cur = pane.index / rate if rate else 0.0
        total = pane.count / rate if rate else 0.0
        self.time_label.setText(f"{fmt_time(cur)} / {fmt_time(total)}")

        img = pane.canvas._image
        dims = f"{img.width()}x{img.height()}" if img and not img.isNull() else "-"
        kind = "동영상" if pane.source.kind == "video" else "이미지 폴더"
        extra = ""
        if len(self.panes) > 1:
            playing = sum(1 for p in self.panes if p.playing)
            loaded = sum(1 for p in self.panes if p.source is not None)
            extra = (f"   |   대상 {len(targets)}개 · 재생 {playing} / "
                     f"열린 화면 {loaded}개")
        self.setWindowTitle(
            f"Data Player — {os.path.basename(pane.source.path.rstrip(os.sep))}")
        self.statusBar().showMessage(
            f"[{kind}] {pane.source.label(pane.index)}   "
            f"{pane.index + 1} / {pane.count} 프레임   {dims}   "
            f"{pane.base_fps:g} fps x {pane.speed:g} = {rate:.2f} fps{extra}")

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
            self.goto_all(0)
        elif key == Qt.Key.Key_End:
            self.goto_end()
        elif key == Qt.Key.Key_I:
            self.mark_range_in()
        elif key == Qt.Key.Key_O:
            self.mark_range_out()
        elif key == Qt.Key.Key_Up:
            self.set_speed((self.active.speed if self.active else 1.0) * 1.25)
        elif key == Qt.Key.Key_Down:
            self.set_speed((self.active.speed if self.active else 1.0) / 1.25)
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
        self._timer.stop()
        for pane in self.panes:
            pane.close_source()
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
