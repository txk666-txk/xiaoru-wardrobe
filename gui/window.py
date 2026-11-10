import html
import os
import re
import socket
import sys
import time

from PyQt5 import QtWidgets
from PyQt5.QtCore import QStandardPaths, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QIcon
from PyQt5.QtWebEngineWidgets import (
    QWebEnginePage,
    QWebEngineDownloadItem,
    QWebEngineProfile,
    QWebEngineSettings,
    QWebEngineView,
)
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QVBoxLayout,
)

from scheduler.thread_manager import stopAll


FAY_URL = QUrl("http://127.0.0.1:5000/")
FAY_HOST = "127.0.0.1"
FAY_PORT = 5000


def _resolve_runtime_dir():
    if getattr(sys, "frozen", False):
        return os.path.abspath(os.path.dirname(sys.executable))
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _safe_download_filename(filename):
    filename = os.path.basename(str(filename or "").replace("\\", "/"))
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(" .")
    if not filename:
        return "fay-download.png"
    if filename.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }:
        filename = "fay-" + filename
    return filename


class MainWindow(QMainWindow):
    SigSendMessageToJS = pyqtSignal(str)

    def __init__(self):
        super(MainWindow, self).__init__()
        self._allow_close = False
        self._shutdown_in_progress = False
        self._tray_hint_shown = False
        self._tray_icon = None
        self._backend_attempts = 0
        self._application_loaded = False
        self._active_downloads = {}

        self.setWindowTitle("小如的衣橱 - 本地语音 Agent")
        self.setGeometry(0, 0, 16 * 70, 9 * 70)
        self.showMaximized()

        self.browser = QWebEngineView()
        profile = QWebEngineProfile.defaultProfile()
        profile.clearHttpCache()
        self._configure_browser()
        self.setCentralWidget(self.browser)

        self._init_tray_icon()
        self._show_starting_page()

        self._backend_timer = QTimer(self)
        self._backend_timer.setInterval(400)
        self._backend_timer.timeout.connect(self._wait_for_backend)
        self._backend_timer.start()
        QTimer.singleShot(0, self._wait_for_backend)

    def _configure_browser(self):
        settings = self.browser.settings()
        autoplay_setting = getattr(
            QWebEngineSettings, "PlaybackRequiresUserGesture", None
        )
        if autoplay_setting is not None:
            settings.setAttribute(autoplay_setting, False)

        settings.setAttribute(QWebEngineSettings.FullScreenSupportEnabled, True)
        self.browser.page().featurePermissionRequested.connect(
            self._handle_feature_permission
        )
        self.browser.loadFinished.connect(self._on_load_finished)
        self.browser.page().profile().downloadRequested.connect(
            self._handle_download_requested
        )

    def _handle_download_requested(self, download):
        if download.page() != self.browser.page():
            return
        downloads_dir = QStandardPaths.writableLocation(QStandardPaths.DownloadLocation)
        if not downloads_dir:
            downloads_dir = os.path.expanduser("~")
        suggested_path = os.path.join(
            downloads_dir, _safe_download_filename(download.downloadFileName())
        )
        # The native dialog confirms overwriting an existing file by default.
        save_path, _ = QFileDialog.getSaveFileName(
            self, "保存下载结果", suggested_path, "所有文件 (*)"
        )
        if not save_path:
            download.cancel()
            self.statusBar().showMessage("已取消保存下载结果", 5000)
            return

        download_id = download.id()
        self._active_downloads[download_id] = {
            "item": download,
            "path": save_path,
        }
        download.finished.connect(lambda: self._on_download_finished(download_id))
        try:
            download.setPath(save_path)
            download.accept()
        except Exception as exc:
            self._active_downloads.pop(download_id, None)
            download.cancel()
            self.statusBar().showMessage("下载失败，未保存结果", 10000)
            QMessageBox.warning(self, "下载失败", f"无法保存下载结果：{exc}")
            return
        if download_id in self._active_downloads:
            self.statusBar().showMessage(f"正在保存：{save_path}")

    def _on_download_finished(self, download_id):
        active_download = self._active_downloads.pop(download_id, None)
        if active_download is None:
            return
        download = active_download["item"]
        if download.state() == QWebEngineDownloadItem.DownloadCompleted:
            self.statusBar().showMessage(
                f"下载已保存：{active_download['path']}", 15000
            )
        elif download.state() == QWebEngineDownloadItem.DownloadCancelled:
            self.statusBar().showMessage("已取消保存下载结果", 5000)
        else:
            reason = download.interruptReasonString() or "请检查保存位置及网络连接"
            self.statusBar().showMessage(f"下载失败：{reason}", 10000)
            QMessageBox.warning(self, "下载失败", f"未能保存下载结果：{reason}")

    def _show_starting_page(self, detail="正在等待本地服务启动，请稍候…"):
        page_html = """
            <!doctype html>
            <html lang="zh-CN">
              <head>
                <meta charset="utf-8">
                <style>
                  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
                         color: #253047; background: #f4f7fb;
                         font-family: "Microsoft YaHei UI", sans-serif; }
                  main { width: min(520px, 80vw); padding: 42px; text-align: center;
                         background: white; border-radius: 18px;
                         box-shadow: 0 14px 40px rgba(32, 55, 90, .12); }
                  .dot { width: 14px; height: 14px; margin: 0 auto 24px;
                         border-radius: 50%; background: #597ef7;
                         box-shadow: 0 0 0 0 rgba(89, 126, 247, .45);
                         animation: pulse 1.25s infinite; }
                  h1 { margin: 0 0 12px; font-size: 25px; }
                  p { margin: 0; color: #65728a; line-height: 1.7; }
                  @keyframes pulse { 70% { box-shadow: 0 0 0 16px rgba(89,126,247,0); } }
                </style>
              </head>
              <body><main><div class="dot"></div><h1>小如正在准备</h1><p>__DETAIL__</p></main></body>
            </html>
            """
        self.browser.setHtml(
            page_html.replace("__DETAIL__", html.escape(str(detail)))
        )

    @staticmethod
    def _is_backend_ready():
        try:
            with socket.create_connection((FAY_HOST, FAY_PORT), timeout=0.2):
                return True
        except OSError:
            return False

    def _wait_for_backend(self):
        if self._application_loaded:
            self._backend_timer.stop()
            return

        self._backend_attempts += 1
        if not self._is_backend_ready():
            if self._backend_attempts % 25 == 0:
                waited = max(1, round(self._backend_attempts * 0.4))
                self._show_starting_page(f"本地服务仍在准备中（已等待 {waited} 秒）…")
            return

        self._backend_timer.stop()
        self.browser.load(FAY_URL)

    def _on_load_finished(self, ok):
        current_url = self.browser.url()
        if current_url.host() not in {"127.0.0.1", "localhost"}:
            return

        if ok:
            self._application_loaded = True
            self.browser.setFocus()
            return

        self._application_loaded = False
        self._backend_attempts = 0
        self._show_starting_page("页面暂时没有加载成功，正在自动重试…")
        QTimer.singleShot(1200, self._backend_timer.start)

    def _handle_feature_permission(self, origin, feature):
        media_features = {
            value
            for value in (
                getattr(QWebEnginePage, "MediaAudioCapture", None),
                getattr(QWebEnginePage, "MediaVideoCapture", None),
                getattr(QWebEnginePage, "MediaAudioVideoCapture", None),
            )
            if value is not None
        }
        is_local = origin.host() in {"127.0.0.1", "localhost"}
        policy = (
            QWebEnginePage.PermissionGrantedByUser
            if is_local and feature in media_features
            else QWebEnginePage.PermissionDeniedByUser
        )
        self.browser.page().setFeaturePermission(origin, feature, policy)

    def _resolve_app_icon(self):
        runtime_dir = _resolve_runtime_dir()
        for icon_name in ("favicon.ico", "icon.png"):
            icon_path = os.path.join(runtime_dir, icon_name)
            if os.path.exists(icon_path):
                return QIcon(icon_path)
        return self.windowIcon()

    def _init_tray_icon(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        tray_icon = QSystemTrayIcon(self)
        tray_icon.setIcon(self._resolve_app_icon())
        tray_icon.setToolTip("小如的衣橱")

        tray_menu = QMenu(self)
        show_action = QAction("打开小如的衣橱", self)
        show_action.triggered.connect(self.show_from_tray)
        tray_menu.addAction(show_action)

        browser_action = QAction("在浏览器中打开", self)
        browser_action.triggered.connect(lambda: QDesktopServices.openUrl(FAY_URL))
        tray_menu.addAction(browser_action)

        tray_menu.addSeparator()

        exit_action = QAction("退出", self)
        exit_action.triggered.connect(self.exit_from_tray)
        tray_menu.addAction(exit_action)

        tray_icon.setContextMenu(tray_menu)
        tray_icon.activated.connect(self._on_tray_icon_activated)
        tray_icon.show()
        self._tray_icon = tray_icon

    def _show_tray_message_once(self):
        if self._tray_icon is None or self._tray_hint_shown:
            return
        self._tray_hint_shown = True
        self._tray_icon.showMessage(
            "小如的衣橱",
            "窗口已最小化到托盘，双击图标可以恢复。",
            QSystemTrayIcon.Information,
            3000,
        )

    def _shutdown_services(self):
        if self._shutdown_in_progress:
            return

        self._shutdown_in_progress = True
        try:
            import fay_booter

            if fay_booter.is_running():
                print("Stopping Fay services...")
                fay_booter.stop()
                time.sleep(0.5)
        except BaseException as exc:
            print(f"Failed to stop Fay services: {exc}")

        try:
            stopAll()
        except BaseException as exc:
            print(f"Failed to stop background threads: {exc}")

    def show_from_tray(self):
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def exit_from_tray(self):
        self._allow_close = True
        if self._tray_icon is not None:
            self._tray_icon.hide()
        self._shutdown_services()
        QApplication.instance().quit()
        os._exit(0)

    def _on_tray_icon_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_from_tray()

    def closeEvent(self, event):
        if self._allow_close:
            event.accept()
            return

        if self._tray_icon is None:
            event.ignore()
            self.exit_from_tray()
            return

        event.ignore()
        self.hide()
        self._show_tray_message_once()

    def center(self):
        screen = QtWidgets.QDesktopWidget().screenGeometry()
        size = self.geometry()
        self.move((screen.width() - size.width()) / 2, (screen.height() - size.height()) / 2)

    def keyPressEvent(self, event):
        reload_shortcut = (
            event.key() == Qt.Key_F5
            or (
                event.key() == Qt.Key_R
                and bool(event.modifiers() & Qt.ControlModifier)
            )
        )
        if reload_shortcut:
            self.browser.reload()
            event.accept()
            return
        super().keyPressEvent(event)

    def OnReceiveMessageFromJS(self, strParameter):
        if not strParameter:
            return


class TDevWindow(QDialog):
    def __init__(self):
        super(TDevWindow, self).__init__()
        self.init_ui()

    def init_ui(self):
        self.mpJSWebView = QWebEngineView(self)
        self.url = "https://www.baidu.com/"
        self.mpJSWebView.page().load(QUrl(self.url))
        self.mpJSWebView.show()

        self.pJSTotalVLayout = QVBoxLayout()
        self.pJSTotalVLayout.setSpacing(0)
        self.pJSTotalVLayout.addWidget(self.mpJSWebView)
        self.pWebGroup = QGroupBox("Web View", self)
        self.pWebGroup.setLayout(self.pJSTotalVLayout)

        self.mainLayout = QHBoxLayout()
        self.mainLayout.setSpacing(5)
        self.mainLayout.addWidget(self.pWebGroup)
        self.setLayout(self.mainLayout)
        self.setMinimumSize(800, 800)
