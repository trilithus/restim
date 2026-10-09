import time
import logging

from net.media_source.interface import MediaConnectionState
from net.media_source.mediasource import MediaSource, MediaStatusReport

logger = logging.getLogger('restim.media.rest')


class RestMediaSource(MediaSource):
    """
    Media source controlled through the REST api. Keeps its own clock,
    so funscripts can be played back without an external media player.
    """
    def __init__(self, parent):
        super(RestMediaSource, self).__init__(parent)
        self._enabled = False
        self._path = ''

    def enable(self):
        self._enabled = True
        self._report(MediaConnectionState.CONNECTED_BUT_NO_FILE_LOADED if not self._path
                     else MediaConnectionState.CONNECTED_AND_PAUSED, 0)

    def disable(self):
        self._enabled = False
        self.set_state(MediaStatusReport(time.time()))

    def is_enabled(self) -> bool:
        return self._enabled

    def _report(self, state: MediaConnectionState, position: float):
        self.set_state(MediaStatusReport(
            timestamp=time.time(),
            connectionState=state,
            filePath=self._path if state.is_file_loaded() else '',
            claimed_media_position=position,
        ))

    def load(self, path: str):
        """
        Load a media file (or any path; funscripts are searched next to it).
        Playback starts paused at position 0.
        """
        if self.is_media_loaded():
            self.unload()
        self._path = path
        self._report(MediaConnectionState.CONNECTED_AND_PAUSED, 0)

    def unload(self):
        self._path = ''
        self._report(MediaConnectionState.CONNECTED_BUT_NO_FILE_LOADED, 0)

    def play(self):
        if self.is_media_loaded() and not self.is_playing():
            self._report(MediaConnectionState.CONNECTED_AND_PLAYING, self.position())

    def pause(self):
        if self.is_playing():
            self._report(MediaConnectionState.CONNECTED_AND_PAUSED, self.position())

    def seek(self, position: float):
        position = max(0.0, float(position))
        if self.is_playing():
            # set_state ignores small drift while playing, so adjust the clock directly.
            self.last_state.media_play_timestamp = time.time()
            self.last_state.cursor = position
        elif self.is_media_loaded():
            self._report(MediaConnectionState.CONNECTED_AND_PAUSED, position)

    def position(self) -> float:
        """media position in seconds, excluding the media sync offset"""
        if not self.is_media_loaded():
            return 0.0
        if self.is_playing():
            return time.time() - self.last_state.media_play_timestamp + self.last_state.cursor
        return self.last_state.cursor
