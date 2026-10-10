import json
import logging
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone

from PySide6 import QtCore
from PySide6.QtCore import QUrl, QUrlQuery
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from net.media_source.mediasource import MediaSource, MediaStatusReport, MediaConnectionState
from qt_ui import settings

logger = logging.getLogger('restim.media.jellyfin')

TICKS_PER_SECOND = 10_000_000

# Jellyfin only knows the position from the client's last progress report, which can be several seconds old.
# Re-sync when the median drift of the last few fresh reports exceeds this.
RESYNC_DRIFT_THRESHOLD = 0.05  # seconds
RESYNC_DRIFT_SAMPLES = 3
# allow the clock offset estimate to slowly recover if the server or local clock is adjusted
CLOCK_OFFSET_DECAY = 0.001  # seconds per fresh report


def parse_jellyfin_date(value: str):
    """
    Parse a Jellyfin timestamp such as '2026-10-10T12:34:56.1234567Z' to unix time.
    Jellyfin uses 7 fractional digits, which datetime does not accept.
    """
    m = re.match(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$', value or '')
    if not m:
        return None
    base, fraction, tz = m.groups()
    dt = datetime.fromisoformat(base + (tz if tz and tz != 'Z' else '+00:00'))
    return dt.timestamp() + (float('0.' + fraction) if fraction else 0.0)


def binary_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(sys.argv[0]))


class Jellyfin(MediaSource):
    """
    Polls the Jellyfin /Sessions endpoint and follows the first session that is playing something
    (optionally filtered by user name and/or device name).
    """
    def __init__(self, parent):
        super(Jellyfin, self).__init__(parent)

        self._enabled = False
        self.network_manager = QNetworkAccessManager(self)
        self.pending_reply: QNetworkReply = None
        self.jellyfin_offset = 0.0  # seconds, positive = funscript plays ahead of the video

        # item id -> path, for when the session report does not contain the path
        self.path_cache = {}
        self.pending_path_lookups = set()

        # estimate of (local clock - server clock), see _extrapolate_position()
        self.clock_offset = None
        self.last_check_in = None
        self.fresh_report = False
        self.drift_samples = []

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.timeout)
        self.timer.setSingleShot(False)
        self.timer.setInterval(500)

    def _base_url(self) -> str:
        return settings.media_sync_jellyfin_address.get().rstrip('/')

    def _make_request(self, path: str, query: dict) -> QNetworkRequest:
        url = QUrl.fromUserInput(self._base_url() + path)
        q = QUrlQuery()
        for k, v in query.items():
            q.addQueryItem(k, v)
        url.setQuery(q)
        request = QNetworkRequest(url)
        request.setTransferTimeout(2000)
        api_key = settings.media_sync_jellyfin_api_key.get()
        request.setRawHeader(b'Authorization', f'MediaBrowser Token="{api_key}"'.encode('utf-8'))
        return request

    def timeout(self):
        if not self._enabled:
            return
        if self.pending_reply is not None:
            return  # previous request still in flight
        self.jellyfin_offset = settings.media_sync_jellyfin_offset_ms.get() / 1000.0
        request = self._make_request('/Sessions', {'activeWithinSeconds': '60'})
        request_time = time.time()
        self.pending_reply = self.network_manager.get(request)
        self.pending_reply.finished.connect(lambda reply=self.pending_reply: self.sessions_finished(reply, request_time))

    def sessions_finished(self, reply: QNetworkReply, request_time: float):
        self.pending_reply = None
        reply.deleteLater()
        if not self._enabled:
            return

        if reply.error() != QNetworkReply.NetworkError.NoError:
            logger.debug('error: %s', reply.errorString())
            self.set_state(MediaStatusReport(time.time(), reply.errorString(), MediaConnectionState.NOT_CONNECTED))
            return

        try:
            sessions = json.loads(bytes(reply.readAll()).decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as e:
            logger.warning('invalid response: %s', e)
            self.set_state(MediaStatusReport(time.time(), str(e), MediaConnectionState.NOT_CONNECTED))
            return

        # assume the position is measured halfway the request
        timestamp = (request_time + time.time()) / 2

        session = self._select_session(sessions)
        if session is None:
            self.set_state(MediaStatusReport(timestamp, '', MediaConnectionState.CONNECTED_BUT_NO_FILE_LOADED))
            return

        item = session['NowPlayingItem']
        play_state = session.get('PlayState', {})
        path = self._item_path(item, session)
        if not path:
            # path lookup in progress or failed
            self.set_state(MediaStatusReport(timestamp, '', MediaConnectionState.CONNECTED_BUT_NO_FILE_LOADED))
            return

        position = play_state.get('PositionTicks', 0) / TICKS_PER_SECOND
        paused = play_state.get('IsPaused', False)
        if not paused:
            position = self._extrapolate_position(position, session, timestamp)

        self.set_state(
            MediaStatusReport(
                timestamp,
                '',
                MediaConnectionState.CONNECTED_AND_PAUSED if paused else MediaConnectionState.CONNECTED_AND_PLAYING,
                path,
                1,
                position,
            )
        )

    def _extrapolate_position(self, position: float, session, timestamp: float) -> float:
        """
        PositionTicks is the position at the client's last progress report (LastPlaybackCheckIn, server clock).
        Add the time elapsed since then. The server clock is not synchronized to ours, so estimate the offset:
        timestamp - check_in = clock_offset + report_age, with report_age >= 0. The minimum over fresh reports
        converges to the clock offset plus the minimal report age (at most the poll interval).
        """
        check_in = parse_jellyfin_date(session.get('LastPlaybackCheckIn'))
        if check_in is None:
            self.fresh_report = True
            return position

        self.fresh_report = check_in != self.last_check_in
        self.last_check_in = check_in

        sample = timestamp - check_in
        if self.clock_offset is None:
            self.clock_offset = sample
        elif self.fresh_report:
            self.clock_offset = min(self.clock_offset + CLOCK_OFFSET_DECAY, sample)

        age = max(0.0, timestamp - check_in - self.clock_offset)
        logger.debug('position %.3f, report age %.3f, clock offset %.3f', position, age, self.clock_offset)
        return position + age

    def needs_resync(self, drift: float) -> bool:
        if abs(drift) > 2.0:
            self.drift_samples.clear()
            return True
        # only fresh reports carry new information, extrapolated ones repeat the last report
        if not self.fresh_report:
            return False
        self.drift_samples = (self.drift_samples + [drift])[-RESYNC_DRIFT_SAMPLES:]
        if len(self.drift_samples) == RESYNC_DRIFT_SAMPLES:
            median = statistics.median(self.drift_samples)
            if abs(median) > RESYNC_DRIFT_THRESHOLD:
                logger.info('median drift %.3f s, re-sync', median)
                self.drift_samples.clear()
                return True
        return False

    def _select_session(self, sessions):
        user_filter = settings.media_sync_jellyfin_user.get().strip().lower()
        device_filter = settings.media_sync_jellyfin_device.get().strip().lower()
        for session in sessions:
            if not session.get('NowPlayingItem'):
                continue
            if user_filter and session.get('UserName', '').lower() != user_filter:
                continue
            if device_filter and device_filter not in session.get('DeviceName', '').lower():
                continue
            return session
        return None

    def _item_path(self, item, session) -> str:
        if item.get('Path'):
            return item['Path']
        item_id = item.get('Id')
        if not item_id:
            return ''
        if item_id in self.path_cache:
            return self.path_cache[item_id]
        if item_id not in self.pending_path_lookups:
            self.pending_path_lookups.add(item_id)
            query = {'Ids': item_id, 'Fields': 'Path', 'Recursive': 'true'}
            path = '/Items'
            if session.get('UserId'):
                path = f'/Users/{session["UserId"]}/Items'
            reply = self.network_manager.get(self._make_request(path, query))
            reply.finished.connect(lambda reply=reply: self.item_finished(reply, item_id))
        return ''

    def item_finished(self, reply: QNetworkReply, item_id: str):
        reply.deleteLater()
        self.pending_path_lookups.discard(item_id)
        if reply.error() != QNetworkReply.NetworkError.NoError:
            logger.warning('item lookup failed: %s', reply.errorString())
            return
        try:
            data = json.loads(bytes(reply.readAll()).decode('utf-8'))
            items = data.get('Items', [])
            if items and items[0].get('Path'):
                self.path_cache[item_id] = items[0]['Path']
                logger.info('resolved item %s to %s', item_id, self.path_cache[item_id])
        except (ValueError, UnicodeDecodeError, AttributeError) as e:
            logger.warning('invalid item response: %s', e)

    def map_timestamp(self, timestamp):
        # positive offset = funscript is played ahead of the video
        return super().map_timestamp(timestamp) + self.jellyfin_offset

    def funscript_search_paths(self, media_path: str) -> list[str]:
        """
        The path reported by Jellyfin is a server-side path, which may not exist on this machine.
        Try the absolute paths first, then relative to the binary and to the video file directory.
        For every candidate, also try a sub-folder named after the media file (without extension).
        """
        media_dir = os.path.dirname(media_path)
        stem = os.path.splitext(os.path.basename(media_path))[0]
        extra_paths = settings.additional_search_paths.get()

        absolute = []
        relative = []
        for p in [media_dir] + extra_paths:
            p = os.path.expanduser(p)
            if os.path.isabs(p):
                absolute.append(p)
                # server path may not be valid locally, try it relative too
                drive, tail = os.path.splitdrive(p)
                tail = tail.lstrip('/\\')
                if tail:
                    relative.append(tail)
            else:
                relative.append(p)

        bases = [binary_dir()]
        if media_dir:
            bases.append(media_dir)

        candidates = list(absolute)
        for base in bases:
            for r in relative:
                candidates.append(os.path.normpath(os.path.join(base, r)))
            candidates.append(base)

        result = []
        for c in candidates:
            for d in (c, os.path.join(c, stem)):
                if d not in result:
                    result.append(d)
        return result

    def enable(self):
        self._enabled = True
        self.path_cache.clear()
        self.clock_offset = None
        self.last_check_in = None
        self.drift_samples.clear()
        self.jellyfin_offset = settings.media_sync_jellyfin_offset_ms.get() / 1000.0
        self.timer.start()
        self.timeout()

    def disable(self):
        self._enabled = False
        self.timer.stop()
        if self.pending_reply is not None:
            self.pending_reply.abort()
            self.pending_reply = None
        self.set_state(MediaStatusReport(time.time(), '', MediaConnectionState.NOT_CONNECTED))

    def is_enabled(self) -> bool:
        return self._enabled
