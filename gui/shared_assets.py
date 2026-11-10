"""Serve shared UI assets while keeping each workbench's own overrides."""

from pathlib import Path

from flask import Flask, send_from_directory
from werkzeug.exceptions import NotFound


class SharedStaticFlask(Flask):
    def __init__(self, *args, shared_static_folder: str | Path, **kwargs):
        self.shared_static_folder = Path(shared_static_folder).resolve()
        super().__init__(*args, **kwargs)

    def send_static_file(self, filename: str):
        try:
            return super().send_static_file(filename)
        except NotFound:
            # send_from_directory keeps Flask's path traversal protection.
            return send_from_directory(
                self.shared_static_folder,
                filename,
                max_age=self.get_send_file_max_age(filename),
            )
