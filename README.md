# proxy_service — a headless tunnel for the host and for `order_process`

Three containers, one job. `proxy_service` runs [mihomo](https://github.com/MetaCubeX/mihomo)
from a static config and serves a mixed HTTP/SOCKS proxy. `proxy_refresher` keeps its
node list fresh from the provider account, because the subscription URL rotates its
host every few hours and only the credential is durable. `proxy_rotator` moves the
tunnel to a different country every quarter hour, so no single exit IP faces the
consumer's upstream long enough to be worth blocking. A consumer that needs one unit of
work to keep one exit names a country in its proxy username and stays there, whatever the
rotator does meanwhile.

There is no GUI, no desktop, no VNC and no watchdog. The tunnel exists from the moment
the core starts, and a `docker compose up -d` on a host that has only `.env` is the
whole deployment.

```text
Windows 127.0.0.1:1082            order_process containers
                 \                 /  (http://proxy_service:1082; http://US:<id>@… pins to US)
                  proxy_service ──── mixed port 1082 ─┬─ IN-USER,<country> ── that country's group
                        ▲   ▲                         └─ MATCH ── PROXY ─┬─ JP ── the 日本 node in play ── internet
                        │   │ PUT /proxies/PROXY                         ├─ US ── the 美国 node in play
                        │   └──── proxy_rotator ── every 15 min, least recently used; sooner if it dies
                        │            (shares this container's network namespace, so the
                        │             control API stays on loopback; answers GET /current
                        │             on proxy_service:1083 — which country to pin to)
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
| `PROXY_ROTATION_SECONDS` | `900` | How long one country serves before the rotator moves on. Minimum 60 |
| `PROXY_ROTATION_JITTER_SECONDS` | `90` | Spread each switch over ±this, so the changeover is not a clean quarter hour. At most half the window; `0` disables |
| `PROXY_CURRENT_PORT` | `1083` | The rotator's `GET /current`, inside the core's namespace: `proxy_service:1083` on `order_process`. Not published on the host |
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
| `make rotate` | Switch country now — the answer to a block you have actually seen |
| `make pin C=US` | Hold one country (stops the rotator); `make up` resumes rotation |
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

A consumer whose unit of work opens many connections should pin it to one exit instead
of riding the rotation — see "Pinning one unit of work to one exit" below.

## Rotating between countries

The nodes the refresher publishes are grouped by country in `mihomo/config.yaml` — one
`url-test` group each for `JP US TW KR DE IN` — and `PROXY` is a plain selector over
them. `proxy_rotator` moves that selector every `PROXY_ROTATION_SECONDS`, picking the
**least recently used country that still has a live node**. With every country healthy
that is a round-robin; a country skipped for being dead keeps its place in the queue
rather than forfeiting its turn, which matters because several countries here are a
single node.

Between switches it keeps looking. Every 15 s it re-reads the core's health, and the
moment the country in play has no live node left — its last node failed its check, or
a list change took it away — it switches at once instead of letting a dead country
serve out its window. A single-node country like `DE` has no other defence; a country
with several nodes fails over inside itself first. The seconds after a node-list change,
when every node is rebuilt with its checks still pending, do not read as dead, so a
refresh never trips this.

Inside the window the node is mihomo's job: the country's `url-test` group picks its
fastest live node, stays on it until it dies, then moves to the next live node of the
same country without waiting for the next switch. Staying put is `tolerance` in
`mihomo/config.yaml`, set high enough that no live node can displace the one in play —
a pinned consumer needs one exit for its whole run, not the fastest exit each minute.

Two limits worth knowing:

- **A health check proves a node reaches Google, not that Google still accepts it.**
  Rotation is prophylaxis on a clock; it cannot detect a block. `make rotate` is the
  answer to one you have observed, and `make pin C=US` parks the tunnel while you look.
- **Which countries exist is `mihomo/config.yaml`'s list; which are populated is
  `PROXY_REGION_FILTER`'s.** A group whose filter matches no published node holds only
  `REJECT`, reads as `0/0` in `make status`, and is skipped — so a country can be added
  to or dropped from `.env` without touching the core's config, as long as its group is
  declared there. Adding a country the config does not know needs three lines: its
  group, its name in `PROXY`'s `proxies:`, and its `IN-USER` rule.

The rotator reaches the core's control API by sharing the core's network namespace
(`network_mode: service:proxy_service`), which is why `external-controller` can stay
bound to `127.0.0.1` and out of reach of everything else on `order_process`. The cost
is one operational rule: **always `make up`, never `docker restart proxy_service`** — a
container in another's namespace goes stale when that one is recreated.

## Pinning one unit of work to one exit

A consumer that opens many connections for one piece of work — an agy run opens dozens
over its lifetime — must not have the country change halfway through: the work would
leave from two countries, and its upstream sees one session that moved continent. A
switch only affects connections opened after it, so the fix is for each unit of work to
choose its exit once, at the start, and keep it.

1. Ask which country is current: `GET http://proxy_service:1083/current` answers
   `{"country": "US", "via": "美国03-×0.3"}`. Add `?request=<id>` and the rotator logs
   `current: <id> -> US via 美国03-×0.3`, the proxy-side end of a trace.
2. Name that country in the proxy username for the whole unit of work:
   `HTTPS_PROXY=http://US:<request id>@proxy_service:1082`. Every connection carrying
   that name goes to the `US` group, whatever `PROXY` does meanwhile. Nothing here reads
   the password, so a request id costs nothing there and ties a packet capture to a trace.
3. Start the work. The node inside the country stays until it dies, so in practice the
   exit is one IP for the whole run.

Rotation is then gradual: work in flight keeps its country, new work starts on the new
one. Untagged traffic rotates exactly as before, so nothing changes for a consumer that
does not pin, and the two sides can be deployed in either order — a tag sent to a core
without the rules simply rotates.

`make status` prints `pins:` with every country a tag reaches. A country missing there
has no `- IN-USER,<name>,<name>` line above `MATCH`, and the rotator says so in its log
at start.

## Reading `make status`

```text
NAME              STATUS
proxy_service     Up 3 hours (healthy)
proxy_refresher   Up 3 hours (healthy)
proxy_rotator     Up 3 hours
nodes:     24, published 12 min ago
country:   US via 美国03-×0.3
live:      JP 8/8  US 7/7  TW 6/6  KR 1/1  DE 1/1  IN 0/1
pins:      JP US TW KR DE IN
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
- **`live: JP 8/8 …`** — how many of each country's nodes answered their last health
  check. A country at `0/n` is skipped rather than served, and left within seconds if
  it was the one serving; every country at `0` is why a `verdict: DOWN` happened, and
  the rotator says so once per retry.
- **`pins: JP US …`** — the countries a proxy username reaches. A country under
  `(no IN-USER rule for …)` has no line in `mihomo/config.yaml`, so a consumer naming it
  rotates with everyone else and cannot tell.

## What each failure looks like

| Symptom | What happened | What to do |
| --- | --- | --- |
| `refresh failed: … answered HTTP 401` | Wrong `PANEL_EMAIL`/`PANEL_PASSWORD`, or the account lapsed | Fix `.env`, `make refresh` |
| `refresh failed: … answered HTTP 403` | The panel's Cloudflare rejected our browser signature (`error code: 1010`) | Change `PANEL_USER_AGENT` in `refresher/refresh.py`; any value but Python's default has passed so far |
| `refresh failed: … carries no proxies: list` | The subscription host answered a dialect we do not speak | Re-run the §"Re-verify" procedure in `CONTEXT.md` |
| `refresh failed: 0 node(s) match the region filter` | `PROXY_REGION_FILTER` matches nothing the provider currently sells | `make nodes` (old list), widen the filter, `make refresh` |
| `proxy_service` unhealthy, `verdict: DOWN` | Every published node is dead, or `PROXY_PORT` disagrees with `mixed-port` | `make logs`; the `live:` line in `make status` says which countries still have anything |
| `rotation held: no country has a live node (…)` | Nothing anywhere answered its health check. The rotator holds rather than parking a window on a dead country | Expected for the first minute after a start; past that, the provider or this host's egress is down |
| `DE 0/1 has no live node left after 212s; switching now` | The country in play lost its last node mid-window; the rotator moved on without waiting for the switch | Nothing. If one country does this every time it is chosen, its node is flapping: drop it from `PROXY_REGION_FILTER` |
| `rotation held: … Connection refused`, or `cannot read the core mid-window (…)` | The core is not listening yet, or was recreated under the rotator | Expected at boot; otherwise `make up` |
| `country: unreadable - is the rotator up, or pinned?` | `make pin` stopped the rotator, or it is crash-looping | `make up` to resume rotation, `make logs` if it is not that |
| `no IN-USER rule pins X`, or `pins: … (no IN-USER rule for X)` | A member of `PROXY` has no tag rule, so a consumer that names it in its proxy username is not pinned | Add `- IN-USER,X,X` above `MATCH` in `mihomo/config.yaml`, then `make down` and `make up` so the core re-reads it |
| `refusing to start: cannot listen on port 1083` | Something else in the core's network namespace holds `PROXY_CURRENT_PORT` | Change it in `.env`, `make up`; tell the consumer |
| `[CacheFile] can't open cache file … read-only file system` | Expected. The rootfs is read-only and nothing in that cache matters here (no stored selection, no fake-ip) | Ignore |

A failed refresh cycle of any cause leaves the previous node list serving and logs how
stale it now is. A dead panel costs staleness, never an outage.

## Design decisions worth knowing before you change something

- **Three decisions, three owners.** *Which nodes exist* is the refresher's
  (`PROXY_REGION_FILTER`, a quota and policy decision). *Which country serves now* is
  the rotator's, on a clock and on health. *Which node inside that country* is mihomo's,
  on latency. None of them can be moved into another without giving something up: the
  refresher has no health data, and the core has no clock.
- **The rotator only ever writes one thing.** `PUT /proxies/PROXY`. It never touches
  the node list, the config, or the country groups, so the worst a broken rotator can
  do is leave the tunnel on the country it was already serving. `/current` is a read.
- **A consumer pins by name, and the name is the group's.** The core routes a proxy
  username to the group of the same name, and `/current` answers with the name the core
  reports, so `mihomo/config.yaml` is the only place countries are named.
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
  TUN's job and TUN was abandoned (`CONTEXT.md` §2). Every endpoint the current
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

Removing a country from the filter needs no change in `mihomo/config.yaml`: its group
goes to `0/0` and the rotator stops choosing it. *Adding* one the config has never
heard of does — copy a `- {<<: *country, …}` line into `proxy-groups`, add its name to
`PROXY`'s `proxies:`, and add its `- IN-USER,<name>,<name>` line above `MATCH`.

## Updating the core

Change the pinned image in `compose.yaml` (tag **and** digest), then `make up`. The
config keys used here are ordinary mihomo ones; `docker compose run --rm --no-deps
proxy_service -d /config -t` validates the config against a new image before you
commit to it.

`CONTEXT.md` carries the engineering notes: what the vendor's API contract is, how it
was measured, and how to re-derive it after a vendor update.
