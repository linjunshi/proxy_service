# CONTEXT — engineering notes

What this file is: the discoveries, decisions and open items behind this repo, for
anyone (human or agent) picking the work up. `README.md` is the operator runbook and
stays authoritative for *how to run it*; this file records *why it is shaped this
way* and *what is known but not yet built*. Dates are absolute. Items marked
**VERIFY** were derived from inspection or documentation, not observed on the
production host.

## 1. Identity and scope

- This is a **generic, independent proxy service**: it packages a reviewed
  third-party proxy client (`wmsxwd`, a clash-family GUI) and offers its tunnel to
  the Docker host and to the `order_process` network. It is not a Gemini project.
  Consumers depend on it; it depends on no consumer, and consumer-specific wiring
  must never land in the base configuration (see §5 for where it goes instead).
- Two ways to consume it:
  1. **Explicit proxy** — `http://wmsxwd:6478` from `order_process`,
     `http://127.0.0.1:6478` from the Windows host. Live today.
  2. **Transparent capture** — a consumer container joins this container's network
     namespace and *all* its egress transits the tunnel via the client's TUN mode.
     Decided 2026-09-01, not built yet (§4–§5).
- First consumer of mode 2: `gemini-local-service`
  (`/Users/Edward/Projects/gemini-local-service`) — §6.

## 2. Environment facts

- Production host: Windows, Docker Desktop (WSL2 backend), Engine ≥ 28, image
  built `linux/amd64`. `order_process` is an external bridge owned by the
  application stack; it is also this container's default route, so the tunnel's
  own upstream connection leaves through it.
- Development host: macOS. Docker Desktop was **not running** during the
  2026-09-01 analysis, so every container-level claim below is inspection-based
  until the verification checklist in §4.6 has been run on the target.
- Names: compose project `proxy-service`, service `wmsxwd`, container
  `proxy-service-wmsxwd-1`. Peers dial **`wmsxwd:6478`** — the service name, which
  Docker's embedded DNS resolves on `order_process`. History: the two-container
  layout had a relay named `wmsxwd-proxy:17890`; after it was removed the README
  and the healthcheck kept dialling `wmsxwd-proxy` although no alias was ever
  declared (the healthcheck could not have passed). Renamed to `wmsxwd` everywhere
  on 2026-09-01.
- The application must have **LAN access** on (binds `0.0.0.0`) and its mixed port
  set to `WMSXWD_PROXY_PORT`; both live in the `wmsxwd_home` volume.

## 3. What the payload is (wmsxwd 1.42.3, `.deb` inspected 2026-09-01)

Established by extracting the package and reading the binaries' string tables —
no source is available.

- Layout: `/opt/wmsxwd/wmsxwd` (Flutter runner, 29 KB), `/opt/wmsxwd/lib/libapp.so`
  (Dart AOT, the whole GUI), `/opt/wmsxwd/lib/AtlasCore_amd64` (36 MB static Go
  binary), `libflutter_linux_gtk.so`, geodata under `data/flutter_assets/assets/`.
- **The core is mihomo** (`github.com/metacubex/mihomo`, Go 1.24.13, built
  `with_gvisor`). Its TUN comes from sing-tun: `auto-route` (netlink policy
  routing), `auto-redirect` (nftables; not used by the GUI), `auto-detect-interface`,
  `dns-hijack`, `route-exclude-address`, stacks `system` / `gvisor` / `mixed`. It
  performs no root check — it needs `CAP_NET_ADMIN` and an openable `/dev/net/tun`,
  nothing else (`SO_BINDTODEVICE` is unprivileged since Linux 5.7).
- **The GUI is Flutter, FlClash lineage.** Its Linux TUN authorization is
  `pkexec setcap cap_net_bind_service,cap_net_admin=+ep <core>`; it checks with
  `getcap` (and `getuid` — running as root also counts). User-facing strings:
  "Administrator Privileges Required", "TUN Mode Needs Re-authorization",
  "TUN interface creation was rejected by the system, usually after an app upgrade
  reset the core's privileges", "1. Switch to TUN mode (first enablement prompts
  for password, then stays authorized) 2. Or launch the app with sudo".
  External programs it executes: `pkexec`, `setcap`, `getcap`, `xdg-open` — no
  `ip`, no `iptables`.
- Flipping the TUN switch writes, then applies with `PATCH /configs`
  (`[ProxyTunManager]`):
  `tun: {enable, stack, auto-route: true, auto-detect-interface: true | false +
  interface-name: "<the 'TUN Outbound Interface' setting>", dns-hijack, device,
  route-exclude-address}` and `dns: {enable, listen: :1053, enhanced-mode: fake-ip,
  fake-ip-range, nameserver, default-nameserver}`. It confirms success by watching
  core logs for `[TUN] Tun adapter listening at:` and fails on
  `Start TUN listening error:` (`[TunOsVerifier]`).
- Runtime: the core's home/config directory is set by the GUI under the volume
  (`[MihomoCoreService] init homeDir`); the core process name is `AtlasCore_amd64`
  (`container/recover-app` kills it by that name). Known defects on this platform
  are in `README.md` ("The in-app status is unreliable").

## 4. Decision (2026-09-01): enable TUN mode in this container

### 4.1 Why, and what it does and does not reach

Consumers exist whose *entire* egress must transit the tunnel without relying on
every client library honouring `HTTPS_PROXY` (DNS, UDP/QUIC, anything that ignores
the environment). A TUN device captures one network namespace:

| Traffic source | Captured |
|---|---|
| This container's own processes | yes |
| Containers joined to this namespace (`network_mode: "container:…"`) | yes |
| Ordinary `order_process` peers dialling `wmsxwd:6478` | no — unchanged, explicit proxy |
| The Windows host | no — VM boundary; host-wide TUN means installing the client on Windows |

This reverses the README's hardening rule ("Do not enable TUN mode,
`CAP_NET_ADMIN` …"), which was deliberate; the Product Owner accepted the residual
risk in §4.5 on 2026-09-01.

### 4.2 Requirements (each non-negotiable)

1. `/dev/net/tun` → `devices: ["/dev/net/tun:/dev/net/tun"]`. The node must be
   world-rw for UID 10000: `CAP_NET_ADMIN` does not bypass file permissions.
   **VERIFY** the mode Docker Desktop gives it; design B is the fallback.
2. `CAP_NET_ADMIN` in the bounding set → `cap_add: [NET_ADMIN, NET_BIND_SERVICE]`,
   keeping `cap_drop: ALL`. Needed for `TUNSETIFF`, netlink routes/rules, `SO_MARK`.
   Do **not** add `NET_RAW`: mihomo does not need it, and it is what raw-socket /
   ARP tricks on the bridge would need.
3. File capabilities on the core → Dockerfile
   `setcap cap_net_bind_service,cap_net_admin=+ep /opt/wmsxwd/lib/AtlasCore_amd64`
   plus `libcap2-bin` in the runtime image (provides `setcap` and the `getcap` the
   GUI's check runs). Reason: for a non-root `user:`, Docker leaves `cap_add`
   capabilities in the bounding set only (moby/moby#8460); a non-root process gains
   them on `execve` only through file capabilities. This is byte-for-byte what the
   GUI itself does on a Linux desktop, so its `alreadyAuthorized` check passes and
   the `pkexec` path — impossible here, no polkit — is never entered.
   **VERIFY** with `grep Cap /proc/<core pid>/status` after the switch is on.
4. `security_opt: no-new-privileges:true` must be removed: under `no_new_privs`
   "file capabilities will not add to the permitted set" (kernel documentation).
   Mitigation: extend the Dockerfile's existing setuid strip (`find … -perm /6000
   … chmod a-s`, currently `/opt/wmsxwd` only) to the whole image, so the core's
   two capabilities are the only privilege any file can confer.
5. Unchanged: `read_only: true`, tmpfs `/tmp`, UID 10000, the default seccomp
   profile (allows `ioctl`, netlink sockets), no host networking, no host mounts.

### 4.3 Designs

- **A — non-root + file capabilities (chosen).** Entrypoint unchanged, still UID
  10000. Only the core binary can obtain `NET_ADMIN`; GUI, Xvnc, websockify and
  the scripts remain capability-less. Vendor-identical model.
- **B — root-started init with ambient capabilities (fallback only).** `tini` /
  `start-wmsxwd` start as root, fix `/dev/net/tun` permissions, then
  `setpriv --reuid=10000 --regid=10000 --init-groups --ambient-caps=+net_admin`
  into supervisord. Keeps `no-new-privileges` (ambient caps survive it) but every
  supervised process holds `NET_ADMIN`, the entrypoint runs as root, and ambient
  caps are cleared on exec of a file that has file caps — so B excludes `setcap`,
  and the GUI's `getcap` check then reports "not authorized" and may refuse the
  switch. Use only if the device node turns out to be `0600`.

### 4.4 Knock-on effects inside this repo

1. **`container/auto-connect` must change — not optional.** `tunnel_state`
   compares the exit IP seen *directly* with the one seen through the mixed port.
   With `auto-route` on, the "direct" probe (`/dev/tcp/api.ipify.org/80`) also
   enters the TUN, both IPs match, and the watchdog would conclude "not tunneled"
   → `recover-app` → repeat every cycle. Fix: take the baseline bound to `eth0`
   (`SO_BINDTODEVICE` escapes policy routing — it is how mihomo's own
   `auto-detect-interface` avoids looping itself), e.g. `curl --interface eth0`,
   which adds `curl` to the image. Alternative: judge health through the core's
   REST API (`external-controller`, secret in the app's config).
2. Healthcheck (`/dev/tcp/wmsxwd/6478`): expected fine — sing-tun's `auto-route`
   keeps directly-connected subnets on `eth0` (main-table lookup with the default
   route suppressed), and even via the TUN a private IP hits a DIRECT rule.
   **VERIFY**.
3. DNS: the GUI switches to fake-ip + `dns-hijack`. Docker's embedded resolver
   forwards external queries from inside the container namespace, so they are
   hijacked and answered with fake IPs; domain rules keep working. **VERIFY**.
4. `recover-app`'s `pkill -9` leaves mihomo's policy rules behind pointing at an
   empty table; traffic falls through to `main`, mihomo re-installs on start.
   Expected harmless. **VERIFY** no stale-route symptom.
5. The TUN switch persists in the volume. A container later started *without* the
   capabilities logs `Start TUN listening error`, the GUI shows the
   re-authorization dialog, and the mixed port keeps serving — loud, not silent.
6. `README.md`: rewrite the trust-boundary section and the closing hardening rule;
   first-start steps gain the TUN toggle; add the `getcap` verification.
7. Prefer the `system` TUN stack in the GUI (kernel TCP, lowest overhead); `gvisor`
   is the fallback if the system stack misbehaves in the container.

### 4.5 Residual risk (accepted)

A closed-source proxy client holds `NET_ADMIN` inside its own namespace: it can
reconfigure routes and firewall for itself and for every joined consumer (by
design — all their traffic transits it). It cannot touch the host's or other
containers' namespaces. Without `NET_RAW` it cannot spoof on the bridge. Dropping
`no-new-privileges` is neutralised by the image-wide setuid strip.

### 4.6 Verification checklist (run on the Windows host after the change)

1. `docker compose exec wmsxwd ls -l /dev/net/tun` → `crw-rw-rw-`.
2. `docker compose exec wmsxwd getcap /opt/wmsxwd/lib/AtlasCore_amd64` →
   `cap_net_admin,cap_net_bind_service=ep`.
3. Flip TUN in the GUI → logs show `[TUN] Tun adapter listening at: Meta(...)`;
   `/proc/net/dev` lists the device (`iproute2` in the image gives `ip rule` /
   `ip route` for diagnosis).
4. Inside the container, no proxy set: `curl http://api.ipify.org` returns the
   tunnel exit; `curl --interface eth0 http://api.ipify.org` returns the direct
   one; `make status` on the host still says `UP`; the watchdog stays quiet.
5. From a joined consumer: `curl http://api.ipify.org` returns the tunnel exit.

## 5. Consumer contract for transparent mode (planned)

- Join with `network_mode: "container:<name>"` — cross-project, so `service:` is
  not available and the container needs a **stable name**: proposal
  `container_name: wmsxwd` (the same word peers already dial; container names are
  DNS-resolvable, so nothing changes for mode-1 consumers).
- A joined consumer **drops** `networks:`, `ports:` and `extra_hosts:` (Docker
  refuses them with container network mode) and shares this container's
  `/etc/hosts` and `/etc/resolv.conf`. Its healthcheck against `127.0.0.1` keeps
  working (same namespace).
- Any port the consumer must publish to the host, and any DNS alias the
  application uses for it (e.g. `gemini_service`), are declared **on this
  container** — the namespace owner owns names and ports. They are site-specific,
  so they belong in a compose override, not in `compose.yaml` (pending decision:
  `compose.override.yaml`, gitignored, with a committed `.example`).
- Restart coupling: restarting or recreating `wmsxwd` gives it a new network
  sandbox; joined consumers lose connectivity until they are restarted too.
  `make restart` / `make up` should therefore also restart joiners (find them
  with `docker inspect -f '{{.HostConfig.NetworkMode}}'`). `make recover`
  restarts only the app inside the container and needs nothing.
- Ordering: this stack must be up before any joiner is created. `docker compose
  down` here leaves joiners with only `lo` — they fail closed.
- Exposure: joiners can reach `127.0.0.1:5901` (VNC) and `:6080` (noVNC), so the
  VNC password stops being optional.
- Recommended joiner posture: keep `HTTPS_PROXY=http://wmsxwd:6478` — it resolves
  to this container's own address inside the shared namespace, is cheaper than
  TUN for HTTP clients, and **fails closed** when the core is down — and let TUN
  catch the remainder (DNS, anything ignoring the environment).
- Known gap — no kill switch: if the core dies its routes vanish and residual
  traffic exits `eth0` directly; if the core is up but 未连接 it serves DIRECT rules
  and everything exits directly (the README's existing caveat). A v2 kill switch
  is an nft `OUTPUT` drop on `eth0` for everything but the core's marked sockets
  and the bridge subnet — needs file caps on `nft` or a root-started init.

## 6. gemini-local-service — the first consumer

- What it is: a Python/uvicorn service driving `agy` (Google's Antigravity CLI, a
  Go binary) subprocesses; passes `HTTPS_PROXY`/`NO_PROXY` from its `.env` to them
  and to its DingTalk alerts; joins `order_process` with its own alias
  `gemini_service` and publishes `127.0.0.1:8000` (admin page).
- Today it proxies through a LAN machine (`HTTPS_PROXY=http://192.168.1.137:1082`),
  not through this stack.
- Why transparent mode: Google restricts certain source IPs for Flash models, and
  community advice is TUN so that *nothing* leaks off the tunnel. Assumption to
  **validate** before generalising: the sign-in ceremony runs in the operator's
  browser on the host, so Google still sees a login IP that differs from the API
  IP; measure whether TUN actually lifts the restriction.
- What changes there (owned by that repo; listed here as the contract): compose
  → `network_mode: "container:wmsxwd"`, drop `networks`, `ports`, `extra_hosts`;
  `.env` → `HTTPS_PROXY=http://wmsxwd:6478`; `tests/test_deployment.py` pins the
  old shape (`networks == [order_process]`, loopback-published port, the
  `extra_hosts` skip, `ProxyHandler({})` in the healthcheck) and must move with
  it. The image **build** is a separate network context — BuildKit's default
  builder cannot join `order_process`, so the build keeps its own proxy variable
  (the LAN proxy is fine: the build only fetches Debian packages and agy).
- This stack's override for it: `ports: ["127.0.0.1:8000:8000"]` and
  `aliases: [gemini_service]` on `order_process`, so the application keeps
  reaching `http://gemini_service:8000` unchanged.

## 7. Stale documentation still to fix

- `README.md` "Automatic connect at boot" describes a one-shot task; since the
  watchdog rewrite `container/auto-connect` is continuous (CONNECT probe every
  120 s, full exit-IP verdict every 600 s, backoff to 900 s) and
  `supervisord.conf` treats it as long-running. The section — and its "nothing
  re-checks the tunnel afterwards" — is wrong.
- `README.md` closing hardening rule and trust-boundary section: rewritten as part
  of the TUN work (§4.4 item 6).

## 8. Decisions pending and next steps

Pending (recommendation first):
1. Override-file mechanism for consumer ports/aliases — `compose.override.yaml`
   gitignored + committed example.
2. `container_name: wmsxwd` — yes.
3. Watchdog baseline — `curl --interface eth0` (adds curl) over the REST API.
4. `iproute2` in the image for diagnosability — yes.
5. Kill switch — v2, after TUN is proven.

Order of work:
1. This repo: Dockerfile (libcap2-bin, setcap, image-wide setuid strip, curl,
   iproute2), compose (device, cap_add, drop no-new-privileges, container_name),
   `auto-connect` baseline, override example, README. Run §4.6 on the host.
2. gemini-local-service: compose, `.env`, tests; restart; confirm exit IP from
   inside it.
3. Measure the Gemini restriction with and without TUN; record the result here.
