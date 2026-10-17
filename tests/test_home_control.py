"""Scoped API regression tests: real resolution/validation, isolated HA services."""
import asyncio
import importlib
import json
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock

import pytest

pkg = types.ModuleType("home_control_test")
pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components/hwhomebridge")]
sys.modules[pkg.__name__] = pkg
m = importlib.import_module("home_control_test.home_control")


def device(entity="light.lamp", name="台灯", area="书房", **kw):
    return m.HomeDevice(entity, name, area, "on", kw.get("attrs", {"supported_color_modes": ["brightness"]}))


def run(controller, **payload):
    return asyncio.run(controller.handle(payload))


def controller(devices):
    call = AsyncMock()
    return m.HomeController(lambda: devices, call), call


def test_scope_reads_card_targets_and_corrected_airer_only():
    config = {"views": [{"path": "curtains", "sections": [{"cards": [
        {"type": "heading", "heading": "阳台"},
        {"entity": "cover.corrected", "name": "晾衣架", "tap_action": {"target": {"entity_id": "cover.raw"}}},
        {"entity": "light.airer"}]}]}]}
    assert m.dashboard_covers(config) == {"cover.corrected": {"name": "晾衣架", "area": "阳台"}}
    assert m.dashboard_covers(config, "absent") == {}


def test_brightness_zero_is_explicit_off():
    c, call = controller([device()])
    assert run(c, command="control", name="台灯", action="brightness", value=0, request_id="request-001")["ok"]
    call.assert_awaited_once_with("light", "turn_off", {"entity_id": "light.lamp"})


@pytest.mark.parametrize("value", [True, -1, 101, 50.5, "50", None])
def test_invalid_percentage_never_calls_ha(value):
    c, call = controller([device()])
    with pytest.raises(m.ControlError):
        run(c, command="control", name="台灯", action="brightness", value=value, request_id="request-001")
    call.assert_not_awaited()


def test_ambiguous_names_need_explicit_area():
    c, call = controller([device(), device("light.other", area="主卧")])
    with pytest.raises(m.ControlError, match="多个") as err:
        run(c, command="control", name="台灯", action="turn_off", request_id="request-001")
    assert len(err.value.candidates) == 2
    call.assert_not_awaited()
    assert run(c, command="control", name="台灯", area="书房", action="turn_off", request_id="request-001")["ok"]


def test_no_arbitrary_entities_domains_or_service_data():
    c, call = controller([device()])
    for payload in ({"entity_id": "light.not_exposed"}, {"domain": "lock", "name": "台灯"},
                    {"name": "台灯", "service": "lock.unlock"}, {"all": True}, {"name": "台灯", "all": True}):
        with pytest.raises(m.ControlError):
            run(c, command="control", action="turn_off", request_id="request-001", **payload)
    call.assert_not_awaited()


def test_group_reports_unsupported_without_wrong_service():
    c, call = controller([device(), device("light.switch", "顶灯", attrs={"supported_color_modes": ["onoff"]})])
    result = run(c, command="control", domain="light", all=True, action="brightness", value=50, request_id="request-001")
    assert not result["ok"]
    assert result["results"][0]["error"] == "unsupported_action"
    call.assert_awaited_once_with("light", "turn_on", {"entity_id": "light.lamp", "brightness_pct": 50})


def test_curtains_group_excludes_airer_and_template_uses_correct_entity():
    curtains = [device(f"cover.c{i}", f"窗帘{i}", attrs={"supported_features": 15}) for i in range(5)]
    airer = device("cover.corrected", "晾衣架", "阳台", attrs={"supported_features": 15})
    c, call = controller(curtains + [airer])
    assert run(c, command="control", domain="cover", kind="curtain", all=True, action="stop", request_id="request-001")["ok"]
    assert call.await_count == 5
    run(c, command="control", name="晾衣架", action="position", value=80, request_id="request-002")
    call.assert_awaited_with("cover", "set_cover_position", {"entity_id": "cover.corrected", "position": 80})


def test_position_zero_and_feature_check():
    c, call = controller([device("cover.curtain", "布帘", attrs={"supported_features": 15})])
    run(c, command="control", name="布帘", action="position", value=0, request_id="request-001")
    call.assert_awaited_once_with("cover", "set_cover_position", {"entity_id": "cover.curtain", "position": 0})


def test_duplicate_and_conflicting_request_ids():
    c, call = controller([device()])
    payload = dict(command="control", name="台灯", action="turn_off", request_id="request-001")
    run(c, **payload)
    assert run(c, **payload)["duplicate"]
    call.assert_awaited_once()
    with pytest.raises(m.ControlError, match="不同操作"):
        run(c, **{**payload, "action": "turn_on"})
    call.assert_awaited_once()


def test_unavailable_and_service_failure_are_not_success():
    d = device()
    d.state = "unavailable"
    c, call = controller([d])
    result = run(c, command="control", name="台灯", action="turn_off", request_id="request-001")
    assert result["results"][0]["error"] == "unavailable"
    assert result["status"] == "failed" and result["message"] == "未执行任何控制指令"
    call.assert_not_awaited()
    d.state = "on"
    call.side_effect = RuntimeError("secret detail")
    result = run(c, command="control", name="台灯", action="turn_off", request_id="request-002")
    assert not result["ok"] and "secret detail" not in str(result)


def test_catalog_and_status_never_control_and_read_fresh_state():
    d = device()
    c, call = controller([d])
    assert run(c, command="catalog", area="书房")["count"] == 1
    d.state = "off"
    assert run(c, command="status", name="台灯")["devices"][0]["state"] == "off"
    call.assert_not_awaited()


def test_group_invalid_value_is_atomic():
    c, call = controller([device(), device("light.two")])
    with pytest.raises(m.ControlError):
        run(c, command="control", domain="light", all=True, action="brightness", value=1000, request_id="request-001")
    call.assert_not_awaited()


def test_concurrent_duplicate_requests_dispatch_once():
    c, call = controller([device()])
    payload = dict(command="control", name="台灯", action="turn_off", request_id="request-001")
    async def concurrent():
        return await asyncio.gather(c.handle(payload), c.handle(payload))
    results = asyncio.run(concurrent())
    assert sum(bool(r.get("duplicate")) for r in results) == 1
    call.assert_awaited_once()


def test_full_cache_keeps_oldest_duplicate_until_eviction():
    c, call = controller([device()])
    async def fill_and_repeat():
        for index in range(256):
            await c.handle(dict(command="control", name="台灯", action="turn_off", request_id=f"request-{index:03}"))
        return await c.handle(dict(command="control", name="台灯", action="turn_off", request_id="request-000"))
    assert asyncio.run(fill_and_repeat())["duplicate"]
    assert call.await_count == 256


def test_ha_computed_alias_markers_are_not_public_strings():
    d = device()
    d.aliases = (object(), "书桌灯")
    c, call = controller([d])
    result = run(c, command="catalog")
    assert json.loads(json.dumps(result))["devices"][0]["aliases"] == ["书桌灯"]
    assert run(c, command="status", name="书桌灯")["ok"]
    call.assert_not_awaited()
