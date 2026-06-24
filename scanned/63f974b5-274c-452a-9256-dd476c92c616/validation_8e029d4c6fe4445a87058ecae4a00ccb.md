### Title
Unbounded `Vec<GetBlocksRequest>` Input Enables Instruction-Budget Exhaustion DoS on ICRC-1 Ledger and Archive Query Endpoints - (File: rs/ledger_suite/icrc1/ledger/src/lib.rs, rs/ledger_suite/icrc1/archive/src/main.rs)

---

### Summary

The `icrc3_get_blocks` query endpoint on both the production ICRC-1 ledger canister and the ICRC-1 archive canister accepts a `Vec<GetBlocksRequest>` with no limit on the number of elements. The early-exit guard only fires when 100 *local* blocks have been accumulated. An unprivileged caller can trivially bypass this guard by supplying an arbitrarily large vector of zero-length or archive-only requests, forcing the canister to iterate over every element and perform per-element work (archive-range lookups, block-range decoding setup) until the per-query instruction limit is exhausted. Because query calls on the Internet Computer are free to the caller, this can be repeated at no cost to the attacker.

---

### Finding Description

**ICRC-1 Ledger — `rs/ledger_suite/icrc1/ledger/src/lib.rs`**

`icrc3_get_blocks` iterates over every element of the caller-supplied `args: Vec<GetBlocksRequest>`:

```rust
pub fn icrc3_get_blocks(&self, args: Vec<GetBlocksRequest>) -> GetBlocksResult {
    const MAX_BLOCKS_PER_RESPONSE: u64 = 100;
    let mut blocks = vec![];
    let mut archived_blocks_by_callback = BTreeMap::new();
    for arg in args {                                   // ← no bound on args.len()
        ...
        let max_length = MAX_BLOCKS_PER_RESPONSE.saturating_sub(blocks.len() as u64);
        if max_length == 0 { break; }                  // ← only exits when 100 LOCAL blocks collected
        let length = max_length.min(length)...;
        let (first_index, local_blocks, archived_ranges) = self.query_blocks(start, length, ...);
        ...
        if blocks.len() as u64 >= MAX_BLOCKS_PER_RESPONSE { break; }
    }
``` [1](#0-0) 

The guard `if max_length == 0 { break; }` is only reached when `blocks.len() == 100`. If every request in the vector has `length = 0`, or if every request targets archived blocks (so no local blocks are ever appended), `blocks.len()` never reaches 100 and the loop runs to completion over the entire input vector, calling `self.query_blocks()` for each element.

The public query handler delegates directly with no additional validation:

```rust
#[query]
fn icrc3_get_blocks(args: Vec<GetBlocksRequest>) -> GetBlocksResult {
    Access::with_ledger(|ledger| ledger.icrc3_get_blocks(args))
}
``` [2](#0-1) 

**ICRC-1 Archive — `rs/ledger_suite/icrc1/archive/src/main.rs`**

The archive canister has the identical pattern:

```rust
#[query]
fn icrc3_get_blocks(reqs: Vec<GetBlocksRequest>) -> GetBlocksResult {
    const MAX_BLOCKS_PER_RESPONSE: u64 = 100;
    let mut blocks = vec![];
    for req in reqs {                                   // ← no bound on reqs.len()
        ...
        let max_length = MAX_BLOCKS_PER_RESPONSE.saturating_sub(blocks.len() as u64);
        if max_length == 0 { break; }
        let length = length.min(max_length);
        let decoded_block_range = decode_block_range(start, length, decode_icrc1_block);
        ...
    }
``` [3](#0-2) 

With `length = 0` per request, `decode_block_range` is invoked for every element but returns an empty iterator, so `blocks.len()` never grows and the loop never exits early.

**Contrast with the read_state endpoint**, which correctly enforces `MAXIMUM_NUMBER_OF_PATHS = 1000` in the validator before any path processing occurs: [4](#0-3) 

No equivalent guard exists for `icrc3_get_blocks`.

---

### Impact Explanation

On the Internet Computer, query calls are **free to the caller** — no cycles are charged. Each query call is bounded by the replica's per-query instruction limit (~5 billion instructions). An attacker who sends a `Vec<GetBlocksRequest>` with, e.g., 500,000 zero-length entries forces the canister to iterate over all 500,000 elements and invoke `query_blocks` / `decode_block_range` for each, exhausting the instruction budget and causing the query to trap. Because the call is free, the attacker can fire this in a tight loop from any identity, saturating the replica threads that serve query calls for the ledger and archive canisters. Legitimate users querying block history (e.g., wallets, explorers, index canisters) experience timeouts or failures. The ICRC-1 ledger and its archives are production system canisters serving the entire IC token ecosystem.

---

### Likelihood Explanation

The `icrc3_get_blocks` endpoint is publicly callable by any anonymous or authenticated principal with no authentication requirement. The ICRC-3 standard explicitly defines the input as `vec GetBlocksRequest`, so tooling and clients already know how to construct the call. No special knowledge, privilege, or on-chain stake is required. The attack requires only a standard IC agent and a loop.

---

### Recommendation

1. **Enforce a hard cap on the number of request items** before entering the loop, analogous to the `MAXIMUM_NUMBER_OF_PATHS` check in the read_state validator:

```rust
const MAX_REQUESTS_PER_CALL: usize = 100; // or a similarly small constant
if args.len() > MAX_REQUESTS_PER_CALL {
    ic_cdk::api::trap("Too many block ranges requested");
}
```

Apply this cap at the top of `icrc3_get_blocks` in both `rs/ledger_suite/icrc1/ledger/src/lib.rs` and `rs/ledger_suite/icrc1/archive/src/main.rs`.

2. **Charge the early-exit condition against total *requests processed*, not only local blocks collected**, so that a flood of zero-length or archive-only requests also triggers an exit.

---

### Proof of Concept

Using any IC agent (e.g., `ic-agent` in Rust or `@dfinity/agent` in JS), send a query call to the ICRC-1 ledger's `icrc3_get_blocks` method with a vector of 500,000 zero-length requests:

```python
# Pseudocode using ic-py or similar
requests = [{"start": 0, "length": 0}] * 500_000
agent.query(ICRC1_LEDGER_CANISTER_ID, "icrc3_get_blocks", encode(requests))
```

The canister iterates over all 500,000 elements, calling `query_blocks(0, 0, ...)` each time. The query exhausts the instruction limit and traps. Repeating this in a loop from multiple clients saturates the replica's query-serving threads for the ledger canister, denying service to legitimate callers. The same call can be directed at any ICRC-1 archive canister ID obtained from `icrc3_get_archives`.

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L1107-1153)
```rust
    pub fn icrc3_get_blocks(&self, args: Vec<GetBlocksRequest>) -> GetBlocksResult {
        const MAX_BLOCKS_PER_RESPONSE: u64 = 100;

        let mut blocks = vec![];
        let mut archived_blocks_by_callback = BTreeMap::new();
        for arg in args {
            let (start, length) = arg
                .as_start_and_length()
                .unwrap_or_else(|msg| ic_cdk::api::trap(&msg));
            let max_length = MAX_BLOCKS_PER_RESPONSE.saturating_sub(blocks.len() as u64);
            if max_length == 0 {
                break;
            }
            let length = max_length.min(length).min(usize::MAX as u64) as usize;
            let (first_index, local_blocks, archived_ranges) = self.query_blocks(
                start,
                length,
                |block| ICRC3Value::from(encoded_block_to_generic_block(block)),
                |canister_id| {
                    QueryArchiveFn::<Vec<GetBlocksRequest>, GetBlocksResult>::new(
                        canister_id,
                        "icrc3_get_blocks",
                    )
                },
            );
            for (id, block) in (first_index..).zip(local_blocks) {
                blocks.push(icrc_ledger_types::icrc3::blocks::BlockWithId {
                    id: Nat::from(id),
                    block,
                });
            }
            for ArchivedRange {
                start,
                length,
                callback,
            } in archived_ranges
            {
                let request = GetBlocksRequest { start, length };
                archived_blocks_by_callback
                    .entry(callback)
                    .or_insert(vec![])
                    .push(request);
            }
            if blocks.len() as u64 >= MAX_BLOCKS_PER_RESPONSE {
                break;
            }
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1171-1174)
```rust
#[query]
fn icrc3_get_blocks(args: Vec<GetBlocksRequest>) -> GetBlocksResult {
    Access::with_ledger(|ledger| ledger.icrc3_get_blocks(args))
}
```

**File:** rs/ledger_suite/icrc1/archive/src/main.rs (L359-390)
```rust
#[query]
fn icrc3_get_blocks(reqs: Vec<GetBlocksRequest>) -> GetBlocksResult {
    const MAX_BLOCKS_PER_RESPONSE: u64 = 100;

    let mut blocks = vec![];
    for req in reqs {
        let mut id = req.start.clone();
        let (start, length) = req
            .as_start_and_length()
            .unwrap_or_else(|msg| ic_cdk::api::trap(&msg));
        let max_length = MAX_BLOCKS_PER_RESPONSE.saturating_sub(blocks.len() as u64);
        if max_length == 0 {
            break;
        }
        let length = length.min(max_length);
        let decoded_block_range = decode_block_range(start, length, decode_icrc1_block);
        for block in decoded_block_range {
            blocks.push(BlockWithId {
                id: id.clone(),
                block: ICRC3Value::from(block),
            });
            id += 1_u64;
        }
    }
    GetBlocksResult {
        // We return the local log length because the archive
        // knows only about its local blocks.
        log_length: candid::Nat::from(with_blocks(|blocks| blocks.len())),
        blocks,
        archived_blocks: vec![],
    }
}
```

**File:** rs/validator/src/ingress_validation.rs (L178-194)
```rust
fn validate_paths_width_and_depth(paths: &[Path]) -> Result<(), RequestValidationError> {
    if paths.len() > MAXIMUM_NUMBER_OF_PATHS {
        return Err(TooManyPaths {
            maximum: MAXIMUM_NUMBER_OF_PATHS,
            length: paths.len(),
        });
    }
    for path in paths {
        if path.len() > MAXIMUM_NUMBER_OF_LABELS_PER_PATH {
            return Err(PathTooLong {
                maximum: MAXIMUM_NUMBER_OF_LABELS_PER_PATH,
                length: path.len(),
            });
        }
    }
    Ok(())
}
```
