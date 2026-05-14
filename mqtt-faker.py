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
EXCLUDED_DOMAINS = {"device_tracker", "media_player"}
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
}


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
        allowed_device_ids = set()
        for entity in self.entities:
            platform = entity.get("platform")
            domain = entity.get("entity_id", "").split(".")[0]
            if domain not in EXCLUDED_DOMAINS and platform not in EXCLUDED_PLATFORMS:
                device_id = entity.get("device_id")
                if device_id:
                    allowed_device_ids.add(device_id)
        for device in self.devices:
            if device.get("disabled_by") is None and device["id"] in allowed_device_ids:
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
        except Exception as e:
            print(f"Error: {e}")


def start_emulation():
    reg = Registry()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.on_message = on_message
    for _, info in reg.get_target_devices().items():
        name = slugify(info.get("name_by_user") or info.get("name"))
        client.subscribe(f"{BASE_TOPIC}/{name}/+/set/#")
    client.loop_forever()


def get_comp_config(entity, reg, slug_name, name, domain, registry_obj_id):
    unique_id = entity.get("unique_id")
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

    if unit == "kWh" and dev_class == "power":
        dev_class = "energy"
    elif unit == "W" and dev_class == "energy":
        dev_class = "power"

    if domain == "sensor" and dev_class not in ALLOWED_SENSOR_CLASSES:
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
                "value_template": "{{ value_json."
                + registry_obj_id.split("_")[-1]
                + " }}"
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

    universal_players = {}
    for entity in reg.entities:
        e_id = entity.get("entity_id", "")
        if e_id.startswith("media_player."):
            universal_players[e_id.split(".")[1]] = slugify(e_id.split(".")[1])

    for device_id, info in reg.get_target_devices().items():
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

        # Inject mock children for universal players
        for mp_name, slug in universal_players.items():
            if slug_name in slug:
                for e_id, u_id, name in [
                    (f"switch.{mp_name}_power", f"{mp_name}_power", "Power"),
                    (
                        f"sensor.{mp_name}_volume_level",
                        f"{mp_name}_volume",
                        "Volume level",
                    ),
                    (f"switch.{mp_name}_mute_status", f"{mp_name}_mute", "Mute status"),
                ]:
                    entities.append(
                        {
                            "entity_id": e_id,
                            "unique_id": u_id,
                            "original_name": name,
                            "platform": "mqtt",
                        }
                    )

        for entity in entities:
            if entity.get("entity_id", "").startswith("media_player."):
                continue
            e_id = entity.get("entity_id")
            domain, registry_obj_id = e_id.split(".", 1)
            payload["cmps"][f"{domain}_{registry_obj_id}"] = get_comp_config(
                entity, reg, slug_name, name, domain, registry_obj_id
            )

        topic = f"homeassistant/device/{mqtt_id}/config"
        client.publish(topic, "", retain=True).wait_for_publish()
        client.publish(topic, json.dumps(payload), retain=True)

    if universal_players:
        with open(MOCK_YAML_PATH, "w") as f:
            f.write("media_player:\n")
            for name, slug in universal_players.items():
                f.write(f"""  - platform: universal
    name: {name}
    children:
      - switch.{slug}_power
    commands:
      turn_on:
        service: mqtt.publish
        data:
          topic: {BASE_TOPIC}/{slug}/power_power/set
          payload: 'ON'
      turn_off:
        service: mqtt.publish
        data:
          topic: {BASE_TOPIC}/{slug}/power_power/set
          payload: 'OFF'
      volume_set:
        service: mqtt.publish
        data:
          topic: {BASE_TOPIC}/{slug}/volume_volume/set
          payload_template: '{{{{ volume }}}}'
""")

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
