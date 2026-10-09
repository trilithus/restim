from __future__ import annotations

import functools
import logging
import os
import typing

from PySide6.QtCore import QObject
from PySide6.QtHttpServer import QHttpServerRequest
from PySide6.QtWidgets import QAbstractButton, QDoubleSpinBox

from qt_ui import settings
from qt_ui.device_wizard.axes import AxisEnum
from qt_ui.device_wizard.enums import DeviceConfiguration, DeviceType, WaveformType
from stim_math import limits
from qt_ui.models.funscript_kit import FunscriptKitModel
from net.http_server import HttpServer, ApiError, json_body, query_param

if typing.TYPE_CHECKING:
    from qt_ui.mainwindow import Window

logger = logging.getLogger('restim.rest_api')

# waveform types per device type, same options as the device selection wizard. First is the default.
DEVICE_WAVEFORMS = {
    DeviceType.AUDIO_THREE_PHASE: [WaveformType.PULSE_BASED, WaveformType.CONTINUOUS, WaveformType.A_B_TESTING],
    DeviceType.FOCSTIM_THREE_PHASE: [WaveformType.PULSE_BASED, WaveformType.A_B_TESTING],
    DeviceType.FOCSTIM_FOUR_PHASE: [WaveformType.PULSE_BASED],
    DeviceType.NEOSTIM_THREE_PHASE: [WaveformType.PULSE_BASED],
}


class WidgetSetting:
    """
    A setting exposed through the REST api, backed by a widget, so changes
    behave exactly like edits in the ui.
    """
    def __init__(self, widget: QDoubleSpinBox | QAbstractButton, default, description: str):
        self.widget = widget
        self.default = default  # value, or callable returning the value
        self.description = description

    def is_bool(self) -> bool:
        return isinstance(self.widget, QAbstractButton)

    def get(self):
        if self.is_bool():
            return self.widget.isChecked()
        return self.widget.value()

    def default_value(self):
        return self.default() if callable(self.default) else self.default

    def set(self, value):
        if self.is_bool():
            if not isinstance(value, bool):
                raise ApiError(400, 'value must be a boolean')
            self.widget.setChecked(value)
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ApiError(400, 'value must be a number')
            # setValue clamps to the widget range
            self.widget.setValue(value)

    def describe(self) -> dict:
        d = {'value': self.get(), 'default': self.default_value(), 'description': self.description}
        if not self.is_bool():
            d['min'] = self.widget.minimum()
            d['max'] = self.widget.maximum()
        return d


class RestApi(QObject):
    """
    REST endpoints for remote control of restim.
    All handlers are invoked from the Qt event loop, so it is safe to touch the UI.
    """
    def __init__(self, window: Window, http_server: HttpServer):
        super().__init__(window)
        self.window = window
        self.http = http_server

        self.settings = self.create_settings()
        self.http.route_json('/v1/settings', {'GET': self.get_settings, 'PUT': self.set_settings})
        for name in self.settings:
            self.http.route_json(f'/v1/settings/{name}', {
                'GET': functools.partial(self.get_setting, name),
                'PUT': functools.partial(self.set_setting, name),
                'DELETE': functools.partial(self.reset_setting, name),
            })

        self.http.route_json('/v1/device', {'GET': self.get_device, 'PUT': self.set_device})

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

    # settings

    def create_settings(self) -> dict[str, WidgetSetting]:
        w = self.window
        vol = w.tab_volume
        pulse = w.tab_pulse_settings
        return {
            'volume': WidgetSetting(
                w.doubleSpinBox_volume, settings.volume_default_level.get,
                'master volume [%]. Default is the startup volume from the preferences.'),
            'volume_ramp_enabled': WidgetSetting(vol.checkBox_ramp_enabled, False, 'enable volume ramp'),
            'volume_ramp_target': WidgetSetting(
                vol.doubleSpinBox_ramp_target, settings.volume_ramp_target.default_value * 100, 'volume ramp target [%]'),
            'volume_ramp_rate': WidgetSetting(
                vol.doubleSpinBox_ramp_rate, settings.volume_ramp_increment_rate.default_value, 'volume ramp rate [%/minute]'),
            'volume_inactivity_threshold': WidgetSetting(
                vol.doubleSpinBox_inactivity_threshold, settings.volume_inactivity_threshold.default_value, 'inactivity threshold'),
            'volume_inactivity_ramp_time': WidgetSetting(
                vol.doubleSpinBox_inactivity_ramp_time, settings.volume_inactivity_time.default_value, 'inactivity ramp time [s]'),
            'volume_inactivity_volume': WidgetSetting(
                vol.doubleSpinBox_inactivity_volume, settings.volume_inactivity_volume.default_value, 'inactivity volume reduction'),
            'volume_slow_start_time': WidgetSetting(
                vol.doubleSpinBox_slow_start, settings.volume_slow_start_time.default_value, 'slow start time [s]'),
            'tau_us': WidgetSetting(vol.doubleSpinBox_tau, settings.tau_us.default_value, 'tau [us]'),
            'pulse_frequency_calibration_enable': WidgetSetting(
                vol.checkBox_pulse_frequency_enable, settings.pulse_frequency_calibration_enable.default_value,
                'volume-frequency calibration'),
            'burst_gap_enable': WidgetSetting(
                vol.checkbox_burst_gap_enable, settings.burst_gap_enable.default_value, 'burst gap'),
            'pulse_carrier_frequency': WidgetSetting(
                pulse.carrier_controller.control, settings.pulse_carrier_frequency.default_value,
                'pulse carrier frequency [Hz], limited by the device frequency range'),
            'pulse_frequency': WidgetSetting(
                pulse.pulse_frequency_controller.control, settings.pulse_frequency.default_value, 'pulse frequency [Hz]'),
            'pulse_width': WidgetSetting(
                pulse.pulse_width_controller.control, settings.pulse_width.default_value, 'pulse width [carrier cycles]'),
            'pulse_interval_random': WidgetSetting(
                pulse.pulse_interval_random_controller.control, settings.pulse_interval_random.default_value,
                'pulse interval random [%]'),
            'pulse_rise_time': WidgetSetting(
                pulse.pulse_rise_time_controller.control, settings.pulse_rise_time.default_value,
                'pulse rise time [carrier cycles]'),
            'continuous_carrier_frequency': WidgetSetting(
                w.tab_carrier.carrier_controller.control, settings.mk312_carrier.default_value,
                'carrier frequency for continuous waveforms [Hz]'),
            'media_sync_offset': WidgetSetting(
                w.page_media.media_offset_spinbox, settings.media_sync_time_offset_ms.default_value / 1000,
                'media sync offset [s]'),
        }

    def get_settings(self, request: QHttpServerRequest):
        return {name: setting.describe() for name, setting in self.settings.items()}

    def set_settings(self, request: QHttpServerRequest):
        """set multiple settings at once. body: {"volume": 30, "pulse_frequency": 50}"""
        body = json_body(request)
        unknown = [name for name in body if name not in self.settings]
        if unknown:
            raise ApiError(404, f'unknown settings: {", ".join(unknown)}')
        for name, value in body.items():
            try:
                self.settings[name].set(value)
            except ApiError as e:
                raise ApiError(e.status, f'{name}: {e.message}')
        return self.get_settings(request)

    def get_setting(self, name: str, request: QHttpServerRequest):
        return self.settings[name].describe()

    def set_setting(self, name: str, request: QHttpServerRequest):
        body = json_body(request)
        if 'value' not in body:
            raise ApiError(400, '"value" is required')
        self.settings[name].set(body['value'])
        return self.settings[name].describe()

    def reset_setting(self, name: str, request: QHttpServerRequest):
        setting = self.settings[name]
        setting.set(setting.default_value())
        return setting.describe()

    # device

    @staticmethod
    def frequency_limits(device_type: DeviceType) -> tuple[float, float]:
        if device_type in (DeviceType.FOCSTIM_THREE_PHASE, DeviceType.FOCSTIM_FOUR_PHASE):
            return limits.CarrierFrequencyFOC.min, limits.CarrierFrequencyFOC.max
        return 1, 10000  # same as the wizard

    def get_device(self, request: QHttpServerRequest):
        config = DeviceConfiguration.from_settings()
        return {
            'device_type': config.device_type.name,
            'waveform_type': config.waveform_type.name,
            'min_frequency': config.min_frequency,
            'max_frequency': config.max_frequency,
            'waveform_amplitude_amps': config.waveform_amplitude_amps,
            'options': {
                'device_type': {t.name: [w.name for w in waveforms] for t, waveforms in DEVICE_WAVEFORMS.items()},
                'frequency': {t.name: self.frequency_limits(t) for t in DEVICE_WAVEFORMS},
                'waveform_amplitude_amps': [limits.WaveformAmpltiudeFOC.min, limits.WaveformAmpltiudeFOC.max],
            },
        }

    def set_device(self, request: QHttpServerRequest):
        """
        Change the device configuration, like the device selection wizard does.
        All fields are optional, missing fields keep their current value.
        body: {"device_type": "FOCSTIM_FOUR_PHASE", "waveform_type": "PULSE_BASED",
               "min_frequency": 500, "max_frequency": 1000, "waveform_amplitude_amps": 0.12}
        Stops playback.
        """
        body = json_body(request)
        unknown = set(body) - {'device_type', 'waveform_type', 'min_frequency', 'max_frequency', 'waveform_amplitude_amps'}
        if unknown:
            raise ApiError(400, f'unknown fields: {", ".join(sorted(unknown))}')
        config = DeviceConfiguration.from_settings()

        if 'device_type' in body:
            try:
                config.device_type = DeviceType[str(body['device_type']).upper()]
            except KeyError:
                config.device_type = None
            if config.device_type not in DEVICE_WAVEFORMS:
                raise ApiError(400, f'device_type must be one of: {", ".join(t.name for t in DEVICE_WAVEFORMS)}')

        allowed_waveforms = DEVICE_WAVEFORMS.get(config.device_type, [])
        if 'waveform_type' in body:
            try:
                config.waveform_type = WaveformType[str(body['waveform_type']).upper()]
            except KeyError:
                config.waveform_type = None
            if config.waveform_type not in allowed_waveforms:
                raise ApiError(400, f'waveform_type for {config.device_type.name} must be one of: '
                                    f'{", ".join(w.name for w in allowed_waveforms)}')
        elif allowed_waveforms and config.waveform_type not in allowed_waveforms:
            config.waveform_type = allowed_waveforms[0]

        for key in ('min_frequency', 'max_frequency', 'waveform_amplitude_amps'):
            if key in body:
                value = body[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ApiError(400, f'{key} must be a number')
                setattr(config, key, float(value))

        lo, hi = self.frequency_limits(config.device_type)
        if not (lo <= config.min_frequency < config.max_frequency <= hi):
            raise ApiError(400, f'frequency range must satisfy {lo} <= min_frequency < max_frequency <= {hi}')
        if not (limits.WaveformAmpltiudeFOC.min <= config.waveform_amplitude_amps <= limits.WaveformAmpltiudeFOC.max):
            raise ApiError(400, f'waveform_amplitude_amps must be between '
                                f'{limits.WaveformAmpltiudeFOC.min} and {limits.WaveformAmpltiudeFOC.max}')

        self.window.apply_device_configuration(config)
        return self.get_device(request)
