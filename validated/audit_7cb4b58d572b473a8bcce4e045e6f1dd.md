### Title
API Boundary Node Disaster-Recovery Container Runs Entrypoint as Root, Amplifying Any `ic-boundary` Exploit - (File: `ic-os/api-bn-recovery/Dockerfile`)

---

### Summary

The `ic-os/api-bn-recovery/Dockerfile` builds a runtime container that exposes HTTP/HTTPS ingress ports and launches `ic-boundary` and `ic-registry-replicator` without ever switching away from the default `root` user. Any memory-safety or logic vulnerability in `ic-boundary` reachable by an unprivileged external caller immediately yields root-level access inside the container, enabling theft of TLS private keys, ACME credentials, and registry state stored in the mounted volumes.

---

### Finding Description

The Dockerfile starts from `ubuntu:26.04`, installs packages, copies binaries, and defines an `ENTRYPOINT` — all without a single `USER` instruction:

```dockerfile
FROM ubuntu:26.04                          # implicit root
...
COPY ic-boundary /opt/ic/bin/ic-boundary
COPY ic-registry-replicator /opt/ic/bin/ic-registry-replicator
...
VOLUME ["/data/ic_registry_local_store", "/data/acme", "/certs"]
ENTRYPOINT ["/opt/ic/bin/entrypoint.sh"]   # runs as root
``` [1](#0-0) 

The entrypoint script then launches both long-running network services as root:

```bash
/opt/ic/bin/ic-registry-replicator ... &
...
/opt/ic/bin/ic-boundary "${BOUNDARY_ARGS[@]}" &
``` [2](#0-1) 

`ic-boundary` listens on ports 8080 (HTTP) and 443 (HTTPS), directly processing ingress requests from any external caller. [3](#0-2) 

---

### Impact Explanation

If an attacker exploits any memory-safety or parsing vulnerability in `ic-boundary` via a crafted HTTP/HTTPS request, they obtain a shell or arbitrary code execution inside the container **as root (UID 0)**. From that position they can:

- **Read or exfiltrate TLS private keys** from the `/certs` volume (used for HTTPS termination).
- **Read or overwrite ACME/Let's Encrypt credentials** in `/data/acme`, allowing them to obtain fraudulent certificates for the boundary node's hostname.
- **Tamper with `/data/ic_registry_local_store`**, corrupting the local registry snapshot that `ic-boundary` uses to route canister calls — potentially redirecting traffic or causing denial of service for all canisters served through this recovery node.
- **Facilitate container escape** more easily, since many kernel exploits and `CAP_*` abuses require UID 0.

The sensitive volumes are declared explicitly: [4](#0-3) 

---

### Likelihood Explanation

`ic-boundary` is a Rust binary processing untrusted HTTP/HTTPS traffic from the public internet on ports 8080 and 443. While Rust mitigates many memory-safety bugs, logic vulnerabilities (request smuggling, header injection, deserialization issues) remain realistic. The container is explicitly designed for disaster-recovery deployments where it is exposed directly to the internet. No authentication is required to reach the HTTP endpoint. The absence of a `USER` instruction is a straightforward omission with no compensating control visible in the Dockerfile.

---

### Recommendation

Add an unprivileged user in the Dockerfile and switch to it before the `ENTRYPOINT`:

```dockerfile
RUN groupadd -r icbn && useradd -r -g icbn -d /opt/ic -s /sbin/nologin icbn && \
    chown -R icbn:icbn /opt/ic /data/ic_registry_local_store /data/acme

USER icbn:icbn

ENTRYPOINT ["/opt/ic/bin/entrypoint.sh"]
```

Ensure the mounted volumes (`/data/acme`, `/certs`, `/data/ic_registry_local_store`) are owned or writable by the new user at runtime (e.g., via an init container or volume mount options), so the services retain the access they need without running as root.

---

### Proof of Concept

1. Deploy the container from `ic-os/api-bn-recovery/Dockerfile` as documented.
2. Send a crafted HTTP request to port 8080 that triggers a hypothetical code-execution bug in `ic-boundary`.
3. Verify the resulting shell session is UID 0 (`id` returns `uid=0(root)`).
4. From that shell: `cat /certs/*` or `cat /data/acme/**/*.key` to exfiltrate TLS private key material — no further privilege escalation required.

The root cause is solely the missing `USER` instruction in: [1](#0-0)

### Citations

**File:** ic-os/api-bn-recovery/Dockerfile (L1-31)
```text
FROM ubuntu:26.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create directories
RUN mkdir -p /opt/ic/bin /opt/ic/share \
    /data/ic_registry_local_store \
    /data/acme

# Copy binaries (built externally via Bazel)
COPY ic-boundary /opt/ic/bin/ic-boundary
COPY ic-registry-replicator /opt/ic/bin/ic-registry-replicator

# Copy NNS public key
COPY nns_public_key.pem /opt/ic/share/nns_public_key.pem

# Copy entrypoint
COPY entrypoint.sh /opt/ic/bin/entrypoint.sh
RUN chmod +x /opt/ic/bin/entrypoint.sh \
    /opt/ic/bin/ic-boundary \
    /opt/ic/bin/ic-registry-replicator

# HTTP, HTTPS, ic-boundary metrics, replicator metrics
EXPOSE 8080 443 9090 9092

# Persistent data
VOLUME ["/data/ic_registry_local_store", "/data/acme", "/certs"]

ENTRYPOINT ["/opt/ic/bin/entrypoint.sh"]
```

**File:** ic-os/api-bn-recovery/entrypoint.sh (L61-134)
```shellscript
/opt/ic/bin/ic-registry-replicator \
    --nns-pub-key-pem "${NNS_PUB_KEY}" \
    --nns-url "${NNS_URL}" \
    --local-store-path "${LOCAL_STORE_PATH}" \
    --metrics-listen-addr "${REPLICATOR_METRICS_ADDR}" \
    --log-as-text \
    &
REPLICATOR_PID=$!

# ─── Wait for registry to bootstrap ───
echo "[2/3] Waiting for registry to bootstrap (timeout: ${BOOTSTRAP_TIMEOUT}s)..."
WAITED=0
while true; do
    # Check that replicator is still alive
    if ! kill -0 "$REPLICATOR_PID" 2>/dev/null; then
        echo "ERROR: Registry replicator exited unexpectedly."
        wait "$REPLICATOR_PID" || true
        exit 1
    fi

    # Check if local store has been populated
    if [ -d "${LOCAL_STORE_PATH}" ] && [ -n "$(ls -A "${LOCAL_STORE_PATH}" 2>/dev/null)" ]; then
        echo "Registry bootstrapped after ${WAITED}s."
        break
    fi

    if [ "$WAITED" -ge "$BOOTSTRAP_TIMEOUT" ]; then
        echo "ERROR: Registry failed to bootstrap within ${BOOTSTRAP_TIMEOUT}s."
        cleanup
        exit 1
    fi

    sleep 2
    WAITED=$((WAITED + 2))
    if [ $((WAITED % 10)) -eq 0 ]; then
        echo "  Still waiting for registry data... (${WAITED}s)"
    fi
done

# ─── Build ic-boundary arguments ───
BOUNDARY_ARGS=(
    --registry-local-store-path "${LOCAL_STORE_PATH}"
    --listen-http-port "${HTTP_PORT}"
    --obs-log-stdout
    --obs-metrics-addr "${METRICS_ADDR}"
)

# TLS arguments
if [ -n "${TLS_HOSTNAME}" ]; then
    BOUNDARY_ARGS+=(
        --listen-https-port "${HTTPS_PORT}"
        --tls-hostname "${TLS_HOSTNAME}"
        --tls-acme-credentials-path "${TLS_ACME_CREDENTIALS_PATH}"
    )
elif [ -n "${TLS_CERT_PATH}" ] && [ -n "${TLS_PKEY_PATH}" ]; then
    BOUNDARY_ARGS+=(
        --listen-https-port "${HTTPS_PORT}"
        --tls-cert-path "${TLS_CERT_PATH}"
        --tls-pkey-path "${TLS_PKEY_PATH}"
    )
fi

if [ "${SKIP_REPLICA_TLS_VERIFICATION}" = "true" ]; then
    BOUNDARY_ARGS+=(--skip-replica-tls-verification)
fi

# Pass through any extra arguments from CMD
if [ $# -gt 0 ]; then
    BOUNDARY_ARGS+=("$@")
fi

# ─── Start ic-boundary ───
echo "[3/3] Starting ic-boundary..."
/opt/ic/bin/ic-boundary "${BOUNDARY_ARGS[@]}" &
```
