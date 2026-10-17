"""Light regression tests; HA registries and native SDK are isolated test doubles.

Run: python -m pytest -q (requires aiofiles, psutil, pytest).
"""
import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hwhomebridge"
package = types.ModuleType("bridge_test")
package.__path__ = [str(COMPONENT)]
sys.modules[package.__name__] = package

# Do not import the integration entry point or load the Linux .so on Windows.
for name in ("homeassistant", "homeassistant.core", "homeassistant.helpers",
             "homeassistant.helpers.device_registry", "homeassistant.helpers.entity_registry",
             "homeassistant.helpers.area_registry", "homeassistant.const"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["homeassistant.core"].HomeAssistant = object
sys.modules["homeassistant.helpers.device_registry"].EVENT_DEVICE_REGISTRY_UPDATED = "device_registry_updated"
sys.modules["homeassistant.helpers.entity_registry"].EVENT_ENTITY_REGISTRY_UPDATED = "entity_registry_updated"
sys.modules["homeassistant.const"].EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
sys.modules["homeassistant.const"].EVENT_STATE_CHANGED = "state_changed"

bridge = importlib.import_module("bridge_test.hwbridge")
Router = importlib.import_module("bridge_test.service_router").ServiceRouter
support = importlib.import_module("bridge_test.light_support")


@pytest.fixture
def env(monkeypatch):
    router = Router()
    assert asyncio.run(router.load_product_registry(str(COMPONENT / "config/product_registry.json")))
    states, devices, entities = {}, {}, {}
    entry = NS(data={})
    def update_entry(config_entry, *, data):
        config_entry.data = data
    config_entries = NS(async_entries=lambda domain: [entry], async_update_entry=update_entry)
    loop = NS(call_later=Mock(return_value=NS(cancel=Mock())))
    hass = NS(states=NS(get=states.get, async_all=lambda domain: [s for s in states.values()
                    if s.entity_id.startswith(domain + ".")]),
              config_entries=config_entries, loop=loop)
    dev_reg = NS(devices=devices, async_get=devices.get)
    ent_reg = NS(entities=entities, async_get=entities.get)
    monkeypatch.setattr(bridge.dr, "async_get", lambda hass: dev_reg, raising=False)
    monkeypatch.setattr(bridge.er, "async_get", lambda hass: ent_reg, raising=False)
    monkeypatch.setattr(bridge.er, "async_entries_for_device", lambda reg, did: [
        entity for entity in entities.values() if entity.device_id == did], raising=False)
    monkeypatch.setattr(bridge, "ghass", hass)
    monkeypatch.setattr(bridge, "service_router", router)
    monkeypatch.setattr(bridge, "registered_device_ids", set())
    monkeypatch.setattr(bridge, "saved_device_ids", set())
    monkeypatch.setattr(bridge, "_excluded_device_sns", set())
    monkeypatch.setattr(bridge, "_disabled_device_sns", set())
    monkeypatch.setattr(bridge, "_save_device_id", Mock())
    monkeypatch.setattr(bridge, "_create_bridge_device_entry", Mock())
    monkeypatch.setattr(bridge, "_remove_bridge_device_entry", Mock())
    monkeypatch.setattr(bridge, "bridge_work", True)
    monkeypatch.setattr(bridge, "start_work", True)
    monkeypatch.setattr(bridge, "_light_discovery_timer", None)
    router.register_device_to_hilink = Mock(return_value=True)
    router.set_hass(hass)
    calls = Mock()
    router.action_dispatcher._call_service_async = calls

    def add(entity_id, device_id=None, name="通道", device_name="Generic device", modes=("onoff",),
            platform="xiaomi_home", registry_id=None, disabled=None, registered=True, attrs=None):
        if device_id:
            devices.setdefault(device_id, NS(name=device_name, name_by_user=None, model="",
                               manufacturer="", disabled_by=None))
        if registered:
            entities[entity_id] = NS(entity_id=entity_id, id=registry_id or entity_id,
                                    device_id=device_id, disabled_by=disabled, platform=platform,
                                    device_class=None, name=None)
        states[entity_id] = NS(entity_id=entity_id, name=name, state="off", attributes={
            "supported_color_modes": list(modes), **(attrs or {})})
    return NS(router=router, states=states, devices=devices, entities=entities,
              add=add, calls=calls, entry=entry, hass=hass)


def test_all_19_channels_with_real_device_name_patterns(env):
    # The inspected home has 18 devices and 19 light entities; one switch has two channels.
    for index in range(19):
        device_id = "entrance" if index in (10, 11) else f"device{index}"
        device_name = {0: "Aqara 妙控开关 S1E", 3: "米家智能晾衣机 1S",
                       17: "主卧开关", 18: "主卧卫生间"}.get(index, "Generic device")
        modes = ("brightness",) if index == 3 else ("color_temp",) if 4 <= index <= 9 else ("onoff",)
        env.add(f"light.channel_{index}", device_id, device_name=device_name, modes=modes)
    bridge._discover_and_register_devices(env.hass)
    vds = env.router.sn_manager.get_all_devices()
    assert len(vds) == 19
    assert len({vd.sn for vd in vds}) == 19
    for index in range(19):
        vd = env.router.sn_manager.get_virtual_device_by_entity(f"light.channel_{index}")
        assert vd.get_entity_for_service("switch") == f"light.channel_{index}"
    # Different entrance SNs route to different channels.
    for index in (10, 11):
        vd = env.router.sn_manager.get_virtual_device_by_entity(f"light.channel_{index}")
        assert env.router.route_action(vd.sn, {"on": 1})
        assert env.calls.call_args.kwargs["entity_id"] == f"light.channel_{index}"
    count = env.router.register_device_to_hilink.call_count
    bridge._discover_and_register_devices(env.hass)
    assert env.router.register_device_to_hilink.call_count == count


def test_template_group_and_state_only_lights(env):
    env.add("light.template", platform="template")
    env.add("light.group", platform="group")
    env.add("light.yaml", registered=False)
    bridge._discover_and_register_devices(env.hass)
    assert env.router.sn_manager.get_device_count() == 3


def test_disabled_and_reverse_bridge_filtered(env):
    env.add("light.disabled", "disabled", disabled="user")
    env.add("light.reverse", "reverse", platform="huawei_smarthome")
    env.add("light.disabled_device", "disabled_device")
    env.devices["disabled_device"].disabled_by = "user"
    env.add("light.ok", "ok")
    bridge._discover_and_register_devices(env.hass)
    assert env.router.sn_manager.get_device_count() == 1


def test_primary_sn_and_secondary_rename_survive_restart_and_reorder(env):
    env.add("light.first", "multi", registry_id="first")
    env.add("light.second", "multi", registry_id="second")
    bridge._discover_and_register_devices(env.hass)
    original = {entity_id: env.router.sn_manager.get_entity_sn(entity_id) for entity_id in env.states}
    assert original["light.first"] == bridge.SNManager.generate_sn("multi")
    env.entities["light.renamed"] = env.entities.pop("light.second")
    env.entities["light.renamed"].entity_id = "light.renamed"
    env.states["light.renamed"] = env.states.pop("light.second")
    env.states["light.renamed"].entity_id = "light.renamed"
    first = env.entities.pop("light.first")
    env.entities["light.first"] = first
    env.router.sn_manager.clear()  # config entry primary_lights persists over restart
    bridge.registered_device_ids.clear()
    bridge._discover_and_register_devices(env.hass)
    assert env.router.sn_manager.get_entity_sn("light.first") == original["light.first"]
    assert env.router.sn_manager.get_entity_sn("light.renamed") == original["light.second"]


@pytest.mark.parametrize("modes,expected", [(("onoff",), {"switch"}),
    (("brightness",), {"switch", "brightness"}),
    (("color_temp",), {"switch", "brightness", "cct"}),
    (("rgb",), {"switch", "brightness"})])
def test_capability_mapping(env, modes, expected):
    env.add("light.test", "device", modes=modes)
    bridge._discover_and_register_devices(env.hass)
    vd = env.router.sn_manager.get_by_device_id("device")
    assert set(vd.service_entries) == expected


def test_brightness_zero_and_actual_color_temperature_limits(env):
    env.add("light.test", "device", modes=("color_temp",), attrs={
        "min_color_temp_kelvin": 2700, "max_color_temp_kelvin": 5700})
    bridge._discover_and_register_devices(env.hass)
    vd = env.router.sn_manager.get_by_device_id("device")
    assert env.router.route_action(vd.sn, {"brightness": 0})
    assert env.calls.call_args.kwargs["data"] == {"brightness": 0}
    assert env.router.route_action(vd.sn, {"colorTemperature": 6500})
    assert env.calls.call_args.kwargs["data"] == {"color_temp_kelvin": 5700}
    assert env.router.route_action(vd.sn, {"colorTemperature": 2000})
    assert env.calls.call_args.kwargs["data"] == {"color_temp_kelvin": 2700}


def test_excluded_light_cannot_reenable_itself_from_state_events(env):
    env.add("light.test", "device")
    bridge._discover_and_register_devices(env.hass)
    vd = env.router.sn_manager.get_by_device_id("device")
    bridge._excluded_device_sns.add(vd.sn)
    vd.online = False
    env.router.register_device_to_hilink.reset_mock()
    old = NS(state="unavailable", attributes={})
    bridge.handle_state_event(NS(data={"entity_id": "light.test", "old_state": old,
                                      "new_state": env.states["light.test"]}))
    env.router.register_device_to_hilink.assert_not_called()


def test_secondary_entity_disabled_and_removed(env):
    env.add("light.first", "multi")
    env.add("light.second", "multi")
    bridge._discover_and_register_devices(env.hass)
    env.entities["light.second"].disabled_by = "user"
    bridge._refresh_lights()
    assert env.router.sn_manager.get_virtual_device_by_entity("light.second") is None
    assert env.router.sn_manager.get_device_count() == 1
    env.entities["light.second"].disabled_by = None
    bridge._refresh_lights()
    assert env.router.sn_manager.get_device_count() == 2


def test_legacy_temperature_conversion():
    assert support.color_temperature_kelvin({"color_temp": 250}) == 4000
    assert support.color_temperature_kelvin({"color_temp_kelvin": 3700}) == 3700
    assert support.color_temperature_kelvin({"color_temp": None}) is None


def test_renaming_live_secondary_does_not_delete_cloud_device(env):
    env.add("light.first", "multi", registry_id="first")
    env.add("light.second", "multi", registry_id="second")
    bridge._discover_and_register_devices(env.hass)
    sn = env.router.sn_manager.get_entity_sn("light.second")
    env.entities["light.new_name"] = env.entities.pop("light.second")
    env.entities["light.new_name"].entity_id = "light.new_name"
    env.states["light.new_name"] = env.states.pop("light.second")
    env.states["light.new_name"].entity_id = "light.new_name"
    env.router.register_device_to_hilink.reset_mock()
    bridge._refresh_lights()
    assert env.router.sn_manager.get_entity_sn("light.new_name") == sn
    assert not any(call.args[1] == 2 for call in env.router.register_device_to_hilink.call_args_list)


def test_unavailable_initial_light_and_recovery(env):
    env.add("light.offline", "device")
    env.states["light.offline"].state = "unavailable"
    bridge._discover_and_register_devices(env.hass)
    vd = env.router.sn_manager.get_by_device_id("device")
    assert not vd.online
    assert env.router.register_device_to_hilink.call_args.args == (vd.sn, 0)
    env.states["light.offline"].state = "off"
    bridge.handle_state_event(NS(data={"entity_id": "light.offline", "old_state":
        NS(state="unavailable", attributes={"supported_color_modes": ["onoff"]}),
        "new_state": env.states["light.offline"]}))
    assert vd.online
    assert env.router.register_device_to_hilink.call_args.args == (vd.sn, 1)


def test_state_sync_reports_each_light_service_once(env):
    env.add("light.dimmable", "device", modes=("color_temp",), attrs={
        "brightness": 128, "color_temp_kelvin": 3700})
    env.states["light.dimmable"].state = "on"
    bridge._discover_and_register_devices(env.hass)
    vd = env.router.sn_manager.get_by_device_id("device")
    env.router._update_hw_bridge_status = Mock()
    env.router.sync_device_state(vd)
    reports = env.router._update_hw_bridge_status.call_args_list
    assert len(reports) == 3
    assert {call.args[1] for call in reports} == {"switch", "brightness", "cct"}


def test_failed_rematch_retains_old_mapping(env):
    env.add("light.test", "device")
    bridge._discover_and_register_devices(env.hass)
    vd = env.router.sn_manager.get_by_device_id("device")
    assert env.router.register_device("device", "Unknown", [{"entity_id": "sensor.only",
        "domain": "sensor"}], reuse_sn=vd.sn) is None
    assert env.router.sn_manager.get_by_sn(vd.sn) is vd


def test_ha_2026_device_collection_yields_entries(env, monkeypatch):
    env.add("light.test", "device")
    env.devices["device"].id = "device"
    registry = NS(devices=list(env.devices.values()), async_get=env.devices.get)
    monkeypatch.setattr(bridge.dr, "async_get", lambda hass: registry)
    bridge._discover_and_register_devices(env.hass)
    assert env.router.sn_manager.get_device_count() == 1


def test_sdk_cache_is_immutable_across_updates(tmp_path):
    source = tmp_path / "component.so"
    cache = tmp_path / "runtime"
    source.write_bytes(b"SDK version one")
    first = Path(bridge._prepare_native_library(str(source), str(cache)))
    assert Path(bridge._prepare_native_library(str(source), str(cache))) == first
    source.write_bytes(b"SDK version two")
    second = Path(bridge._prepare_native_library(str(source), str(cache)))
    assert second != first
    assert first.read_bytes() == b"SDK version one"
    assert second.read_bytes() == b"SDK version two"
