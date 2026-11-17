# Both official image indexes include Linux amd64 and arm64 variants. Matching
# Bookworm bases keep the copied Node executable and C++ runtime ABI compatible.
FROM node:24.22.2-bookworm-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9 AS node-dependencies

WORKDIR /opt/outage
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && mkdir /node-libs \
    && find /usr/lib -name libstdc++.so.6 -exec cp -L '{}' /node-libs/ ';' \
    && find /usr/lib -name libgcc_s.so.1 -exec cp -L '{}' /node-libs/ ';'

FROM python:3.15.0-slim-bookworm@sha256:0e019bbdfb5d9874cb2389f1c2884b64aa2ac97c99960b5bc521cef978c0617d AS python-wheel

WORKDIR /build
COPY pyproject.toml ./
COPY src/ ./src/
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.15.0-slim-bookworm@sha256:0e019bbdfb5d9874cb2389f1c2884b64aa2ac97c99960b5bc521cef978c0617d AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OUTAGE_PROJECT_DIR=/opt/outage \
    OUTAGE_DATA_DIR=/appdata/data \
    OUTAGE_STATE_DIR=/appdata/state \
    OUTAGE_SANDBOX_NODE_MODULES=/opt/outage/node_modules \
    OUTAGE_LOCAL_HTTP=1

WORKDIR /opt/outage
COPY --from=node-dependencies /usr/local/bin/node /usr/local/bin/node
COPY --from=node-dependencies /node-libs/ /usr/local/lib/
COPY --from=node-dependencies /opt/outage/node_modules/ ./node_modules/
COPY --from=python-wheel /wheels/ /wheels/
COPY requirements.txt /tmp/requirements.txt
RUN ldconfig \
    && python -m pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && python -m pip install --no-cache-dir --no-deps /wheels/*.whl \
    && python -m pip check \
    && python -m pip uninstall --yes pip \
    && rm -rf /wheels /tmp/requirements.txt \
    && groupadd --gid 10001 outage \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin outage \
    && mkdir -p /appdata/data /appdata/state \
    && chown -R 10001:10001 /appdata

WORKDIR /appdata
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "outage_explorer.web.local", "serve", "--host", "0.0.0.0", "--port", "8000"]
