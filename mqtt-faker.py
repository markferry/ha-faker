import re
import json
import paho.mqtt.client as mqtt

# Configuration
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
DEVICE_REGISTRY_PATH = "dev-scripts/.storage/core.device_registry"
ENTITY_REGISTRY_PATH = "dev-scripts/.storage/core.entity_registry"
LOVELACE_PATH = "ui-lovelace.yaml"


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


class Registry:
    def __init__(self):
        self.devices = load_json(DEVICE_REGISTRY_PATH)["data"]["devices"]
        self.entities = load_json(ENTITY_REGISTRY_PATH)["data"]["entities"]

        with open(LOVELACE_PATH, "r") as f:
            lovelace_content = f.read()

        # Extract potential entity IDs from Lovelace config
        self.entities_in_lovelace = set(
            re.findall(r"[a-z0-9_]+\.[a-z0-9_]+", lovelace_content)
        )

        self.device_entities = {}
        for entity in self.entities:
            device_id = entity.get("device_id")
            if device_id:
                if device_id not in self.device_entities:
                    self.device_entities[device_id] = []
                self.device_entities[device_id].append(entity)

    def get_target_devices(self):
        targets = {}
        excluded_domains = {"device_tracker", "media_player"}

        # Identify all device IDs that should be excluded
        excluded_device_ids = set()
        for entity in self.entities:
            domain = entity.get("entity_id", "").split(".")[0]
            if domain in excluded_domains:
                device_id = entity.get("device_id")
                if device_id:
                    excluded_device_ids.add(device_id)

        # Filter devices
        for device in self.devices:
            if device.get("disabled_by") is None:
                if device["id"] not in excluded_device_ids:
                    targets[device["id"]] = device
        return targets


def publish_discovery():
    reg = Registry()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
    except Exception as e:
        print(f"Failed to connect to MQTT broker: {e}")
        return

    client.loop_start()

    devices = reg.get_target_devices()

    for device_id, info in devices.items():
        name = info.get("name_by_user") or info.get("name")
        mqtt_id = None
        for id_list in info.get("identifiers", []):
            if id_list[0] in ["mqtt", "mpd"]:
                mqtt_id = id_list[1]
                break

        if not mqtt_id:
            mqtt_id = f"dummy_{name}_{device_id[-6:]}"

        entities = reg.device_entities.get(device_id, [])

        # Identify the primary entity (heuristic: original_name is None and preferred domain)
        primary_entity = None
        candidates = [e for e in entities if e.get("original_name") is None]
        if candidates:
            priority = [
                "climate",
                "light",
                "switch",
                "fan",
                "lock",
                "cover",
                "media_player",
            ]
            candidates.sort(
                key=lambda e: (
                    priority.index(e["entity_id"].split(".")[0])
                    if e["entity_id"].split(".")[0] in priority
                    else 99
                )
            )
            primary_entity = candidates[0]

        # Build Device Discovery Payload
        payload = {
            "dev": {
                k: v
                for k, v in {
                    "ids": [mqtt_id],
                    "name": name,
                    "mf": info.get("manufacturer"),
                    "mdl": info.get("model"),
                    "sw": info.get("sw_version"),
                    "hw": info.get("hw_version"),
                }.items()
                if v is not None
            },
            "o": {
                "name": "DumpToMQTT Script",
            },
            "cmps": {},
        }

        # Normalize device name for prefix stripping
        dev_name_norm = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")

        for entity in entities:
            entity_id = entity.get("entity_id")
            domain, registry_obj_id = entity_id.split(".", 1)

            # MQTT discovery does not support media_player components
            if domain == "media_player":
                continue

            unique_id = entity.get("unique_id")

            # Use a domain-prefixed key to avoid naming collisions in the cmps map
            cmp_key = f"{domain}_{registry_obj_id}"

            comp_config = {
                "p": domain,
                "unique_id": unique_id,
                "state_topic": f"mock/{name}",
            }

            # Use 'has_entity_name' to follow modern naming conventions
            # Match live system naming by using original_name directly
            comp_config["has_entity_name"] = True
            comp_config["name"] = entity.get("original_name")

            # Match the live system's entity_id exactly by using the full object_id from the registry.
            # Providing 'obj_id' in MQTT discovery overrides automatic generation and prefixing.
            comp_config["obj_id"] = registry_obj_id

            if entity.get("entity_id") in reg.entities_in_lovelace:
                comp_config["enabled_by_default"] = True

            # Use 'dev_cla' for device_class as per MQTT Discovery Payload recommendations
            # Prefer original_device_class from the registry if available
            device_class = entity.get("original_device_class") or entity.get(
                "device_class"
            )
            unit = entity.get("unit_of_measurement")

            # Fix common unit/device_class mismatches to satisfy HA validation
            if unit == "kWh" and device_class == "power":
                device_class = "energy"
            elif unit == "W" and device_class == "energy":
                device_class = "power"

            if device_class:
                comp_config["dev_cla"] = device_class

            if entity.get("entity_category"):
                comp_config["entity_category"] = entity.get("entity_category")
            if entity.get("icon"):
                comp_config["icon"] = entity.get("icon")
            if unit:
                comp_config["unit_of_measurement"] = unit

            # Domain-specific configuration
            if domain in [
                "switch",
                "number",
                "select",
                "button",
                "text",
                "lock",
                "light",
                "media_player",
                "fan",
                "cover",
            ]:
                comp_config["command_topic"] = f"mock/{name}/set"

            if domain == "light":
                comp_config.update(
                    {
                        "brightness": True,
                        "brightness_scale": 254,
                        "schema": "json",
                        "supported_color_modes": ["brightness"],
                    }
                )
            elif domain == "climate":
                comp_config.update(
                    {
                        "current_temperature_topic": f"mock/{name}",
                        "current_temperature_template": "{{ value_json.local_temperature }}",
                        "temperature_command_topic": f"mock/{name}/set/current_heating_setpoint",
                        "temperature_state_topic": f"mock/{name}",
                        "temperature_state_template": "{{ value_json.current_heating_setpoint }}",
                        "modes": ["off", "heat"],
                        "mode_command_topic": f"mock/{name}/set/system_mode",
                        "mode_state_topic": f"mock/{name}",
                        "mode_state_template": "{{ value_json.system_mode }}",
                    }
                )
            elif domain == "sensor":
                comp_config.update(
                    {
                        "value_template": "{{ value_json."
                        + (registry_obj_id.split("_")[-1])
                        + " }}",
                    }
                )
            elif domain in [
                "switch",
                "number",
                "select",
                "binary_sensor",
                "button",
                "text",
                "lock",
                "media_player",
            ]:
                if domain == "switch":
                    comp_config.update({"payload_on": "ON", "payload_off": "OFF"})
                elif domain == "number":
                    caps = entity.get("capabilities") or {}
                    for k in ["min", "max", "step"]:
                        if k in caps:
                            comp_config[k] = caps[k]
                elif domain == "select":
                    caps = entity.get("capabilities") or {}
                    if "options" in caps:
                        comp_config["options"] = caps["options"]
                elif domain == "binary_sensor":
                    comp_config.pop("command_topic", None)
                    comp_config.update({"value_template": "{{ value_json.state }}"})
                elif domain == "media_player":
                    comp_config.update(
                        {
                            "state_topic": f"mock/{name}",
                        }
                    )

            # Clean up empty values (omit keys)
            comp_config = {
                k: v for k, v in comp_config.items() if v is not None or k == "name"
            }

            payload["cmps"][cmp_key] = comp_config

        # Published under the device topic
        topic = f"homeassistant/device/{mqtt_id}/config"

        # Clear existing discovery to ensure a clean state
        client.publish(topic, "", retain=True).wait_for_publish()

        print(json.dumps(payload))
        client.publish(topic, json.dumps(payload), retain=True)
        print(f"Published device discovery for {name} to {topic}")

    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    publish_discovery()
