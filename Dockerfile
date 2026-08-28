# Digest-pinned like the .deb: the same bytes build the same image everywhere.
ARG DEBIAN_IMAGE=debian:12-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171
ARG WMSXWD_VERSION=1.42.3
ARG WMSXWD_SHA256=91c1b6f8eab810858fd5997d18de3790784ed6a3ca8400413c05e439c10d5b85

FROM ${DEBIAN_IMAGE} AS package

ARG WMSXWD_VERSION
ARG WMSXWD_SHA256

COPY vendor/wmsxwd1-${WMSXWD_VERSION}-Linux.deb /tmp/wmsxwd.deb

RUN echo "${WMSXWD_SHA256}  /tmp/wmsxwd.deb" | sha256sum --check --strict \
    && mkdir /payload \
    && dpkg-deb --extract /tmp/wmsxwd.deb /payload

FROM ${DEBIAN_IMAGE}

ARG APP_UID=10000
ARG APP_GID=10000

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        at-spi2-core \
        ca-certificates \
        dbus-x11 \
        desktop-file-utils \
        fonts-dejavu-core \
        fonts-noto-cjk \
        gnome-keyring \
        libayatana-appindicator3-1 \
        libgl1 \
        libgl1-mesa-dri \
        libgtk-3-0 \
        libjavascriptcoregtk-4.1-0 \
        libsecret-1-0 \
        libsoup-3.0-0 \
        libwebkit2gtk-4.1-0 \
        novnc \
        procps \
        supervisor \
        tigervnc-common \
        tigervnc-standalone-server \
        tigervnc-tools \
        tini \
        websockify \
        x11-utils \
        x11-xserver-utils \
        xdg-utils \
        xdotool \
        xfce4-panel \
        xfce4-session \
        xfce4-settings \
        xfdesktop4 \
        xfwm4 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" app \
    && useradd \
        --uid "${APP_UID}" \
        --gid "${APP_GID}" \
        --create-home \
        --shell /usr/sbin/nologin \
        app \
    && install -d -o app -g app \
        /home/app/.config \
        /home/app/.local/share

# The reviewed package's only post-install action refreshes desktop/icon caches.
# Copy only the application directory so package files cannot replace base-image
# configuration, users, libraries, or executables.
COPY --from=package /payload/opt/wmsxwd/ /opt/wmsxwd/
# The tray and window icon resolve through the icon theme by name.
COPY --from=package /payload/usr/share/icons/ /usr/share/icons/

COPY container/start-wmsxwd /usr/local/bin/start-wmsxwd
COPY container/wait-for-display /usr/local/bin/wait-for-display
COPY container/auto-connect /usr/local/bin/auto-connect
COPY container/recover-app /usr/local/bin/recover-app
COPY container/supervisord.conf /etc/supervisor/conf.d/wmsxwd.conf

RUN chmod 0755 \
        /usr/local/bin/start-wmsxwd \
        /usr/local/bin/wait-for-display \
        /usr/local/bin/auto-connect \
        /usr/local/bin/recover-app \
    && chown -R root:root /opt/wmsxwd \
    && chmod -R go-w /opt/wmsxwd \
    && find /opt/wmsxwd -xdev -type f -perm /6000 -exec chmod a-s {} +

# Missing runtime pieces must fail the build, not a started container.
# LD_LIBRARY_PATH mirrors the loader view of the bundled lib directory, so
# only genuinely absent system libraries are reported.
RUN set -eu \
    && for tool in Xtigervnc websockify supervisord supervisorctl \
        dbus-run-session tigervncpasswd gnome-keyring-daemon xfce4-session \
        xdpyinfo tini xdotool pkill; do \
        command -v "${tool}" >/dev/null || { echo "Missing tool: ${tool}" >&2; exit 1; }; \
    done \
    && unresolved=$(find /opt/wmsxwd -type f \( -name '*.so' -o -perm -0100 \) \
        -exec env LD_LIBRARY_PATH=/opt/wmsxwd/lib ldd {} \; 2>/dev/null \
        | grep 'not found' || true) \
    && if [ -n "${unresolved}" ]; then \
        printf 'Unresolved libraries:\n%s\n' "${unresolved}" >&2; exit 1; \
    fi

ENV DISPLAY=:1 \
    GDK_BACKEND=x11 \
    HOME=/home/app \
    LANG=C.UTF-8 \
    LIBGL_ALWAYS_SOFTWARE=1 \
    NO_AT_BRIDGE=1 \
    USER=app \
    XDG_CONFIG_HOME=/home/app/.config \
    XDG_DATA_HOME=/home/app/.local/share \
    XDG_RUNTIME_DIR=/tmp/runtime

WORKDIR /home/app
USER app

# The image tag no longer carries the version, so keep it inspectable here.
ARG WMSXWD_VERSION
LABEL org.opencontainers.image.version="${WMSXWD_VERSION}"

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-wmsxwd"]
