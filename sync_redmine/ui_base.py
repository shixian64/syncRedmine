# -*- coding: utf-8 -*-
"""UI 基础组件：阴影、渐变面板、动画对话框、平滑滚动、图标生成。"""

from PyQt5 import QtCore
from PyQt5.QtWidgets import (
    QLabel, QFrame, QDialog, QScrollArea,
    QGraphicsDropShadowEffect,
)
from PyQt5.QtCore import (
    Qt, QRectF, QPoint,
    QPropertyAnimation, QEasingCurve, QAbstractAnimation,
)
from PyQt5.QtGui import (
    QFont, QIcon, QPixmap, QPainter, QColor,
    QLinearGradient, QPainterPath,
)


# ═══════════════════════════════════════════════════════════════════════════════
# UI 基础
# ═══════════════════════════════════════════════════════════════════════════════
def apply_shadow(widget, blur=42, y_offset=12, alpha=30):
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(blur)
    shadow.setOffset(0, y_offset)
    shadow.setColor(QColor(15, 23, 42, alpha))
    widget.setGraphicsEffect(shadow)


def make_badge(text, bg='#dbeafe', fg='#1d4ed8'):
    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    label.setStyleSheet(
        "QLabel{"
        f"background:{bg};color:{fg};"
        "border-radius:11px;padding:6px 10px;"
        "font-size:9pt;font-weight:700;}")
    return label


def tint_badge(label, text, bg, fg):
    label.setText(text)
    label.setStyleSheet(
        "QLabel{"
        f"background:{bg};color:{fg};"
        "border-radius:11px;padding:6px 10px;"
        "font-size:9pt;font-weight:700;}")


def make_divider():
    line = QFrame()
    line.setObjectName("Divider")
    return line


class GradientPanel(QFrame):
    def __init__(self, start_color, end_color, glow_color, parent=None):
        super().__init__(parent)
        self.start_color = QColor(start_color)
        self.end_color = QColor(end_color)
        self.glow_color = QColor(glow_color)
        self.setMinimumHeight(156)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        path = QPainterPath()
        path.addRoundedRect(rect, 24, 24)

        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0, self.start_color)
        gradient.setColorAt(1, self.end_color)
        painter.fillPath(path, gradient)
        painter.setClipPath(path)

        glow = QColor(self.glow_color)
        glow.setAlpha(85)
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QRectF(rect.width() * 0.64, -rect.height() * 0.18,
                                   rect.width() * 0.42, rect.height() * 0.80))

        soft = QColor(255, 255, 255, 26)
        painter.setBrush(soft)
        painter.drawEllipse(QRectF(-rect.width() * 0.10, rect.height() * 0.62,
                                   rect.width() * 0.36, rect.height() * 0.48))

        painter.setPen(QColor(255, 255, 255, 34))
        painter.drawPath(path)
        super().paintEvent(event)


class AnimatedDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._fade_anim = None
        self._has_animated = False

    def showEvent(self, event):
        super().showEvent(event)
        if self._has_animated:
            return
        self._has_animated = True
        self.setWindowOpacity(0.0)
        self._fade_anim = QPropertyAnimation(self, b'windowOpacity', self)
        self._fade_anim.setDuration(220)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._fade_anim.start()


class SmoothScrollArea(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scroll_anim = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._scroll_anim.setDuration(180)
        self._scroll_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._target_value = 0

        bar = self.verticalScrollBar()
        bar.setSingleStep(24)
        bar.rangeChanged.connect(self._on_range_changed)

    def _clamp_value(self, value):
        bar = self.verticalScrollBar()
        return max(bar.minimum(), min(bar.maximum(), int(value)))

    def _on_range_changed(self, minimum, maximum):
        self._target_value = max(minimum, min(maximum, self._target_value))
        if self._scroll_anim.state() == QAbstractAnimation.Running:
            current = self.verticalScrollBar().value()
            target = self._clamp_value(self._target_value)
            if target != self._scroll_anim.endValue():
                self._scroll_anim.stop()
                self._scroll_anim.setStartValue(current)
                self._scroll_anim.setEndValue(target)
                self._scroll_anim.start()

    def animate_to(self, value, duration=None):
        bar = self.verticalScrollBar()
        target = self._clamp_value(value)
        current = bar.value()
        if target == current and self._scroll_anim.state() != QAbstractAnimation.Running:
            self._target_value = target
            return

        distance = abs(target - current)
        self._target_value = target
        self._scroll_anim.stop()
        self._scroll_anim.setDuration(duration or max(140, min(260, 140 + distance // 4)))
        self._scroll_anim.setStartValue(current)
        self._scroll_anim.setEndValue(target)
        self._scroll_anim.start()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            super().wheelEvent(event)
            return

        bar = self.verticalScrollBar()
        if bar.maximum() <= bar.minimum():
            super().wheelEvent(event)
            return

        pixel_delta = event.pixelDelta().y()
        if pixel_delta:
            distance = -pixel_delta
        else:
            angle = event.angleDelta().y()
            if not angle:
                super().wheelEvent(event)
                return
            distance = -(angle / 120.0) * (bar.singleStep() * 2.35)

        base = self._target_value if self._scroll_anim.state() == QAbstractAnimation.Running else bar.value()
        self.animate_to(base + distance)
        event.accept()

    def scroll_widget_into_view(self, widget, top_margin=20, bottom_margin=28, animate=True):
        container = self.widget()
        if not container or widget is None:
            return

        top = widget.mapTo(container, QPoint(0, 0)).y()
        height = max(widget.height(), widget.sizeHint().height())
        bottom = top + height

        bar = self.verticalScrollBar()
        view_top = bar.value()
        view_bottom = view_top + self.viewport().height()

        target = view_top
        if top - top_margin < view_top:
            target = top - top_margin
        elif bottom + bottom_margin > view_bottom:
            target = bottom + bottom_margin - self.viewport().height()

        target = self._clamp_value(target)
        if animate:
            self.animate_to(target, duration=180)
        else:
            self._target_value = target
            bar.setValue(target)


# ═══════════════════════════════════════════════════════════════════════════════
# 图标
# ═══════════════════════════════════════════════════════════════════════════════
def make_icon(color='#4a90d9', badge=None):
    pix = QPixmap(36, 36)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    base = QColor(color)
    rect = QRectF(2, 2, 32, 32)
    path = QPainterPath()
    path.addRoundedRect(rect, 10, 10)
    grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
    grad.setColorAt(0, base.lighter(118))
    grad.setColorAt(1, base.darker(110))
    p.fillPath(path, grad)
    p.setPen(QColor(255, 255, 255, 42))
    p.drawPath(path)
    p.setPen(Qt.white)
    p.setFont(QFont('', 13, QFont.Bold))
    p.drawText(pix.rect(), Qt.AlignCenter, 'R')
    if badge:
        p.setBrush(QColor('#f44336'))
        p.setPen(Qt.NoPen)
        p.drawEllipse(23, 1, 11, 11)
        p.setPen(Qt.white)
        p.setFont(QFont('', 7, QFont.Bold))
        p.drawText(QtCore.QRect(23, 1, 11, 11), Qt.AlignCenter, badge[:1])
    p.end()
    return QIcon(pix)
