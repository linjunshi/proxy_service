# CONTEXT — engineering notes

What this file is: the discoveries and decisions behind this repo, for anyone (human or
agent) picking the work up. `README.md` is the operator runbook and stays authoritative
for *how to run it*; this file records *why it is shaped this way*, *what the vendor's
API contract is*, and *how to re-derive that contract* when the vendor ships a new
build. Dates are absolute.

## 1. Identity and scope

- A **generic, independent proxy service**: mihomo (upstream's image, unmodified) plus
  two small sidecars — one that keeps its node list fresh from the provider account,
  one that moves the live country on a clock. It offers its
  tunnel to the Docker host and to the `order_process` network. It is not a Gemini
  project: consumers depend on it, it depends on no consumer, and consumer-specific
  wiring must never land here.
- One way to consume it: **explicit proxy** — `http://proxy_service:1082` from
  `order_process`, `http://127.0.0.1:1082` from the Windows host.
- Production host: Windows, Docker Desktop (WSL2 backend). `order_process` is an
  external bridge owned by the application stack; it is also this stack's default
  route, so both containers reach the internet through it.
- First consumer: `gemini-local-service` (`/Users/Edward/Projects/gemini-local-service`),
  which sets `HTTPS_PROXY=http://proxy_service:1082` and changes nothing else.

## 2. Why the vendor GUI was retired (2026-09-03)

The repo used to package `wmsxwd` 1.42.3 — a Flutter GUI over a mihomo core — and run
it under a virtual X display with `Xtigervnc`, `xfce4-session`, `gnome-keyring`,
`websockify` and a bash watchdog that pressed the GUI's Start button with `xdotool`.

The application has no headless mode and no auto-connect. On every start it launched a
*temporary* core that served the mixed port but created no TUN device and displayed
`未连接`; only a click spawned the real core. `container/auto-connect` simulated that
click, judged the result by comparing exit IPs, and on a bad verdict ran `recover-app`,
which `pkill -9`ed the core and restarted the GUI.

**The incident.** On 2026-09-02 the Gemini service returned a burst of 502s under
concurrent load:

- 14:36:51 UTC — every `agy` subprocess got `proxyconnect tcp: … connect: connection
  refused`. Nothing was listening on the mixed port: the core was between a `pkill -9`
  and its restart.
- 14:37:00 UTC — the proxy answered again, reached Google, and Google replied
  `FAILED_PRECONDITION (code 400): User location is not supported for the API use.`
  That is the temporary core in `未连接` mode, exiting from somewhere unselected.

Two hours later `make status` still reported `tun: NO device` with the container at
260 % CPU. In that state the watchdog pressed Start every cycle and killed the core on
every third failed press. **The watchdog was the outage generator**, and the thing it
was nursing was a GUI that could not be driven any other way.

**TUN was abandoned, not fixed.** Earlier notes in this file presented TUN mode as the
direction of travel (a container with `CAP_NET_ADMIN`, `/dev/net/tun`, file
capabilities on the core, and consumers joining its network namespace). That plan is
dead. TUN was never observed up on the Windows host, and every endpoint `agy` touches
honours `HTTPS_PROXY`, so the tunnel bought the one consumer nothing measurable. Do not
resurrect it from the git history without a consumer that demonstrably needs it — see
§8 for the trigger that would.

What replaced it: mihomo run directly from a static config, plus one sidecar that keeps
the node list fresh using the account credentials. No desktop, no display, no simulated
clicks, no watchdog. The tunnel exists from the moment the process starts.

## 3. What the vendor package is (wmsxwd 1.42.3, `.deb` inspected 2026-09-01/03)

Established by extracting the package and reading the binaries' string tables — no
source is available. The `.deb` and its checksum stay in `vendor/` because §5 is the
procedure that reads them.

- Layout: `/opt/wmsxwd/wmsxwd` (Flutter runner), `/opt/wmsxwd/lib/libapp.so` (Dart AOT,
  the whole GUI), `/opt/wmsxwd/lib/AtlasCore_amd64` (36 MB static Go binary).
- **The core is mihomo** (`github.com/metacubex/mihomo` 1.19.24, Go 1.24.13, built
  `with_gvisor`). It contains `resource.FileVehicle`, `resource.HTTPVehicle`,
  `provider.HealthCheck`, an `fsnotify` watcher and `[Provider] %s not updated for a
  long time, force refresh` — i.e. a file-backed proxy provider with health checks and
  hot reload is supported by the same core the vendor ships. This stack runs upstream's
  own image of the same minor line instead.
- **The GUI is a multi-panel storefront** (FlClash lineage): `panelType` values
  `v2board`, `xboard`, `sspanel`, `ppanel`; the whole order/plan/ticket endpoint set;
  an AES-encrypted list of candidate API hosts that it race-tests
  (`[ApiEndpointManager]`, `[AppConfigService]`). Five bootstrap URLs are baked in; one
  (`https://wtv18.oss-cn-shanghai.aliyuncs.com/v/v3421.json`) was live and returned
  exactly the bundled `assets/data/c.dat`. The decryption key was not recovered **and is
  not needed** — the panel base URL is simply the site the operator logs into.
- It fetches the node list with `User-Agent: clash.meta/1.0` and feeds mihomo on stdin.

Only the node-list fetch matters to us. Everything else the GUI added is a storefront.

## 4. What the refresher depends on — the contract

Each item is what `refresher/refresh.py` would break on, with the evidence that
established it. Measured 2026-09-03 against the live account with `panel_probe.py` and
`sub_format_probe.py`.

| # | The contract | Evidence |
| --- | --- | --- |
| 1 | Panel `https://api.wmsxwd-3.men` is a v2board/xboard backend | `panelType` strings in `libapp.so`; the endpoints answer |
| 2 | `POST api/v1/passport/auth/login` with form `email`+`password` returns `data.auth_data` | Headless login works; no captcha challenged |
| 3 | `GET api/v1/user/getSubscribe` with `Authorization: <auth_data>` returns `data.subscribe_url` | Also returns `token`, `device_limit`, `alive_ip`, `expired_at`, `plan_id`, `reset_day`, `transfer_enable`, `u`, `d` |
| 4 | **The subscribe URL's host rotates; the token does not** | Two runs hours apart returned the same 62-char token on `http://139.162.80.238:3690/w/wm` and on `https://wm1855.999631.xyz/w/wm`. Two logins 60 s apart returned an identical URL. |
| 5 | The panel's own `/api/v1/client/subscribe?token=` returns 404, as does a `/clash` path suffix | The rotating host is the only subscription source |
| 6 | The subscription endpoint always answers `Content-Type: text/html`; the body depends on how you ask | The dialect table below |
| 7 | Node names are Chinese and carry a traffic multiplier, e.g. `台湾06-×1` | Region filtering and `PROXY_REGION_EXCLUDE` both key off the name |
| 8 | **Every panel request must send a User-Agent.** The panel is behind Cloudflare, which answers `403` with body `error code: 1010` — a banned browser signature — to Python's default `Python-urllib/3.x`. Any other value passes, including an empty one. | Measured 2026-09-03: `curl` 200, `Python-urllib/3.12` 403, `clash.meta/1.0` 200 |

Item 4 is the load-bearing fact: **the credential is the only durable secret**, which is
why mihomo cannot simply point an `http` provider at a saved URL, and why the refresher
exists at all.

### The dialect table (measured 2026-09-03 with a YAML parser, from the refresher itself)

| asked as | body | proxies | groups | Taiwan |
| --- | --- | ---: | ---: | ---: |
| `flag=clash.meta` + UA `clash.meta/1.0` | Clash YAML | **37** | 11 | **6** |
| `flag=meta`, any UA | Clash YAML | 37 | 11 | 6 |
| no flag + UA `clash.meta/1.0` | Clash YAML | 37 | 11 | 6 |
| `flag=clash` + UA `ClashforWindows/*` | Clash YAML | **10** | 11 | **1** |
| `flag=mihomo` + UA `mihomo/1.19.x` | base64 URI list | — | — | — |
| no flag + an ordinary UA | base64 URI list | — | — | — |

Either the flag or a meta User-Agent is enough to select the full list; the refresher
sends both, because the two rows at the bottom are what a naive client gets. **mihomo
sends its own User-Agent**, so a `proxy-providers: {type: http}` aimed straight at this
URL would receive the base64 list — unparseable here — and `flag=clash` would yield one
Taiwan node instead of six.

An earlier run of this table recorded "48 proxies, 11 groups" for the meta row. That 48
was a line count that included the group entries; parsed, the same body holds 37
proxies. Count with a parser, not with `grep`.

Account facts from the same run: `device_limit` 3, `alive_ip` 1, plan expiry
2026-12-06, 20.9 GiB of 100 GiB used, `profile-update-interval` 24 hours. The refresher
runs every 30 minutes anyway, because the host rotates faster than a day and a cycle
costs one login and one download.

### Where this can break silently

The dialect pin is the one that fails without a symptom: if the row yielding the
largest Clash list moves to a different flag or User-Agent, the refresher keeps
publishing — just a smaller or wrong-format list. Two guards catch it:
`select()` refuses a body with no `proxies:` list, and `PROXY_MIN_NODES` refuses a
shrunken one. Both leave the previous list serving and say so in the log.

TLS verification stays on for both the panel and the rotating host. If the vendor ever
serves the subscription from a host with an untrusted certificate, every cycle will
fail loudly rather than accept an injected node list — a node list is code as far as
this stack is concerned, and an attacker who supplies one sees all the traffic.

## 5. Re-verify after a vendor update

### 5.1 From a new package

```sh
# 1. Extract the new package beside the old one.
mkdir -p /tmp/wm && cd /tmp/wm
ar x /path/to/wmsxwd1-<new version>-Linux.deb && tar -xf data.tar.zst

# 2. The endpoint set. Diff this against §4.
strings -n 5 opt/wmsxwd/lib/libapp.so \
  | grep -oE "(api/v1/[a-zA-Z0-9_/]*|/auth/[a-z]+|/user/[a-zA-Z0-9_]+)" | sort -u

# 3. The panel families and the service names that implement them.
strings -n 5 opt/wmsxwd/lib/libapp.so | grep -E "^(v2board|xboard|sspanel|ppanel)$"
strings -n 8 opt/wmsxwd/lib/libapp.so | grep -E "^\[[A-Za-z]+\]" | sort -u

# 4. The bootstrap host list, in case the panel domain moved.
strings -n 8 opt/wmsxwd/lib/libapp.so | grep -oE "https?://[a-zA-Z0-9._/?=&%-]+" | sort -u

# 5. The core's version, to confirm the config keys we use still exist.
strings -n 6 opt/wmsxwd/lib/AtlasCore_amd64 | grep -E "^1\.[0-9]+\.[0-9]+$"
```

### 5.2 Against the live account — this is what actually matters

Run it inside the refresher, which already holds the credentials and PyYAML. Nothing to
install, no secret to copy anywhere, and it exercises the same code path the sidecar
uses. Self-contained on purpose: the scratch scripts an earlier investigation used are
not in this repo.

```sh
docker compose exec -T refresher python - <<'PY'
import json, os, re, urllib.parse, urllib.request, yaml

base = os.environ["PANEL_BASE_URL"].rstrip("/")
form = urllib.parse.urlencode({"email": os.environ["PANEL_EMAIL"],
                               "password": os.environ["PANEL_PASSWORD"]}).encode()
# A User-Agent is mandatory -- contract item 8.
head = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "clash.meta/1.0"}
auth = json.load(urllib.request.urlopen(
    urllib.request.Request(base + "/api/v1/passport/auth/login", data=form, headers=head), timeout=20
))["data"]["auth_data"]

sub = json.load(urllib.request.urlopen(urllib.request.Request(
    base + "/api/v1/user/getSubscribe",
    headers={"Authorization": auth, "User-Agent": "clash.meta/1.0"}), timeout=20
))["data"]["subscribe_url"]
print("subscription host:", sub.split("?")[0])   # run twice, hours apart: it rotates

region = re.compile(os.environ["PROXY_REGION_FILTER"])
for flag, ua in [("clash.meta", "clash.meta/1.0"), ("meta", None), (None, "clash.meta/1.0"),
                 ("clash", "ClashforWindows/0.20.39"), ("mihomo", "mihomo/1.19.30"), (None, None)]:
    body = urllib.request.urlopen(urllib.request.Request(
        sub + (f"&flag={flag}" if flag else ""),
        headers={"User-Agent": ua or "curl/8.7.1"}), timeout=30).read()
    doc = yaml.safe_load(body)
    if isinstance(doc, dict) and isinstance(doc.get("proxies"), list):
        kept = [p for p in doc["proxies"] if region.search(p["name"])]
        print(f"  {flag or '-':<12} {ua or 'curl/8.7.1':<26} "
              f"{len(doc['proxies']):3d} proxies, {len(kept):3d} match the filter")
    else:
        print(f"  {flag or '-':<12} {ua or 'curl/8.7.1':<26} not Clash YAML")
PY
```

**If the combination yielding the largest Clash proxy list is no longer
`flag=clash.meta` + `clash.meta/1.0`, change `SUBSCRIPTION_FLAG` and
`SUBSCRIPTION_USER_AGENT` in `refresher/refresh.py` to match**, and record the new
table in §4. That breakage is the silent one: the refresher would keep publishing, just
a smaller or wrong-format list.

## 6. Country rotation (2026-09-04)

**The problem.** A long-lived exit IP is a blockable one. The consumer's upstream
(Gemini) will refuse an address it has decided it does not like, and no health check
can see that: a blocked node still fetches `generate_204` in 80 ms. So the defence has
to be prophylactic — never serve one country long enough to be worth the block.

**Why it cannot live in the core.** mihomo has no time-window primitive. `url-test`
ranks on latency, `fallback` on health, `load-balance` spreads *per connection* and
`sticky-sessions` keys on destination. None of them expresses "stay here for fifteen
minutes". The decision has to come from outside.

**Why it is not the refresher.** The obvious cheap version — have the refresher publish
only the active country's nodes and rotate on its own clock — needs no new container
and no config change, and was rejected for one reason: the refresher has no health
data. It would switch onto a country whose nodes are all down and serve `REJECT` for
the whole window. With this account that is not hypothetical: 韩国, 德国 and 印度 are a
**single node each** (measured 2026-09-04: 日本 8, 美国 7, 台湾 6, 韩国 1, 德国 1,
印度 1 after the filter), so half the countries have no internal redundancy at all.
Republishing the provider every window would also reset every health-check history and
drop in-flight connections.

**The shape built instead.** One `url-test` group per country in `mihomo/config.yaml`,
each with a `filter` over the shared provider; `PROXY` becomes a `select` over those
groups; `proxy_rotator` reads the core's own health and `PUT`s the selector. The three
decisions end up with three owners — the refresher chooses which nodes exist, the
rotator which country serves now, mihomo which node inside it — and none of them can
be folded into another without losing something the others cannot supply.

**Why the rotator shares the core's network namespace.** `external-controller` is
`127.0.0.1:9090` *inside* the core's container, and §"Where this can break silently"
already treats `MIHOMO_API_SECRET` as defence in depth rather than a boundary. Binding
the API to `0.0.0.0` so a sibling container could reach it would promote that secret to
*the* boundary, on a network the consumer stack shares. `network_mode:
service:proxy_service` reaches loopback without widening anything. There is no TUN, so
sharing the namespace does not put the rotator behind the proxy.

Two consequences to know: a container in another's namespace goes stale when that one
is recreated (always `make up`), and it cannot declare its own `networks:` or `ports:`.
It depends on the core `service_started`, **not** `service_healthy` — the core is
unhealthy exactly when the country in play is dead, which is when the rotator is needed
most.

### Measured against the live core, 2026-09-04, mihomo v1.19.30

1. **Provider nodes are not in `/proxies`.** That endpoint returns the groups and the
   core's built-in outbounds (`DIRECT`, `REJECT`, `COMPATIBLE`, `PASS`, …) and nothing
   else; the nodes behind a `proxy-providers` entry — and their health — live under
   **`/providers/proxies`**, keyed by provider name. A rotator that looks up a group's
   `all` members in the `/proxies` map finds nothing and reads every country as dead.
   This cost a debugging cycle; the two bodies are why `survey()` takes two arguments.
2. **A group's own `alive` flag is optimistic and useless here.** With every one of the
   24 published nodes reporting `alive: false` and `delay: 0`, all six country groups
   still reported `alive: true`. Liveness is read from each node's last recorded delay
   instead — `history[-1].delay > 0`, or the same under `extra[<testUrl>]`, which is
   where a check made under a group's own URL is mirrored.
3. **YAML merge keys work.** `- &country {...}` plus `- {<<: *country, name: US,
   filter: 美国}` parses correctly in the pinned image, with the explicit keys winning
   over the merged ones. That is what keeps "how a node is tested" one decision instead
   of six copies.
4. **`PUT /proxies/PROXY` with `{"name": "US"}` is accepted** and takes effect at once;
   `store-selected: false` means the choice does not survive a core restart, and the
   rotator re-asserts within seconds of one.
5. **A `filter` matching no published node yields a group holding only `REJECT`.** It
   does not stop the core and does not fall back to DIRECT, because `empty-fallback:
   REJECT` is set on every country group. The rotator reads that as `0/0` and skips it,
   which is what lets `.env` add and drop countries without touching the core's config.

### Leaving a dead country mid-window (2026-09-07)

**The problem.** The rotator chose a country and slept out the whole window. When that
country's last node died mid-window -- and 韩国, 德国 and 印度 are one node each -- the
selector stayed on it until the next switch, and the core kept dialing the dead node:
`url-test` with nothing alive parks on its first member, so every connection got the
`200` then `EOF` of §9. Up to a quarter of an hour of that, from a health check the
core had already failed.

**What was built.** The sleep became a watch (`watch()` in `refresher/rotate.py`):
every `WATCH_SECONDS` (15) the rotator re-reads the same two bodies `survey()` always
read, and ends the window the moment the country the core is serving is *dead*. The
next cycle is the ordinary one, so the LRU order and the "hold when nothing is live"
rule are unchanged; the early switch just arrives sooner. Detection is bounded by the
core, not the rotator: the health check runs once a minute with a 5 s timeout, so a
dead country is left within about 65 s worst case plus one look.

**Dead is narrower than ineligible, on purpose.** A real change to the node list
rebuilds every node with `alive: true` and no history (§9), so for a few seconds every
country reads `0/n`. Had the watch treated that as dead, every refresh that changed the
list would have forced a switch. So `Country` now carries `untested` beside `live`, and
`is_dead` is "no live node and none still awaiting its first check" -- an unknown
country is a reason to wait, not to leave. `is_eligible` (a destination) is still
`live > 0`. A member the providers do not report at all has no check coming and counts
as failed, and a country whose members all left the list (`0/0`) is dead: nothing
there will ever answer.

Not changed: a core that cannot be read mid-window is not a dead country, so the watch
logs and goes on rather than ending the window on a transient. A pinned consumer whose
country dies is still broken until its next unit of work, which is the exception §9
accepted.

### A crash found while verifying this (fixed 2026-09-04)

The refresher had been dying and restarting rather than retrying. `urllib` converts
only the errors raised by the *request* into `URLError`; one raised while reading the
**response** — `http.client.RemoteDisconnected` when the subscription host closes the
connection mid-answer — propagates raw, past `_get`'s `except urllib.error.URLError`,
and out of `main`. `restart: unless-stopped` hid it: the container came back and the
next cycle usually worked. It defeated the documented intent that "a dead panel costs
staleness, never an outage", and re-logged in on every crash. Both sidecars now catch
`(OSError, http.client.HTTPException)`, and `TransportTest` in each test file pins it.

## 7. What the core was measured to do (2026-09-03, local, mihomo v1.19.30)

These four behaviours shaped `mihomo/config.yaml` and `compose.yaml`. Re-check them
when the pinned image changes.

1. **An empty or missing provider file does NOT stop the core — it routes DIRECT.**
   mihomo logs `initial proxy provider subscription error: …`, keeps running, and the
   group falls back to `COMPATIBLE`, which is a *Direct* outbound: every request then
   left from the host's own address and the container still reported healthy. This is
   the 2026-09-02 incident's second failure mode reappearing in a new costume.
   **`empty-fallback: REJECT` on the group is the fix**, verified: with an empty
   provider the group holds only `REJECT`, connections are refused, and the healthcheck
   turns the container unhealthy. Do not remove that line.
2. **`os.replace` onto the provider file hot-reloads within ~10 s** —
   `[Provider] subscription's content update`, and the group's selection follows. This
   is what makes the refresher's atomic publish safe.
3. **The image's `wget` is busybox and cannot CONNECT through a proxy.** It ignores
   `https_proxy` (only `http_proxy` and `ftp_proxy` are read) and, for an `https://`
   URL with a proxy set, sends an absolute-URI `GET` instead of `CONNECT`. So the
   container healthcheck fetches `http://www.gstatic.com/generate_204` through the
   proxy — still end to end, still proof the tunnel carries traffic — and `make status`
   covers the HTTPS/CONNECT path from the host with `curl`.
4. **`read_only: true` costs one warning and nothing else.**
   `[CacheFile] can't open cache file: open /config/cache.db: read-only file system` is
   expected: that cache holds a stored selection (`store-selected: false` here) and
   fake-ip mappings (DNS disabled here), so nothing needed is lost.

Two smaller ones, for whoever changes the volumes: the config lives at `/config`
rather than `/root/.config/mihomo` so that the core can run as uid 10000 (root's home
is `0700` in that image), and the `proxy_nodes` volume takes its ownership from
whichever container mounts it first — Docker's copy-up chowns the volume root to match
the image directory while the volume is still empty, so the refresher (uid 10001) can
write it regardless of start order.

## 8. Deferred, with the trigger that would revive it

- **Weighted rotation.** Every country gets an equal window, which is what maximises
  IP spread but ignores that 德国 and 印度 are one slow node each while 日本 is eight.
  The seam is `Rotation.next`. The trigger would be a consumer that measurably suffers
  during the far-country windows — and the cheaper answer first is to drop that country
  from `PROXY_REGION_FILTER`, not to invent a weighting language.
- **Rotating the node inside a country.** `url-test` serves one node per country and,
  since §9, stays on it until it dies, so a full cycle shows the upstream six IPs rather
  than twenty-four. If six is not enough spread,
  the options are a `load-balance` group per country (spreads per connection, at the
  cost of using slow nodes and changing source IP mid-session) or having the rotator
  choose the node as well as the country (maximum spread, but it reimplements
  `url-test`'s failover and a node dying mid-window would stay dead until the switch).
- **Reacting to a block instead of pre-empting it.** The rotator cannot see a block; a
  consumer that gets `FAILED_PRECONDITION` can. A generic "switch now" trigger the
  consumer could pull is buildable — `make rotate` is that, by hand — but consumer-specific
  wiring must not land here (§1). The rotator's read-only `/current` (§9) is that plain
  endpoint, and a "switch now" verb would sit beside it.
- **A kill switch.** Nothing here stops traffic that ignores `HTTPS_PROXY`. That was
  TUN's job. Every endpoint `agy` touches honours the variable, so the exposure equals
  what the pre-GUI deployment already had. Revive only if a consumer appears that does
  not honour it — and note that it costs `CAP_NET_ADMIN` and a namespace join, i.e.
  everything §2 just removed.
- **Token caching.** The refresher logs in once per cycle. If the provider ever
  rate-limits logins, cache `auth_data` and re-login only on 401.
- **Traffic awareness.** `subscription-userinfo` carries usage and quota on every
  fetch. Logging it per cycle, and alerting near the 100 GiB cap, is cheap and
  currently unbuilt.

## 9. Pinning a unit of work to its exit (2026-09-05)

**The problem.** Consumer requests failed when the rotator switched country mid-request,
and worked when the consumer started just after a switch. What the v1.19.30 source and
this host's core log show:

- `PUT /proxies/PROXY` closes nothing. `Selector.Set` assigns a name and returns
  (`hub/route/proxies.go`, `adapter/outboundgroup/selector.go`), so a connection keeps
  its node to the end and only later connections see the new country.
- One agy run is not one connection. The core logs 23-48 CONNECT tunnels per agy start,
  one per HTTP request, with gaps of up to 13 s. Each picks its exit as it opens, so a
  switch mid-run splits the run between two countries and the upstream sees one
  authenticated session change continent.
- The core answers CONNECT with `200 Connection established` *before* it dials the node
  (`listener/http/proxy.go`). A dial that fails after that, or a group holding only
  REJECT, reaches the client as `EOF` in the TLS handshake. agy reads an EOF on its auth
  path as "not logged in", waits 60 s for a sign-in, then reports "authentication failed
  or timed out" — which the consumer took for a lapsed login, judged by an egress check
  that runs only after that minute, by which time the proxy is fine again.
- A provider hot reload closes nothing either: `closeAllConnections` runs only in
  `Initial()`, and the fetcher skips content whose hash has not changed
  (`component/resource/fetcher.go`). A real list change does rebuild every node with
  `alive: true` and no history.

**What was chosen.** The unit worth protecting is one agy subprocess, and only the
consumer knows where one begins and ends. So the consumer stamps each subprocess: it
asks the rotator which country is current and names it in the proxy username for that
subprocess's whole life. The core routes that name to the country's group (`IN-USER`
rules) while `PROXY` keeps rotating for everyone else, which makes rotation gradual —
work in flight keeps its country, new work starts on the new one.

Three facts from the v1.19.30 source hold this up:

1. **The proxy username is read even with no `authentication:` set.** `authenticate()`
   in `listener/http/proxy.go` parses `Proxy-Authorization` first and treats a nil
   authenticator as authorised; the user lands in `metadata.InUser`, which `IN-USER`
   matches exactly. The password is never read, logged or exposed — the `/connections`
   API carries `inboundUser` only — so a request id there serves packet captures and
   uniqueness, not the core.
2. **A per-listener `proxy:` binding exists** (`listeners:` with `type: mixed`;
   `SpecialProxy` in `listener/inbound/base.go`, resolved in `tunnel.resolveMetadata`).
   That was the other way to stamp a run, one port per country, and it lost on rollout:
   a consumer pointed at a port that does not exist yet fails outright, while a tag sent
   to a core without the rules merely rotates.
3. **url-test's `tolerance` is added to a 16-bit delay.** An untested node reports
   `0xffff`, and `0xffff + 50` wraps to 49, so `tolerance: 50` jumped the group onto an
   untested first-listed node after every real list change. At 5000, above the health
   check's own timeout, no live node can displace the one in play: the group stays until
   that node dies, which is what a pinned consumer needs.

Pinning to a node rather than a country was rejected. Rules and listeners are static
while the node list is not, so it would need pre-declared slot groups and a lease
protocol, and would give up the in-country failover `url-test` provides for free. A
sticky country group is the same guarantee with one exception, the node dying, which is
the right exception.

**The contract.** `GET http://proxy_service:${PROXY_CURRENT_PORT}/current` answers
`{"country": "<PROXY's now>", "via": "<that group's node>"}` straight from the core, so
a group renamed in `mihomo/config.yaml` propagates with no other change. `?request=<id>`
makes the rotator log `current: <id> -> US via <node>`, the proxy-side end of a trace.
The consumer then starts its subprocess with
`HTTPS_PROXY=http://<country>:<request id>@proxy_service:1082`. After its first cycle
the rotator reads `/rules` and logs an error for every member of `PROXY` with no
`IN-USER,<name>,<name>` line; `make status` prints the same under `pins:`.

Found on the way: the consumer's container on this host predated its own egress-check
commits, so its boot gate passed a `REJECT` tunnel — a stand-in that answers `200
Connection established` and closes, which that build read as "carrying traffic" and the
current code reports as `UNEXPECTED_EOF_WHILE_READING`.

## 10. Re-verification log

| date | vendor build | what changed | what was done |
| --- | --- | --- | --- |
| 2026-09-03 | wmsxwd 1.42.3 | Baseline. §4's contract and dialect table established against the live account; §7 measured against mihomo v1.19.30. | The GUI runtime was removed and replaced by this stack. |
| 2026-09-03 | — | First live run of the refresher found contract item 8: the panel's Cloudflare answers `403 error code: 1010` to urllib's default User-Agent. The dialect table was re-measured with a parser and the meta row corrected from 48 to 37 proxies. | `PANEL_USER_AGENT` added; §4's table corrected. |
| 2026-09-04 | — | Country rotation built (§6). Two core behaviours measured that contradict the obvious assumption: provider nodes are absent from `/proxies`, and a group's `alive` flag stays true while every member is dead. A pre-existing crash in the refresher's HTTP error handling was found and fixed. Country populations measured at 日本 8, 美国 7, 台湾 6, 韩国 1, 德国 1, 印度 1. | `proxy_rotator` added; `mihomo/config.yaml` grew six country groups and a selector; `PROXY_ROTATION_SECONDS` and `PROXY_ROTATION_JITTER_SECONDS` added to `.env`. |
| 2026-09-05 | — | Consumer requests failed when the rotator switched mid-run (§9). Established that a switch closes nothing, that one agy run is 23-48 tunnels each choosing its exit when it opens, and that a failed dial reaches the client as `EOF` after a `200` to CONNECT. Verified that the proxy username is parsed with no authentication configured, and a 16-bit wrap in url-test's tolerance comparison. | `IN-USER` pin rules and `tolerance: 5000` in `mihomo/config.yaml`; `GET /current` and a `pins:` status line in the rotator; `PROXY_CURRENT_PORT` in `.env`. |
| 2026-09-07 | — | A single-node country that died mid-window kept the selector until the next switch (§6, "Leaving a dead country mid-window"). The rotator's window sleep became a 15 s watch that leaves a dead country at once; "dead" excludes nodes still awaiting their first check, so a node-list reload does not trip it. | `watch()`, `WATCH_SECONDS` and `Country.untested` / `is_dead` in `refresher/rotate.py`. |
