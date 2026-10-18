"""Behaviour test for the room_lighting blueprint.

Boots a headless Home Assistant with the blueprint and one test room using
second-scale timeouts, drives fake sensors, and checks what the light does.

  pip install homeassistant
  python tests/test_room_lighting.py
"""
import asyncio
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

from homeassistant import bootstrap, runner
from homeassistant.core import ServiceCall

REPO = Path(__file__).resolve().parent.parent
BLUEPRINTS = REPO / "homeassistant" / "blueprints"
CFG = Path(tempfile.mkdtemp(prefix="ha-test-")) / "config"
CFG.mkdir(parents=True)
shutil.copytree(BLUEPRINTS, CFG / "blueprints")

(CFG / "configuration.yaml").write_text(textwrap.dedent("""
    homeassistant:
      time_zone: Europe/London
    input_boolean:
      auto_lighting:
        name: Automatic lighting
        initial: true
    automation: !include automations.yaml
"""))
# Timeouts in fractions of a minute: vacancy 3 s, stillness 6 s, spot 6 s.
(CFG / "automations.yaml").write_text(textwrap.dedent("""
    - id: test_room
      alias: Test room
      use_blueprint:
        path: distributed-sensor-system/room_lighting.yaml
        input:
          presence_sensor: binary_sensor.presence
          motion_sensor: binary_sensor.motion
          illuminance_sensor: sensor.lux
          doorway_sensors: [binary_sensor.door]
          lights: [light.room]
          lux_threshold: 40
          vacancy_minutes: 0.05
          stillness_minutes: 0.1
          entry_grace_seconds: 2
          spot_sensors: [binary_sensor.spot]
          spot_minutes: 0.1
          enable_entities: [input_boolean.auto_lighting]
    # A room with no spot or doorway inputs must also load and work.
    - id: plain_room
      alias: Plain room
      use_blueprint:
        path: distributed-sensor-system/room_lighting.yaml
        input:
          presence_sensor: binary_sensor.presence_2
          motion_sensor: binary_sensor.motion_2
          illuminance_sensor: sensor.lux
          lights: [light.room_2]
"""))

results = []


def check(name, cond):
    results.append((name, cond))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


async def main():
    hass = await bootstrap.async_setup_hass(runner.RuntimeConfig(config_dir=str(CFG)))

    async def turn_on(call: ServiceCall):
        for e in call.data["entity_id"]:
            hass.states.async_set(e, "on")

    async def turn_off(call: ServiceCall):
        for e in call.data["entity_id"]:
            hass.states.async_set(e, "off")

    hass.services.async_register("light", "turn_on", turn_on)
    hass.services.async_register("light", "turn_off", turn_off)

    def s(entity, state):
        hass.states.async_set(entity, state)

    def lux(value):
        hass.states.async_set("sensor.lux", str(value),
                              {"device_class": "illuminance", "unit_of_measurement": "lx"})

    for e in ("binary_sensor.presence", "binary_sensor.motion", "binary_sensor.door",
              "binary_sensor.spot", "light.room",
              "binary_sensor.presence_2", "binary_sensor.motion_2", "light.room_2"):
        s(e, "off")
    lux(5)
    await hass.async_start()
    await hass.async_block_till_done()

    def light():
        return hass.states.get("light.room").state

    async def wait(seconds=0.3):
        await asyncio.sleep(seconds)
        await hass.async_block_till_done()

    async def set_enabled(on):
        await hass.services.async_call("input_boolean", "turn_on" if on else "turn_off",
                                       {"entity_id": "input_boolean.auto_lighting"}, blocking=True)
        await wait(0.1)

    async def run_periodic():
        """Fire the automation as its 5-minute time_pattern trigger would."""
        entity = hass.data["automation"].get_entity("automation.test_room")
        await entity.async_trigger(
            {"trigger": {"platform": "time_pattern", "id": "periodic", "idx": "5"}})
        await wait()

    async def empty_room():
        for e in ("binary_sensor.presence", "binary_sensor.motion", "binary_sensor.door",
                  "binary_sensor.spot"):
            s(e, "off")
        await wait(7)          # longer than every timeout
        s("light.room", "off")
        await wait()

    # --- Room without spot or doorway inputs ---------------------------------
    check("plain room automation loaded",
          hass.states.get("automation.plain_room") is not None
          and hass.states.get("automation.plain_room").state == "on")
    s("binary_sensor.motion_2", "on"); await wait()
    check("plain room: motion in the dark switches lights on",
          hass.states.get("light.room_2").state == "on")

    # --- Doorway -------------------------------------------------------------
    s("binary_sensor.door", "on"); await wait(0.2)
    check("doorway into empty dark room: lights on at once", light() == "on")
    s("binary_sensor.door", "off"); await wait(2.8)
    check("nobody confirmed after entry grace: lights off", light() == "off")

    # --- Vacancy -------------------------------------------------------------
    s("binary_sensor.presence", "on"); await wait()
    check("presence in the dark: lights on", light() == "on")
    s("binary_sensor.presence", "off"); await wait(1.5)
    check("lights stay on during vacancy timeout", light() == "on")
    await wait(2.5)
    check("room empty for vacancy timeout: lights off", light() == "off")

    # --- Stillness -------------------------------------------------------------
    await empty_room()
    s("binary_sensor.presence", "on"); s("binary_sensor.motion", "on"); await wait()
    check("occupied: lights on", light() == "on")
    s("binary_sensor.motion", "off"); await wait(3)
    check("lights stay on before stillness timeout", light() == "on")
    await wait(3.5)
    check("present but still for stillness timeout: lights off", light() == "off")

    # Regression: radar dropout over a still person must not relight the room.
    s("binary_sensor.presence", "off"); await wait(0.5)
    s("binary_sensor.presence", "on"); await wait()
    check("radar dropout over still person: lights stay off", light() == "off")

    s("binary_sensor.motion", "on"); await wait()
    check("movement after stillness: lights on again", light() == "on")

    # Regression: controller reboot (unavailable -> on) is not an arrival.
    s("binary_sensor.motion", "off"); s("light.room", "off"); await wait()
    s("binary_sensor.presence", "unavailable"); s("binary_sensor.motion", "unavailable"); await wait()
    s("binary_sensor.presence", "on"); s("binary_sensor.motion", "on"); await wait()
    check("controller reboot: lights not switched on", light() == "off")

    # Real arrival detected by radar before the PIR still turns lights on.
    await empty_room()
    s("binary_sensor.presence", "on"); await wait()
    check("arrival seen only by radar after long vacancy: lights on", light() == "on")

    # --- Spot ------------------------------------------------------------------
    await empty_room()
    s("binary_sensor.presence", "on"); s("binary_sensor.motion", "on"); await wait()
    s("binary_sensor.spot", "on"); await wait(3)
    check("just sat in spot: lights still on", light() == "on")
    await wait(3.5)
    check("in spot for spot time: lights off", light() == "off")

    s("binary_sensor.motion", "off"); await wait(0.3)
    s("binary_sensor.motion", "on"); await wait()
    check("moving while in spot: lights stay off", light() == "off")

    s("binary_sensor.spot", "off"); await wait()
    check("got up from spot: lights back on", light() == "on")

    # Short visit to the spot changes nothing.
    s("binary_sensor.spot", "on"); await wait(1)
    s("binary_sensor.spot", "off"); await wait(3)
    check("brief visit to spot: lights stay on", light() == "on")

    # Lights switched on by hand while in the spot are left alone.
    await empty_room()
    s("binary_sensor.presence", "on"); s("binary_sensor.motion", "on"); await wait()
    s("binary_sensor.spot", "on"); await wait(3)
    s("light.room", "off"); await wait(0.2); s("light.room", "on")  # user switches on
    await wait(4)
    check("lights switched on by hand while in spot: left on", light() == "on")
    s("binary_sensor.spot", "off")

    # Lights switched on by hand while sitting still are left alone.
    await empty_room()
    s("binary_sensor.presence", "on"); s("binary_sensor.motion", "on"); await wait()
    s("binary_sensor.motion", "off"); await wait(3)
    s("light.room", "off"); await wait(0.2); s("light.room", "on")  # user switches on
    await wait(4)
    check("lights switched on by hand while still: left on", light() == "on")
    await run_periodic()
    check("periodic check also leaves hand-switched lights on", light() == "on")

    # Periodic check catches stillness whose timer was missed (e.g. HA restart).
    await empty_room()
    s("binary_sensor.presence", "on"); s("binary_sensor.motion", "on"); await wait()
    check("setup: lights on", light() == "on")
    await set_enabled(False)          # the "for" timer fires while disabled
    s("binary_sensor.motion", "off"); await wait(6.5)
    await set_enabled(True)
    check("missed stillness timer: lights still on", light() == "on")
    await run_periodic()
    check("periodic check after missed timer: lights off", light() == "off")

    # --- Darkness and enable switch -----------------------------------------
    await empty_room()
    lux(500)
    s("binary_sensor.presence", "on"); await wait()
    check("bright room: presence does not switch lights on", light() == "off")
    await empty_room()
    lux(5)
    await set_enabled(False)
    s("binary_sensor.presence", "on"); await wait()
    check("automation disabled: nothing happens", light() == "off")

    await hass.async_stop()
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


asyncio.run(main())
