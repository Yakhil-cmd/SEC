### Title
Unprivileged Caller Can Inject Arbitrary Interop Roots via Missing Emitter Check in `interop_root_reporter_event_hook` - (File: system_hooks/src/event_hooks/interop_root_reporter.rs)

### Summary

The `interop_root_reporter_event_hook` fires on any `InteropRootAdded` event regardless of which contract emitted it. The hook function receives no emitter address and performs no origin check, so any unprivileged contract that emits a log whose first topic matches `INTEROP_ROOT_ADDED_EVENT_SIG` can inject an arbitrary `InteropRoot` into the system's interop root storage. This corrupts the rolling-hash commitment that is included in the block's ZK proof public inputs.

### Finding Description

`interop_root_reporter_event_hook` is the event-hook analog of the call-hook pattern used by `l1_messenger_hook`, `set_bytecode_on_address_hook`, and `mint_base_token_hook`. All three call hooks guard their privileged action with an explicit `caller != EXPECTED_ADDRESS` check before proceeding. [1](#0-0) [2](#0-1) [3](#0-2) 

The event hook has no equivalent guard. Its full function signature is:

```rust
pub fn interop_root_reporter_event_hook<S: EthereumLikeTypes>(
    topics: &arrayvec::ArrayVec<..., MAX_EVENT_TOPICS>,
    data: &[u8],
    _caller_ee: u8,          // unused – no emitter address parameter at all
    system: &mut System<S>,
    resources: &mut S::Resources,
) -> Result<(), SystemError>
``` [4](#0-3) 

The hook only validates the event signature in `topics[0]`, the ABI offset, and the array length: [5](#0-4) 

After those structural checks it unconditionally calls:

```rust
system.io.add_interop_root(
    ExecutionEnvironmentType::NoEE,
    resources,
    InteropRoot { root, block_or_batch_number, chain_id },
)?;
``` [6](#0-5) 

`add_interop_root` pushes the root into `interop_root_storage` without any further validation of origin: [7](#0-6) 

The `InteropRootStorage` itself performs no origin check either: [8](#0-7) 

The interop root list is consumed during block finalization and its rolling hash is included in the ZK proof's public inputs. Injecting a fake root corrupts that commitment.

### Impact Explanation

An attacker deploys a contract that emits a log whose `topics[0]` equals `INTEROP_ROOT_ADDED_EVENT_SIG`, `topics[1]` is an arbitrary `chain_id`, `topics[2]` is an arbitrary `block_or_batch_number`, and whose `data` encodes a non-zero fake `root`. When the transaction executes, the event hook fires and appends the fake `InteropRoot` to the block's interop root list. The rolling hash over that list is committed to in the ZK proof public inputs. Consequences:

- **State-transition integrity break**: the sequencer and prover commit to different interop root sets, causing proof verification failure (chain halt / DoS).
- **Cross-chain fraud**: a carefully chosen fake root could be used to spoof cross-chain message inclusion proofs on a settlement layer that trusts the committed rolling hash.

### Likelihood Explanation

The attacker needs only to submit a standard EVM transaction that calls a contract emitting the crafted event. No privileged key, governance majority, or oracle manipulation is required. The entry path is fully reachable by any unprivileged L2 user.

### Recommendation

Pass the emitter address into the event hook (or make it available via the dispatch layer) and reject events not originating from `L2_INTEROP_ROOT_STORAGE_ADDRESS`:

```rust
if emitter != L2_INTEROP_ROOT_STORAGE_ADDRESS {
    return Ok(());
}
```

This mirrors the pattern already used by every call hook in the codebase. [9](#0-8) 

### Proof of Concept

1. Deploy a contract with the following EVM bytecode that emits a crafted `InteropRootAdded` log:
   - `topics[0]` = `0x6b451b8422636e45b93bf7f594fa2c1769d039766c4254a6e7f9c0ee1715cdb0`
   - `topics[1]` = attacker-chosen `chain_id` (e.g. `0x1`)
   - `topics[2]` = attacker-chosen `block_or_batch_number` (e.g. `0x1`)
   - `data` = ABI-encoded `(uint256 offset=32, uint256 len=1, bytes32 fakeRoot)` where `fakeRoot != 0`
2. Submit a transaction calling that contract.
3. Observe that `system.io.add_interop_root` is called with the attacker-supplied values.
4. The block's interop root rolling hash now includes the fake root, diverging from the honest expected value and corrupting the ZK proof public input. [10](#0-9)

### Citations

**File:** system_hooks/src/call_hooks/l1_messenger.rs (L44-55)
```rust
    // Can be used only by L1 messenger system contract
    if caller != L1_MESSENGER_ADDRESS {
        system_log!(
            system,
            "L1 messenger hook: invalid caller (caller={caller:?})\n"
        );
        // Pretend to be an empty account
        return Ok((
            make_return_state_from_returndata_region(available_resources, &[]),
            return_memory,
        ));
    }
```

**File:** system_hooks/src/call_hooks/mint_base_token.rs (L39-46)
```rust
    // Only allow L2 base token contract to mint tokens
    if caller != L2_BASE_TOKEN_ADDRESS {
        // Pretend to be an empty account
        return Ok((
            make_return_state_from_returndata_region(available_resources, &[]),
            return_memory,
        ));
    }
```

**File:** system_hooks/src/call_hooks/set_bytecode_on_address.rs (L39-50)
```rust
    // Can be used only by Contract Deployer system contract or directly by complex upgrader
    if caller != CONTRACT_DEPLOYER_ADDRESS && caller != COMPLEX_UPGRADER_ADDRESS {
        system_log!(
            system,
            "Set bytecode hook: invalid caller (caller={caller:?})\n"
        );
        // Pretend to be an empty account
        return Ok((
            make_return_state_from_returndata_region(available_resources, &[]),
            return_memory,
        ));
    }
```

**File:** system_hooks/src/event_hooks/interop_root_reporter.rs (L1-82)
```rust
//!
//! Interop root reporter system hook implementation.
//!
use super::super::*;
use ruint::aliases::U256;
use zk_ee::types_config::SystemIOTypesConfig;
use zk_ee::{
    common_structs::interop_root_storage::InteropRoot,
    execution_environment_type::ExecutionEnvironmentType, internal_error,
    storage_types::MAX_EVENT_TOPICS, system::errors::system::SystemError, utils::Bytes32,
};

// InteropRootAdded(uint256,uint256,bytes32[]) - 6b451b8422636e45b93bf7f594fa2c1769d039766c4254a6e7f9c0ee1715cdb0
pub const INTEROP_ROOT_ADDED_EVENT_SIG: [u8; 32] = [
    0x6b, 0x45, 0x1b, 0x84, 0x22, 0x63, 0x6e, 0x45, 0xb9, 0x3b, 0xf7, 0xf5, 0x94, 0xfa, 0x2c, 0x17,
    0x69, 0xd0, 0x39, 0x76, 0x6c, 0x42, 0x54, 0xa6, 0xe7, 0xf9, 0xc0, 0xee, 0x17, 0x15, 0xcd, 0xb0,
];

pub fn interop_root_reporter_event_hook<S: EthereumLikeTypes>(
    topics: &arrayvec::ArrayVec<<S::IOTypes as SystemIOTypesConfig>::EventKey, MAX_EVENT_TOPICS>,
    data: &[u8],
    _caller_ee: u8,
    system: &mut System<S>,
    resources: &mut S::Resources,
) -> Result<(), SystemError>
where
{
    // First, ensure we're capturing the InteropRootAdded event
    if topics.is_empty() || topics[0].as_u8_array() != INTEROP_ROOT_ADDED_EVENT_SIG {
        return Ok(());
    }
    // Internal error if the data supplied doesn't match the expected value
    if data.len() != 96 {
        return Err(internal_error!("Interop root reporter event hook received bad data").into());
    }

    // Parse data
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
    // Validate topics length
    if topics.len() != 3 {
        return Err(internal_error!("Interop root reporter event hook received bad topics").into());
    }

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

    Ok(())
}
```

**File:** basic_system/src/system_implementation/system/io_subsystem.rs (L230-246)
```rust
    fn add_interop_root(
        &mut self,
        _ee_type: ExecutionEnvironmentType,
        resources: &mut Self::Resources,
        interop_root: InteropRoot,
    ) -> Result<(), SystemError> {
        // For native we charge for the storage and the computation of the rolling
        // hash (keccak of old hash || new root).
        let native = <Self::Resources as Resources>::Native::from_computational(
            INTEROP_ROOT_STORAGE_NATIVE_COST + per_root_computational_native_cost(),
        );

        let to_charge = Self::Resources::from_native(native);
        resources.charge(&to_charge)?;

        self.interop_root_storage.push_root(interop_root)
    }
```

**File:** zk_ee/src/common_structs/interop_root_storage.rs (L41-45)
```rust
    pub fn push_root(&mut self, interop_root: InteropRoot) -> Result<(), SystemError> {
        self.list.push(interop_root, ());

        Ok(())
    }
```

**File:** system_hooks/src/addresses_constants.rs (L37-39)
```rust
pub const L2_INTEROP_ROOT_STORAGE_ADDRESS_LOW: u32 = 0x10008;
pub const L2_INTEROP_ROOT_STORAGE_ADDRESS: B160 =
    B160::from_limbs([L2_INTEROP_ROOT_STORAGE_ADDRESS_LOW as u64, 0, 0]);
```
