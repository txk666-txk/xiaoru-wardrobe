"""Download lifecycle regression coverage without opening a native dialog."""

import os
import unittest
from unittest.mock import Mock, patch

from PyQt5.QtWebEngineWidgets import QWebEngineDownloadItem

from gui.window import MainWindow, _safe_download_filename


TEST_PAGE = object()


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self):
        for callback in self.callbacks:
            callback()


class FakeDownload:
    def __init__(self, download_id=1, filename="fay-tryon.png"):
        self.download_id = download_id
        self.filename = filename
        self.finished = FakeSignal()
        self.current_state = QWebEngineDownloadItem.DownloadRequested
        self.saved_path = None
        self.accepted = False
        self.cancelled = False

    def id(self):
        return self.download_id

    def page(self):
        return TEST_PAGE

    def downloadFileName(self):
        return self.filename

    def setPath(self, path):
        self.saved_path = path

    def accept(self):
        self.accepted = True
        self.current_state = QWebEngineDownloadItem.DownloadInProgress

    def cancel(self):
        self.cancelled = True
        self.finish(QWebEngineDownloadItem.DownloadCancelled)

    def state(self):
        return self.current_state

    def interruptReasonString(self):
        return "File access denied"

    def finish(self, state):
        self.current_state = state
        self.finished.emit()


class DownloadHost:
    _handle_download_requested = MainWindow._handle_download_requested
    _on_download_finished = MainWindow._on_download_finished

    def __init__(self):
        self._active_downloads = {}
        self.browser = Mock()
        self.browser.page.return_value = TEST_PAGE
        self.status = Mock()

    def statusBar(self):
        return self.status


class DesktopDownloadTests(unittest.TestCase):
    def setUp(self):
        self.host = DownloadHost()
        self.downloads_dir = os.path.join(os.path.abspath(os.sep), "Downloads")
        self.save_path = os.path.join(self.downloads_dir, "tryon.png")
        self.dialog = patch(
            "gui.window.QFileDialog.getSaveFileName",
            return_value=(self.save_path, "所有文件 (*)"),
        ).start()
        self.warning = patch("gui.window.QMessageBox.warning").start()
        patch(
            "gui.window.QStandardPaths.writableLocation",
            return_value=self.downloads_dir,
        ).start()
        self.addCleanup(patch.stopall)

    def test_save_accepts_request_and_reports_completed_path(self):
        download = FakeDownload()
        self.host._handle_download_requested(download)
        self.assertTrue(download.accepted)
        self.assertEqual(download.saved_path, self.save_path)
        self.assertIs(self.host._active_downloads[1]["item"], download)
        self.assertIn("正在保存", self.host.status.showMessage.call_args.args[0])

        download.finish(QWebEngineDownloadItem.DownloadCompleted)
        self.assertEqual(self.host._active_downloads, {})
        self.host.status.showMessage.assert_called_with(
            "下载已保存：" + self.save_path, 15000
        )
        self.warning.assert_not_called()

    def test_cancelled_save_dialog_never_accepts_or_tracks_request(self):
        self.dialog.return_value = ("", "")
        download = FakeDownload()
        self.host._handle_download_requested(download)
        self.assertTrue(download.cancelled)
        self.assertFalse(download.accepted)
        self.assertIsNone(download.saved_path)
        self.assertEqual(self.host._active_downloads, {})
        self.warning.assert_not_called()

    def test_shared_profile_download_from_other_window_is_ignored(self):
        download = FakeDownload()
        download.page = Mock(return_value=object())
        self.host._handle_download_requested(download)
        self.dialog.assert_not_called()
        self.assertFalse(download.accepted)
        self.assertFalse(download.cancelled)
        self.assertEqual(self.host._active_downloads, {})

    def test_interrupted_download_reports_failure_and_releases_reference(self):
        download = FakeDownload()
        self.host._handle_download_requested(download)
        download.finish(QWebEngineDownloadItem.DownloadInterrupted)
        self.assertEqual(self.host._active_downloads, {})
        self.assertIn("下载失败", self.host.status.showMessage.call_args.args[0])
        self.assertIn("File access denied", self.warning.call_args.args[2])

    def test_cancelling_active_download_releases_only_its_reference(self):
        first = FakeDownload(1)
        second = FakeDownload(2)
        self.host._handle_download_requested(first)
        self.host._handle_download_requested(second)
        first.cancel()
        self.assertNotIn(1, self.host._active_downloads)
        self.assertIs(self.host._active_downloads[2]["item"], second)
        self.assertIn("已取消", self.host.status.showMessage.call_args.args[0])
        self.warning.assert_not_called()

    def test_dialog_uses_system_downloads_and_preserves_overwrite_confirmation(self):
        download = FakeDownload(filename="../../unsafe\\CON.png")
        self.host._handle_download_requested(download)
        self.dialog.assert_called_once_with(
            self.host,
            "保存下载结果",
            os.path.join(self.downloads_dir, "fay-CON.png"),
            "所有文件 (*)",
        )

    def test_accept_failure_is_not_reported_as_success_or_kept_active(self):
        download = FakeDownload()
        download.accept = Mock(side_effect=OSError("write failed"))
        self.host._handle_download_requested(download)
        self.assertTrue(download.cancelled)
        self.assertEqual(self.host._active_downloads, {})
        self.host.status.showMessage.assert_called_with("下载失败，未保存结果", 10000)
        self.assertIn("write failed", self.warning.call_args.args[2])

    def test_filename_is_safe_for_windows_and_cannot_escape_downloads_directory(self):
        self.assertEqual(_safe_download_filename("../../x\\tryon.png"), "tryon.png")
        self.assertEqual(_safe_download_filename(" . "), "fay-download.png")
        self.assertEqual(_safe_download_filename('photo:"bad".png'), "photo__bad_.png")
        self.assertEqual(_safe_download_filename("LPT1.png"), "fay-LPT1.png")


if __name__ == "__main__":
    unittest.main()
