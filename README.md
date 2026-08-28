# wmsxwd isolated proxy

This stack places the reviewed wmsxwd payload into the image at build time. The
application is already present before a container starts; startup only launches
the virtual desktop and the GUI.

One container serves everything: the noVNC desktop on the host loopback, and
the application's own mixed proxy port to both the host loopback and the
`order_process` network. Peers reach it as `wmsxwd`, the service name Docker
resolves on that network.

> **Trust boundary.** The container joins `order_process` directly, so every
> peer on that network can reach *all* of its ports — including the noVNC port,
> which drives the logged-in desktop. Docker networks have no per-port ACL, so
> the VNC password is what protects the desktop from peers. Set one unless the
> whole network is trusted. Port 5901 stays loopback-only (`-localhost yes`).
>
> `order_process` is also the container's default route, so the tunnel reaches
> its provider through it. If its owner ever makes that network `internal`,
> this stack loses egress and the tunnel dies.

## Architecture

```text
Windows browser 127.0.0.1:6080
  -> noVNC -> TigerVNC/Xfce -> wmsxwd GUI

Windows 127.0.0.1:6478            order_process containers
                 \                 /  (http://wmsxwd:6478)
                  -> wmsxwd container: mixed port 6478 -> tunnel
```

### Settings live in exactly one place

`.env` (copied from `.env.example`) is the only file that carries a value.
Compose, the Makefile, and the in-container scripts all read it and none of
them defines a fallback, so a missing or mistyped setting fails the command
with a message instead of quietly running on a different number than the one
you think is in effect:

```text
required variable WMSXWD_PROXY_PORT is missing a value: not set - copy .env.example to .env
```

| Variable | Default | What it is |
| --- | --- | --- |
| `WMSXWD_GUI_PORT` | `6080` | noVNC GUI on the host loopback |
| `WMSXWD_PROXY_PORT` | `6478` | The proxy: the mixed port the app binds, published at `127.0.0.1:<port>` on the host **and** reachable as `wmsxwd:<port>` on `order_process` |
| `WMSXWD_AUTO_CONNECT` | `1` | `0` disables the tunnel watchdog |
| `VNC_PASSWORD_FILE` | `./secrets/vnc_password.txt` | Source of the VNC password secret |

There is no longer a separate internal port. The number the application binds,
the number published on the host, and the number peers dial are one value; if
they ever disagree the container goes unhealthy rather than half-working.

Both port variables drive the container side as well as the host side —
`WMSXWD_GUI_PORT` is what websockify binds, not just what Docker publishes —
so there is no second copy anywhere to fall out of step.

Two things deliberately stay out of `.env`. The payload version and its
SHA-256 live in the `Dockerfile`, beside the `sha256sum` check that enforces
them: a build-integrity pin belongs in version control, not in a gitignored
file. And the internal VNC port (`5901`) is never exposed or dialled from
outside the container, so it stays a fixed detail of the image.

The application owns the proxy port: change it in the app GUI first, then
mirror it in `.env` and run `docker compose up -d`. The app must also have
**LAN access enabled** (`允许局域网连接` / "Allow LAN") — without it a
clash-family core binds `127.0.0.1` only and peers get connection-refused,
even though the port is open inside the container.

Neither mistake is silent. The healthcheck connects through the container's
own `wmsxwd` name, which is the same path peers take, so a wrong port
*or* LAN access left off turns the container unhealthy within ~15 s.

Verify what the app bound (`194E` is hex for 6478):

```powershell
docker compose exec wmsxwd grep -i ':194E' /proc/net/tcp
```

`00000000:194E` is `0.0.0.0` — reachable by peers. `0100007F:194E` is
`127.0.0.1` — loopback only, turn on LAN access.

Two states matter and both leave the mixed port open:

- **Not connected** (`未连接`): the core serves DIRECT rules. Traffic through
  the port flows but is NOT tunneled — it exits from this machine directly.
- **Connected** (`已连接`): foreign destinations are tunneled through the
  selected node.

Do not treat a reachable proxy port as proof of tunneling; compare the exit IP
(`curl --proxy http://127.0.0.1:6478 http://api.ipify.org`) with your direct
one when it matters.

## Prerequisites

- Docker Desktop running Linux containers with Engine 28 or newer.
- An existing bridge network named `order_process`.
- `vendor/wmsxwd1-1.42.3-Linux.deb` with this SHA-256:
  `91c1b6f8eab810858fd5997d18de3790784ed6a3ca8400413c05e439c10d5b85`.

## Configure

Copy `.env.example` to `.env` and adjust host ports if needed.

Create `secrets/vnc_password.txt`. The file must exist — Compose refuses to
start the stack when the secret's source is missing — but what it holds decides
the GUI's authentication:

- **Eight random printable ASCII characters** turns on VNC authentication.
  Classic VNC auth uses an eight-character password, and anything else in the
  file (a shorter one, a space, a non-ASCII byte) fails the boot with a message
  rather than starting an open desktop.
- **Empty**, or nothing but whitespace, turns it off: the GUI then accepts any
  connection, and the container says so in `docker compose logs` at every boot.

The GUI port is published only on the Windows loopback interface, but the
container also sits on `order_process`, so with no password any peer container
there can drive the logged-in app. Set a password unless every workload on
that network is trusted. Do not commit this file.

Confirm the external network exists:

```powershell
docker network inspect order_process
```

## Build and start

```powershell
Copy-Item .env.example .env    # only needed once
docker compose build
docker compose up -d
```

`make` creates `.env` from the template by itself, so with `make` available: `make build`, `make up`, `make down`,
`make deploy` (git pull + build + up), `make restart` (re-runs auto-connect),
`make recover` (clean-slate app restart inside the running container),
`make logs`, and `make status` (containers plus a live tunnel verdict).

### Repair a Windows checkout created before the LF policy

Fresh clones keep the Linux container files LF automatically. An older Windows
checkout can retain CRLF files after pulling the policy because their Git blobs
did not change. Refresh tracked files only from a clean worktree:

```powershell
git status --short
# Continue only if the command above prints nothing.
git rm --cached -r -- container scripts
git restore --source=HEAD --staged --worktree -- container scripts
git ls-files --eol -- container scripts
# Every listed file must show w/lf.
docker compose up -d --build --force-recreate
```

The build performs these steps once:

1. Copies the reviewed `.deb` from the narrow `vendor` build context.
2. Verifies its exact SHA-256.
3. Extracts it without executing maintainer scripts, then copies only the
   expected `/opt/wmsxwd` application directory and its icon into the runtime
   image.
4. Installs GUI dependencies from the Debian repositories.
5. Fails if a required runtime tool is absent or a bundled binary cannot
   resolve a shared library against the installed packages.
6. Switches the final runtime to UID/GID `10000`.

No package manager or installation command runs during normal container
startup. The inspected package's only post-install action refreshes desktop and
icon caches, which are unnecessary because the application is launched by its
absolute path.

Open the GUI at:

```text
http://127.0.0.1:6080/vnc.html
```

After login and node selection, Windows applications can use:

```powershell
$env:HTTP_PROXY  = "http://127.0.0.1:6478"
$env:HTTPS_PROXY = "http://127.0.0.1:6478"
$env:NO_PROXY    = "localhost,127.0.0.1,::1"
```

Containers already attached to `order_process` can use Docker DNS:

```yaml
environment:
  HTTP_PROXY: http://wmsxwd:6478
  HTTPS_PROXY: http://wmsxwd:6478
  NO_PROXY: localhost,127.0.0.1,wmsxwd
```

The consuming service must also declare the same external network in its own
Compose project. No container restart is needed after the GUI state changes.

## Automatic connect at boot

The containers restart with Docker (`unless-stopped`). The application
restores its saved connection state on its own shortly after it starts, so the
supervised one-shot task (`auto-connect`) verifies rather than clicks:

1. Poll the tunnel for up to 150 s (exit-IP comparison, never port checks).
2. If still down, do a clean-slate app restart (`recover-app`) and poll again.
3. Only then press the start button once as a last resort, and poll again.

The verdict lands in `docker compose logs wmsxwd`. The node last selected in
the GUI is what it connects to (kept in the data volume; this deployment pins
台湾06). Set `WMSXWD_AUTO_CONNECT=0` in `.env` to disable the task.

If the provider's entry servers are unreachable no amount of clicking helps;
auto-connect then logs `giving up` with the reason candidates.

**This runs once, at boot.** Nothing re-checks the tunnel afterwards, so a node
that dies at 03:00 stays dead until someone runs `make recover`.

## The in-app status is unreliable — trust `make status`

Two defects of this application build inside the container (probed live,
2026-08-28, and not fixable from outside the app):

- The home-screen pill can show **未连接 while the tunnel is up and working**.
  Judge connectedness only by `make status` (exit-IP comparison).
- The start button **silently does nothing** whenever any core process
  already holds the app's ports — which is the normal situation, because the
  app starts its core automatically. Clicking it repeatedly can push the app
  into a core spawn-retry loop.

So: the GUI at `http://127.0.0.1:6080/vnc.html` is for logging in and picking
a node. For everything else use:

- `make status` — containers plus a live tunnel verdict.
- `make recover` — clean-slate app restart inside the running container
  (app and every core process stopped together, then a fresh start restores
  the saved state; auto-connect re-runs and logs the verdict). Use it when
  the tunnel is down or the GUI acts dead.
- `make restart` — full GUI-container restart; heavier version of the same.

Note that `make status` probes with plain HTTP. Consumers using HTTPS go
through `CONNECT`, which it does not exercise; test that separately when
diagnosing a consumer that fails while `make status` reads UP.

## First start is the only manual step

On a fresh deployment, do the one-time setup through the browser GUI:

1. `docker compose build && docker compose up -d`
2. Open `http://127.0.0.1:6080/vnc.html`, log in, then in the app's settings:
   - set the **mixed port** to `6478` — it must match `WMSXWD_PROXY_PORT`, and
     the packaged clash default of `7890` is never used,
   - enable **LAN access**, so the port binds `0.0.0.0` and peers can reach it.
3. Select the node (this deployment uses 台湾06-×1) and press start once. This
   is the one moment the button genuinely works (no core is holding the ports
   yet). The pill may still show 未连接 — confirm with `make status` instead.

Both live in the `wmsxwd_home` volume, so every later container start —
restart, host reboot, image rebuild — boots already logged in with the same
node, and `auto-connect` verifies (and if needed restores) the tunnel
unattended. `docker compose down` keeps the volume; only `down -v` erases the
setup and returns the stack to step 2.

### Optional: back up or clone the configured state

`scripts/export-state.sh` snapshots the volume to `state/wmsxwd-home.tar.gz`
(contains the live login session — gitignored; handle like a credential file).
`scripts/import-state.sh` seeds an empty volume from it on another machine
before the first `up`, skipping the one-time GUI setup there; it refuses to
overwrite existing state. Useful as a backup before application updates.

## Migrating from the two-container layout

Earlier versions ran a second `wmsxwd-proxy` container that listened on
`17890` and relayed into the GUI container over a Unix socket, translating to
the `6478` the application actually binds. The relay, that translation and
the `wmsxwd-proxy` name are all gone: the application serves its own port
directly under its service name, `wmsxwd`.

Because the exposed port is now the one the app already binds, **no port has
to change in the GUI** — but consumers move from `wmsxwd-proxy:17890` to
`wmsxwd:6478`.

1. Deploy, dropping the old relay container:

   ```powershell
   docker compose up -d --remove-orphans
   ```

2. Open `http://127.0.0.1:6080/vnc.html` and enable **LAN access**
   (`允许局域网连接`). Without it the app binds `127.0.0.1` only and no peer
   can reach it; the container reads `unhealthy` until this is done. That is
   the intended signal, not a separate fault.

3. Confirm, then repoint every consumer:

   ```powershell
   docker compose ps                                         # wmsxwd (healthy)
   docker compose exec wmsxwd grep -i ':194E' /proc/net/tcp   # expect 00000000:194E
   make status
   ```

   ```yaml
   HTTP_PROXY:  http://wmsxwd:6478    # was http://wmsxwd-proxy:17890
   HTTPS_PROXY: http://wmsxwd:6478
   ```

4. Clean up what the old layout left behind:

   ```powershell
   docker volume rm proxy-service_proxy_socket
   docker image rm wmsxwd-relay:1.42.3 wmsxwd-local:1.42.3
   ```

The image is now named by Compose (`proxy-service-wmsxwd`) rather than a
hand-written tag, so the version is no longer duplicated between `compose.yaml`
and the `Dockerfile`. Read it back with:

```powershell
docker image inspect proxy-service-wmsxwd --format '{{index .Config.Labels "org.opencontainers.image.version"}}'
```

## Stop and update

```powershell
docker compose down
```

The named volume retains application settings and any credential files the app
creates. A desktop keyring can still ask to be unlocked, so a restart may
require logging in again. Do not add `-v` unless that state should be deleted.

For an application update, replace the `.deb`, change the version and SHA-256
build arguments, and rebuild the image. The read-only runtime intentionally
prevents an updater from replacing `/opt/wmsxwd` while the container is running.

Do not enable TUN mode, `CAP_NET_ADMIN`, host networking, privileged mode, host
folder mounts, or the Docker socket for this container.
