### Title
CBOR-Serialized Nested Structs in Stable Memory Prevent Safe Future Upgrades - (`rs/migration_canister/src/lib.rs`)

### Summary

The migration canister stores `RequestState`, `EventType`, `Event`, and `Request` in stable memory using `serde_cbor` serialization. Each of these types contains nested structs (`Request`, `RecoveryState`, `ControllerRecoveryState`) that are embedded by value. Because CBOR/serde serialization is not field-tag-based like Protobuf, any future upgrade that adds a field to an inner struct will cause deserialization of existing stable storage entries to panic, bricking the canister.

### Finding Description

In `rs/migration_canister/src/lib.rs`, four types implement `Storable` using `serde_cbor::to_vec` / `from_slice` and are persisted in `StableBTreeMap` instances in stable memory:

- `RequestState` (enum) — all nine variants embed `Request` by value; the `Failed` variant also embeds `RecoveryState` by value.
- `EventType` (enum) — both variants embed `Request` by value.
- `Event` (struct) — embeds `EventType` by value, which in turn embeds `Request`.
- `Request` (struct) — a flat struct with seven fields.

`RecoveryState` embeds `ControllerRecoveryState` (from `rs/migration_canister/src/controller_recovery.rs`) by value in two fields.

The `Storable` implementations for all four types call `from_slice(&bytes).expect("... deserialization failed")`, which panics on any deserialization error.

CBOR serialization via `serde` encodes struct fields by name (or enum variants by name/index). It does **not** silently skip unknown fields or supply defaults for missing fields unless `#[serde(default)]` is explicitly annotated on every field. None of the fields in `Request`, `RecoveryState`, or `ControllerRecoveryState` carry `#[serde(default)]`.

Consequently, if any future upgrade adds a field to `Request`, `RecoveryState`, or `ControllerRecoveryState`, the CBOR bytes already stored in stable memory will not contain that field. Deserialization will fail with a missing-field error, causing the `.expect(...)` call to panic. Because this deserialization happens when reading entries from the `StableBTreeMap` (which occurs during normal canister operation after upgrade), the canister becomes permanently inoperable.

This is the direct IC analog of M-18: nested structs embedded by value in an upgradeable storage scheme, where the inner struct cannot be extended without corrupting (or failing to read) existing stored data.

### Impact Explanation

A future upgrade to the migration canister that adds any field to `Request`, `RecoveryState`, or `ControllerRecoveryState` will cause every read of an existing `RequestState` or `EventType` entry from stable memory to panic. This bricks the canister post-upgrade. Active migrations in progress at the time of the upgrade would be left in intermediate states — potentially with the migration canister as the sole controller of user canisters and no way to restore original controllers, since the `RecoveryState` tracking that restoration cannot be read.

### Likelihood Explanation

The migration canister is actively developed infrastructure. `Request` currently has seven fields; `RecoveryState` has two. Both are plausible targets for extension (e.g., adding a timestamp, a retry counter, or a new subnet field to `Request`). Any such addition triggers the bug. The NNS governance upgrade path is a routine, legitimate operation — no attacker action is required; the bug fires on the next schema-extending upgrade.

### Recommendation

Replace `serde_cbor` serialization of nested structs with Protobuf (as used by NNS/SNS governance canisters via `prost`). Protobuf encodes fields by numeric tag and silently ignores unknown tags, making it safe to add fields to inner messages in future upgrades. Alternatively, annotate every field of every inner struct with `#[serde(default)]` and use a versioned envelope, but Protobuf is the established pattern in this codebase (see `rs/nervous_system/common/src/memory_manager_upgrade_storage.rs`).

### Proof of Concept

**Nested struct stored via CBOR:**

`RequestState::Failed` embeds `Request` and `RecoveryState` by value: [1](#0-0) 

`RecoveryState` embeds `ControllerRecoveryState` by value: [2](#0-1) 

`ControllerRecoveryState` is a plain serde-serialized enum with no `#[serde(default)]`: [3](#0-2) 

**Storable implementation panics on deserialization failure:** [4](#0-3) 

**Contrast: the safe pattern used elsewhere in the codebase** stores protobuf via `store_protobuf`/`load_protobuf`, which tolerates added fields in nested messages: [5](#0-4) 

Adding any field to `Request` (e.g., a `migration_timestamp: u64`) and upgrading the canister will cause every subsequent read of an existing `RequestState` entry to call `from_slice` on bytes that lack the new field, returning a serde error, and the `.expect("RequestState deserialization failed")` will panic, trapping the canister.

### Citations

**File:** rs/migration_canister/src/lib.rs (L167-171)
```rust
#[derive(Clone, PartialOrd, Ord, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecoveryState {
    pub restore_migrated_canister_controllers: ControllerRecoveryState,
    pub restore_replaced_canister_controllers: ControllerRecoveryState,
}
```

**File:** rs/migration_canister/src/lib.rs (L299-307)
```rust
    /// Some transition has failed fatally.
    /// We stay in this state until the controllers have been restored and then
    /// transition to a `Failed` state in the `HISTORY`.
    #[strum(to_string = "RequestState::Failed {{ request: {request}, reason: {reason} }}")]
    Failed {
        request: Request,
        recovery_state: RecoveryState,
        reason: String,
    },
```

**File:** rs/migration_canister/src/lib.rs (L408-422)
```rust
impl Storable for RequestState {
    fn to_bytes(&self) -> Cow<'_, [u8]> {
        Cow::Owned(to_vec(&self).expect("RequestState serialization failed"))
    }

    fn into_bytes(self) -> Vec<u8> {
        self.to_bytes().to_vec()
    }

    fn from_bytes(bytes: Cow<[u8]>) -> Self {
        from_slice(&bytes).expect("RequestState deserialization failed")
    }

    const BOUND: Bound = Bound::Unbounded;
}
```

**File:** rs/migration_canister/src/controller_recovery.rs (L52-63)
```rust
#[derive(Debug, Clone, PartialOrd, Ord, PartialEq, Eq, Serialize, Deserialize)]
pub enum ControllerRecoveryState {
    /// Controller recovery is pending and no progress has been made so far.
    NoProgress,
    /// Controller recovery has been confirmed to be necessary (the migration canister
    /// is the only controller) and the canister history has the specified
    /// number of changes in total (used to derive if controller recovery
    /// succeeded).
    TotalNumChangesBefore(u64),
    /// Controller recovery has completed (or it was not needed to be performed at all).
    Done,
}
```

**File:** rs/nervous_system/common/src/memory_manager_upgrade_storage.rs (L53-68)
```rust
/// Store a protobuf into a [ic_stable_structures::Memory] that can be retrieved with [load_protobuf]
pub fn store_protobuf<M: Memory>(
    memory: &M,
    protobuf: &impl prost::Message,
) -> Result<(), std::io::Error> {
    let mut writer = SizeAwareWriter::new(
        memory,
        STABLE_STRUCTURES_WASM_PAGE_SIZE,
        STORAGE_ENCODING_BYTES_RESERVED as u64,
    );

    StorageEncoding::SizeAware.write_byte(memory);
    protobuf
        .encode(&mut writer)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::OutOfMemory, e))
}
```
