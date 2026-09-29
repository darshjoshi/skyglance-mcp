# SkyGlance — Claude Code plugin

Ask Claude what's flying above you. This plugin installs two things together:

- **The SkyGlance MCP server** — 24 tools for live aircraft positions, identity, routes,
  photographs, and your own sighting history
- **The `sky` skill** — teaches Claude to turn `elevation_deg: 42` into "look southeast,
  about 40° up", and to explain aircraft to someone who's never spotted one

## Install

```
/plugin marketplace add darshjoshi/skyglance-mcp
/plugin install skyglance@skyglance
```

Then ask:

```
What's flying over me right now?
What's that plane to the southwest?
Any A380s airborne?
Have I ever seen this one before?
```

## Requirements

[uv](https://docs.astral.sh/uv/getting-started/installation/) on your PATH — the server
runs via `uvx`, so there's no pip install and no virtualenv. uv's installation guide
covers every platform.

No API keys. Every data source is free.

## Set your location

Without it, you'll have to give coordinates on every question, and no sighting history is
recorded. Add to your MCP config:

```json
{ "env": { "SKYGLANCE_HOME_LAT": "40.71", "SKYGLANCE_HOME_LON": "-74.01" } }
```

With a home location set, SkyGlance polls your sky once a minute in the background and
records every pass to `~/.skyglance/sky.db` — locally, uploaded nowhere. That's what makes
"have I seen this plane before?" answerable. Disable with `SKYGLANCE_POLL=0`.

## Does this work where you live?

Free ADS-B depends on a volunteer having a receiver near you. Ask *"what's flying over
me?"* and look at the count below 10,000 ft:

- **Dozens** — dense coverage, everything here works well
- **A handful** — thin, you'll see cruise traffic but miss low aircraft
- **Zero** — either nothing flies over you, or nobody is receiving it

Dense Europe, US and East Asia get 90–95% of what a commercial feed shows. Oceans, Africa
and central Asia are effectively blind. This is geography, not a bug, and no amount of
stacking free sources fixes it.

## Links

- [Repository](https://github.com/darshjoshi/skyglance-mcp) ·
  [PyPI](https://pypi.org/project/skyglance/) ·
  [Data sources and their terms](https://github.com/darshjoshi/skyglance-mcp/blob/main/NOTICE.md)
- [skyglance-mac](https://github.com/darshjoshi/skyglance-mac) — the same data as a macOS
  menu bar app

MIT licensed. Aircraft data from adsb.lol (ODbL), adsb.fi, adsbdb, hexdb
and planespotters.net. Not for operational use.
