### Title
Unbounded Backoff Growth via Byzantine Peer Body-Stall Allows Indefinite CUP Fetch Delay — (`rs/orchestrator/src/catch_up_package_provider.rs`)

---

### Summary

`CatchUpPackageProvider::fetch_catch_up_package` uses a single shared `self.backoff` field as the timeout for reading HTTP response bodies from peers. A Byzantine peer that sends HTTP 200 headers promptly (within the fixed 10-second header timeout) but then stalls the body stream causes `self.backoff` to double on each occurrence with no upper bound. Because `self.backoff` is shared state across all subsequent peer fetches, a single malicious peer can grow the body-read timeout to `Duration::MAX` (~584 years), permanently stalling the node's CUP fetch loop.

---

### Finding Description

In `fetch_catch_up_package`, two separate timeouts govern the HTTP exchange:

**Header/connection timeout** — fixed at 10 seconds, not attacker-influenced: [1](#0-0) 

**Body timeout** — uses the mutable shared `self.backoff`: [2](#0-1) 

On body timeout, the backoff doubles with no cap: [3](#0-2) 

On success, the backoff resets to `initial_backoff` (30 s): [4](#0-3) 

`self.backoff` is a plain `Duration` field on the struct — there is no `MAX_BACKOFF` constant or clamp anywhere in the orchestrator: [5](#0-4) 

The peer-selection logic for an **unassigned node that already has a local CUP** (`current_node_index == None`) limits the attempt to exactly **1 peer per cycle**: [6](#0-5) 

Attack call chain:
```
get_latest_cup
  └─ get_peer_cup          (selects 1 peer for unassigned node)
       └─ fetch_and_verify_catch_up_package
            └─ fetch_catch_up_package
                 ├─ timeout(10s, send_request)   ← fixed, Byzantine peer passes this
                 └─ timeout(self.backoff, body)  ← Byzantine peer stalls here
                      └─ self.backoff *= 2       ← no cap
```

A Byzantine peer that:
1. Completes the TLS handshake and sends HTTP 200 headers within 10 s, and
2. Sends one byte of body then stalls for `self.backoff + ε` seconds

will cause `self.backoff` to double each cycle it is selected. Because the reset only fires on a **successful** body read, consecutive Byzantine selections (or exclusive Byzantine reachability) produce unbounded growth: after ~30 stalls, `saturating_mul(2)` saturates at `Duration::MAX`, making the body timeout effectively infinite for all subsequent peers including honest ones.

---

### Impact Explanation

Once `self.backoff` saturates, the orchestrator's `get_peer_cup` call will block for `Duration::MAX` on every peer's body read — honest or not. The node cannot obtain a newer CUP, cannot detect a replica version change, and cannot execute an upgrade. The node is effectively partitioned from its subnet indefinitely until the orchestrator process is restarted (which resets `self.backoff` to `initial_backoff`). This directly compromises consensus participation and subnet liveness for the affected node.

---

### Likelihood Explanation

The attack is most reliable when the Byzantine peer is the only peer reachable by the victim (e.g., the victim is behind a network partition that leaves only the attacker's node reachable). In that case, the Byzantine peer is selected every cycle and the backoff doubles deterministically. With N honest peers also reachable, the Byzantine peer must be selected K consecutive times (probability `(1/N)^K`) before an honest peer resets the backoff; however, since the backoff doubles on each Byzantine selection and only resets (not decrements) on honest success, even intermittent Byzantine selections can push the backoff to hours or days before an honest reset occurs. The precondition — controlling one reachable peer — is within the fault model for unassigned/joining nodes.

---

### Recommendation

Add a maximum cap when updating `self.backoff`:

```rust
const MAX_BACKOFF: Duration = Duration::from_secs(300); // e.g. 5 minutes

// on timeout:
self.backoff = old_backoff.saturating_mul(2).min(MAX_BACKOFF);
```

This ensures that even after repeated Byzantine stalls, the body-read timeout for honest peers is bounded and the node can recover within a predictable window. [7](#0-6) 

---

### Proof of Concept

The existing test `test_fetch_catch_up_package_body_request_times_out` already demonstrates the doubling behavior: [8](#0-7) 

An extended test with two mock servers — one stalling, one honest — would confirm that after N stalling responses the honest server is contacted with a body timeout of `initial_backoff * 2^N`, not `initial_backoff`, violating the bounded-delay invariant.

### Citations

**File:** rs/orchestrator/src/catch_up_package_provider.rs (L114-116)
```rust
    backoff: Duration,
    initial_backoff: Duration,
    local_cup_reader: LocalCUPReader,
```

**File:** rs/orchestrator/src/catch_up_package_provider.rs (L195-197)
```rust
            // Try only one peer at-a-time if there is already a local CUP,
            (None, _) => 1,
        };
```

**File:** rs/orchestrator/src/catch_up_package_provider.rs (L331-341)
```rust
        let req = timeout(
            Duration::from_secs(10),
            client.request(
                Request::builder()
                    .method(Method::POST)
                    .header(hyper::header::CONTENT_TYPE, "application/cbor")
                    .uri(&url)
                    .body(Full::from(body))
                    .map_err(|e| format!("Failed to create request to {url}: {e:?}"))?,
            ),
        );
```

**File:** rs/orchestrator/src/catch_up_package_provider.rs (L349-349)
```rust
        let body_req = timeout(self.backoff, res.into_body().collect());
```

**File:** rs/orchestrator/src/catch_up_package_provider.rs (L353-354)
```rust
                // Reset backoff on success
                self.backoff = self.initial_backoff;
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

**File:** rs/orchestrator/src/catch_up_package_provider.rs (L794-832)
```rust
    #[tokio::test]
    async fn test_fetch_catch_up_package_body_request_times_out() {
        let send_cup = Arc::new(Mutex::new(false));
        let server_addr = start_server(TestService::SendBodyOrStall(send_cup.clone())).await;
        let url = format!("https://{server_addr}");
        let tmp_dir = tempfile::tempdir().unwrap();
        let node_id = node_test_id(1);

        let initial_backoff = Duration::from_secs(5);
        let mut cup_provider =
            make_cup_provider(tmp_dir.path().to_path_buf(), node_id, initial_backoff);

        let err = cup_provider
            .fetch_catch_up_package(&node_id, url.clone(), None)
            .await
            .expect_err("Expected timeout error when fetching CUP from slow server");

        assert!(
            err.contains("Timed out while reading CUP response body")
                && err.contains("after 5 secs: Elapsed(()). Setting backoff to 10 secs")
        );

        // Verify that the backoff was increased
        assert_eq!(cup_provider.backoff, Duration::from_secs(10));

        // Allow the next request to succeed
        *send_cup.lock().unwrap() = true;

        let cup = cup_provider
            .fetch_catch_up_package(&node_id, url, None)
            .await
            .expect("Expected to fetch the CUP successfully")
            .expect("Expected non-empty CUP");

        assert_eq!(cup, fake_cup());

        // Verify that the backoff was reset after a successful request
        assert_eq!(cup_provider.backoff, initial_backoff);
    }
```
