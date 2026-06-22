### Title
Unauthenticated `todo!()` Panic in `get_block` Hash-Only Branch Causes Rosetta Worker Crash When `RosettaBlocksMode::Enabled` — (`rs/rosetta-api/icp/src/request_handler.rs`)

---

### Summary

When the ICP Rosetta server is started with `RosettaBlocksMode::Enabled`, any unauthenticated HTTP client can send a `POST /block` request with a `PartialBlockIdentifier` containing only a `hash` (no `index`). If that hash does not resolve to a verified block, the server unconditionally executes `todo!("Fetching Rosetta Blocks by hash is not supported yet")`, which expands to `panic!()`. There is no `catch_unwind` anywhere in the actix-web handler chain, so the panic unwinds and crashes the worker thread. Repeated requests exhaust all workers and produce a complete denial of service.

---

### Finding Description

In `rs/rosetta-api/icp/src/request_handler.rs`, the `get_block` function matches on the shape of the incoming `PartialBlockIdentifier`. The hash-only branch (`index: None, hash: Some(hash)`) under `RosettaBlocksMode::Enabled` reads:

```rust
// lines 349-366
Some(PartialBlockIdentifier {
    index: None,
    hash: Some(hash),
}) => {
    if self.is_rosetta_blocks_mode_enabled().await {
        match self.get_verified_block_by_hash(&hash).await {
            Ok(block) => Ok(block),
            Err(ApiError::InvalidBlockId(_, _)) => {
                todo!("Fetching Rosetta Blocks by hash is not supported yet")
            }
            e => e,
        }
    } else {
        self.get_verified_block_by_hash(&hash).await
    }
}
``` [1](#0-0) 

`get_verified_block_by_hash` returns `Err(ApiError::InvalidBlockId(true, Default::default()))` whenever the supplied hash is not present in the verified-block store:

```rust
// lines 458-459
if !blocks.is_verified_by_hash(&hash)? {
    return Err(ApiError::InvalidBlockId(true, Default::default()));
}
``` [2](#0-1) 

That `InvalidBlockId` error is the exact variant matched by the `todo!()` arm, so any hash that is not in the verified-block store — including a hash of a Rosetta block, or any syntactically valid but non-existent 32-byte hex string — unconditionally panics.

The actix-web HTTP layer in `rosetta_server.rs` wraps handler results with `to_rosetta_response`, which only handles `Result` values; there is no `std::panic::catch_unwind` anywhere in the handler chain: [3](#0-2) [4](#0-3) 

The `rosetta-blocks` feature flag and `--enable-rosetta-blocks` CLI flag gate whether `enable_rosetta_blocks` is set to `true` at startup: [5](#0-4) [6](#0-5) 

When that flag is active, `RosettaBlocksMode::Enabled` is set in the ledger client, and the `todo!()` branch becomes reachable.

---

### Impact Explanation

`todo!()` expands to `panic!()`. In actix-web 4.x with a tokio runtime, a panic inside an async handler future — with no `catch_unwind` — unwinds the worker thread. actix-web will restart the crashed worker, but an attacker sending requests continuously can keep crashing workers faster than they restart, exhausting the worker pool and rendering the server completely unresponsive. All concurrent Rosetta clients (exchange integrations, monitoring, construction flows) are denied service for the duration of the attack.

---

### Likelihood Explanation

- **No authentication required**: the `/block` endpoint is a public, unauthenticated `POST`.
- **Trivial to trigger**: the attacker only needs to send a well-formed JSON body with `block_identifier: { hash: "<any 64-char hex string not matching a verified block>" }`. Any random hex string suffices.
- **Condition is operator-controlled but realistic**: the `rosetta-blocks` feature exists precisely to be enabled in production for exchange integrations that need Rosetta block semantics. Any operator who enables it is immediately exposed.
- **No rate limiting or input validation** prevents repeated requests.

---

### Recommendation

Replace the `todo!()` with a proper `ApiError` return:

```rust
Err(ApiError::InvalidBlockId(_, _)) => {
    Err(ApiError::invalid_request(
        "Fetching Rosetta Blocks by hash is not yet supported; provide a block index."
            .to_string(),
    ))
}
```

Additionally, add an integration test that asserts a graceful error (not a panic/500) is returned for hash-only block requests when `rosetta_blocks_mode=Enabled`.

---

### Proof of Concept

```bash
# Start Rosetta with rosetta-blocks enabled
rosetta-api --enable-rosetta-blocks ...

# Trigger the panic with any valid-format hash not in the verified store
curl -X POST http://localhost:8081/block \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
    "block_identifier": {
      "hash": "0000000000000000000000000000000000000000000000000000000000000000"
    }
  }'

# Expected (correct): {"code":700,"message":"Invalid block id","retriable":false}
# Actual: worker thread panics with "not yet implemented: Fetching Rosetta Blocks by hash is not supported yet"
# Repeated requests exhaust all actix-web workers → complete DoS
```

### Citations

**File:** rs/rosetta-api/icp/src/request_handler.rs (L349-366)
```rust
            Some(PartialBlockIdentifier {
                index: None,
                hash: Some(hash),
            }) => {
                if self.is_rosetta_blocks_mode_enabled().await {
                    // We cannot tell whether the hash is of a normal block
                    // or a Rosetta block so we need to try both sequentially
                    match self.get_verified_block_by_hash(&hash).await {
                        Ok(block) => Ok(block),
                        Err(ApiError::InvalidBlockId(_, _)) => {
                            todo!("Fetching Rosetta Blocks by hash is not supported yet")
                        }
                        e => e,
                    }
                } else {
                    self.get_verified_block_by_hash(&hash).await
                }
            }
```

**File:** rs/rosetta-api/icp/src/request_handler.rs (L455-459)
```rust
        let hash = convert::to_hash::<ic_ledger_core::block::EncodedBlock>(block_hash)?;
        let block = {
            let blocks = self.ledger.read_blocks().await;
            if !blocks.is_verified_by_hash(&hash)? {
                return Err(ApiError::InvalidBlockId(true, Default::default()));
```

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L58-68)
```rust
#[post("/block")]
async fn block(
    msg: web::Json<BlockRequest>,
    req_handler: web::Data<RosettaRequestHandler>,
) -> HttpResponse {
    let _timer = req_handler
        .rosetta_metrics()
        .start_request_duration_timer("block");
    let res = req_handler.block(msg.into_inner()).await;
    to_rosetta_response(res, &req_handler.rosetta_metrics())
}
```

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L220-252)
```rust
fn to_rosetta_response<S: serde::Serialize>(
    result: Result<S, ApiError>,
    rosetta_metrics: &RosettaMetrics,
) -> HttpResponse {
    match result {
        Ok(x) => match serde_json::to_string(&x) {
            Ok(resp) => {
                rosetta_metrics.inc_api_status_count("200");
                HttpResponse::Ok()
                    .content_type("application/json")
                    .body(resp)
            }
            Err(e) => {
                internal_error_response(e, Error::serialization_error_json_str(), rosetta_metrics)
            }
        },
        Err(api_err) => {
            let converted = errors::convert_to_error(&api_err);
            match serde_json::to_string(&converted) {
                Ok(resp) => {
                    let err_code = format!("{}", converted.0.code);
                    rosetta_metrics.inc_api_status_count(&err_code);
                    internal_error_response(converted, resp, rosetta_metrics)
                }
                Err(e) => internal_error_response(
                    e,
                    Error::serialization_error_json_str(),
                    rosetta_metrics,
                ),
            }
        }
    }
}
```

**File:** rs/rosetta-api/icp/src/main.rs (L234-237)
```rust
    #[cfg(feature = "rosetta-blocks")]
    #[clap(long = "enable-rosetta-blocks")]
    enable_rosetta_blocks: bool,
}
```

**File:** rs/rosetta-api/icp/src/main.rs (L365-371)
```rust
    #[allow(unused_mut)]
    #[allow(unused_assignments)]
    let mut enable_rosetta_blocks = false;
    #[cfg(feature = "rosetta-blocks")]
    {
        enable_rosetta_blocks = opt.enable_rosetta_blocks;
    }
```
