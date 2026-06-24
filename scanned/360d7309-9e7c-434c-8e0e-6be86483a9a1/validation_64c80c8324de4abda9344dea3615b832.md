### Title
Unauthenticated `todo!()` Panic via `/block` Hash Lookup in RosettaBlocksMode::Enabled — (`rs/rosetta-api/icp/src/request_handler.rs`)

### Summary

When the ICP Rosetta node is configured with `RosettaBlocksMode::Enabled`, any unauthenticated HTTP client can trigger a `todo!()` panic by sending a `POST /block` request with only a `hash` field that does not match any verified (non-Rosetta) ICP block. This violates the invariant that a public API server must never panic on well-formed client input.

### Finding Description

The call chain is fully confirmed in production code:

**Step 1 — Public HTTP endpoint, no authentication:** [1](#0-0) 

The `#[post("/block")]` handler is registered with no authentication middleware and is reachable by any network client.

**Step 2 — Routing to the vulnerable branch:** [2](#0-1) 

When `PartialBlockIdentifier { index: None, hash: Some(h) }` is received and `is_rosetta_blocks_mode_enabled()` returns `true`, the code calls `get_verified_block_by_hash(&hash)`. If the hash does not match any verified ICP block (which is trivially achievable — the attacker can supply any 32-byte hex string), the function returns `Err(ApiError::InvalidBlockId(...))`, and the match arm at line 358–360 executes `todo!("Fetching Rosetta Blocks by hash is not supported yet")`.

In Rust, `todo!()` expands to `panic!()`. In actix-web async handlers, this panic is not caught by the framework's default configuration and crashes the worker thread. Repeated triggering can destabilize or terminate the Rosetta process.

### Impact Explanation

The Rosetta node is a single-replica process. A panic in an actix-web async task crashes the worker thread. An attacker who can reach the HTTP port can send this request in a tight loop, causing continuous worker crashes and rendering the Rosetta API unavailable (denial of service). This is a process-level impact scoped to the single Rosetta replica.

### Likelihood Explanation

- No authentication or authorization is required.
- The request is a standard, well-formed Rosetta API call.
- The triggering condition (hash not matching a verified block) is trivially satisfied with any random hash.
- `RosettaBlocksMode::Enabled` is a real, documented production configuration option. [3](#0-2) 

### Recommendation

Replace the `todo!()` with a proper `ApiError` return, for example:

```rust
Err(ApiError::InvalidBlockId(_, _)) => {
    Err(ApiError::invalid_request(
        "Fetching Rosetta Blocks by hash is not yet supported".to_string()
    ))
}
```

This converts the unimplemented path into a graceful HTTP error response instead of a panic.

### Proof of Concept

```rust
// Construct a BlockRequest with only a hash field (no index)
// pointing to a hash that does not exist in the verified blocks store.
// With RosettaBlocksMode::Enabled active, this triggers:
//   get_verified_block_by_hash(&hash) -> Err(ApiError::InvalidBlockId)
//   -> todo!() -> panic!() -> worker thread crash

POST /block HTTP/1.1
Content-Type: application/json

{
  "network_identifier": { ... },
  "block_identifier": {
    "hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
}
```

The `todo!()` at `request_handler.rs:359` fires unconditionally whenever the hash is not found in the verified blocks store while `RosettaBlocksMode::Enabled` is active. [4](#0-3)

### Citations

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

**File:** rs/rosetta-api/icp/src/request_handler.rs (L349-362)
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
```

**File:** rs/rosetta-api/icp/ledger_canister_blocks_synchronizer/src/blocks.rs (L1-15)
```rust
use crate::rosetta_block::RosettaBlock;
use crate::{iso8601_to_timestamp, timestamp_to_iso8601};
use ic_ledger_canister_core::ledger::LedgerTransaction;
use ic_ledger_core::block::{BlockIndex, BlockType, EncodedBlock};
use ic_ledger_core::tokens::CheckedAdd;
use ic_ledger_hash_of::HashOf;
use icp_ledger::{AccountIdentifier, Block, TimeStamp, Tokens, Transaction};
use rusqlite::{CachedStatement, OptionalExtension, Row, named_params, params};
use serde::{Deserialize, Serialize};
use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap};
use std::convert::TryInto;
use std::path::Path;
use std::sync::Mutex;
use tracing::info;
```
