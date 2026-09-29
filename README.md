<!-- mcp-name: io.github.darshjoshi/skyglance -->
<h1 align="center">SkyGlance</h1>

<p align="center">
  <strong>Ask Claude what's flying above you.</strong><br>
  Live positions, real aircraft identities, routes, photographs — and a memory of everything that's passed over.
</p>

<p align="center">
  <a href="https://pypi.org/project/skyglance/"><img src="https://img.shields.io/pypi/v/skyglance?color=0b7285&label=PyPI" alt="PyPI"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+"></a>
  <a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-Compatible-purple.svg" alt="MCP Compatible"></a>
</p>

---

## Quick Start

```bash
claude mcp add skyglance -- uvx --from skyglance skyglance
```

Then ask Claude: *"What's flying over me right now?"*

> Needs [uv](https://docs.astral.sh/uv/getting-started/installation/). Its installation guide covers every platform. No API keys — every data source here is free.

**Set your location** so you can stop repeating it, and so SkyGlance can build a sighting history:

```bash
claude mcp add skyglance \
  -e SKYGLANCE_HOME_LAT=40.71 -e SKYGLANCE_HOME_LON=-74.01 \
  -- uvx --from skyglance skyglance
```

### Install as a Claude Code plugin

Ships the server together with the `sky` skill, which teaches Claude how to read elevation angles and how to talk about aircraft to someone who has never spotted one.

```
/plugin marketplace add darshjoshi/skyglance-mcp
/plugin install skyglance@skyglance
```

### SkyGlance Spotter: the Claude directory edition

The repository root is also a plugin, **SkyGlance Spotter** (`skyglance-spotter`), built for Anthropic's Claude plugin directory. It runs the same server from this repository's source and differs in four ways:

- **Two tools are left out.** `military_aircraft` and `privacy_blocked_aircraft` aren't included, and `search_aircraft` has no military filter. Those features exist to follow aircraft whose operators or owners would rather not be followed.
- **History is opt-in.** Sighting history is recorded only after you switch on **Record sighting history** in the plugin's settings.
- **Settings instead of environment variables.** Home latitude and longitude are optional plugin settings.
- **History lives in the plugin's data folder.** The history database is kept in a folder Claude Code preserves across plugin updates and deletes when you uninstall. The Python environment `uv` builds on first start sits in the plugin folder and is rebuilt after each update.

```
/plugin marketplace add darshjoshi/skyglance-mcp
/plugin install skyglance-spotter@skyglance
```

It needs [uv](https://docs.astral.sh/uv/getting-started/installation/). On first start, `uv run --locked` installs the exact dependency versions in `uv.lock` (the MCP SDK, httpx and their dependencies) from PyPI, which takes a few seconds. SkyGlance Spotter works in Claude Code and Cowork. It runs locally, so it isn't available in claude.ai chat.

---

## What you can ask

```
"What's flying over me right now?"         → ranked by what you'd actually notice
"What's about to pass overhead?"           → but only 90 seconds ahead, honestly
"What is that plane?"                      → type, operator, route, photograph
"Where is flight UAL123?"                  → anywhere in the world
"Any A380s airborne?"                      → 44 of them, right now
"Any military aircraft near me?"           → and any emergencies, worldwide
"Have I ever seen this plane before?"      → your own history says
"What's the closest one ever?"             → your personal records
"Can I even see anything tonight?"         → cloud, visibility, daylight
```

---

## Why it's shaped this way

**"Over you" is an angle, not a distance.** A radius filter returns an airliner at 38,000 ft that is 19 km away — sitting 32° above the horizon, a speck near the treeline — and gives it the same weight as a helicopter at 800 ft, at 0.7°, invisible behind buildings. SkyGlance computes the elevation angle, the compass bearing to look, and the slant range, so an answer tells you *where to point your face*.

**It won't predict further than it can.** Straight-line extrapolation from track and ground speed is accurate to about 0.11 km at 30 seconds. At four minutes the median miss is 6 km and 77% of aircraft are more than 2 km off, because aircraft on approach turn constantly. So countdowns stop at 90 seconds and anything beyond is reported as "inbound" with no number attached.

**It knows when it can't see.** Free ADS-B coverage comes from volunteers with receivers on their roofs. Dense Europe, US and East Asia get 90–95% of what a commercial feed shows. Oceans, Africa and central Asia get near zero. When results are thin, SkyGlance says so rather than implying an empty sky.

**Your coordinates are rounded before they leave.** Feed queries are coarsened to two decimal places (~1.1 km) so no volunteer feed operator gets a rooftop-accurate log of where you are and when you're awake. Bearing and elevation are still computed locally from your exact position, so what you're told to look at stays precise. There's a test that fails if an uncoarsened coordinate ever reaches a feed.

---

## The 24 tools

| Group | Tools |
|---|---|
| **Overhead** | `whats_overhead` · `coming_overhead` · `nearest_aircraft` |
| **Identify** | `identify_aircraft` · `track_flight` · `track_history` · `airline_info` |
| **Airports** | `airport_activity` · `busiest_airports` |
| **Search the world** | `fleet_view` · `search_aircraft` · `global_stats` |
| **Spotting** | `military_aircraft` · `emergencies` · `find_by_type` · `interesting_nearby` · `privacy_blocked_aircraft` |
| **Context** | `viewing_conditions` · `feed_health` |
| **Your history** | `sighting_history` · `is_this_new` · `my_records` · `spotting_stats` · `poller_status` |

### How this compares to Flightradar24

SkyGlance is a question-answering tool, not a map, so the comparison is about *what you can find out* rather than what you can look at.

| | SkyGlance | FR24 |
|---|---|---|
| Live positions, type, registration, operator, photos | ✅ | Free |
| Squawk, vertical speed, aircraft age | ✅ | **Silver** |
| Filters by airline, type, altitude, speed | ✅ | **Silver** |
| Fleet view — every flight one airline has up | ✅ | **Business** |
| Airport activity, busiest airports | ✅ observed | **Business** |
| Flight path history | ✅ ~24 h | 90 days – 3 years |
| Global stats | ✅ | — |
| **Schedules, delays, gates, ETAs vs timetable** | ❌ | Free |
| **Ocean coverage (satellite ADS-B), MLAT, radar** | ❌ | All tiers |
| Aeronautical charts, oceanic tracks, ATC boundaries | ❌ | **Gold** |

**The honest summary:** for spotting and identification — *what's above me, what is that, where has it been, how many 747s are up* — SkyGlance covers what FR24 charges Silver and Business for, at $0. For travel logistics — *is my flight delayed, what gate* — it cannot compete, because that's licensed schedule data rather than ADS-B. And FR24's satellite network sees oceanic traffic that volunteer receivers simply cannot hear.

So: a strong replacement for the enthusiast half, not for the passenger half.

### What it deliberately can't do

Free ADS-B carries positions, not schedules. So there are no delays, no ETAs against a timetable, no gates or terminals, and no way to search "flights from JFK to London today" — route lookup only runs the other way, from a callsign you already have. `track_history` is a rolling ~24-hour window, not an archive.

These aren't on a roadmap. They're a different data product (Cirium, OAG, FlightAware), and buying one would end the no-API-keys story. The tools refuse these questions explicitly rather than estimating, and the bundled skill tells Claude to do the same.

### Sighting history

With `SKYGLANCE_HOME_LAT`/`LON` set, SkyGlance polls your sky once a minute in the background and records every pass. That's what makes the questions no live feed can answer possible: *have I seen this tail before, is this type new here, what's the lowest anything has ever come over.*

- Stored locally in SQLite at `~/.skyglance/sky.db`. Nothing is uploaded anywhere.
- Only one process polls, even with several Claude sessions open — a pid-stamped lock enforces it, so volunteer feeds don't get hit N times over.
- Turn it off with `SKYGLANCE_POLL=0`.
- Honest limit: passes are inferred from snapshots a minute apart, so a fast low pass between polls isn't recorded. `poller_status` tells you the interval.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SKYGLANCE_HOME_LAT` / `SKYGLANCE_HOME_LON` | unset | Your location. Enables history recording. |
| `SKYGLANCE_POLL` | `1` | Background recording. `1`/`true`/`yes`/`on` enable it; anything else disables it. |
| `SKYGLANCE_POLL_INTERVAL` | `60` | Seconds between polls (minimum 30). |
| `SKYGLANCE_HOME_DIR` | `~/.skyglance` | Where the database and lock live. |
| `SKYGLANCE_EDITION` | unset | `directory` gives the SkyGlance Spotter behaviour described above. The plugin sets it. |

---

## Where the data comes from

Seven free services, no API keys, $0/month. Full terms in [NOTICE.md](NOTICE.md).

| Layer | Services |
|---|---|
| **Positions** | [adsb.lol](https://adsb.lol) · [adsb.fi](https://adsb.fi) |
| **Identity** | [adsbdb.com](https://adsbdb.com) → [hexdb.io](https://hexdb.io) fallback |
| **Photos** | [planespotters.net](https://planespotters.net) |
| **Conditions** | [Open-Meteo](https://open-meteo.com) |

Both position feeds are queried in parallel and **merged**, not failed over. Measured over Newark on 2026-08-02, when a third feed (airplanes.live) was still in the mix: 52 aircraft from the best single source, **61 from the union — 17% more**. Merging buys coverage as well as redundancy. airplanes.live has since closed its API to anyone who hasn't contacted them first, so SkyGlance no longer queries it.

Each source sits behind a circuit breaker (three strikes, 30-second cooldown). These are volunteers paying for their own bandwidth, with no SLA and no obligation to anyone. If both fail, the last good snapshot is served flagged `stale` rather than an error — a 40-second-old aircraft beats a spinner.

**Not suitable for anything operational.** Dispatch, safety, ATC-adjacent — buy a commercial feed. The upstream terms say the same.

## Network access

SkyGlance only makes outbound requests to fetch public aviation data. It sends each service only what the lookup needs: a location **rounded to two decimal places (about 1.1 km)**, or an aircraft identifier, airline code or airport code. It never sends your conversation, files or any account details, and it uses no API keys.

| Host | Why |
|------|-----|
| `api.adsb.lol`, `globe.adsb.lol` (and `adsb.lol`) | Live positions near a location, worldwide lookups by type, callsign or registration, and ~24-hour flight paths |
| `opendata.adsb.fi` | Live positions near a location, merged with adsb.lol |
| `api.adsbdb.com` | Aircraft identity and flight routes |
| `hexdb.io` | Aircraft identity, when adsbdb has no record |
| `api.planespotters.net` | Aircraft photographs, with photographer credit |
| `api.open-meteo.com` | Cloud, visibility and daylight for viewing conditions |
| `pypi.org`, `files.pythonhosted.org` | Plugin only: `uv` installs the locked dependencies on first start |
| `github.com` | Plugin only, and only if no Python 3.10+ is installed: `uv` downloads a standalone Python |

Each service has its own terms. [NOTICE.md](NOTICE.md) lists them and explains how SkyGlance meets them.

Two development-only files in this repository also make network requests. Neither the server nor the plugin ever runs them:

- `tools/build_airports.py` downloads the public-domain OurAirports CSV from `davidmegginson.github.io` to regenerate `src/skyglance/airport_data.py`, and a maintainer runs it by hand.
- `.github/workflows/publish.yml` runs on GitHub Actions when a release is published. It downloads a pinned, checksum-verified `mcp-publisher` from the Model Context Protocol registry's GitHub releases, then publishes to PyPI and the MCP registry. It authenticates with the short-lived OIDC token GitHub Actions issues to that workflow. SkyGlance and its plugins never read, store or send any credential.

## Privacy Policy

This policy covers the SkyGlance MCP server, the `skyglance` plugin and the SkyGlance Spotter plugin.

**Data collection.** SkyGlance has no accounts, analytics, telemetry or crash reporting, and the author receives no data from it. It doesn't read Claude's memory, chat history or your files. It uses your location only when you give one in a question or set a home location.

**Usage.** A location is used to find aircraft near it. Before it leaves your computer it is rounded to about 1.1 km, so no data service receives your exact position. Where to look (direction and elevation) is worked out on your computer from the exact coordinates. Aircraft identifiers, airline codes and airport codes are sent only to the services that look them up.

**Storage.** Everything SkyGlance keeps stays on your computer, in a SQLite database: `~/.skyglance/sky.db` for the server, or the plugin's data folder for SkyGlance Spotter. It holds cached aircraft and airport lookups and, only while sighting history is on, a record of aircraft that passed over your home location. Your home location itself is stored where you configured it: in Claude Code's settings for the plugin, or in your MCP configuration for the server.

**Third-party sharing.** Nothing is shared with the author or sold to anyone. The services listed under [Network access](#network-access) receive the rounded location or identifier needed for each lookup, under their own privacy policies.

**Data retention.** Local data stays until you delete it. Delete `~/.skyglance`, or uninstall SkyGlance Spotter, which removes its data folder. Nothing is retained anywhere else by SkyGlance.

**Contact.** contact@darshjoshi.com, or [open an issue](https://github.com/darshjoshi/skyglance-mcp/issues).

---

## Related

- [skyglance-mac](https://github.com/darshjoshi/skyglance-mac) — the same data as a macOS menu bar app, with the research this is built on ([FREE-STACK.md](https://github.com/darshjoshi/skyglance-mac/blob/main/docs/FREE-STACK.md), [OVERHEAD-DETECTION.md](https://github.com/darshjoshi/skyglance-mac/blob/main/docs/OVERHEAD-DETECTION.md), [COVERAGE-AND-COST.md](https://github.com/darshjoshi/skyglance-mac/blob/main/docs/COVERAGE-AND-COST.md))

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest                    # geometry parity, merge, breakers, privacy, history
```

The geometry tests assert exact-digit parity with `reference/overhead.mjs` from the Mac app, which passes its own 11 known-answer cases. If the port ever drifts, those fail first.

MIT licensed. Not affiliated with any airline, airport, or air navigation service provider.
