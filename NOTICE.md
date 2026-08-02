# Data sources and their terms

The [MIT licence](LICENSE) covers the source code in this repository only.

SkyGlance displays data from third-party services. Each is free, requires no API key, and
carries its own terms. **These obligations travel with the package** — if you fork it,
repackage it, or build on it, they come with you.

| Source | Terms |
|---|---|
| [adsb.lol](https://www.adsb.lol) | **[ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/)** — attribution required |
| [airplanes.live](https://airplanes.live) | Non-commercial use |
| [adsb.fi](https://adsb.fi) | Open data |
| [adsbdb.com](https://www.adsbdb.com) | Free, attribution appreciated |
| [hexdb.io](https://hexdb.io) | Free |
| [planespotters.net](https://www.planespotters.net) | Descriptive User-Agent required, **photographer credit required** |
| [Open-Meteo](https://open-meteo.com) | Free, non-commercial |

## How this package meets them

- **Attribution is returned in the data, not just documented here.** Every tool that
  returns positions includes an `attribution` field listing the contributing sources, and
  every photo carries its `photographer` with `credit_required: true`. Passing those
  through when you display the data is the condition of use.
- **ODbL share-alike** applies to derived *databases*. This package queries live data and
  does not redistribute a database, so the code stays MIT. The attribution requirement is
  not optional and is why `attribution` is in every response.
- **A descriptive User-Agent** identifying this project and linking to its repository is
  sent on every request, as planespotters requires and as courtesy to the rest.
- **OpenSky is deliberately not used**, despite contributing roughly 10% additional
  coverage in testing. Its licence grants use "solely for the purpose of non-profit
  research and non-profit education" and separately requires a written licence for
  operational use. A package that anyone can install and run cannot honour that.
- **Rate limiting and circuit breakers are on by default.** These are volunteers paying
  for their own bandwidth. Requests are spaced to at least the published per-source
  minimum, a source that fails three times in a row is left alone for 30 seconds, and
  background polling defaults to once a minute with a lock that stops several Claude
  sessions from multiplying the load.

## Not for operational use

Free ADS-B is community infrastructure with no SLA and real coverage gaps — oceans,
Africa, central Asia and rural areas are effectively blind. Do not use this for dispatch,
flight safety, or anything where a wrong or missing aircraft could cause harm. The
upstream terms say the same thing.
