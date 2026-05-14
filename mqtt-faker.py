import re
import json
import argparse
import paho.mqtt.client as mqtt

# Configuration
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
DEVICE_REGISTRY_PATH = "dev-scripts/.storage/core.device_registry"
ENTITY_REGISTRY_PATH = "dev-scripts/.storage/core.entity_registry"
LOVELACE_PATH = "ui-lovelace.yaml"
BASE_TOPIC = "mock"


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


def on_message(client, userdata, msg):
    print(f"Received command: {msg.topic} -> {msg.payload.decode()}")
    # Topic: BASE_TOPIC/<name>/<registry_obj_id>/set/...
    # State topic: BASE_TOPIC/<name>/<registry_obj_id>
    parts = msg.topic.split("/")
    if len(parts) >= 4:
        name = parts[1]
        obj_id = parts[2]
        try:
            payload = msg.payload.decode()
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = payload

            # Simple echo for state
            client.publish(
                f"{BASE_TOPIC}/{name}/{obj_id}",
                json.dumps(data) if isinstance(data, dict) else data,
                retain=True,
            )
            print(f"Emulated response: {BASE_TOPIC}/{name}/{obj_id} -> {data}")
        except Exception as e:
            print(f"Error handling command: {e}")


def start_emulation():
    reg = Registry()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(MQTT_BROKER, MQTT_PORT, 60)

    client.on_message = on_message
    devices = reg.get_target_devices()
    for _, info in devices.items():
        name = info.get("name_by_user") or info.get("name")
        # Subscribe to set commands for all components
        client.subscribe(f"{BASE_TOPIC}/{name}/+/set/#")
        print(f"Emulating device: {name}")

    print("Emulation mode active. Press Ctrl+C to exit.")
    client.loop_forever()


def publish_discovery():
    reg = Registry()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
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
            "o": {"name": "MQTT Mocker Script"},
            "cmps": {},
        }

        for entity in entities:
            entity_id = entity.get("entity_id")
            domain, registry_obj_id = entity_id.split(".", 1)

            if domain == "media_player":
                continue

            unique_id = entity.get("unique_id")
            cmp_key = f"{domain}_{registry_obj_id}"

            entity_state_topic = f"{BASE_TOPIC}/{name}/{registry_obj_id}"
            comp_config = {
                "p": domain,
                "unique_id": unique_id,
                "state_topic": entity_state_topic,
            }

            comp_config["has_entity_name"] = True
            comp_config["name"] = entity.get("original_name")
            comp_config["obj_id"] = registry_obj_id

            if entity.get("entity_id") in reg.entities_in_lovelace:
                comp_config["enabled_by_default"] = True

            device_class = entity.get("original_device_class") or entity.get(
                "device_class"
            )
            unit = entity.get("unit_of_measurement")

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

            if domain in [
                "switch",
                "number",
                "select",
                "button",
                "text",
                "lock",
                "light",
                "fan",
                "cover",
            ]:
                comp_config["command_topic"] = (
                    f"{BASE_TOPIC}/{name}/{registry_obj_id}/set"
                )

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
                        "current_temperature_topic": f"{BASE_TOPIC}/{name}",
                        "current_temperature_template": "{{ value_json.local_temperature }}",
                        "temperature_command_topic": f"{BASE_TOPIC}/{name}/set/current_heating_setpoint",
                        "temperature_state_topic": f"{BASE_TOPIC}/{name}",
                        "temperature_state_template": "{{ value_json.current_heating_setpoint }}",
                        "modes": ["off", "heat"],
                        "mode_command_topic": f"{BASE_TOPIC}/{name}/set/system_mode",
                        "mode_state_topic": f"{BASE_TOPIC}/{name}",
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
                "event",
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
                elif domain == "event":
                    caps = entity.get("capabilities") or {}
                    comp_config["event_types"] = caps.get("event_types", [])

            comp_config = {
                k: v for k, v in comp_config.items() if v is not None or k == "name"
            }
            payload["cmps"][cmp_key] = comp_config

        topic = f"homeassistant/device/{mqtt_id}/config"
        client.publish(topic, "", retain=True).wait_for_publish()
        print(json.dumps(payload))
        client.publish(topic, json.dumps(payload), retain=True)
        print(f"Published discovery for {name}")

    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--discover", action="store_true")
    group.add_argument("--emulate", action="store_true")
    args = parser.parse_args()

    if args.discover:
        publish_discovery()
    else:
        start_emulation()
