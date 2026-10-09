from __future__ import annotations

import logging
import os
import typing

from PySide6.QtCore import QObject
from PySide6.QtHttpServer import QHttpServerRequest

from qt_ui.device_wizard.axes import AxisEnum
from qt_ui.models.funscript_kit import FunscriptKitModel
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

        self.http.route_json('/v1/funscripts', {
            'GET': self.get_funscripts,
            'POST': self.add_funscripts,
            'PUT': self.assign_funscript,
            'DELETE': self.remove_funscripts,
        })

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

    # funscripts

    def funscript_list(self) -> dict:
        model = self.window.page_media.model
        return {'funscripts': [
            {
                'id': i,
                'file': item.file_name,
                'path': item.path,
                'source': 'manual' if model.is_manual(item) else 'auto',
                'axis': item.axis.name,
                'active': item.first_of_its_kind,
                'broken': item.has_broken_script(),
            }
            for i, item in enumerate(model.funscript_conifg())
        ]}

    def get_funscripts(self, request: QHttpServerRequest):
        return self.funscript_list()

    def add_funscripts(self, request: QHttpServerRequest):
        """
        Load funscripts from disk and auto-assign them to an axis.
        body: {"paths": ["a.alpha.funscript", ...]} or {"path": "a.alpha.funscript"}
        """
        body = json_body(request)
        paths = body.get('paths', [body['path']] if 'path' in body else None)
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            raise ApiError(400, '"paths" must be a non-empty list of file paths')
        for path in paths:
            if not os.path.isfile(path):
                raise ApiError(404, f'file not found: {path}')
        self.window.page_media.add_funscripts([os.path.abspath(p) for p in paths])
        return self.funscript_list()

    def find_funscript(self, request: QHttpServerRequest, body: dict):
        """
        Find a funscript by "id" (index in GET /v1/funscripts), "path" or "file" name,
        given in the json body or the query string.
        """
        model = self.window.page_media.model
        items = model.funscript_conifg()

        def param(name):
            return body.get(name, query_param(request, name))

        id_ = param('id')
        path = param('path')
        file = param('file')
        if id_ is not None:
            try:
                return items[int(id_)]
            except (ValueError, TypeError, IndexError):
                raise ApiError(404, f'no funscript with id {id_}')
        if path is not None:
            path = os.path.abspath(path)
            for item in items:
                if item.path == path:
                    return item
            raise ApiError(404, f'no funscript with path {path}')
        if file is not None:
            for item in items:
                if item.file_name == file:
                    return item
            raise ApiError(404, f'no funscript with file name {file}')
        raise ApiError(400, 'specify the funscript with "id", "path" or "file"')

    def assignable_axes(self) -> list[AxisEnum]:
        return [AxisEnum.NONE] + [item.axis for item in FunscriptKitModel.load_from_settings().children
                                  if item.allow_funscript_control]

    def assign_funscript(self, request: QHttpServerRequest):
        """
        Change the axis a funscript is assigned to.
        body: {"id": 0, "axis": "POSITION_BETA"}. Use axis "NONE" to unassign,
        or "AUTO" to re-run auto-assignment.
        """
        body = json_body(request)
        item = self.find_funscript(request, body)
        axis_name = body.get('axis')
        if not isinstance(axis_name, str):
            raise ApiError(400, '"axis" is required')
        if item.has_broken_script():
            raise ApiError(409, f'funscript {item.file_name} failed to load and cannot be assigned')
        model = self.window.page_media.model
        if axis_name.upper() == 'AUTO':
            axis = model.auto_link_axis(FunscriptKitModel.load_from_settings(), item) or AxisEnum.NONE
        else:
            allowed = self.assignable_axes()
            axis = next((a for a in allowed if a.name == axis_name.upper()), None)
            if axis is None:
                raise ApiError(400, f'axis must be AUTO or one of: {", ".join(a.name for a in allowed)}')
        model.set_axis(item, axis)
        return self.funscript_list()

    def remove_funscripts(self, request: QHttpServerRequest):
        """
        Unload a funscript selected by id/path/file, or all manually loaded funscripts
        if none is selected. Auto-detected funscripts belong to the loaded media,
        use POST /v1/media/unload to unload those.
        """
        body = json_body(request)
        page = self.window.page_media
        if not any(k in body or query_param(request, k) is not None for k in ('id', 'path', 'file')):
            page.clear_manual_funscripts()
            return self.funscript_list()
        item = self.find_funscript(request, body)
        if not page.model.is_manual(item):
            raise ApiError(409, f'{item.file_name} was auto-detected from the loaded media, use /v1/media/unload')
        page.remove_funscript(item)
        return self.funscript_list()
