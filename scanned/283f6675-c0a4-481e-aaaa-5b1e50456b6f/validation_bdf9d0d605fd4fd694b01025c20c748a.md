I now have all the information needed to make a definitive determination. Let me trace the full path.

**1. The `Add` implementation for `StreamIndex` (`AmountOf<StreamIndexTag, u64>`):** [1](#0-0) 

The `Add` impl delegates directly to `self.0 + rhs.0` — plain Rust `u64` addition with **no overflow protection**. In debug builds this panics; in release builds it wraps silently.

**2. The vulnerable expression:** [2](#0-1) 

With `msg_limit = Some(usize::MAX)` and `msg_from = StreamIndex::new(1)`:
- `n as u64 = u64::MAX` (on 64-bit)
- `StreamIndex::new(1) + StreamIndex::new(u64::MAX)` = `StreamIndex::new(0)` (wrapping in release)
- The filter `end <= &stream.messages_end()` checks `0 <= messages_end()` — **always true** — so the guard does NOT catch the overflow
- Result: `to = 0`, but `msg_from = 1`, violating `to >= msg_from`

In general, for any `msg_from > 0`: `msg_from + u64::MAX` wraps to `msg_from - 1`, which is always `< msg_from` and always `<= messages_end()`, so the filter always passes the corrupted value through.

**3. The attacker-controlled entrypoint:** [3](#0-2) 

The XNet HTTP handler parses `msg_limit` from a URL query parameter as `u64` then casts to `usize` with no upper-bound validation. Any client that can reach the XNet port can send `?msg_limit=18446744073709551615`.

**4. No authentication on the XNet endpoint:** [4](#0-3) 

`route_request` performs no authentication before calling `encode_certified_stream_slice`. The XNet endpoint is designed to be publicly readable (responses are certified), so any network-reachable client is an unprivileged attacker.

---

### Title
Integer overflow in `encode_certified_stream_slice` via `msg_limit=usize::MAX` bypasses `messages_end()` guard, producing inverted slice bounds — (`rs/state_manager/src/lib.rs`)

### Summary
An unprivileged HTTP client can send a crafted XNet stream request with `msg_limit=18446744073709551615` to trigger a u64 wrapping overflow in `msg_from + StreamIndex::new(n as u64)`. The subsequent `filter(|end| end <= &stream.messages_end())` guard fails to detect the wrapped value because the result (`msg_from - 1`) is always ≤ `messages_end()`. The corrupted `to` index (`< msg_from`) is then passed into `encode_stream_slice` and `stream_slice_partial_tree`, violating the invariant `msg_from <= to`.

### Finding Description
In `StateManagerImpl::encode_certified_stream_slice` (`rs/state_manager/src/lib.rs:4067–4070`):

```rust
let to = msg_limit
    .map(|n| msg_from + StreamIndex::new(n as u64))   // overflow here
    .filter(|end| end <= &stream.messages_end())        // guard bypassed
    .unwrap_or_else(|| stream.messages_end());
```

`StreamIndex` is `AmountOf<StreamIndexTag, u64>`, whose `Add` impl uses plain `self.0 + rhs.0` with no overflow check. In a release build, `msg_from.get() + u64::MAX` wraps to `msg_from.get() - 1`. Since `msg_from - 1 <= messages_end()` is always true when `msg_from > 0`, the filter passes the corrupted value. The XNet HTTP handler (`rs/http_endpoints/xnet/src/lib.rs:398`) accepts `msg_limit` as a raw `u64` query parameter and casts it to `usize` without any upper-bound cap.

### Impact Explanation
- **Release build (wrapping):** `to = msg_from - 1 < msg_from` is passed to `encode_stream_slice` and `stream_slice_partial_tree`. Downstream code that asserts `msg_from <= to` will panic (node DoS for that request handler thread). If no assertion exists, an empty or malformed certified slice is returned, corrupting the XNet payload builder's view of the stream.
- **Debug build / `overflow-checks = true`:** Unconditional panic at the addition site — DoS of the XNet endpoint worker.

### Likelihood Explanation
The XNet HTTP endpoint is unauthenticated for reads and network-reachable by any client that can connect to the XNet port. The trigger requires a single crafted HTTP GET request with one query parameter set to `u64::MAX`. No privileged access, key material, or subnet-majority corruption is needed.

### Recommendation
Cap `msg_limit` before the arithmetic, or use saturating/checked addition:

```rust
// Option A: saturating add
.map(|n| msg_from.saturating_add(StreamIndex::new(n as u64)))

// Option B: cap n to the available message count before adding
.map(|n| {
    let available = (stream.messages_end() - msg_from).get() as usize;
    msg_from + StreamIndex::new(n.min(available) as u64)
})
```

Additionally, the XNet HTTP handler should cap `msg_limit` to a reasonable maximum (e.g., `POOL_SLICE_BYTE_SIZE_MAX / min_message_size`) before passing it into `encode_certified_stream_slice`.

### Proof of Concept
```rust
// In a unit test for StateManagerImpl:
// Stream: messages_begin=0, messages_end=10
// Request: msg_begin=Some(1), msg_limit=Some(usize::MAX)
//
// Expected (correct): to = messages_end() = 10
// Actual (buggy release build): 
//   n as u64 = u64::MAX
//   1u64 + u64::MAX = 0  (wrapping)
//   0 <= 10  → filter passes
//   to = 0  <  msg_from = 1  → INVARIANT VIOLATED
//
// Downstream encode_stream_slice(state, h, subnet, msg_from=1, to=0, ...)
// → panic or empty/malformed slice
```

### Citations

**File:** rs/phantom_newtype/src/amountof.rs (L290-298)
```rust
impl<Unit, Repr> Add for AmountOf<Unit, Repr>
where
    Repr: Add<Output = Repr>,
{
    type Output = Self;
    fn add(self, rhs: Self) -> Self {
        Self(self.0 + rhs.0, PhantomData)
    }
}
```

**File:** rs/state_manager/src/lib.rs (L4067-4070)
```rust
        let to = msg_limit
            .map(|n| msg_from + StreamIndex::new(n as u64))
            .filter(|end| end <= &stream.messages_end())
            .unwrap_or_else(|| stream.messages_end());
```

**File:** rs/http_endpoints/xnet/src/lib.rs (L356-425)
```rust
/// Routes an `XNetEndpoint` request to the appropriate handler; or produces an
/// HTTP 404 Not Found response if the URL doesn't match any handler.
fn route_request(
    url: Url,
    certified_stream_store: &impl CertifiedStreamStore,
    metrics: &XNetEndpointMetrics,
) -> Response<Body> {
    let since = Instant::now();
    let mut resource = RESOURCE_ERROR;
    let response = match url.path() {
        API_URL_STREAMS => {
            resource = RESOURCE_STREAMS;
            handle_streams(certified_stream_store, metrics)
        }

        stream_url if stream_url.starts_with(API_URL_STREAM_PREFIX) => {
            resource = RESOURCE_STREAM;
            let subnet_id_str = &stream_url[API_URL_STREAM_PREFIX.len()..];
            let subnet_id = match PrincipalId::from_str(subnet_id_str) {
                Ok(subnet_id) => SubnetId::from(subnet_id),
                Err(_) => {
                    return bad_request(format!(
                        "Invalid subnet ID: {subnet_id_str} in {stream_url}"
                    ));
                }
            };

            let mut witness_begin = None;
            let mut msg_begin = None;
            let mut msg_limit = None;
            let mut byte_limit = None;
            for (param, value) in url.query_pairs() {
                let value = match value.parse::<u64>() {
                    Ok(v) => v,
                    Err(_) => {
                        return bad_request(format!("Invalid query param: {param}"));
                    }
                };
                match param.as_ref() {
                    "witness_begin" => witness_begin = Some(StreamIndex::new(value)),
                    "index" => msg_begin = Some(StreamIndex::new(value)),
                    "msg_begin" => msg_begin = Some(StreamIndex::new(value)),
                    "msg_limit" => msg_limit = Some(value as usize),
                    "byte_limit" => byte_limit = Some(value as usize),
                    _ => {
                        return bad_request(format!("Unexpected query param: {param}"));
                    }
                }
            }

            handle_stream(
                subnet_id,
                witness_begin,
                msg_begin,
                msg_limit,
                byte_limit,
                certified_stream_store,
                metrics,
            )
        }

        _ => not_found("Not Found"),
    };
    metrics
        .request_duration
        .with_label_values(&[resource, response.status().as_str()])
        .observe(since.elapsed().as_secs_f64());

    response
}
```
