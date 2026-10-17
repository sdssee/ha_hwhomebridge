"""Capability checks shared by light discovery, control and reporting."""


def supports_light_action(attributes: dict, action: str) -> bool:
    """Use advertised capabilities, including older HA light attributes."""
    if action == "turn_on_off":
        return True
    modes = attributes.get("supported_color_modes")
    if modes is not None:
        modes = set(modes)
        if action == "set_brightness":
            return bool(modes - {"onoff", "unknown"})
        if action == "set_color_temp":
            return "color_temp" in modes
        if action == "set_color":
            return bool(modes & {"hs", "xy", "rgb", "rgbw", "rgbww"})
    features = attributes.get("supported_features", 0) or 0
    if action == "set_brightness":
        return bool(features & 1) or "brightness" in attributes
    if action == "set_color_temp":
        return bool(features & 2) or any(key in attributes for key in (
            "color_temp_kelvin", "color_temp", "min_color_temp_kelvin", "min_mireds"
        ))
    if action == "set_color":
        return bool(features & 16) or any(key in attributes for key in (
            "hs_color", "rgb_color", "xy_color"
        ))
    return True


def color_temperature_kelvin(attributes: dict):
    """Read Kelvin; accept mired state attributes from older integrations."""
    kelvin = attributes.get("color_temp_kelvin")
    if kelvin is not None:
        return int(kelvin)
    mired = attributes.get("color_temp")
    return round(1000000 / mired) if mired and mired > 0 else None
