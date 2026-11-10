r"""Real Qt WebEngine download checks, separate from Fay and model services.

Run: .\.venv\python.exe -m unittest test.test_desktop_download_integration -v
Manual save-dialog check: .\.venv\python.exe -m test.test_desktop_download_integration --manual
The test uses an isolated local HTTP server and temporary output directory.
"""

import base64
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PyQt5.QtCore import QEventLoop, QTimer, QUrl
from PyQt5.QtWebEngineWidgets import (
    QWebEngineDownloadItem,
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineView,
)
from PyQt5.QtWidgets import QApplication, QMainWindow

from gui.window import MainWindow


PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+aD1cAAAAASUVORK5CYII="
)
PNG_BYTES = base64.b64decode(PNG_BASE64)
HTML_BYTES = ("""<!doctype html><meta charset="utf-8">
<title>Fay download verification</title>
<style>body{font:20px sans-serif;padding:40px}button,a{display:block;
margin:24px 0;padding:14px;width:420px}p{color:#47606b}</style>
<h1>Fay 下载保存验收</h1><p>独立测试窗口，不连接用户照片或试衣模型。</p>
<a id="attachment-download" href="/result.png" download="fay-tryon-http.png">保存 HTTP 图片结果</a>
<button id="blob-download">保存 Blob 图片结果</button>
<script>document.getElementById('blob-download').onclick=function(){
const raw=atob('__PNG__');const data=Uint8Array.from(raw,c=>c.charCodeAt(0));
const url=URL.createObjectURL(new Blob([data],{type:'image/png'}));
const link=document.createElement('a');link.href=url;
link.download='fay-tryon-blob.png';document.body.appendChild(link);link.click();
link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
};</script>""").replace("__PNG__", PNG_BASE64).encode("utf-8")


class DownloadHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        is_image = self.path == "/result.png"
        body = PNG_BYTES if is_image else HTML_BYTES
        self.send_response(200)
        self.send_header("Content-Type", "image/png" if is_image else "text/html; charset=utf-8")
        if is_image:
            self.send_header("Content-Disposition", 'attachment; filename="fay-tryon-http.png"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class IsolatedDownloadWindow(MainWindow):
    def __init__(self):
        QMainWindow.__init__(self)
        self._allow_close = True
        self._active_downloads = {}
        self.browser = QWebEngineView(self)
        self.download_profile = QWebEngineProfile(self)
        self.browser.setPage(QWebEnginePage(self.download_profile, self.browser))
        self._configure_browser()
        self.setCentralWidget(self.browser)
        self.setWindowTitle("Fay 下载保存验收（独立测试）")
        self.resize(900, 500)

    def _on_load_finished(self, ok):
        # This fixture intentionally does not launch or wait for Fay services.
        pass


def make_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), DownloadHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class DesktopDownloadIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Keep model GPUs free. The window stays hidden but uses the native Qt
        # platform: Qt WebEngine profiles do not support Windows' offscreen plugin.
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.server = make_server()
        self.window = IsolatedDownloadWindow()
        self.tmp = tempfile.TemporaryDirectory(prefix="fay-qt-download-")
        self.target = Path(self.tmp.name) / "downloaded-result.png"

    def tearDown(self):
        self.window.close()
        self.window.browser.setPage(QWebEnginePage(self.window.browser))
        self.window.download_profile.deleteLater()
        self.app.processEvents()
        self.window.deleteLater()
        self.app.processEvents()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def run_download(self, start_download):
        loop = QEventLoop()
        completed_states = []
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)

        def watch_download(download):
            def finished():
                completed_states.append(download.state())
                loop.quit()

            download.finished.connect(finished)

        self.window.download_profile.downloadRequested.connect(watch_download)
        try:
            with patch("gui.window.QFileDialog.getSaveFileName", return_value=(str(self.target), "")):
                timeout.start(15000)
                QTimer.singleShot(0, start_download)
                loop.exec_()
        finally:
            timeout.stop()
            self.window.download_profile.downloadRequested.disconnect(watch_download)

        self.assertEqual(completed_states, [QWebEngineDownloadItem.DownloadCompleted])
        self.assertEqual(self.target.read_bytes(), PNG_BYTES)
        self.assertEqual(self.window._active_downloads, {})
        self.assertIn(str(self.target), self.window.statusBar().currentMessage())

    def test_real_http_attachment_is_saved_with_exact_bytes(self):
        self.run_download(lambda: self.window.browser.page().download(
            QUrl(f"http://127.0.0.1:{self.server.server_port}/result.png")
        ))

    def test_real_blob_anchor_download_is_saved_with_exact_bytes(self):
        loop = QEventLoop()
        loaded = []
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)

        def page_loaded(ok):
            loaded.append(ok)
            loop.quit()

        self.window.browser.loadFinished.connect(page_loaded)
        timeout.start(15000)
        self.window.browser.load(QUrl(f"http://127.0.0.1:{self.server.server_port}/"))
        loop.exec_()
        timeout.stop()
        self.window.browser.loadFinished.disconnect(page_loaded)
        self.assertEqual(loaded, [True])
        self.run_download(lambda: self.window.browser.page().runJavaScript(
            "document.getElementById('blob-download').click()"
        ))


def manual_check():
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
    app = QApplication.instance() or QApplication([])
    server = make_server()
    window = IsolatedDownloadWindow()
    window.browser.load(QUrl(f"http://127.0.0.1:{server.server_port}/"))
    window.show()
    try:
        return app.exec_()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    if "--manual" in sys.argv:
        sys.exit(manual_check())
    unittest.main()
