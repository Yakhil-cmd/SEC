The `todo!()` at line 359 is concretely reachable. Here is the full trace and assessment.

---

### Title
Unprivileged Client Can Trigger Unconditional `todo!()` Panic via `/block` Hash-Only Request in ICP Rosetta Node — (`rs/rosetta-api/icp/src/request_handler.rs`)

### Summary

When the ICP Rosetta node runs with `RosettaBlocksMode::Enabled`, any unauthenticated HTTP client can crash the actix-web worker thread by sending a `/block` POST request containing only a block hash (no index). The handler unconditionally hits a `todo!()` macro whenever the supplied hash does not match a verified block, causing a panic.

### Finding Description

**Exact code path:**

**Step 1** — The public `/block` actix-web handler in `rosetta_server.rs` (line 58–68) accepts any POST with a valid JSON body and delegates to `req_handler.block()`: [1](#0-0) 

**Step 2** — `block()` in `request_handler.rs` (line 298–305) calls `get_block(Some(msg.block_identifier))`. When the client sends `{ "block_identifier": { "hash": "<hex>" } }` with no `index` field, `block_identifier` is `PartialBlockIdentifier { index: None, hash: Some(hash) }`: [2](#0-1) 

**Step 3** — Inside `get_block()`, the `index: None, hash: Some(hash)` arm is matched (lines 349–366). When `is_rosetta_blocks_mode_enabled()` returns `true`, `get_verified_block_by_hash(&hash)` is called: [3](#0-2) 

**Step 4** — `get_verified_block_by_hash()` (lines 451–465) calls `blocks.is_verified_by_hash(&hash)?`. For any hash not present in the SQLite DB, `get_block_idx_by_block_hash` returns `BlockStoreError::NotFound(...)`, which propagates via `?` and is converted by `From<BlockStoreError> for ApiError` (errors.rs line 111–113) to `ApiError::InvalidBlockId(false, ...)`: [4](#0-3) [5](#0-4) 

**Step 5** — Back in `get_block()`, the returned `Err(ApiError::InvalidBlockId(false, ...))` matches the arm at line 358, and `todo!()` panics unconditionally: [6](#0-5) 

There is **no guard** between the attacker-controlled hash and the `todo!()`. The only precondition is that `RosettaBlocksMode::Enabled` is active, which is a documented, supported deployment configuration.

### Impact Explanation

`todo!()` expands to `panic!()`. In actix-web, a panic in an async handler propagates through the tokio task. actix-web worker threads that panic are restarted by the supervisor, but:

- The panicking request receives no response (connection dropped).
- The worker thread is torn down and restarted, briefly reducing server capacity.
- An attacker sending a continuous stream of such requests can keep all worker threads in a crash-restart cycle, effectively denying service to legitimate Rosetta API users (exchange integrations, wallets, tooling relying on the ICP Rosetta node).

### Likelihood Explanation

- No authentication is required.
- The request body is trivial: any 64-character hex string that is not a real block hash suffices.
- `RosettaBlocksMode::Enabled` is a production configuration (the `rosetta_blocks` SQLite table is created when the feature is enabled).
- The bug is triggered on every such request without any rate limiting visible in the handler.

### Recommendation

Replace the `todo!()` with a proper error return, e.g.:

```rust
Err(ApiError::InvalidBlockId(false, "Fetching Rosetta Blocks by hash is not yet supported".into()))
```

Or implement the missing Rosetta-block-by-hash lookup. The comment at line 354–355 already acknowledges the ambiguity; the correct fix is to attempt a Rosetta block hash lookup and return `InvalidBlockId` if neither lookup succeeds.

### Proof of Concept

```bash
# Assumes Rosetta node running with RosettaBlocksMode::Enabled on localhost:8080
curl -s -X POST http://localhost:8080/block \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
    "block_identifier": {
      "hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  }'
# Expected: worker thread panics with "not yet implemented: Fetching Rosetta Blocks by hash is not supported yet"
# Server logs show panic; worker restarts; repeating the request reproduces the panic indefinitely.
```

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

**File:** rs/rosetta-api/icp/src/request_handler.rs (L298-305)
```rust
    pub async fn block(&self, msg: models::BlockRequest) -> Result<BlockResponse, ApiError> {
        verify_network_id(self.ledger.ledger_canister_id(), &msg.network_identifier)?;
        let block = self.get_block(Some(msg.block_identifier)).await?;
        Ok(BlockResponse {
            block: Some(block),
            other_transactions: None,
        })
    }
```

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

**File:** rs/rosetta-api/icp/src/request_handler.rs (L451-465)
```rust
    async fn get_verified_block_by_hash(
        &self,
        block_hash: &str,
    ) -> Result<rosetta_core::objects::Block, ApiError> {
        let hash = convert::to_hash::<ic_ledger_core::block::EncodedBlock>(block_hash)?;
        let block = {
            let blocks = self.ledger.read_blocks().await;
            if !blocks.is_verified_by_hash(&hash)? {
                return Err(ApiError::InvalidBlockId(true, Default::default()));
            }
            let block_index = blocks.get_block_idx_by_block_hash(&hash)?;
            blocks.get_hashed_block(&block_index)
        }?;
        self.hashed_block_to_rosetta_core_block(block).await
    }
```

**File:** rs/rosetta-api/icp/src/errors.rs (L108-120)
```rust
impl From<BlockStoreError> for ApiError {
    fn from(e: BlockStoreError) -> Self {
        match e {
            BlockStoreError::NotFound(idx) => {
                ApiError::invalid_block_id(format!("Block not found: {idx}"))
            }
            BlockStoreError::NotAvailable(idx) => {
                ApiError::invalid_block_id(format!("Block not available for query: {idx}"))
            }
            BlockStoreError::Other(msg) => ApiError::internal_error(msg),
        }
    }
}
```
