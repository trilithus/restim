from __future__ import annotations

import logging
import os
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

        self.http.route_json('/v1/media', {'GET': self.get_media})
        self.http.route_json('/v1/media/source', {'GET': self.get_media_source, 'PUT': self.set_media_source})
        self.http.route_json('/v1/media/load', {'POST': self.load_media})
        self.http.route_json('/v1/media/unload', {'POST': self.unload_media})

    # media

    def require_rest_media_sync(self):
        media = self.window.page_media.rest_media_sync()
        if media is None:
            raise ApiError(409, f'media source must be "REST API", currently "{self.window.page_media.current_media_sync_name()}"')
        return media

    def media_status(self) -> dict:
        page = self.window.page_media
        media = page.current_media_sync()
        status = {
            'source': page.current_media_sync_name(),
            'state': media.state().name,
            'path': media.media_path(),
        }
        rest = page.rest_media_sync()
        if rest is not None:
            status['position'] = rest.position()
        return status

    def get_media(self, request: QHttpServerRequest):
        return self.media_status()

    def get_media_source(self, request: QHttpServerRequest):
        page = self.window.page_media
        return {'source': page.current_media_sync_name(), 'available': page.media_sync_names()}

    def set_media_source(self, request: QHttpServerRequest):
        source = json_body(request).get('source')
        if not isinstance(source, str) or not self.window.page_media.set_media_sync_by_name(source):
            raise ApiError(400, f'source must be one of: {", ".join(self.window.page_media.media_sync_names())}')
        return self.get_media_source(request)

    def load_media(self, request: QHttpServerRequest):
        """
        Load a media file into the REST media source. Funscripts next to it
        (and in the additional search paths) are auto-detected and auto-linked.
        """
        path = json_body(request).get('path')
        if not isinstance(path, str) or not path:
            raise ApiError(400, '"path" is required')
        if not os.path.isfile(path):
            raise ApiError(404, f'file not found: {path}')
        self.require_rest_media_sync().load(os.path.abspath(path))
        return self.media_status()

    def unload_media(self, request: QHttpServerRequest):
        self.require_rest_media_sync().unload()
        return self.media_status()
