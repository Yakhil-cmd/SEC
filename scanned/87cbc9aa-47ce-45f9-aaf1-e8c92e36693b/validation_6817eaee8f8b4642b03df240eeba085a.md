### Title
Unbounded Memory Allocation in CUP Response Body Collection — (`rs/orchestrator/src/catch_up_package_provider.rs`)

### Summary

`fetch_catch_up_package` collects the full HTTP response body from a peer node into memory with no byte-size cap. Only a time-based `timeout(self.backoff, …)` guards the collection. A single Byzantine subnet node can stream a multi-gigabyte body within the 30-second window, exhausting the orchestrator process's memory and causing an OOM crash. This prevents the targeted node from ever catching up or upgrading.

### Finding Description

At line 349, the body is collected with no size bound:

```rust
let body_req = timeout(self.backoff, res.into_body().collect());
``` [1](#0-0) 

`self.backoff` starts at 30 seconds. [2](#0-1) 

The raw `collect()` call accumulates every byte chunk into a single `Bytes` buffer in the heap. There is no `http_body_util::Limited` wrapper, no `Content-Length` check, and no per-response byte counter anywhere in the orchestrator's CUP path — confirmed by a full grep of `rs/orchestrator/` returning zero matches for `Limited::new`, `body_size_limit`, or `max_response_size`.

Compare this to the HTTPS outcalls adapter, which correctly wraps the body before collecting:

```rust
http_body_util::Limited::new(http_resp.into_body(), remaining_limit as usize)
    .collect()
    .await
``` [3](#0-2) 

The orchestrator's CUP fetcher has no equivalent guard.

### Impact Explanation

An OOM crash of the orchestrator process means the node can no longer:
- detect that a newer CUP exists,
- download and verify a new CUP,
- trigger a replica upgrade.

The node stalls at its current height indefinitely. If the attack is repeated on restart (the orchestrator loops and retries), the node never recovers without operator intervention. While the subnet itself continues (other nodes are unaffected), the targeted node is permanently excluded from participation until manually remediated.

### Likelihood Explanation

The attacker must control one registered subnet node with valid TLS credentials — a single Byzantine node, which is explicitly within the IC's fault-tolerance model ("protocol peer behavior below the consensus fault threshold"). The TLS handshake authenticates the peer's node identity but does not bound the response body size. At 100 MB/s sustained throughput, 3 GB can be pushed in 30 seconds before the timeout fires. The backoff doubles on timeout, so subsequent attempts give the attacker an even larger window (60 s, 120 s, …), making the attack progressively easier over time. [4](#0-3) 

### Recommendation

Replace the bare `.collect()` with a size-bounded variant, mirroring the pattern already used in the HTTPS outcalls adapter:

```rust
use http_body_util::Limited;

const MAX_CUP_RESPONSE_BYTES: usize = 10 * 1024 * 1024; // e.g. 10 MiB

let body_req = timeout(
    self.backoff,
    Limited::new(res.into_body(), MAX_CUP_RESPONSE_BYTES).collect(),
);
```

If the limit is exceeded, `collect()` returns a `LengthLimitError`, which should be mapped to an `Err` and logged, without doubling the backoff (to avoid the attacker extending the window).

### Proof of Concept

1. Register a node on the target subnet (Byzantine node scenario).
2. Serve a custom HTTPS endpoint at `/_/catch_up_package` that returns HTTP 200 with `Transfer-Encoding: chunked` and streams 4 GB of zero bytes at maximum bandwidth.
3. Ensure the target orchestrator selects this node as a peer (it will, since it is a registered subnet member).
4. Observe the orchestrator process's RSS grow unboundedly during the 30-second `self.backoff` window.
5. The orchestrator OOM-crashes; the node stops catching up and upgrading.
6. On restart the backoff has doubled, giving the attacker a 60-second window for the next attempt.

### Citations

**File:** rs/orchestrator/src/catch_up_package_provider.rs (L129-137)
```rust
        Self::new_with_initial_backoff(
            registry,
            local_cup_reader,
            crypto,
            crypto_tls_config,
            logger,
            node_id,
            Duration::from_secs(30),
        )
```

**File:** rs/orchestrator/src/catch_up_package_provider.rs (L348-356)
```rust
        let status = res.status();
        let body_req = timeout(self.backoff, res.into_body().collect());

        let bytes = match body_req.await {
            Ok(result) => {
                // Reset backoff on success
                self.backoff = self.initial_backoff;
                match result {
                    Ok(bytes) => bytes.to_bytes(),
```

**File:** rs/orchestrator/src/catch_up_package_provider.rs (L364-374)
```rust
            Err(timeout_err) => {
                let old_backoff = self.backoff;
                self.backoff = old_backoff.saturating_mul(2);
                return Err(format!(
                    "Timed out while reading CUP response body of {} after {} secs: {:?}. Setting backoff to {} secs",
                    url,
                    old_backoff.as_secs(),
                    timeout_err,
                    self.backoff.as_secs()
                ));
            }
```

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L420-424)
```rust
            let body_bytes =
                http_body_util::Limited::new(http_resp.into_body(), remaining_limit as usize)
                    .collect()
                    .await
                    .map(|col| col.to_bytes())
```
