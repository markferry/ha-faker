import re
import json
import argparse
import os
import paho.mqtt.client as mqtt

# Configuration
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
DEVICE_REGISTRY_PATH = "dev-scripts/.storage/core.device_registry"
ENTITY_REGISTRY_PATH = "dev-scripts/.storage/core.entity_registry"
LOVELACE_PATH = "ui-lovelace.yaml"
MOCK_YAML_PATH = "mock.yaml"
BASE_TOPIC = "mock"

ALLOWED_SENSOR_CLASSES = {
    "date",
    "enum",
    "timestamp",
    "absolute_humidity",
    "apparent_power",
    "aqi",
    "area",
    "atmospheric_pressure",
    "battery",
    "blood_glucose_concentration",
    "carbon_monoxide",
    "carbon_dioxide",
    "conductivity",
    "current",
    "data_rate",
    "data_size",
    "distance",
    "duration",
    "energy",
    "energy_distance",
    "energy_storage",
    "frequency",
    "gas",
    "humidity",
    "illuminance",
    "irradiance",
    "moisture",
    "monetary",
    "nitrogen_dioxide",
    "nitrogen_monoxide",
    "nitrous_oxide",
    "ozone",
    "ph",
    "pm1",
    "pm10",
    "pm25",
    "pm4",
    "power_factor",
    "power",
    "precipitation",
    "precipitation_intensity",
    "pressure",
    "reactive_energy",
    "reactive_power",
    "signal_strength",
    "sound_pressure",
    "speed",
    "sulphur_dioxide",
    "temperature",
    "temperature_delta",
    "volatile_organic_compounds",
    "volatile_organic_compounds_parts",
    "voltage",
    "volume",
    "volume_storage",
    "volume_flow_rate",
    "water",
    "weight",
    "wind_direction",
    "wind_speed",
}

EXCLUDED_PLATFORMS = {"nmap_tracker", "unifi"}
EXCLUDED_DOMAINS = {"device_tracker", "camera"}
COMMAND_DOMAINS = {
    "switch",
    "number",
    "select",
    "button",
    "text",
    "lock",
    "light",
    "fan",
    "cover",
    "event",
    "media_player",
}


def slugify(text):
    return re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


class Registry:
    def __init__(self):
        self.devices = {
            d["id"]: d for d in load_json(DEVICE_REGISTRY_PATH)["data"]["devices"]
        }
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

    def _is_valid_entity(self, entity):
        platform = entity.get("platform")
        domain = entity.get("entity_id", "").split(".")[0]
        return (
            domain not in EXCLUDED_DOMAINS
            and platform not in EXCLUDED_PLATFORMS
            and not entity.get("disabled_by")
        )

    def get_target_devices(self):
        targets = {}
        for device_id, device in self.devices.items():
            if device.get("disabled_by") is not None:
                continue

            entities = self.device_entities.get(device_id, [])
            if any(self._is_valid_entity(e) for e in entities):
                targets[device_id] = device

        floating_entities = [
            e
            for e in self.entities
            if not e.get("device_id") and self._is_valid_entity(e)
        ]

        return targets, floating_entities


class StateStore:
    def __init__(self, reg):
        self.reg = reg
        self.states = {}
        devices, floating = reg.get_target_devices()

        for device_id, device in devices.items():
            slug = slugify(device.get("name_by_user") or device.get("name"))
            self.states[slug] = {}
            for entity in reg.device_entities.get(device_id, []):
                self._init_entity(slug, entity)

        for entity in floating:
            slug = slugify(entity["entity_id"].split(".", 1)[1])
            self.states[slug] = {}
            self._init_entity(slug, entity)

    def _init_entity(self, slug, entity):
        domain, obj_id = entity["entity_id"].split(".", 1)
        if domain == "climate":
            self.states[slug][obj_id] = {
                "local_temperature": 21.0,
                "current_heating_setpoint": 21.0,
                "system_mode": "heat",
            }
        elif domain == "light":
            self.states[slug][obj_id] = {"state": "OFF", "brightness": 128}
        elif domain == "media_player":
            self.states[slug][obj_id] = {"state": "idle", "volume_level": 0.5}
        elif domain == "switch":
            self.states[slug][obj_id] = "OFF"
        elif domain == "sensor":
            self.states[slug][obj_id] = 0.0

    def update(self, device_slug, obj_id, payload):
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            data = payload

        if device_slug not in self.states:
            self.states[device_slug] = {}

        if obj_id not in self.states[device_slug]:
            self.states[device_slug][obj_id] = data
            return data

        current = self.states[device_slug][obj_id]
        if isinstance(current, dict) and isinstance(data, dict):
            current.update(data)
        elif isinstance(current, dict) and not isinstance(data, dict):
            if "state" in current:
                current["state"] = data
            else:
                self.states[device_slug][obj_id] = data
        else:
            self.states[device_slug][obj_id] = data

        return self.states[device_slug][obj_id]

    def set_attribute(self, device_slug, obj_id, attr, value):
        if device_slug not in self.states:
            self.states[device_slug] = {}
        if obj_id not in self.states[device_slug]:
            self.states[device_slug][obj_id] = {}

        current = self.states[device_slug][obj_id]
        if isinstance(current, dict):
            try:
                current[attr] = float(value)
            except ValueError:
                current[attr] = value
        return current


def on_message(client, userdata, msg):
    state_store = userdata["state_store"]
    parts = msg.topic.split("/")
    if len(parts) >= 4:
        device_slug, obj_id = parts[1], parts[2]
        payload = msg.payload.decode()

        if len(parts) == 5 and parts[4]:
            new_state = state_store.set_attribute(
                device_slug, obj_id, parts[4], payload
            )
        else:
            new_state = state_store.update(device_slug, obj_id, payload)

        client.publish(
            f"{BASE_TOPIC}/{device_slug}/{obj_id}",
            json.dumps(new_state) if isinstance(new_state, dict) else new_state,
            retain=True,
        )


def start_emulation():
    reg = Registry()
    state_store = StateStore(reg)
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, userdata={"state_store": state_store}
    )
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.on_message = on_message

    devices, floating = reg.get_target_devices()
    for _, info in devices.items():
        name = slugify(info.get("name_by_user") or info.get("name"))
        client.subscribe(f"{BASE_TOPIC}/{name}/+/set/#")
    for entity in floating:
        name = slugify(entity["entity_id"].split(".", 1)[1])
        client.subscribe(f"{BASE_TOPIC}/{name}/+/set/#")

    client.loop_forever()


def get_comp_config(entity, reg, slug_name, name, domain, registry_obj_id):
    # Ensure a unique ID by combining device/entity info
    unique_id = f"{entity.get('device_id', 'no_dev')}_{entity.get('unique_id', registry_obj_id)}"
    entity_state_topic = f"{BASE_TOPIC}/{slug_name}/{registry_obj_id}"

    comp = {
        "p": domain,
        "unique_id": unique_id,
        "state_topic": entity_state_topic,
        "has_entity_name": True,
        "name": entity.get("original_name") or domain.capitalize(),
        "obj_id": registry_obj_id,
    }

    if entity.get("entity_id") in reg.entities_in_lovelace:
        comp["enabled_by_default"] = True

    if domain in COMMAND_DOMAINS:
        comp["command_topic"] = f"{BASE_TOPIC}/{slug_name}/{registry_obj_id}/set"

    dev_class = entity.get("original_device_class") or entity.get("device_class")
    unit = entity.get("unit_of_measurement")

    if unit == "kWh" and dev_class != "energy":
        dev_class = "energy"
    elif unit == "W" and dev_class != "power":
        dev_class = "power"
    elif unit == "Power Factor" or unit == "%":
        if dev_class == "power":
            dev_class = "power_factor"

    if dev_class == "power_factor" and unit == "Power Factor":
        unit = None

    if domain == "sensor" and dev_class not in ALLOWED_SENSOR_CLASSES:
        dev_class = None

    if dev_class == "power" and unit not in ["W", "kW", "mW"]:
        dev_class = None
    if dev_class == "energy" and unit not in ["Wh", "kWh", "MWh"]:
        dev_class = None

    if dev_class:
        comp["dev_cla"] = dev_class

    if entity.get("entity_category"):
        comp["entity_category"] = entity.get("entity_category")
    if entity.get("icon"):
        comp["icon"] = entity.get("icon")
    if unit:
        comp["unit_of_measurement"] = unit

    if domain == "light":
        comp.update(
            {
                "brightness": True,
                "brightness_scale": 254,
                "schema": "json",
                "supported_color_modes": ["brightness"],
            }
        )
    elif domain == "media_player":
        comp.update(
            {
                "schema": "json",
                "state_topic": entity_state_topic,
                "command_topic": f"{BASE_TOPIC}/{slug_name}/{registry_obj_id}/set",
            }
        )
    elif domain == "climate":
        comp.update(
            {
                "current_temperature_topic": entity_state_topic,
                "current_temperature_template": "{{ value_json.local_temperature }}",
                "temperature_command_topic": f"{BASE_TOPIC}/{slug_name}/{registry_obj_id}/set/current_heating_setpoint",
                "temperature_state_topic": entity_state_topic,
                "temperature_state_template": "{{ value_json.current_heating_setpoint }}",
                "modes": ["off", "heat"],
                "mode_command_topic": f"{BASE_TOPIC}/{slug_name}/{registry_obj_id}/set/system_mode",
                "mode_state_topic": entity_state_topic,
                "mode_state_template": "{{ value_json.system_mode }}",
            }
        )
    elif domain == "sensor":
        comp.update(
            {
                "value_template": (
                    "{{ value_json." + registry_obj_id.split("_")[-1] + " }}"
                    if "_" in registry_obj_id
                    else "{{ value_json.value }}"
                )
            }
        )
    elif domain == "switch":
        comp.update({"payload_on": "ON", "payload_off": "OFF"})
    elif domain == "select":
        comp.update(
            {"options": entity.get("capabilities", {}).get("options", ["Unknown"])}
        )
    elif domain == "event":
        comp.update(
            {
                "event_types": entity.get("capabilities", {}).get(
                    "event_types", ["click", "double_click", "long_press"]
                )
            }
        )

    return {k: v for k, v in comp.items() if v is not None or k == "name"}


def publish_discovery():
    reg = Registry()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()

    devices, floating = reg.get_target_devices()
    entity_to_slug = {}

    for device_id, info in devices.items():
        name = info.get("name_by_user") or info.get("name")
        slug = slugify(name)
        mqtt_id = next(
            (id[1] for id in info.get("identifiers", []) if id[0] in ["mqtt", "mpd"]),
            f"dummy_{slug}_{device_id[-6:]}",
        )

        payload = {
            "dev": {
                k: v
                for k, v in {
                    "ids": [mqtt_id],
                    "name": name,
                    "mf": info.get("manufacturer"),
                    "mdl": info.get("model"),
                    "sw": info.get("sw_version"),
                }.items()
                if v is not None
            },
            "o": {"name": "MQTT Mocker Script"},
            "cmps": {},
        }
        for entity in reg.device_entities.get(device_id, []):
            if not reg._is_valid_entity(entity):
                continue
            e_id = entity["entity_id"]
            domain, obj_id = e_id.split(".", 1)
            entity_to_slug[e_id] = slug
            payload["cmps"][f"{domain}_{obj_id}"] = get_comp_config(
                entity, reg, slug, name, domain, obj_id
            )

        if payload["cmps"]:
            topic = f"homeassistant/device/{mqtt_id}/config"
            client.publish(topic, json.dumps(payload), retain=True)

    for entity in floating:
        e_id = entity["entity_id"]
        domain, obj_id = e_id.split(".", 1)
        slug = slugify(obj_id)
        entity_to_slug[e_id] = slug
        name = obj_id.replace("_", " ").capitalize()

        payload = {
            "dev": {"ids": [f"dummy_{slug}"], "name": name},
            "o": {"name": "MQTT Mocker Script"},
            "cmps": {
                f"{domain}_{obj_id}": get_comp_config(
                    entity, reg, slug, name, domain, obj_id
                )
            },
        }
        client.publish(
            f"homeassistant/device/dummy_{slug}/config",
            json.dumps(payload),
            retain=True,
        )

    media_players = [
        e for e in reg.entities if e["entity_id"].startswith("media_player.")
    ]
    if media_players:
        with open(MOCK_YAML_PATH, "w") as f:
            f.write("media_player:\n")
            for mp in media_players:
                e_id = mp["entity_id"]
                name = e_id.split(".")[1]
                slug = entity_to_slug.get(e_id, slugify(name))
                f.write(
                    f'  - platform: universal\n    name: {name}\n    children:\n      - media_player.{name}\n    commands:\n      turn_on:\n        service: mqtt.publish\n        data:\n          topic: {BASE_TOPIC}/{slug}/{name}/set\n          payload: \'{{"state": "ON"}}\'\n      turn_off:\n        service: mqtt.publish\n        data:\n          topic: {BASE_TOPIC}/{slug}/{name}/set\n          payload: \'{{"state": "OFF"}}\'\n      volume_set:\n        service: mqtt.publish\n        data:\n          topic: {BASE_TOPIC}/{slug}/{name}/set\n          payload_template: \'{{"volume_level": {{{{ volume }}}}}}\'\n'
                )

    client.loop_stop()
    client.disconnect()


def cleanup_registries():
    print(
        "Cleaning up Home Assistant registries (WIPING ALL DEVICES, ENTITIES AND STATES)..."
    )

    # Files using the {"data": {"key": []}} structure
    storage_files = [
        (".storage/core.device_registry", "devices"),
        (".storage/core.entity_registry", "entities"),
    ]

    for path, key in storage_files:
        if not os.path.exists(path):
            continue
        try:
            data = load_json(path)
            original_count = len(data["data"][key])
            data["data"][key] = []  # Wipe everything
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            print(f"Removed all {original_count} items from {path}")
        except Exception as e:
            print(f"Error cleaning {path}: {e}")

    # Special handling for restore_state which uses {"data": []}
    restore_path = ".storage/core.restore_state"
    if os.path.exists(restore_path):
        try:
            data = load_json(restore_path)
            original_count = len(data["data"])
            data["data"] = []  # Wipe everything
            with open(restore_path, "w") as f:
                json.dump(data, f, indent=2)
            print(f"Removed all {original_count} items from {restore_path}")
        except Exception as e:
            print(f"Error cleaning {restore_path}: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="MQTT Mocker Script for Home Assistant"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Discover command
    subparsers.add_parser("discover", help="Publish MQTT discovery messages")

    # Emulate command
    subparsers.add_parser("emulate", help="Start MQTT state emulation loop")

    # Clean command
    subparsers.add_parser("clean", help="Wipe Home Assistant registries (DANGER)")

    # Mock command
    subparsers.add_parser("mock", help="Regenerate mock.yaml configuration")

    args = parser.parse_args()

    if args.command == "clean":
        cleanup_registries()
    elif args.command == "discover":
        publish_discovery()
    elif args.command == "mock":
        reg = Registry()
        entity_to_slug = get_entity_to_slug(reg)
        write_mock_yaml(reg, entity_to_slug)
    elif args.command == "emulate":
        start_emulation()
