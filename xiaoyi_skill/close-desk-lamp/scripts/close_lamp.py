#!/usr/bin/env python3
"""Xiaoyi CLI for the scoped HA home API; credentials stay in settings.json."""

import argparse
import http.client
import json
import os
from pathlib import Path
import ssl
import sys
from urllib.parse import urlsplit
import uuid

HEADERS = {"Content-Type": "application/json", "User-Agent": "hwhomebridge-xiaoyi/1.1", "Connection": "keep-alive"}
MAX_RESPONSE = 131072


def read_json(response):
    body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise ValueError("Response too large")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("Invalid response")
    return value


def load_settings():
    path = Path(__file__).resolve().parents[1] / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    endpoint = os.environ.get("HWHOMEBRIDGE_HOME_ENDPOINT") or settings.get("endpoint", "")
    target = urlsplit(endpoint)
    if (target.scheme != "https" or not target.hostname or target.username or target.password
            or target.query or target.fragment or not target.path):
        raise ValueError("Missing or invalid HTTPS endpoint")
    return target, settings


def open_verified_connection(target, settings):
    context = ssl.create_default_context()
    if settings.get("tls12_compat", True):
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
    # Only GETs may retry; catalog and control POSTs are each sent once.
    for _ in range(3):
        connection = http.client.HTTPSConnection(target.hostname, target.port or 443, timeout=5, context=context)
        try:
            connection.request("GET", target.path, headers=HEADERS)
            response = connection.getresponse()
            health = read_json(response)
            if (response.status == 200 and health.get("ok") is True and
                    health.get("api_version") == 1 and health.get("service") == "hwhomebridge_home"
                    and not response.will_close and connection.sock is not None):
                return connection
        except (OSError, ValueError, http.client.HTTPException):
            pass
        connection.close()
    raise ConnectionError("Gateway unavailable")


def request(payload):
    target, settings = load_settings()
    connection = open_verified_connection(target, settings)
    try:
        connection.sock.settimeout(20)
        connection.request("POST", target.path, body=json.dumps(payload, ensure_ascii=False).encode(), headers=HEADERS)
        response = connection.getresponse()
        result = read_json(response)
        if not 200 <= response.status < 300:
            result["ok"] = False
        return result
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="HA 家庭灯光、窗帘与晾衣架控制")
    parser.add_argument("--command", choices=("catalog", "status", "control"), default="control")
    parser.add_argument("--domain", choices=("light", "cover"))
    parser.add_argument("--kind", choices=("light", "curtain", "airer"))
    parser.add_argument("--name")
    parser.add_argument("--area")
    parser.add_argument("--entity-id")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--action", choices=("turn_on", "turn_off", "brightness", "color_temperature", "open", "close", "stop", "position"))
    parser.add_argument("--value", type=int)
    args = vars(parser.parse_args(argv))
    payload = {key: value for key, value in args.items() if value is not None and value is not False}
    # Preserve the original no-argument close-desk-lamp behavior.
    if payload == {"command": "control"}:
        payload.update(domain="light", name="台灯", action="turn_off")
    if payload["command"] == "control":
        payload["request_id"] = uuid.uuid4().hex
    try:
        result = request(payload)
    except (OSError, ValueError, http.client.HTTPException):
        result = {"ok": False, "error": "network_error",
                  "message": "网络请求未完成，执行状态未知。请先查询设备状态，不要自动重发控制指令。"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
