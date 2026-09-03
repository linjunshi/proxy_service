# Digest-pinned so the same bytes build the same image everywhere.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ARG PYYAML_VERSION=6.0.2

# The tests run here, against the interpreter and PyYAML the sidecar will run on. The
# final stage copies refresh.py *out of this stage*, so a failing test fails the build
# rather than shipping.
FROM ${PYTHON_IMAGE} AS test
ARG PYYAML_VERSION
RUN pip install --no-cache-dir "pyyaml==${PYYAML_VERSION}"
WORKDIR /src
COPY . .
RUN python -m unittest discover -s . -v

FROM ${PYTHON_IMAGE}
ARG PYYAML_VERSION
ARG APP_UID=10001
ARG APP_GID=10001

RUN pip install --no-cache-dir "pyyaml==${PYYAML_VERSION}" \
    && groupadd --gid "${APP_GID}" refresher \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home --shell /usr/sbin/nologin refresher \
# A fresh named volume inherits the ownership of the image directory it covers, which
# is the only way this non-root process can write to it.
    && install -d -o refresher -g refresher /providers

COPY --from=test /src/refresh.py /app/refresh.py

ENV PYTHONDONTWRITEBYTECODE=1

USER refresher
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-u", "/app/refresh.py"]
