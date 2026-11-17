"""Additive camera YAML configuration with redacted, round-trippable secrets."""
import re
from urllib.parse import urlsplit

import yaml
from pydantic import ValidationError


class SecretRef(str):
    pass


class CameraLoader(yaml.SafeLoader):
    pass


class CameraDumper(yaml.SafeDumper):
    pass


def mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("Camera configuration keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


CameraLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
CameraLoader.add_constructor("!secret", lambda loader, node: SecretRef(loader.construct_scalar(node)))
CameraDumper.add_representer(SecretRef, lambda dumper, value: dumper.represent_scalar("!secret", str(value)))


def export_cameras(cameras):
    result = {}
    for c in cameras:
        result[c["id"]] = {k: v for k, v in c.items() if k not in {"id", "rtsp_url"}}
        result[c["id"]]["rtsp_url"] = SecretRef(c["id"] + "_rtsp") if c.get("rtsp_url") else ""
    return "# LumiNext camera config. Saving adds/updates cameras; omitted cameras are kept.\n# !secret preserves a URL already stored locally. Paste a real RTSP URL for a new camera.\n" + yaml.dump({"cameras": result}, Dumper=CameraDumper, sort_keys=False, allow_unicode=True)


def parse_cameras(text, existing, camera_model):
    if len(text) > 300_000:
        raise ValueError("Camera configuration exceeds 300 KB")
    try:
        root = yaml.load(text, Loader=CameraLoader)
    except yaml.YAMLError:
        raise ValueError("Invalid camera YAML; check indentation and syntax") from None
    if not isinstance(root, dict) or set(root) != {"cameras"} or not isinstance(root["cameras"], dict):
        raise ValueError("Use a top-level cameras mapping. This editor supports camera configuration, not a complete Frigate config.")
    if len(set(existing) | set(root["cameras"])) > 32:
        raise ValueError("At most 32 cameras are supported")
    output = []
    fields = set(camera_model.model_fields)

    def require_mapping(value, label, allowed):
        if not isinstance(value, dict) or set(value) - set(allowed):
            raise ValueError(f"Unsupported options in {label}")
        return value

    for id, values in root["cameras"].items():
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", id):
            raise ValueError("Camera IDs must use letters, numbers, dots, underscores or hyphens")
        require_mapping(values, id, (fields - {"id"}) | {"ffmpeg", "detect"})
        old = existing.get(id, {})
        data = {k: v for k, v in old.items() if k != "rtsp_url"}
        data.update({k: v for k, v in values.items() if k not in {"ffmpeg", "detect"}})
        data.update(id=id)
        data.setdefault("name", id.replace("_", " ").title())
        data.setdefault("rtsp_url", None)
        if "ffmpeg" in values:
            ffmpeg = require_mapping(values["ffmpeg"], "ffmpeg", {"inputs"})
            inputs = ffmpeg.get("inputs")
            if not isinstance(inputs, list) or len(inputs) != 1:
                raise ValueError("Use one ffmpeg input with the detect role per camera")
            item = require_mapping(inputs[0], "ffmpeg input", {"path", "roles"})
            if item.get("roles", ["detect"]) != ["detect"]:
                raise ValueError("Only the detect role is supported; recording is not enabled")
            data["rtsp_url"] = item.get("path", None)
        if "detect" in values:
            data.update(require_mapping(values["detect"], "detect", {"enabled", "width", "height", "fps"}))
        url = data.get("rtsp_url")
        if isinstance(url, SecretRef):
            if str(url) != id + "_rtsp" or not old.get("rtsp_url"):
                raise ValueError(f"No stored RTSP secret for {id}; enter its URL locally")
            data["rtsp_url"] = None
        elif url and (not isinstance(url,str) or urlsplit(url).scheme not in {"rtsp", "rtsps"}):
            raise ValueError(f"{id}: stream URL must use rtsp:// or rtsps://")
        try:
            camera = camera_model.model_validate(data)
        except ValidationError as error:
            detail = "; ".join(f"{'.'.join(map(str,e['loc']))}: {e['msg']}" for e in error.errors())
            raise ValueError(f"{id}: {detail}") from None
        if camera.min_area > camera.max_area or camera.min_ratio > camera.max_ratio:
            raise ValueError(f"{id}: invalid object filter ranges")
        if len({z.id for z in camera.zones}) != len(camera.zones):
            raise ValueError(f"{id}: zone IDs must be unique")
        output.append(camera)
    return output
