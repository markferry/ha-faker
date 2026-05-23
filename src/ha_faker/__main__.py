import argparse
import json
import os
import re
from collections.abc import Sequence

import paho.mqtt.client as mqtt

from . import __version__

__all__ = ["main"]

# Configuration
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
BASE_TOPIC = "fake"

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
    def __init__(self, device_registry_path, entity_registry_path, lovelace_path):
        self.devices = {
            d["id"]: d for d in load_json(device_registry_path)["data"]["devices"]
        }
        self.entities = load_json(entity_registry_path)["data"]["entities"]
        with open(lovelace_path, "r") as f:
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
            self.states[slug][f"{obj_id}_power"] = "OFF"
            self.states[slug][f"{obj_id}_mute"] = "false"
            self.states[slug][f"{obj_id}_volume"] = 0.5
            self.states[slug][f"{obj_id}_playback_state"] = "idle"
            self.states[slug][f"{obj_id}_media_content_type"] = "music"
            self.states[slug][f"{obj_id}_source"] = "hdmi1"
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

    def set_attribute(self, device_slug, entity_obj_id, attr, value):
        if device_slug not in self.states:
            self.states[device_slug] = {}

        if entity_obj_id not in self.states[device_slug]:
            parent_key = entity_obj_id
            self.states[device_slug][parent_key] = {}
        else:
            parent_key = entity_obj_id

        current = self.states[device_slug].get(parent_key)
        if not isinstance(current, dict):
            current = {"state": current}

        try:
            current[attr] = float(value)
        except (ValueError, TypeError):
            current[attr] = value

        self.states[device_slug][parent_key] = current
        return current


def on_message(client, userdata, msg):
    state_store = userdata["state_store"]
    # Topic format: fake/{device_slug}/{entity_id}/set/{attr?}
    parts = msg.topic.split("/")
    if len(parts) >= 4:
        device_slug = parts[1]
        entity_id = parts[2]
        payload = msg.payload.decode()

        if len(parts) > 4:
            # Attribute set: fake/{device_slug}/{entity_id}/set/{attr}
            attr = parts[4]
            new_state = state_store.set_attribute(device_slug, entity_id, attr, payload)
        else:
            # Basic state update: fake/{device_slug}/{entity_id}/set
            new_state = state_store.update(device_slug, entity_id, payload)

        # Publish the full state for that entity
        if isinstance(new_state, dict):
            # Check if this is a composite entity state (has a "state" key)
            # or a raw device dict (set_attribute returns the whole device state)
            if entity_id in new_state:
                publish_data = new_state[entity_id]
            elif "state" in new_state or len(new_state) <= 3:
                publish_data = new_state
            else:
                # set_attribute returned the full device state; find the right key
                publish_data = new_state.get(entity_id, new_state)
        else:
            publish_data = new_state

        client.publish(
            f"{BASE_TOPIC}/{device_slug}/{entity_id}",
            (
                json.dumps(publish_data)
                if isinstance(publish_data, dict)
                else str(publish_data)
            ),
            retain=True,
        )


def publish_initial_state(client, state_store, reg):
    devices, floating = reg.get_target_devices()
    for device_id, info in devices.items():
        slug = slugify(info.get("name_by_user") or info.get("name"))
        for entity in reg.device_entities.get(device_id, []):
            if not reg._is_valid_entity(entity):
                continue
            domain, obj_id = entity["entity_id"].split(".", 1)
            _publish_entity_state(client, state_store, slug, domain, obj_id)
    for entity in floating:
        if not reg._is_valid_entity(entity):
            continue
        domain, obj_id = entity["entity_id"].split(".", 1)
        slug = slugify(obj_id)
        _publish_entity_state(client, state_store, slug, domain, obj_id)


def _publish_entity_state(client, state_store, slug, domain, obj_id):
    if domain == "media_player":
        suffixes = ["power", "mute", "volume", "playback_state", "media_content_type"]
        for suffix in suffixes:
            key = f"{obj_id}_{suffix}"
            value = state_store.states.get(slug, {}).get(key)
            if value is not None:
                topic = f"{BASE_TOPIC}/{slug}/{key}"
                payload = json.dumps(value) if isinstance(value, dict) else str(value)
                client.publish(topic, payload, retain=True)
    else:
        value = state_store.states.get(slug, {}).get(obj_id)
        if value is not None:
            topic = f"{BASE_TOPIC}/{slug}/{obj_id}"
            payload = json.dumps(value) if isinstance(value, dict) else str(value)
            client.publish(topic, payload, retain=True)


def start_emulation(reference_dir, test_dir, base_topic):
    reg = Registry(
        os.path.join(reference_dir, ".storage/core.device_registry"),
        os.path.join(reference_dir, ".storage/core.entity_registry"),
        os.path.join(test_dir, "ui-lovelace.yaml"),
    )
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

    publish_initial_state(client, state_store, reg)

    client.loop_forever()


def get_comp_config(entity, reg, slug_name, name, domain, registry_obj_id):
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


def get_entity_to_slug(reg):
    devices, floating = reg.get_target_devices()
    entity_to_slug = {}
    for device_id, info in devices.items():
        name = info.get("name_by_user") or info.get("name")
        slug = slugify(name)
        for entity in reg.device_entities.get(device_id, []):
            if reg._is_valid_entity(entity):
                entity_to_slug[entity["entity_id"]] = slug
    for entity in floating:
        e_id = entity["entity_id"]
        domain, obj_id = e_id.split(".", 1)
        entity_to_slug[e_id] = slugify(obj_id)
    return entity_to_slug


def get_fake_entity_id(domain, slug, obj_id, suffix):
    base = f"{obj_id}_{suffix}"
    if base.startswith(slug):
        return f"{domain}.{base}"
    return f"{domain}.{slug}_{base}"


def write_fake_yaml(reg, entity_to_slug, output_path):
    media_players = [
        e for e in reg.entities if e["entity_id"].startswith("media_player.")
    ]
    if not media_players:
        return

    with open(output_path, "w") as f:
        # Template sensors that expose media_content_type as an attribute
        # so the universal media_player can pick it up via _child_attr.
        # (universal media_player uses _child_attr for media_content_type,
        #  which only reads from an active child entity's attributes,
        #  ignoring the attributes: override dict.)
        f.write("template:\n")
        f.write("  - sensor:\n")
        for mp in media_players:
            e_id = mp["entity_id"]
            obj_id = e_id.split(".")[1]
            slug = entity_to_slug.get(e_id, slugify(obj_id))
            playback_state_id = get_fake_entity_id(
                "select", slug, obj_id, "playback_state"
            )
            media_content_type_id = get_fake_entity_id(
                "select", slug, obj_id, "media_content_type"
            )

            # This sensor acts as the active child for the universal
            # media_player. Its state tracks the playback state (which
            # must be a valid media_player state like playing/paused/idle
            # so the universal media_player selects it as active child),
            # and it exposes media_content_type as an attribute (which
            # the universal media_player reads via _child_attr).
            f.write(f"    - name: {obj_id}_mp_child\n")
            f.write(f"      state: \"{{{{ states('{playback_state_id}') }}}}\"\n")
            f.write("      attributes:\n")
            f.write(
                f"        media_content_type: \"{{{{ states('{media_content_type_id}') }}}}\"\n"
            )
        f.write("\n")

        f.write("media_player:\n")
        for mp in media_players:
            e_id = mp["entity_id"]
            obj_id = e_id.split(".")[1]
            slug = entity_to_slug.get(e_id, slugify(obj_id))

            power_id = get_fake_entity_id("switch", slug, obj_id, "power")
            mute_id = get_fake_entity_id("switch", slug, obj_id, "mute")
            volume_id = get_fake_entity_id("number", slug, obj_id, "volume")
            state_id = get_fake_entity_id("select", slug, obj_id, "playback_state")
            media_content_type_id = get_fake_entity_id(
                "select", slug, obj_id, "media_content_type"
            )
            child_id = f"sensor.{obj_id}_mp_child"

            t_base = f"{BASE_TOPIC}/{slug}/{obj_id}"

            f.write("  - platform: universal\n")
            f.write(f"    name: {obj_id}\n")
            f.write("    children:\n")
            f.write(f"      - {child_id}\n")
            f.write("    state_template: >\n")
            f.write(f"      {{% if is_state('{power_id}', 'off') %}} off\n")
            f.write(
                f"      {{% else %}} {{{{ states('{state_id}') }}}} {{% endif %}}\n"
            )
            f.write("    attributes:\n")
            f.write(f"      is_volume_muted: {mute_id}|state\n")
            f.write(f"      volume_level: {volume_id}|state\n")
            f.write("    commands:\n")
            f.write(
                f'      turn_on: {{action: mqtt.publish, data: {{topic: {t_base}_power/set, payload: "ON"}}}}\n'
            )
            f.write(
                f'      turn_off: {{action: mqtt.publish, data: {{topic: {t_base}_power/set, payload: "OFF"}}}}\n'
            )
            f.write(
                f'      volume_set: {{action: mqtt.publish, data: {{topic: {t_base}_volume/set, payload: "{{{{ volume_level }}}}"}}}}\n'
            )
            f.write(
                f'      volume_mute: {{action: mqtt.publish, data: {{topic: {t_base}_mute/set, payload: "{{{{ is_volume_muted | lower }}}}"}}}}\n'
            )
            f.write(
                f'      media_play: {{action: mqtt.publish, data: {{topic: {t_base}_playback_state/set, payload: "playing"}}}}\n'
            )
            f.write(
                f'      media_pause: {{action: mqtt.publish, data: {{topic: {t_base}_playback_state/set, payload: "paused"}}}}\n'
            )
            f.write(
                f'      media_stop: {{action: mqtt.publish, data: {{topic: {t_base}_playback_state/set, payload: "idle"}}}}\n'
            )


def publish_discovery(reference_dir, test_dir, faker_yaml_name):
    reg = Registry(
        os.path.join(reference_dir, ".storage/core.device_registry"),
        os.path.join(reference_dir, ".storage/core.entity_registry"),
        os.path.join(test_dir, "ui-lovelace.yaml"),
    )
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
            "o": {"name": "MQTT Faker Script"},
            "cmps": {},
        }
        for entity in reg.device_entities.get(device_id, []):
            if not reg._is_valid_entity(entity):
                continue
            e_id = entity["entity_id"]
            domain, obj_id = e_id.split(".", 1)
            entity_to_slug[e_id] = slug

            if domain == "media_player":
                unique_base = f"{entity.get('device_id', 'no_dev')}_{entity.get('unique_id', obj_id)}"

                # Power switch
                payload["cmps"][f"switch_{obj_id}_power"] = {
                    "p": "switch",
                    "unique_id": f"{unique_base}_power",
                    "name": "Power",
                    "obj_id": f"{obj_id}_power",
                    "command_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_power/set",
                    "state_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_power",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "has_entity_name": True,
                }
                # Mute switch
                payload["cmps"][f"switch_{obj_id}_mute"] = {
                    "p": "switch",
                    "unique_id": f"{unique_base}_mute",
                    "name": "Mute",
                    "obj_id": f"{obj_id}_mute",
                    "command_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_mute/set",
                    "state_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_mute",
                    "payload_on": "true",
                    "payload_off": "false",
                    "has_entity_name": True,
                }
                # Volume number
                payload["cmps"][f"number_{obj_id}_volume"] = {
                    "p": "number",
                    "unique_id": f"{unique_base}_volume",
                    "name": "Volume",
                    "obj_id": f"{obj_id}_volume",
                    "command_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_volume/set",
                    "state_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_volume",
                    "min": 0,
                    "max": 1,
                    "step": 0.01,
                    "has_entity_name": True,
                }
                # Playback state select
                payload["cmps"][f"select_{obj_id}_playback_state"] = {
                    "p": "select",
                    "unique_id": f"{unique_base}_playback_state",
                    "name": "Playback State",
                    "obj_id": f"{obj_id}_playback_state",
                    "command_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_playback_state/set",
                    "state_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_playback_state",
                    "options": ["idle", "playing", "paused", "buffering", "on", "off"],
                    "has_entity_name": True,
                }
                # Content type select
                payload["cmps"][f"select_{obj_id}_media_content_type"] = {
                    "p": "select",
                    "unique_id": f"{unique_base}_media_content_type",
                    "name": "Content Type",
                    "obj_id": f"{obj_id}_media_content_type",
                    "command_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_media_content_type/set",
                    "state_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_media_content_type",
                    "options": ["music", "movie", "tvshow", "channel", "playlist"],
                    "has_entity_name": True,
                }
            else:
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
            "o": {"name": "MQTT Faker Script"},
            "cmps": {},
        }

        if domain == "media_player":
            unique_base = f"no_dev_{entity.get('unique_id', obj_id)}"

            payload["cmps"][f"switch_{obj_id}_power"] = {
                "p": "switch",
                "unique_id": f"{unique_base}_power",
                "name": "Power",
                "obj_id": f"{obj_id}_power",
                "command_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_power/set",
                "state_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_power",
                "payload_on": "ON",
                "payload_off": "OFF",
                "has_entity_name": True,
            }
            payload["cmps"][f"switch_{obj_id}_mute"] = {
                "p": "switch",
                "unique_id": f"{unique_base}_mute",
                "name": "Mute",
                "obj_id": f"{obj_id}_mute",
                "command_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_mute/set",
                "state_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_mute",
                "payload_on": "true",
                "payload_off": "false",
                "has_entity_name": True,
            }
            payload["cmps"][f"number_{obj_id}_volume"] = {
                "p": "number",
                "unique_id": f"{unique_base}_volume",
                "name": "Volume",
                "obj_id": f"{obj_id}_volume",
                "command_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_volume/set",
                "state_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_volume",
                "min": 0,
                "max": 1,
                "step": 0.01,
                "has_entity_name": True,
            }
            payload["cmps"][f"select_{obj_id}_playback_state"] = {
                "p": "select",
                "unique_id": f"{unique_base}_playback_state",
                "name": "Playback State",
                "obj_id": f"{obj_id}_playback_state",
                "command_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_playback_state/set",
                "state_topic": f"{BASE_TOPIC}/{slug}/{obj_id}_playback_state",
                "options": ["idle", "playing", "paused", "buffering", "on", "off"],
                "has_entity_name": True,
            }
        else:
            payload["cmps"][f"{domain}_{obj_id}"] = get_comp_config(
                entity, reg, slug, name, domain, obj_id
            )

        client.publish(
            f"homeassistant/device/dummy_{slug}/config",
            json.dumps(payload),
            retain=True,
        )

    faker_yaml_path = os.path.join(test_dir, faker_yaml_name)
    write_fake_yaml(reg, entity_to_slug, faker_yaml_path)

    client.loop_stop()
    client.disconnect()


def cleanup_registries(test_dir, force=False):
    storage_dir = os.path.join(test_dir, ".storage")

    if not force:
        print("This will wipe ALL devices, entities, and states in:", flush=True)
        print(f"  {storage_dir}", flush=True)
        answer = input("Are you sure? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.", flush=True)
            return

    # Files using the {"data": {"key": []}} structure
    storage_files = [
        ("core.device_registry", "devices"),
        ("core.entity_registry", "entities"),
    ]

    for filename, key in storage_files:
        path = os.path.join(storage_dir, filename)
        if not os.path.exists(path):
            continue
        try:
            data = load_json(path)
            original_count = len(data["data"][key])
            data["data"][key] = []
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            print(f"Removed all {original_count} items from {path}", flush=True)
        except Exception as e:
            print(f"Error cleaning {path}: {e}", flush=True)

    # Special handling for restore_state which uses {"data": []}
    restore_path = os.path.join(storage_dir, "core.restore_state")
    if os.path.exists(restore_path):
        try:
            data = load_json(restore_path)
            original_count = len(data["data"])
            data["data"] = []
            with open(restore_path, "w") as f:
                json.dump(data, f, indent=2)
            print(f"Removed all {original_count} items from {restore_path}", flush=True)
        except Exception as e:
            print(f"Error cleaning {restore_path}: {e}", flush=True)


def main(args: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="MQTT Faker — emulate real MQTT devices for a test HA instance"
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=__version__,
    )
    parser.add_argument(
        "-t",
        "--test-dir",
        default=os.getcwd(),
        help="Path to the test HA instance containing .storage and ui-lovelace.yaml (default: current directory)",
    )
    parser.add_argument(
        "-f",
        "--faker-yaml",
        default="faker.yaml",
        dest="faker_yaml",
        help="Filename for the faker YAML output, relative to --test-dir (default: faker.yaml)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("discover", "emulate", "fake"):
        p = subparsers.add_parser(name, help=f"{name.capitalize()} the test instance")
        p.add_argument(
            "reference_dir",
            help="Path to the reference HA instance containing .storage",
        )

    clean_parser = subparsers.add_parser(
        "clean", help="Wipe test instance registries and states"
    )
    clean_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )

    args = parser.parse_args()

    test_dir = args.test_dir
    faker_yaml = args.faker_yaml

    if args.command == "clean":
        cleanup_registries(test_dir, force=args.force)
    else:
        reference_dir = args.reference_dir
        if args.command == "discover":
            publish_discovery(reference_dir, test_dir, faker_yaml)
        elif args.command == "fake":
            reg = Registry(
                os.path.join(reference_dir, ".storage/core.device_registry"),
                os.path.join(reference_dir, ".storage/core.entity_registry"),
                os.path.join(test_dir, "ui-lovelace.yaml"),
            )
            entity_to_slug = get_entity_to_slug(reg)
            faker_yaml_path = os.path.join(test_dir, faker_yaml)
            write_fake_yaml(reg, entity_to_slug, faker_yaml_path)
        elif args.command == "emulate":
            start_emulation(reference_dir, test_dir, BASE_TOPIC)


if __name__ == "__main__":
    main()
