from __future__ import annotations

import logging
import typing

from PySide6.QtCore import QObject
from PySide6.QtHttpServer import QHttpServerRequest

from net.http_server import HttpServer, ApiError, json_body, query_param

if typing.TYPE_CHECKING:
    from qt_ui.mainwindow import Window

logger = logging.getLogger('restim.rest_api')


class RestApi(QObject):
    """
    REST endpoints for remote control of restim.
    All handlers are invoked from the Qt event loop, so it is safe to touch the UI.
    """
    def __init__(self, window: Window, http_server: HttpServer):
        super().__init__(window)
        self.window = window
        self.http = http_server
