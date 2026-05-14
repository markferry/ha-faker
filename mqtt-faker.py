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
MOCK_YAML_PATH = "mock.yaml"
BASE_TOPIC = "mock"


def slugify(text):
    return re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


class Registry:
    def __init__(self):
        self.devices = load_json(DEVICE_REGISTRY_PATH)["data"]["devices"]
        self.entities = load_json(ENTITY_REGISTRY_PATH)["data"]["entities"]

        with open(LOVELACE_PATH, "r") as f:
            lovelace_content = f.read()

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
        excluded_domains = {"device_tracker"}

        excluded_device_ids = set()
        for entity in self.entities:
            domain = entity.get("entity_id", "").split(".")[0]
            if domain in excluded_domains:
                device_id = entity.get("device_id")
                if device_id:
                    excluded_device_ids.add(device_id)

        for device in self.devices:
            if device.get("disabled_by") is None:
                if device["id"] not in excluded_device_ids:
                    targets[device["id"]] = device
        return targets


def on_message(client, userdata, msg):
    parts = msg.topic.split("/")
    if len(parts) >= 4:
        name, obj_id = parts[1], parts[2]
        try:
            payload = msg.payload.decode()
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = payload
            client.publish(
                f"{BASE_TOPIC}/{name}/{obj_id}",
                json.dumps(data) if isinstance(data, dict) else data,
                retain=True,
            )
        except (UnicodeDecodeError, TypeError, ValueError) as e:
            print(f"Error handling MQTT message: {e}")


def start_emulation():
    reg = Registry()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.on_message = on_message
    devices = reg.get_target_devices()
    for _, info in devices.items():
        name = slugify(info.get("name_by_user") or info.get("name"))
        client.subscribe(f"{BASE_TOPIC}/{name}/+/set/#")
    client.loop_forever()


def publish_discovery():
    reg = Registry()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()

    devices = reg.get_target_devices()
    universal_players = {}

    for entity in reg.entities:
        e_id = entity.get("entity_id", "")
        if e_id.startswith("media_player."):
            name = e_id.split(".")[1]
            if name not in universal_players:
                universal_players[name] = {"entities": []}
            universal_players[name]["entities"].append(e_id)

    for device_id, info in devices.items():
        name = info.get("name_by_user") or info.get("name")
        slug_name = slugify(name)
        mqtt_id = next(
            (
                id_list[1]
                for id_list in info.get("identifiers", [])
                if id_list[0] in ["mqtt", "mpd"]
            ),
            f"dummy_{slug_name}_{device_id[-6:]}",
        )
        entities = reg.device_entities.get(device_id, [])
        payload = {
            "dev": {"ids": [mqtt_id], "name": name},
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
            state_topic = f"{BASE_TOPIC}/{slug_name}/{registry_obj_id}"

            comp = {
                "p": domain,
                "unique_id": unique_id,
                "state_topic": state_topic,
                "has_entity_name": True,
                "name": entity.get("original_name") or domain.capitalize(),
                "obj_id": registry_obj_id,
            }
            if entity_id in reg.entities_in_lovelace:
                comp["enabled_by_default"] = True

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
                comp["command_topic"] = (
                    f"{BASE_TOPIC}/{slug_name}/{registry_obj_id}/set"
                )

            if domain == "light":
                comp.update(
                    {
                        "brightness": True,
                        "brightness_scale": 254,
                        "schema": "json",
                        "supported_color_modes": ["brightness"],
                    }
                )
            elif domain == "sensor":
                comp.update(
                    {
                        "value_template": "{{ value_json."
                        + registry_obj_id.split("_")[-1]
                        + " }}"
                    }
                )
            elif domain == "switch":
                comp.update({"payload_on": "ON", "payload_off": "OFF"})

            payload["cmps"][cmp_key] = {
                k: v for k, v in comp.items() if v is not None or k == "name"
            }

        topic = f"homeassistant/device/{mqtt_id}/config"
        client.publish(topic, "", retain=True).wait_for_publish()
        client.publish(topic, json.dumps(payload), retain=True)

    if universal_players:
        with open(MOCK_YAML_PATH, "w") as f:
            f.write("media_player:\n")
            for name, data in universal_players.items():
                slug = slugify(name)
                f.write(
                    f"  - platform: universal\n    name: {name}\n    children:\n      - switch.{slug}_power\n    commands:\n      turn_on:\n        service: mqtt.publish\n        data:\n          topic: {BASE_TOPIC}/{slug}/power_power/set\n          payload: 'ON'\n      turn_off:\n        service: mqtt.publish\n        data:\n          topic: {BASE_TOPIC}/{slug}/power_power/set\n          payload: 'OFF'\n"
                )

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
