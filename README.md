[![CI](https://github.com/markferry/ha-faker/actions/workflows/ci.yml/badge.svg)](https://github.com/markferry/ha-faker/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/markferry/ha-faker/branch/main/graph/badge.svg)](https://codecov.io/gh/markferry/ha-faker)
[![PyPI](https://img.shields.io/pypi/v/ha-faker.svg)](https://pypi.org/project/ha-faker)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# ha_faker

Home Assistant Device Faker

Emulate a live HA system for development and testing.

|   What   |                      Where                       |
| :------: | :----------------------------------------------: |
|  Source  |     <https://github.com/markferry/ha-faker>      |
| ~~PyPI~~ |            ~~`pip install ha-faker`~~            |
| Releases | <https://github.com/markferry/ha-faker/releases> |

## Operation

`ha-faker` lets you create a _test system_ from a _live system_ by emulating the real devices.

`ha-faker` looks at reference state data (a live `.storage`) and generates
[MQTT Discovery messages](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
for supported devices found there.

It publishes the discovery messages to a local MQTT broker.

**⚠️ DO NOT RUN THIS ON THE SAME HOST AS YOUR LIVE MQTT BROKER**. It will create quite a mess.

## Installation

```bash
git clone https://github.com/markferry/ha-faker.git
cd ha-faker
pip install .
```

## Usage

### Prerequisites

- You have a local MQTT broker running **WHICH IS NOT USED BY THE LIVE SYSTEM**
- You have made the `.storage` directory of your reference (live) system
  accessible locally (by `scp`, `sshfs`, Samba mount, or whatever other means)
- You have cloned your HA configuration files (with or without `.storage`) for development
- You agree that `.storage` was the worst thing to happen to Home Assistant

### Create Fake Devices

```bash
hass -c .  # run your test system
# wait until it's started
ha-faker discover ./path/to/live/config  # it must contain a .storage
```

Your test system parses the MQTT Discovery messages and populates its Devices and Entities.

### Emulate Device Behaviour

```bash
ha-faker emulate ./path/to/live/config  # it must contain a .storage
```

It's no good if the fake devices don't do anything.
This runs an MQTT client which responds to commands from the test system.

### Fakes for Media Players

Domains like `media_player` are not supported by the MQTT platform.

For these we generate fake devices as a
[YAML package](https://www.home-assistant.io/docs/configuration/packages/)
and include it in our test config.

```bash
ha-faker fake ./path/to/live/config
```

By default this will write a `faker.yaml` file to the local directory.

Include it in your test HA config:

```yaml
homeassistant:
  ...
  packages:
    package_faker: !include faker.yaml
```

Media Players are faked using a
[Universal Media Player](https://www.home-assistant.io/integrations/universal/)
backed by MQTT-discovered child devices.

There is very limited support for media controls and metadata.
You can test `media_players` by sending MQTT commands to the underlying devices.

e.g. :

```bash
DEVICE="my_player"; mosquitto_pub -t "fake/${DEVICE}/${DEVICE}_playback_state/set" -m "playing"
```

### Updating the Test Devices

If you add or remove devices from you reference system you must regenerate the
test system state data.

First stop your test system and **clear retained MQTT messages**.
(e.g. restart mosquitto or delete the message DB).

```bash
cd path/to/test/config
ha-faker clean  # clean all devices and entities from ./.storage
```

Then run `fake` and `discover` again.

Again, **⚠️ DO NOT RUN THIS ON YOUR LIVE SYSTEM**. It will create quite a void.
