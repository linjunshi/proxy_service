# proxy_service — a headless tunnel for the host and for `order_process`

Two containers, one job. `proxy_service` runs [mihomo](https://github.com/MetaCubeX/mihomo)
from a static config and serves a mixed HTTP/SOCKS proxy. `proxy_refresher` keeps its
node list fresh from the provider account, because the subscription URL rotates its
host every few hours and only the credential is durable.

There is no GUI, no desktop, no VNC and no watchdog. The tunnel exists from the moment
the core starts, and a `docker compose up -d` on a host that has only `.env` is the
whole deployment.

```text
Windows 127.0.0.1:1082            order_process containers
                 \                 /  (http://proxy_service:1082)
                  proxy_service ──── mixed port 1082 ── selected node ── internet
                        ▲
                        │ reads /config/providers/nodes.yaml (read-only)
                        │
                  proxy_refresher ── every 30 min ── panel API + rotating host
```

The refresher sits on `order_process` with ordinary egress and **not** behind the
proxy. That is deliberate: the component that fetches the node list must not depend on
the tunnel those nodes provide, or a dead node list becomes unrecoverable.

## Configure

Copy `.env.example` to `.env` (`make` does it for you) and fill in the account:

| Variable | Default | What it is |
| --- | --- | --- |
| `PANEL_BASE_URL` | `https://api.wmsxwd-3.men` | The panel the operator logs into |
| `PANEL_EMAIL` | — | Required |
| `PANEL_PASSWORD` | — | Required |
| `PROXY_PORT` | `1082` | The mixed port: `127.0.0.1:1082` on the host, `proxy_service:1082` on `order_process` |
| `PROXY_REGION_FILTER` | `台湾\|台灣\|臺灣\|Taiwan\|TW` | Regex over the node name; only matching nodes are published |
| `PROXY_REGION_EXCLUDE` | unset | Optional regex; `×[2-9]` skips expensive traffic multipliers |
| `PROXY_MIN_NODES` | `1` | Below this the refresher refuses to publish and keeps the list it has |
| `REFRESH_INTERVAL_SECONDS` | `1800` | Between successful cycles |
| `RETRY_INTERVAL_SECONDS` | `300` | After a failed cycle |
| `MIHOMO_API_SECRET` | empty | The core's control API, which is bound to loopback inside its own container |

Two numbers must agree and only one of them is in `.env`: `PROXY_PORT` here and
`mixed-port` in `mihomo/config.yaml`. mihomo cannot read an environment variable, so
change both together. The mistake is not silent — the healthcheck fetches *through*
`PROXY_PORT` from inside the core's own container, so a disagreement turns
`proxy_service` unhealthy within 30 s.

`order_process` must already exist (`docker network inspect order_process`).

## Run

```powershell
make build     # builds the refresher; its unit tests run inside the build
make up
make status
```

Without `make`: `docker compose build`, `docker compose up -d`.

| Target | What it does |
| --- | --- |
| `make build` | Builds the refresher image. A failing unit test fails the build. |
| `make up` / `make down` | Start / stop the stack |
| `make deploy` | `git pull`, build, up |
| `make refresh` | Restart the refresher, which runs a cycle immediately |
| `make logs` | Follow both containers |
| `make status` | The verdict — see below |
| `make nodes` | The node names currently published, for checking a region filter |

## Consume it

```powershell
$env:HTTPS_PROXY = "http://127.0.0.1:1082"
$env:NO_PROXY    = "localhost,127.0.0.1,::1"
```

```yaml
environment:
  HTTP_PROXY: http://proxy_service:1082
  HTTPS_PROXY: http://proxy_service:1082
  NO_PROXY: localhost,127.0.0.1,proxy_service
```

The consuming service declares the same external `order_process` network in its own
Compose project. Nothing about the consumer's lifetime is coupled to this stack's.

## Reading `make status`

```text
NAME              STATUS
proxy_service     Up 3 hours (healthy)
proxy_refresher   Up 3 hours (healthy)
nodes:     6, published 12 min ago
selected:  台湾03-×1-客户端
exit:      1.34.56.78 through the proxy in 0.31s, 203.0.113.9 direct
verdict:   UP - HTTPS leaves through the tunnel
```

- **`verdict: UP`** — the two exit IPs differ, so HTTPS really leaves through the node.
- **`verdict: DOWN`** — the proxy refused the connection. This is the *intended*
  failure: the core is stopped, or no node is usable. Nothing leaked.
- **`verdict: LEAKING`** — the proxy answered from this host's own address. It should
  be impossible (`empty-fallback: REJECT` in `mihomo/config.yaml` exists precisely to
  prevent it) and it means a request went out untunneled. Treat as an incident.
- **`nodes: … published N min ago`** — a number climbing past `REFRESH_INTERVAL_SECONDS`
  means refresh cycles are failing; `make logs` says why, in one line per cycle.

## What each failure looks like

| Symptom | What happened | What to do |
| --- | --- | --- |
| `refresh failed: … answered HTTP 401` | Wrong `PANEL_EMAIL`/`PANEL_PASSWORD`, or the account lapsed | Fix `.env`, `make refresh` |
| `refresh failed: … answered HTTP 403` | The panel's Cloudflare rejected our browser signature (`error code: 1010`) | Change `PANEL_USER_AGENT` in `refresher/refresh.py`; any value but Python's default has passed so far |
| `refresh failed: … carries no proxies: list` | The subscription host answered a dialect we do not speak | Re-run the §"Re-verify" procedure in `CONTEXT.md` |
| `refresh failed: 0 node(s) match the region filter` | `PROXY_REGION_FILTER` matches nothing the provider currently sells | `make nodes` (old list), widen the filter, `make refresh` |
| `proxy_service` unhealthy, `verdict: DOWN` | Every published node is dead, or `PROXY_PORT` disagrees with `mixed-port` | `make logs`; widen the filter if the whole region is down |
| `[CacheFile] can't open cache file … read-only file system` | Expected. The rootfs is read-only and nothing in that cache matters here (no stored selection, no fake-ip) | Ignore |

A failed refresh cycle of any cause leaves the previous node list serving and logs how
stale it now is. A dead panel costs staleness, never an outage.

## Design decisions worth knowing before you change something

- **Region policy is the refresher's; failover is mihomo's.** The refresher publishes
  only nodes matching `PROXY_REGION_FILTER`; mihomo runs one `url-test` group over
  whatever is in the file and picks the fastest that can reach Google. Adding a region
  is one regex edit and `make refresh`, and cross-region failover then happens on
  latency automatically.
- **The health-check URL is a Google endpoint on purpose.** The one consumer talks to
  Google, so a node that cannot is useless however fast it answers something else.
- **Fail closed.** There is no `DIRECT` in the rules and `empty-fallback: REJECT` on
  the group. With the core down the port refuses connections; with no usable node the
  group refuses them. Both are refusals, never a silent direct exit from the host.
- **The node list is replaced atomically and never shrunk blindly.** A cycle that
  cannot log in, fetch, parse, or that yields fewer than `PROXY_MIN_NODES` nodes leaves
  the file untouched.
- **The core's image is upstream's, unmodified and pinned by digest.** Everything this
  deployment decides lives in `mihomo/config.yaml` and in `.env`.
- **No kill switch.** Traffic that ignores `HTTPS_PROXY` is not captured; that was
  TUN's job and TUN was abandoned (`CONTEXT.md` §4). Every endpoint the current
  consumer touches honours the variable.

## Changing the region filter

```powershell
make nodes       # what is published today
# Edit PROXY_REGION_FILTER in .env, then:
make up          # recreates the refresher with the new value
make refresh     # runs a cycle now instead of in 30 minutes
make nodes       # confirm what got published
```

To see everything the provider currently sells, filter included, run the
re-verification snippet in `CONTEXT.md` §5.2 — it executes inside the refresher, so it
needs nothing installed and no credential copied anywhere.

If the new filter matches nothing, the refresher refuses to publish and the old list
keeps serving — so a typo costs a log line, not the tunnel.

## Updating the core

Change the pinned image in `compose.yaml` (tag **and** digest), then `make up`. The
config keys used here are ordinary mihomo ones; `docker compose run --rm --no-deps
proxy_service -d /config -t` validates the config against a new image before you
commit to it.

`CONTEXT.md` carries the engineering notes: what the vendor's API contract is, how it
was measured, and how to re-derive it after a vendor update.
