"""Scoped home control shared by the HA webhook and regression tests."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
import time
import unicodedata

from .light_support import supports_light_action


class ControlError(ValueError):
    def __init__(self, code, message, candidates=None):
        super().__init__(message)
        self.code = code
        self.candidates = candidates or []


@dataclass
class HomeDevice:
    entity_id: str
    name: str
    area: str = ""
    state: str = "unavailable"
    attributes: dict = field(default_factory=dict)
    aliases: tuple = ()

    @property
    def domain(self):
        return self.entity_id.split(".", 1)[0]

    @property
    def kind(self):
        if self.domain == "light":
            return "light"
        return "airer" if "晾衣" in self.name or "airer" in self.entity_id else "curtain"

    def actions(self):
        if self.domain == "light":
            actions = ["turn_on", "turn_off"]
            if supports_light_action(self.attributes, "set_brightness"):
                actions.append("brightness")
            if supports_light_action(self.attributes, "set_color_temp"):
                actions.append("color_temperature")
            return actions
        features = self.attributes.get("supported_features", 0) or 0
        return [action for action, bit in (("open", 1), ("close", 2),
                ("position", 4), ("stop", 8)) if features & bit]

    def as_dict(self):
        result = {"entity_id": self.entity_id, "name": self.name, "area": self.area,
                  "domain": self.domain, "kind": self.kind, "state": self.state,
                  "available": self.state not in ("unknown", "unavailable"),
                  "actions": self.actions(), "aliases": sorted({a for a in self.aliases if isinstance(a, str)})}
        for key in ("current_position", "color_temp_kelvin", "min_color_temp_kelvin",
                    "max_color_temp_kelvin"):
            if key in self.attributes:
                result[key] = self.attributes[key]
        brightness = self.attributes.get("brightness")
        result["brightness_pct"] = round(brightness * 100 / 255) if brightness is not None else None
        return result


def _normalize(value):
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def dashboard_covers(config, view_path="curtains"):
    """Read displayed cover cards, including template entities and card names.

    Only entity cards are considered, never arbitrary service targets in actions.
    The returned scope fails closed when the configured view is absent.
    """
    result = {}

    def walk(node, area=""):
        if isinstance(node, list):
            for child in node:
                if isinstance(child, dict) and child.get("type") == "heading":
                    area = child.get("heading", area)
                walk(child, area)
        elif isinstance(node, dict):
            entity = node.get("entity")
            if isinstance(entity, str) and entity.startswith("cover."):
                result[entity] = {"name": node.get("name"), "area": area}
            for key in ("sections", "cards", "card", "entities"):
                if key in node:
                    walk(node[key], area)
    for view in config.get("views", []):
        if view.get("path") == view_path:
            walk(view.get("sections", view.get("cards", [])))
    return result


class HomeController:
    """Resolve exact targets and dispatch only supported absolute commands."""

    def __init__(self, catalog, call_service):
        self.catalog = catalog
        self.call_service = call_service
        self.lock = asyncio.Lock()
        self.requests = OrderedDict()

    def resolve(self, payload, devices):
        domain = payload.get("domain")
        if domain is not None and domain not in ("light", "cover"):
            raise ControlError("invalid_domain", "只支持灯光和窗帘与晾晒设备")
        candidates = [d for d in devices if domain is None or d.domain == domain]
        area = payload.get("area")
        if area is not None:
            if not isinstance(area, str) or not area.strip():
                raise ControlError("invalid_target", "房间必须是非空名称")
            candidates = [d for d in candidates if _normalize(d.area) == _normalize(area)]
        kind = payload.get("kind")
        if kind is not None:
            if kind not in ("light", "curtain", "airer"):
                raise ControlError("invalid_target", "设备类别无效")
            candidates = [d for d in candidates if d.kind == kind]
        entity_id, name = payload.get("entity_id"), payload.get("name")
        all_devices = payload.get("all", False)
        if not isinstance(all_devices, bool) or (entity_id is not None and name is not None):
            raise ControlError("invalid_target", "请使用一个明确目标")
        if all_devices:
            if domain is None or entity_id is not None or name is not None:
                raise ControlError("invalid_target", "批量控制须指定类别，不能同时指定单个设备")
        elif entity_id is not None:
            if not isinstance(entity_id, str):
                raise ControlError("invalid_target", "实体标识无效")
            candidates = [d for d in candidates if d.entity_id == entity_id]
        elif name is not None:
            if not isinstance(name, str) or not name.strip():
                raise ControlError("invalid_target", "设备名称不能为空")
            candidates = [d for d in candidates if _normalize(name) in
                          {_normalize(n) for n in (d.name, d.area + d.name, *d.aliases) if isinstance(n, str)}]
        else:
            raise ControlError("missing_target", "请指定设备名称，批量操作须明确 all=true")
        if not candidates:
            raise ControlError("not_found", "设备未在允许的家庭概览范围中")
        if len(candidates) > 1 and not all_devices:
            raise ControlError("ambiguous_target", "找到多个设备，请指定房间或实体", [d.as_dict() for d in candidates])
        return candidates

    @staticmethod
    def plan(device, action, value):
        if device.state in ("unavailable", "unknown"):
            raise ControlError("unavailable", "设备当前不可用")
        if action not in device.actions():
            raise ControlError("unsupported_action", "该设备不支持此操作")
        if action in ("brightness", "position", "color_temperature"):
            if type(value) is not int:
                raise ControlError("invalid_value", "设置值必须是整数")
            if action != "color_temperature" and not 0 <= value <= 100:
                raise ControlError("invalid_value", "百分比必须在 0 到 100 之间")
        elif value is not None:
            raise ControlError("invalid_value", "此操作不接受数值参数")
        data = {"entity_id": device.entity_id}
        if action == "brightness":
            # HA brightness 0 explicitly switches off; do not replace 0 by a default.
            if value == 0:
                return "light", "turn_off", data
            return "light", "turn_on", {**data, "brightness_pct": value}
        if action == "color_temperature":
            low = device.attributes.get("min_color_temp_kelvin", 2000)
            high = device.attributes.get("max_color_temp_kelvin", 6500)
            if not low <= value <= high:
                raise ControlError("invalid_value", f"色温须在 {low} 到 {high} K 之间")
            return "light", "turn_on", {**data, "color_temp_kelvin": value}
        if device.domain == "light":
            return "light", action, data
        service = {"open": "open_cover", "close": "close_cover", "stop": "stop_cover",
                   "position": "set_cover_position"}[action]
        if action == "position":
            data["position"] = value
        return "cover", service, data

    async def handle(self, payload):
        if not isinstance(payload, dict):
            raise ControlError("invalid_request", "请求必须是 JSON 对象")
        allowed = {"command", "domain", "kind", "area", "name", "entity_id", "all", "action", "value", "request_id"}
        if set(payload) - allowed:
            raise ControlError("invalid_request", "请求包含不支持的字段")
        command = payload.get("command")
        if command not in ("catalog", "status", "control"):
            raise ControlError("invalid_request", "command 须为 catalog、status 或 control")
        devices = self.catalog()
        if command == "catalog":
            if set(payload) - {"command", "domain", "kind", "area"}:
                raise ControlError("invalid_request", "设备列表请求包含多余字段")
            domain, kind, area = payload.get("domain"), payload.get("kind"), payload.get("area")
            if domain is not None and domain not in ("light", "cover"):
                raise ControlError("invalid_domain", "只支持 light 和 cover")
            if kind is not None and kind not in ("light", "curtain", "airer"):
                raise ControlError("invalid_target", "设备类别无效")
            if area is not None and not isinstance(area, str):
                raise ControlError("invalid_target", "房间名称无效")
            matches = [d for d in devices if (domain is None or d.domain == domain) and
                       (kind is None or d.kind == kind) and (area is None or _normalize(d.area) == _normalize(area))]
            return {"ok": True, "devices": [d.as_dict() for d in matches], "count": len(matches)}
        targets = self.resolve(payload, devices)
        if command == "status":
            if any(k in payload for k in ("action", "value", "request_id")):
                raise ControlError("invalid_request", "状态查询不接受控制参数")
            return {"ok": True, "devices": [d.as_dict() for d in targets]}
        action = payload.get("action")
        if not isinstance(action, str):
            raise ControlError("invalid_action", "请提供明确动作")
        # Validate all parameters before executing any part of a group operation.
        plans, errors = [], []
        for device in targets:
            try:
                plans.append((device, self.plan(device, action, payload.get("value"))))
            except ControlError as err:
                if err.code == "invalid_value":
                    raise
                errors.append({"entity_id": device.entity_id, "ok": False, "error": err.code, "message": str(err)})
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128:
            raise ControlError("invalid_request", "控制请求须提供 8 到 128 字符的 request_id")
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        async with self.lock:
            now = time.monotonic()
            while self.requests and next(iter(self.requests.values()))[0] < now - 600:
                self.requests.popitem(last=False)
            if request_id in self.requests:
                _, old_hash, result = self.requests[request_id]
                if old_hash != fingerprint:
                    raise ControlError("request_id_conflict", "同一请求编号不能用于不同操作")
                return {**result, "duplicate": True}
            if len(self.requests) >= 256:
                self.requests.popitem(last=False)
            async def execute(plan):
                device, (domain, service, data) = plan
                try:
                    await self.call_service(domain, service, data)
                    return {"entity_id": device.entity_id, "name": device.name, "ok": True,
                            "status": "accepted"}
                except Exception:
                    # No raw exception (which may contain device credentials) in public output.
                    return {"entity_id": device.entity_id, "ok": False,
                            "error": "service_failed", "message": "HA 执行失败，请查询设备状态"}
            results = list(errors) + list(await asyncio.gather(*(execute(plan) for plan in plans)))
            accepted = sum(r["ok"] for r in results)
            result = {"ok": accepted == len(results), "results": results,
                      "status": "accepted" if accepted == len(results) else "partial" if accepted else "failed",
                      "message": "指令已提交；实际状态请使用 status 查询" if accepted else "未执行任何控制指令"}
            self.requests[request_id] = (time.monotonic(), fingerprint, result)
            return result
