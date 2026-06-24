The code confirms the claim exactly. All cited line numbers and code snippets match the repository.

Audit Report

## Title
Unprivileged Caller Can Trap `get_block_pb` on Any Archive Node With Non-Zero Offset via `checked_sub().unwrap()` Panic — (`rs/ledger_suite/icp/archive/src/main.rs`)

## Summary
`get_block` at line 255 of `rs/ledger_suite/icp/archive/src/main.rs` performs `block_height.checked_sub(block_height_offset()).unwrap()` with no prior bounds check. The public query endpoint `get_block_pb` (line 262) has no caller guard, so any anonymous principal can supply a `block_height` below the node's offset and deterministically trap the query. Every ICP ledger archive node after the first has a non-zero offset, making this condition trivially met on mainnet.

## Finding Description
`get_block` (lines 254–260) unconditionally calls `.unwrap()` on the result of `checked_sub`:

```rust
fn get_block(block_height: BlockIndex) -> BlockRes {
    let adjusted_height = block_height.checked_sub(block_height_offset()).unwrap();
    ...
}
```

`get_block_pb` (lines 262–270) is a raw `canister_query` export with no caller restriction; it decodes the argument and calls `get_block` directly. When `block_height < block_height_offset()`, `checked_sub` returns `None` and `.unwrap()` causes a Wasm trap.

All three sibling endpoints handle this correctly:
- `get_blocks_pb` (lines 332–348) validates the requested range against `local_blocks_range` and returns a structured error string before any subtraction.
- `read_encoded_blocks` (lines 476–484), used by `get_blocks` and `get_encoded_blocks`, returns `Err(GetBlocksError::BadFirstBlockIndex{...})` when `start < block_range.start`.
- The ICRC1 archive's `get_transaction` (lines 263–270 of `rs/ledger_suite/icrc1/archive/src/main.rs`) uses `(idx_offset <= index).then_some(index - idx_offset)?` to return `None` gracefully.

Only `get_block_pb` is missing the bounds check.

## Impact Explanation
This matches **High ($2,000–$10,000): Significant ledger/Rosetta/financial-integration security impact with concrete user or protocol harm.** Clients such as Rosetta, the index canister, and the CMC that call `get_block_pb` without pre-filtering by archive node range receive a trap response instead of a graceful `BlockRes(None)` or structured error for any height below the node's offset. This breaks the expected contract of the endpoint and can cause these financial-integration clients to fail block retrieval silently or propagate errors upstream. The canister's state is unaffected (query traps do not mutate state), but the endpoint is rendered unreliable for any caller that does not already know the exact offset of each archive node.

## Likelihood Explanation
The precondition is trivially met on mainnet: every ICP ledger archive node after the first has a non-zero `block_height_offset`. The attacker input is a single protobuf-encoded `u64` sent as an anonymous query — no cycles, no authentication, no prior state required. The exploit is deterministic, repeatable, and locally reproducible.

## Recommendation
Replace the bare `.unwrap()` with an explicit bounds check, mirroring the pattern already used in `read_encoded_blocks`:

```rust
fn get_block(block_height: BlockIndex) -> BlockRes {
    let offset = block_height_offset();
    let adjusted_height = match block_height.checked_sub(offset) {
        Some(h) => h,
        None => return BlockRes(None),
    };
    BlockRes(get_block_stable(adjusted_height).map(Ok))
}
```

This matches the behavior of `get_blocks_pb` and the ICRC1 archive's `get_transaction`.

## Proof of Concept
1. Install the ICP archive canister with `block_height_offset = 500`.
2. Append at least one block so the canister is live.
3. As anonymous principal, call `get_block_pb` with `block_height = 499` (protobuf-encoded `u64`).
4. Observe: canister traps instead of returning `BlockRes(None)`.
5. Call `get_block_pb` with `block_height = 500` — returns the block normally.
6. Fuzz all `u64` values in `[0, 499]`: every call traps deterministically.
7. Fuzz all `u64` values in `[500, 500 + blocks_len)`: no trap.