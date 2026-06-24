Let me check the `check_merkle_root` and `compute_merkle_root` behavior for empty txdata, and also look at how the block sync queue is populated to understand if re-requesting is possible.

### Title
Malicious Peer Can Permanently Poison Block Cache With Empty-txdata Block, Causing ckBTC to Miss Deposits — (`rs/bitcoin/adapter/src/blockchainmanager.rs`, `rs/bitcoin/adapter/src/blockchainstate.rs`)

---

### Summary

`received_block_message` removes a block hash from `getdata_request_info` on **first receipt**, without verifying the responding peer is the one the `getdata` was sent to. Simultaneously, `add_block` skips merkle root validation when `txdata` is empty (because `compute_merkle_root()` returns `None` for an empty transaction list). A connected malicious peer can race the legitimate peer, inject a crafted block with a valid header but `txdata=[]`, permanently occupy the cache slot for that block hash, and cause the ckBTC canister to process a block with zero transactions — silently dropping all Bitcoin deposits in that block.

---

### Finding Description

**Step 1 — No per-peer validation in `received_block_message`.** [1](#0-0) 

The only peer check is whether the sender is *any* known peer (`peer_info.contains_key(addr)`). There is no check that `addr` matches the `socket` field recorded in `getdata_request_info` for that block hash. Any connected peer can respond to a `getdata` that was addressed to a different peer.

**Step 2 — Merkle root check is bypassed for empty `txdata`.** [2](#0-1) 

The guard is `block.compute_merkle_root().is_some() && !block.check_merkle_root()`. For a block with `txdata = []`, `compute_merkle_root()` returns `None` (no transactions to hash), so the entire condition evaluates to `false` and the check is skipped. The block is serialized and inserted into the cache unconditionally.

**Step 3 — The codebase itself confirms empty-txdata blocks are accepted.** [3](#0-2) 

The existing test adds `Block { header: h3, txdata: Vec::new() }` and calls `.unwrap()` — proving the path succeeds in production code.

**Step 4 — Once cached, the block is never re-fetched.** [4](#0-3) 

`block_sync_queue` only enqueues a hash when it is absent from both `getdata_request_info` and the block cache. After the crafted block occupies the cache slot, the legitimate block is never requested again.

---

### Impact Explanation

The ckBTC canister consumes blocks from the adapter's block cache. A block delivered with `txdata=[]` contains no transactions. Every Bitcoin deposit (UTXO) included in the real block is invisible to the canister for that block height. Affected deposits are permanently lost from the ckBTC perspective unless the canister has an independent re-scan mechanism (it does not — it relies entirely on the adapter's block stream). The attacker can repeat this for every block the adapter requests, effectively halting all ckBTC minting.

---

### Likelihood Explanation

- **Attacker entry point**: connecting as a Bitcoin peer to the adapter — this is the normal Bitcoin P2P protocol, requiring no credentials.
- **Knowledge of target hashes**: Bitcoin block hashes are public. The adapter's sync position is inferrable from the headers it has accepted.
- **Race condition**: the attacker only needs to respond before the legitimate peer. Since the attacker controls their own response latency and the adapter sends `getdata` to one peer at a time with a configurable timeout, the window is wide.
- **No retry after cache hit**: the poisoned cache entry is permanent until the adapter is restarted or the block is explicitly pruned.

---

### Recommendation

1. **Enforce per-peer matching**: in `received_block_message`, after removing the entry from `getdata_request_info`, verify that `addr == request.socket`. If not, re-insert the entry and return an error. [5](#0-4) 

2. **Reject blocks with empty `txdata`**: in `add_block`, add an explicit check that `txdata` is non-empty before the merkle root guard, or treat `compute_merkle_root() == None` as an invalid block rather than skipping validation. [2](#0-1) 

---

### Proof of Concept

```
1. Adapter connects to peer_A (legitimate) and peer_B (attacker).
2. Adapter sends GetData([Block(hash_B)]) to peer_A.
3. peer_B (already connected, passes peer_info check) immediately sends:
       NetworkMessage::Block(Block { header: <valid header for hash_B>, txdata: [] })
4. received_block_message:
   - peer_B passes the peer_info.contains_key check (line 422).
   - getdata_request_info.remove(&hash_B) succeeds (line 427).
   - add_block is called:
       * compute_merkle_root() returns None for txdata=[] → merkle check skipped (line 237).
       * Block serialized and inserted into block_cache.
5. peer_A's legitimate Block response arrives:
   - getdata_request_info.remove(&hash_B) returns None → ReceivedBlockMessageError::UnknownBlock (line 431).
   - Legitimate block is discarded.
6. block_sync_queue never re-enqueues hash_B (it is in block_cache).
7. ckBTC canister receives the block at height B with txdata=[] — all deposits in that block are missed.
```

### Citations

**File:** rs/bitcoin/adapter/src/blockchainmanager.rs (L158-166)
```rust
    /// This queue stores the set of block hashes belonging to blocks that have yet to be synced by the BlockChainManager
    /// and stored into the block cache.
    ///
    /// A block hash is added when the `GetSuccessors` request is processed. If the block hash cannot be
    /// found in the `getdata_request_info` field or in the `blockchain`'s block cache, the block hash
    /// is added to the queue.
    ///
    /// A block hash is removed when it is determined a peer can receive another `getdata` message.
    block_sync_queue: LinkedHashSet<BlockHash>,
```

**File:** rs/bitcoin/adapter/src/blockchainmanager.rs (L422-433)
```rust
        if !self.peer_info.contains_key(addr) {
            return Err(ReceivedBlockMessageError::UnknownPeer);
        }
        let block_hash = block.block_hash();
        //Remove the corresponding `getdata` request from peer_info and getdata_request_info.
        let request = match self.getdata_request_info.remove(&block_hash) {
            Some(request) => request,
            None => {
                // Exit early. If the block is not in the `getdata_request_info`, the block is no longer wanted.
                return Err(ReceivedBlockMessageError::UnknownBlock);
            }
        };
```

**File:** rs/bitcoin/adapter/src/blockchainstate.rs (L237-239)
```rust
        if block.compute_merkle_root().is_some() && !block.check_merkle_root() {
            return Err(AddBlockError::InvalidMerkleRoot(block_hash));
        }
```

**File:** rs/bitcoin/adapter/src/blockchainstate.rs (L859-873)
```rust
        state
            .add_block(Block {
                header: h3,
                txdata: Vec::new(),
            })
            .await
            .unwrap();
        state
            .add_block(Block {
                header: h4,
                txdata: Vec::new(),
            })
            .await
            .unwrap();
        assert_eq!(state.get_active_chain_tip().header, h4);
```
