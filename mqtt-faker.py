import json
import paho.mqtt.client as mqtt

# Configuration
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
DEVICE_REGISTRY_PATH = "dev-scripts/.storage/core.device_registry"
ENTITY_REGISTRY_PATH = "dev-scripts/.storage/core.entity_registry"

TARGET_DEVICE_NAMES = [
    "lounge_east_dimmer",
    "lounge_west_dimmer",
    "lounge_north_dimmer",
]


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


class Registry:
    def __init__(self):
        self.devices = load_json(DEVICE_REGISTRY_PATH)["data"]["devices"]
        self.entities = load_json(ENTITY_REGISTRY_PATH)["data"]["entities"]

        self.device_entities = {}
        for entity in self.entities:
            device_id = entity.get("device_id")
            if device_id:
                if device_id not in self.device_entities:
                    self.device_entities[device_id] = []
                self.device_entities[device_id].append(entity)

    def get_target_devices(self):
        targets = {}
        for device in self.devices:
            name = device.get("name") or ""
            name_by_user = device.get("name_by_user") or ""
            if name in TARGET_DEVICE_NAMES or name_by_user in TARGET_DEVICE_NAMES:
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
            if id_list[0] == "mqtt":
                mqtt_id = id_list[1]
                break

        if not mqtt_id:
            mqtt_id = f"dummy_{name}_{device_id[-6:]}"

        entities = reg.device_entities.get(device_id, [])

        # Build Device Discovery Payload
        payload = {
            "dev": {
                "ids": [mqtt_id],
                "name": name,
                "mf": info.get("manufacturer"),
                "mdl": info.get("model"),
                "sw": info.get("sw_version"),
                "hw": info.get("hw_version"),
            },
            "o": {
                "name": "DumpToMQTT Script",
            },
            "cmps": {},
            "state_topic": f"zigbee2mqtt/{name}",
        }

        for entity in entities:
            entity_id = entity.get("entity_id")
            domain = entity_id.split(".")[0]
            unique_id = entity.get("unique_id")
            object_id = entity.get("suggested_object_id") or entity_id.split(".", 1)[1]

            # Use a domain-prefixed key to avoid naming collisions in the cmps map
            cmp_key = f"{domain}_{object_id}"

            comp_config = {
                "p": domain,
                "unique_id": unique_id,
                "state_topic": f"zigbee2mqtt/{name}",
            }

            if domain == "light":
                # Mark light as the primary feature
                comp_config["has_entity_name"] = True
                comp_config["name"] = None
            else:
                comp_config["has_entity_name"] = False
                comp_config["name"] = (
                    entity.get("original_name") or entity.get("name") or object_id
                )

            # Use 'dev_cla' for device_class as per MQTT Discovery Payload recommendations
            # Prefer original_device_class from the registry if available
            device_class = entity.get("original_device_class") or entity.get(
                "device_class"
            )
            if device_class:
                comp_config["dev_cla"] = device_class

            if entity.get("entity_category"):
                comp_config["entity_category"] = entity.get("entity_category")
            if entity.get("icon"):
                comp_config["icon"] = entity.get("icon")

            # Domain-specific configuration
            if domain == "light":
                comp_config.update(
                    {
                        "brightness": True,
                        "brightness_scale": 254,
                        "command_topic": f"zigbee2mqtt/{name}/set",
                        "schema": "json",
                        "supported_color_modes": ["brightness"],
                    }
                )
            elif domain == "sensor":
                comp_config.update(
                    {
                        "unit_of_measurement": entity.get("unit_of_measurement"),
                        "value_template": "{{ value_json."
                        + (object_id.split("_")[-1])
                        + " }}",
                    }
                )
            elif domain in ["switch", "number", "select", "binary_sensor"]:
                comp_config.update(
                    {
                        "command_topic": f"zigbee2mqtt/{name}/set",
                    }
                )
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

            # Clean up empty values (omit keys)
            comp_config = {k: v for k, v in comp_config.items() if v is not None or k == "name"}

            payload["cmps"][cmp_key] = comp_config

        # Published under the device topic
        topic = f"homeassistant/device/{mqtt_id}/config"
        print(json.dumps(payload)); client.publish(topic, json.dumps(payload), retain=True)
        print(f"Published device discovery for {name} to {topic}")

    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    publish_discovery()
