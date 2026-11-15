# Cannalog for Home Assistant

Send your grow room's sensor data from Home Assistant to [Cannalog](https://cannalog.co.za), the grow journal. Temperature, humidity, VPD, pH, EC and more are logged to your Cannalog environments and reservoirs every few minutes and charted next to your own journal entries.

- **Push only.** Home Assistant sends data to Cannalog. Cannalog never connects into your home network and never stores a Home Assistant token.
- **Nothing lost.** Readings are averaged per interval and queued on disk, so an internet or Cannalog outage just delays them.
- **Any sensor.** Map any Home Assistant sensor to any Cannalog environment (tent, room) or reservoir. One Home Assistant can feed several tents, and one sensor can feed more than one.

## Install

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=cannalog&repository=cannalog-ha&category=integration)

1. In HACS, open **⋮ → Custom repositories**, add `https://github.com/cannalog/cannalog-ha` as an **Integration**, then download **Cannalog**. (The button above does this for you.)
2. Restart Home Assistant.

Manual install: copy `custom_components/cannalog` into your Home Assistant `config/custom_components/` folder and restart.

## Connect

[![Open your Home Assistant instance and start setting up Cannalog.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=cannalog)

1. In the Cannalog app open **Settings → Integrations → Home Assistant** and tap **Get pairing code**, then **Copy**.
2. In Home Assistant add the **Cannalog** integration (button above) and paste the code. Codes are valid for 15 minutes.
3. Open the integration's **Configure** menu:
   - **Map sensors to an environment or reservoir**: pick a tent or reservoir, then choose the sensor for each reading. Repeat for every tent.
   - **Upload settings**: averaging and upload interval (default 5 minutes).

## What can be sent

| Cannalog target | Readings |
|---|---|
| Environment | temperature, humidity, VPD, leaf temperature, dew point, CO₂, PPFD |
| Reservoir | pH, EC, water temperature, TDS, ORP, level |

Units are converted automatically (°F → °C, µS/cm → mS/cm).

## Diagnostics

The integration adds four diagnostic sensors: **Last upload**, **Queued readings**, **Readings sent** and **Mapped sensors**.

To disconnect, remove the integration in Home Assistant or tap **Disconnect** in the Cannalog app. Readings already sent are kept.
