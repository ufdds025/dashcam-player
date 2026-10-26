"""Speed ROI 調整對話框

提供 UI 讓用戶框選車道標線偵測區域，支援：
- 影片畫面顯示
- 可拖曳/縮放的 ROI 選取框
- 自動偵測按鈕（呼叫 SpeedROIDetector）
- 側向選擇（left/right/both）
"""

from __future__ import annotations

import cv2
import numpy as np
from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from dashcam_player.models.speed_roi import SpeedROISetting
from dashcam_player.utils.cv_utils import open_video_capture
from dashcam_player.utils.log import get_logger

log = get_logger()


class _ResizableRectItem(QGraphicsRectItem):
    """可拖曳/縮放的矩形選取框（共用元件）"""

    HANDLE_SIZE = 8
    HANDLE_MARGIN = 4

    def __init__(self, rect: QRectF, parent=None) -> None:
        super().__init__(rect, parent)
        self._resizing = False
        self._dragging = False
        self._drag_start: QPointF | None = None
        self._rect_start: QRectF | None = None
        self._resize_handle = -1
        self.setAcceptHoverEvents(True)
        self.setFlags(
            QGraphicsRectItem.ItemIsSelectable
            | QGraphicsRectItem.ItemSendsGeometryChanges
        )
        pen = QPen(QColor(0, 255, 0), 2)
        self.setPen(pen)
        self.setBrush(QBrush(QColor(0, 255, 0, 30)))
        self.setZValue(10)

    def _handles(self, r: QRectF) -> list[tuple[float, float]]:
        return [
            (r.left(), r.top()),
            (r.right(), r.top()),
            (r.left(), r.bottom()),
            (r.right(), r.bottom()),
            (r.center().x(), r.top()),
            (r.right(), r.center().y()),
            (r.center().x(), r.bottom()),
            (r.left(), r.center().y()),
        ]

    def _handle_at(self, pos) -> int:
        r = self.rect()
        for i, (hx, hy) in enumerate(self._handles(r)):
            if abs(pos.x() - hx) <= self.HANDLE_MARGIN + self.HANDLE_SIZE // 2 and abs(
                pos.y() - hy
            ) <= self.HANDLE_MARGIN + self.HANDLE_SIZE // 2:
                return i
        return -1

    def hoverMoveEvent(self, event) -> None:
        handle = self._handle_at(event.pos())
        if handle >= 0:
            self.setCursor(Qt.SizeAllCursor if handle < 4 else Qt.SizeFDiagCursor)
        else:
            self.setCursor(Qt.ArrowCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            handle = self._handle_at(event.pos())
            if handle >= 0:
                self._resizing = True
                self._resize_handle = handle
                self._drag_start = event.scenePos()
                self._rect_start = self.rect()
                event.accept()
                return
            if self.contains(event.pos()):
                self._dragging = True
                self._drag_start = event.scenePos()
                self._rect_start = self.rect()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._resizing and self._drag_start and self._rect_start:
            delta = event.scenePos() - self._drag_start
            r = QRectF(self._rect_start)
            h = self._resize_handle
            if h == 0:
                r.setLeft(r.left() + delta.x())
                r.setTop(r.top() + delta.y())
            elif h == 1:
                r.setRight(r.right() + delta.x())
                r.setTop(r.top() + delta.y())
            elif h == 2:
                r.setLeft(r.left() + delta.x())
                r.setBottom(r.bottom() + delta.y())
            elif h == 3:
                r.setRight(r.right() + delta.x())
                r.setBottom(r.bottom() + delta.y())
            elif h == 4:
                r.setTop(r.top() + delta.y())
            elif h == 5:
                r.setRight(r.right() + delta.x())
            elif h == 6:
                r.setBottom(r.bottom() + delta.y())
            elif h == 7:
                r.setLeft(r.left() + delta.x())
            r = r.normalized()
            parent_rect = self.parentItem().boundingRect() if self.parentItem() else QRectF(0, 0, 1, 1)
            if r.left() < parent_rect.left():
                r.moveLeft(parent_rect.left())
            if r.top() < parent_rect.top():
                r.moveTop(parent_rect.top())
            if r.right() > parent_rect.right():
                r.moveRight(parent_rect.right())
            if r.bottom() > parent_rect.bottom():
                r.moveBottom(parent_rect.bottom())
            if r.width() > 20 and r.height() > 8:
                self.setRect(r)
            event.accept()
            return
        if self._dragging and self._drag_start and self._rect_start:
            delta = event.scenePos() - self._drag_start
            r = QRectF(self._rect_start)
            r.translate(delta.x(), delta.y())
            parent_rect = self.parentItem().boundingRect() if self.parentItem() else QRectF(0, 0, 1, 1)
            if r.left() < parent_rect.left():
                r.moveLeft(parent_rect.left())
            if r.top() < parent_rect.top():
                r.moveTop(parent_rect.top())
            if r.right() > parent_rect.right():
                r.moveRight(parent_rect.right())
            if r.bottom() > parent_rect.bottom():
                r.moveBottom(parent_rect.bottom())
            self.setRect(r)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._resizing = False
        self._dragging = False
        self._drag_start = None
        self._rect_start = None
        super().mouseReleaseEvent(event)

    def paint(self, painter, option, widget=None) -> None:
        super().paint(painter, option, widget)
        r = self.rect()
        painter.setPen(QPen(QColor(0, 255, 0), 1))
        painter.setBrush(QBrush(QColor(0, 255, 0, 180)))
        for hx, hy in self._handles(r):
            painter.drawRect(
                int(hx - self.HANDLE_SIZE // 2),
                int(hy - self.HANDLE_SIZE // 2),
                self.HANDLE_SIZE,
                self.HANDLE_SIZE,
            )


class _ImageItem(QGraphicsRectItem):
    """顯示影像的底層項目"""

    def __init__(self, pixmap: QPixmap, parent=None) -> None:
        super().__init__(parent)
        self._pixmap = pixmap
        self.setRect(0, 0, pixmap.width(), pixmap.height())

    def paint(self, painter, option, widget=None) -> None:
        painter.drawPixmap(0, 0, self._pixmap)


class SpeedROIAdjustDialog(QDialog):
    """車道標線偵測區域設定對話框"""

    def __init__(
        self,
        video_path: str,
        video_width: int,
        video_height: int,
        file_format: str,
        app_settings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._video_path = video_path
        self._video_width = video_width
        self._video_height = video_height
        self._file_format = file_format
        self._app_settings = app_settings
        self._roi_setting: SpeedROISetting | None = None
        self._frame: np.ndarray | None = None
        self._image_item: _ImageItem | None = None
        self._auto_hood: float | None = None  # 自動偵測時的車體上緣（_accept 存檔用）

        self.setWindowTitle("車道標線偵測區域設定")
        self.setMinimumSize(800, 600)

        self._init_ui()
        self._load_frame()
        self._apply_existing_roi()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # 影像顯示區域
        self._scene = QGraphicsScene(self)
        self._view = QGraphicsView(self._scene)
        self._view.setRenderHint(QPainter.SmoothPixmapTransform)
        self._view.setDragMode(QGraphicsView.NoDrag)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._view.setMinimumHeight(400)
        layout.addWidget(self._view, 1)

        # 資訊列
        info_layout = QHBoxLayout()
        info_layout.addWidget(QLabel(f"解析度：{self._video_width} x {self._video_height}"))
        info_layout.addWidget(QLabel(f"檔案格式：{self._file_format}"))
        info_layout.addStretch()
        info_layout.addWidget(QLabel("拖曳邊框縮放，拖曳區域移動"))
        layout.addLayout(info_layout)

        # 設定區
        settings_group = QGroupBox("ROI 設定")
        settings_layout = QHBoxLayout(settings_group)

        settings_layout.addWidget(QLabel("標線側向："))
        self._side_combo = QComboBox()
        self._side_combo.addItems(["both", "left", "right"])
        self._side_combo.setToolTip("both: 兩側都有標線\nleft: 只有左側有標線\nright: 只有右側有標線")
        settings_layout.addWidget(self._side_combo)

        settings_layout.addStretch()

        self._auto_btn = QPushButton("自動偵測")
        self._auto_btn.clicked.connect(self._auto_detect)
        settings_layout.addWidget(self._auto_btn)

        layout.addWidget(settings_group)

        # 按鈕
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self._accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _load_frame(self) -> None:
        """從影片取樣一幀作為背景"""
        cap = open_video_capture(self._video_path)
        if not cap.isOpened():
            log.warning("無法開啟影片取樣幀")
            return
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return
        self._frame = frame
        self._display_frame(frame)

    def _display_frame(self, frame: np.ndarray, roi_rect: tuple[float, float, float, float] | None = None) -> None:
        """顯示影像並疊加 ROI 選取框"""
        self._scene.clear()
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = frame.shape[:2]
        qimg = QImage(rgb.tobytes(), w, h, rgb.strides[0], QImage.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(qimg)

        image_item = _ImageItem(pixmap)
        self._scene.addItem(image_item)
        self._image_item = image_item

        if roi_rect is not None:
            rx, ry, rw, rh = roi_rect
            rect = QRectF(rx * w, ry * h, rw * w, rh * h)
        else:
            # 預設 ROI：畫面中段 40%-85%
            rect = QRectF(w * 0.05, h * 0.40, w * 0.90, h * 0.45)

        self._roi_item = _ResizableRectItem(rect, parent=image_item)
        self._view.fitInView(image_item, Qt.KeepAspectRatio)

    def _apply_existing_roi(self) -> None:
        """套用現有的 ROI 設定（如果有）"""
        roi = self._app_settings.load_speed_roi(
            self._video_width, self._video_height, self._file_format
        )
        if roi and self._frame is not None:
            self._side_combo.setCurrentText(roi.side)
            self._display_frame(
                self._frame,
                (roi.roi_x, roi.roi_y, roi.roi_w, roi.roi_h),
            )

    def _auto_detect(self) -> None:
        """觸發自動偵測"""
        self._auto_btn.setEnabled(False)
        self._auto_btn.setText("偵測中...")

        from PyQt5.QtCore import QCoreApplication
        QCoreApplication.processEvents()

        try:
            from dashcam_player.utils.speed_roi_detector import SpeedROIDetector
            detector = SpeedROIDetector(self._video_path)
            result = detector.detect()
            if result is not None and self._frame is not None:
                self._display_frame(self._frame, result.detected_roi)
                self._side_combo.setCurrentText(result.detected_side)
                # 記住本次車體上緣，_accept 存檔時一併寫入（跨機器 key 撞車比對用）；
                # 純手調（未按自動偵測）則為 None
                self._auto_hood = result.hood_top_y
                log.info(f"SpeedROI 自動偵測成功: side={result.detected_side}")
            else:
                log.info("SpeedROI 自動偵測未找到標線")
        except Exception as e:
            log.warning(f"SpeedROI 自動偵測失敗: {e}")
        finally:
            self._auto_btn.setEnabled(True)
            self._auto_btn.setText("自動偵測")

    def _accept(self) -> None:
        """確認並儲存"""
        if self._roi_item is None or self._frame is None:
            self.reject()
            return

        r = self._roi_item.rect()
        h, w = self._frame.shape[:2]
        rx = r.left() / w
        ry = r.top() / h
        rw = r.width() / w
        rh = r.height() / h

        self._roi_setting = SpeedROISetting(
            resolution_w=self._video_width,
            resolution_h=self._video_height,
            file_format=self._file_format,
            roi_x=rx,
            roi_y=ry,
            roi_w=rw,
            roi_h=rh,
            side=self._side_combo.currentText(),
            source="user_adjusted",
            confidence=1.0,
            hood_top_y=self._auto_hood,
        )

        self._app_settings.save_speed_roi(self._roi_setting)
        self.accept()

    def get_result(self) -> SpeedROISetting | None:
        return self._roi_setting
