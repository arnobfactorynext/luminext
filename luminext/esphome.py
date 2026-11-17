"""Map ESPHome native API entities and states without changing their stable IDs."""
import dataclasses
import enum
import math

from aioesphomeapi import model as m

DOMAINS = {
    "Switch": "switch", "Sensor": "sensor", "BinarySensor": "binary_sensor",
    "TextSensor": "text_sensor", "Number": "number", "Select": "select",
    "Light": "light", "Fan": "fan", "Cover": "cover", "Climate": "climate",
    "Button": "button", "Text": "text", "Lock": "lock", "Valve": "valve",
    "Date": "date", "Time": "time", "DateTime": "datetime", "Siren": "siren",
    "MediaPlayer": "media_player", "AlarmControlPanel": "alarm_control_panel",
    "WaterHeater": "water_heater", "Update": "update", "Event": "event",
}
CONTROLLED = {"switch", "number", "select", "light", "fan", "cover", "climate", "button", "text"}


def plain(value):
    if isinstance(value, enum.Enum):
        return value.name.lower() if value.name else str(value.value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if dataclasses.is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value) if f.name not in {"key", "device_id", "data"}}
    if isinstance(value, (list, tuple)):
        return [plain(x) for x in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    return value


def entity_id(domain, device, key, child=0):
    suffix = f"_{child}" if child else ""
    return f"{domain}.{device}{suffix}_{key}"


def describe(device, info):
    prefix = type(info).__name__.removesuffix("Info")
    domain = DOMAINS.get(prefix)
    if not domain:
        return None
    unsafe = any(word in (info.name + " " + info.object_id + " " + getattr(info, "device_class", "")).lower() for word in ("factory", "reset", "restart", "reboot", "ota"))
    category = int(info.entity_category or 0)
    # Never expose maintenance commands as loads. Diagnostic readings remain useful.
    if domain in CONTROLLED and (unsafe or category != 0 or info.disabled_by_default):
        return None
    metadata = plain(info)
    if domain == "text" and metadata.get("mode") == "password":
        return None
    return {"id": entity_id(domain, device["id"], info.key, info.device_id), "type": domain,
            "name": f"{device['name']} · {info.name or info.object_id}", "native_name": info.name or info.object_id,
            "device_id": device["id"], "api_device_id": info.device_id, "key": info.key,
            "area": device["area"], "simulated": False, "unit": getattr(info, "unit_of_measurement", ""),
            "device_class": getattr(info, "device_class", ""), "capabilities": metadata,
            "controllable": domain in CONTROLLED}


def decode(device_id, state):
    prefix = type(state).__name__.removesuffix("State")
    prefix = {"LockEntity": "Lock", "Event": "Event"}.get(prefix, prefix)
    domain = DOMAINS.get(prefix)
    if not domain:
        return None
    details = plain(state)
    value = details.get("state", details.get("position", details.get("current_temperature", details.get("event_type", details))))
    missing = getattr(state, "missing_state", False) or value is None
    return entity_id(domain, device_id, state.key, state.device_id), "unknown" if missing else value, details, not missing


def validate_command(entity, data):
    try:
        return _validate_command(entity,data)
    except (KeyError,TypeError,OverflowError):
        raise ValueError("Invalid native command options or capabilities") from None


def _validate_command(entity, data):
    """Return a native method and validated arguments. Readings stay read-only."""
    domain, caps = entity["type"], entity.get("capabilities", {})
    if domain not in CONTROLLED or domain == "switch":
        raise ValueError("This entity is read-only or requires the relay command endpoint")
    allowed = {
        "number": {"value"}, "select": {"value"}, "text": {"value"}, "button": set(),
        "light": {"state", "brightness"}, "fan": {"state", "speed_level"},
        "cover": {"position", "stop"}, "climate": {"mode", "target_temperature"},
    }[domain]
    if set(data) - allowed or (not data and domain != "button"):
        raise ValueError("Unsupported or missing control options")
    args = {"key": entity["key"], "device_id": entity.get("api_device_id", 0)}
    if domain == "button":
        return "button_command", args
    if domain in {"number", "select", "text"}:
        if "value" not in data:
            raise ValueError("value is required")
        value = data["value"]
        if domain == "number":
            value = float(value)
            minimum, maximum, step = caps.get("min_value", 0), caps.get("max_value", 0), caps.get("step", 0)
            if not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError("Value is outside the device's supported range")
            if step and abs((value-minimum)/step - round((value-minimum)/step)) > .001:
                raise ValueError("Value does not match the device's step")
        elif domain == "select" and value not in caps.get("options", []):
            raise ValueError("Choose one of the device's advertised options")
        elif domain == "text":
            value = str(value)
            if not caps.get("min_length", 0) <= len(value) <= caps.get("max_length", 255):
                raise ValueError("Text length is outside the device's supported range")
        args["state"] = value
    else:
        args.update(data)
        for key in {"state", "stop"} & set(data):
            if not isinstance(data[key], bool):
                raise ValueError(f"{key} must be true or false")
        for key in {"brightness", "position"} & set(data):
            value = float(data[key])
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{key} must be 0..1")
            if key == "position" and not caps.get("supports_position") and value not in {0,1}:
                raise ValueError("This cover only supports fully open/closed positions")
            if key == "brightness" and caps.get("supported_color_modes") == ["on_off"]:
                raise ValueError("This light does not support brightness control")
            args[key] = value
        if "speed_level" in data:
            value = int(data["speed_level"])
            if not 1 <= value <= caps.get("supported_speed_count", 0):
                raise ValueError("Unsupported fan speed")
            args["speed_level"] = value
        if "mode" in data:
            try:
                value = m.ClimateMode[str(data["mode"]).upper()]
            except KeyError:
                raise ValueError("Unsupported climate mode") from None
            if value.name.lower() not in caps.get("supported_modes", []):
                raise ValueError("Climate mode not advertised by this device")
            args["mode"] = value
        if "target_temperature" in data:
            value = float(data["target_temperature"])
            if not math.isfinite(value) or not caps.get("visual_min_temperature", 5) <= value <= caps.get("visual_max_temperature", 35):
                raise ValueError("Target temperature is outside the supported range")
            args["target_temperature"] = value
    return domain + "_command", args
