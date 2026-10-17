"""Optional Xiaoyi webhook; independent of the native HiLink product profiles."""

from pathlib import Path
import asyncio
import json
import logging
import secrets

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.util.yaml import load_yaml

from .const import DOMAIN
from .home_control import ControlError, HomeController, HomeDevice, dashboard_covers

CONFIG_KEY = "xiaoyi_control"
DEFAULT_DASHBOARD = "family-overview.yaml"
_LOGGER = logging.getLogger(__name__)


class XiaoyiAPI:
    def __init__(self, hass, settings):
        self.hass = hass
        self.settings = settings
        self.covers = {}
        self.controller = HomeController(self.catalog, self.call_service)

    async def refresh_scope(self):
        filename = self.settings.get("dashboard_file", DEFAULT_DASHBOARD)
        root = Path(self.hass.config.config_dir).resolve()
        path = (root / filename).resolve()
        if not path.is_relative_to(root):
            raise ControlError("invalid_dashboard", "仪表盘文件必须位于 HA 配置目录中")
        def read():
            return dashboard_covers(load_yaml(str(path)), self.settings.get("cover_view", "curtains"))
        try:
            self.covers = await self.hass.async_add_executor_job(read)
        except Exception as err:
            self.covers = {}
            raise ControlError("dashboard_unavailable", "无法读取配置的窗帘页面，未执行控制") from err

    def catalog(self):
        entity_registry = er.async_get(self.hass)
        device_registry = dr.async_get(self.hass)
        area_registry = ar.async_get(self.hass)
        result = []
        for domain in ("light", "cover"):
            for state in self.hass.states.async_all(domain):
                entity = entity_registry.async_get(state.entity_id)
                device = device_registry.async_get(entity.device_id) if entity and entity.device_id else None
                if entity and (entity.disabled_by or entity.hidden_by or entity.entity_category):
                    continue
                if device and device.disabled_by:
                    continue
                card = self.covers.get(state.entity_id)
                if domain == "cover" and card is None:
                    continue
                area_id = (entity.area_id if entity else None) or (device.area_id if device else None)
                area = area_registry.async_get_area(area_id) if area_id else None
                # Native /light lists the lights assigned to a visible area.
                if domain == "light" and area is None:
                    continue
                result.append(HomeDevice(
                    state.entity_id, (card or {}).get("name") or state.name,
                    area.name if area else (card or {}).get("area", ""), state.state,
                    dict(state.attributes), tuple(alias for alias in (entity.aliases or ())
                                                  if isinstance(alias, str)) if entity else (),
                ))
        return sorted(result, key=lambda d: (d.domain, d.area, d.name, d.entity_id))

    async def call_service(self, domain, service, data):
        async with asyncio.timeout(10):
            await self.hass.services.async_call(domain, service, data, blocking=True)

    async def handle(self, hass, webhook_id, request):
        # Health contains no catalog or control data and can reuse a TLS connection.
        if request.method == "GET":
            return web.json_response({"ok": True, "api_version": 1, "service": "hwhomebridge_home"})
        if request.content_length is not None and request.content_length > 16384:
            return web.json_response({"ok": False, "error": "request_too_large"}, status=413)
        try:
            raw = await request.content.read(16385)
            if len(raw) > 16384:
                return web.json_response({"ok": False, "error": "request_too_large"}, status=413)
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
        try:
            await self.refresh_scope()
            return web.json_response(await self.controller.handle(payload))
        except ControlError as err:
            return web.json_response({"ok": False, "error": err.code, "message": str(err),
                                      "candidates": err.candidates}, status=400)
        except Exception:
            _LOGGER.error("Xiaoyi home API failed; request details withheld")
            return web.json_response({"ok": False, "error": "internal_error",
                                      "message": "家庭控制接口异常，请检查 HA 日志"}, status=500)


async def async_setup_xiaoyi(hass, entry):
    settings = entry.data.get(CONFIG_KEY, {})
    if not settings.get("enabled"):
        return
    if not settings.get("webhook_id"):
        settings = {**settings, "webhook_id": secrets.token_hex(32)}
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONFIG_KEY: settings})
    api = XiaoyiAPI(hass, settings)
    webhook.async_register(hass, DOMAIN, "Xiaoyi home control", settings["webhook_id"], api.handle,
                           allowed_methods={"GET", "POST"}, local_only=False)
    hass.data.setdefault(DOMAIN, {})["xiaoyi_api"] = api
    entry.async_on_unload(lambda: webhook.async_unregister(hass, settings["webhook_id"]))
