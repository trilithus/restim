import logging
import json

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QTcpServer, QHostAddress
from PySide6.QtHttpServer import QHttpServer, QHttpServerRequest, QHttpServerResponse, QHttpServerResponder

from qt_ui import settings

logger = logging.getLogger('restim.http')


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def json_body(request: QHttpServerRequest) -> dict:
    """
    Parse the request body as a json object. An empty body is treated as {}.
    """
    data = bytes(request.body().data())
    if not data.strip():
        return {}
    try:
        body = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ApiError(400, f'invalid json: {e}')
    if not isinstance(body, dict):
        raise ApiError(400, 'request body must be a json object')
    return body


def query_param(request: QHttpServerRequest, name: str) -> str | None:
    query = request.query()
    if query.hasQueryItem(name):
        return query.queryItemValue(name)
    return None


class HttpServer(QObject):
    def __init__(self, parent):
        super().__init__(parent)

        self.http_server = QHttpServer(self)
        self.tcp_server = QTcpServer(self)
        self.bottomless_pit = []
        self.actions = []

        # status code of the response currently being generated.
        # QHttpServer (PySide) only supports string return values with status 200,
        # so the response is patched in the after-request handler.
        self.pending_status = None

        enabled = settings.rest_enabled.get()
        port = settings.rest_port.get()
        localhost_only = settings.rest_localhost_only.get()

        if not enabled:
            logger.info("REST API not enabled.")
            return

        address = QHostAddress.SpecialAddress.LocalHost if localhost_only else QHostAddress.SpecialAddress.Any

        if not self.tcp_server.listen(address, port):
            logger.error("HTTP server failed to listen on port")
            logger.error(f"{self.tcp_server.errorString()}")
            return


        if not self.http_server.bind(self.tcp_server):
            logger.error("HTTP server failed to bind to socket")
            return

        logger.info(f"HTTP server active at localhost:{port}")

        self.bottomless_pit.append(self.after_request)
        self.http_server.addAfterRequestHandler(self, self.bottomless_pit[-1])
        self.route("/v1/actions", self.api_actions)

    def add_action(self, action_string, fn):
        self.actions.append(action_string)
        self.route(f"/v1/actions/{action_string}", fn)

    def route(self, rule, fn):
        # store fn somewhere, because QHttpServer::route
        # forgets to increase the object refcount
        self.bottomless_pit.append(fn)
        self.http_server.route(rule, fn)

    def route_json(self, rule, handlers: dict):
        """
        Register a json endpoint.
        :param rule: exact path, for example '/v1/funscripts'
        :param handlers: {'GET': fn, 'POST': fn, ...}. fn(request) returns a json-serializable
            object, or raises ApiError.
        """
        handlers = {method.upper(): fn for method, fn in handlers.items()}

        def dispatch(request: QHttpServerRequest):
            method = request.method().name.upper()
            fn = handlers.get(method)
            try:
                if fn is None:
                    raise ApiError(405, f'method {method} not allowed, use one of: {", ".join(handlers)}')
                result = fn(request)
                self.pending_status = 200
                return json.dumps({} if result is None else result)
            except ApiError as e:
                self.pending_status = e.status
                return json.dumps({'error': e.message})
            except Exception as e:
                logger.exception(f'error handling {method} {rule}')
                self.pending_status = 500
                return json.dumps({'error': str(e)})

        self.route(rule, dispatch)

    def after_request(self, request: QHttpServerRequest, response: QHttpServerResponse):
        if self.pending_status is None:
            return
        status = QHttpServerResponder.StatusCode(self.pending_status)
        self.pending_status = None
        response.swap(QHttpServerResponse(b"application/json", response.data(), status))

    def api_actions(self, request: QHttpServerRequest):
        return json.dumps({'actions': self.actions})
