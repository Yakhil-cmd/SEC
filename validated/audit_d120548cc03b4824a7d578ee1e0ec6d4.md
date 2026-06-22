### Title
Insufficient `max_size` in `StorableRegistryKey::BOUND` Omits One `u64` Field — (`File: rs/registry/canister-client/src/stable_memory.rs`)

---

### Summary

The `Storable` implementation for `StorableRegistryKey` declares a `Bound::Bounded { max_size }` that accounts for only **one** of the two `u64` fields serialized by `to_bytes()`. The missing 8 bytes mean any registry key whose string component is between 193 and 200 bytes long will produce a serialized blob that exceeds the declared `max_size`, causing `ic_stable_structures::StableBTreeMap` to panic and trap the canister.

---

### Finding Description

`StorableRegistryKey` holds three fields:

```rust
pub struct StorableRegistryKey {
    pub key: String,           // variable, up to MAX_REGISTRY_KEY_SIZE = 200 bytes
    pub version: u64,          // 8 bytes
    pub timestamp_nanoseconds: u64, // 8 bytes
}
```

`to_bytes()` concatenates all three:

```rust
storable_key.extend_from_slice(&key_b);      // ≤ 200 bytes
storable_key.extend_from_slice(&version_b);  // 8 bytes
storable_key.extend_from_slice(&timestamp_b); // 8 bytes
```

Maximum serialized size = **200 + 8 + 8 = 216 bytes**.

The declared bound is:

```rust
const BOUND: Bound = Bound::Bounded {
    max_size: MAX_REGISTRY_KEY_SIZE + size_of::<u64>() as u32,
    is_fixed_size: false,
};
```

= **200 + 8 = 208 bytes** — exactly one `u64` (8 bytes) short. [1](#0-0) [2](#0-1) [3](#0-2) 

`ic_stable_structures::StableBTreeMap` uses `max_size` to size the on-disk node slots. When `is_fixed_size = false`, the library stores the actual length alongside the data but still enforces that the actual length ≤ `max_size`. Exceeding it causes a panic, which traps the canister message. [4](#0-3) 

The insertion path is `StableCanisterRegistryClient::add_deltas`, called whenever the node-rewards canister syncs registry data from the NNS.

---

### Impact Explanation

Any registry key whose UTF-8 byte length is in the range **[193, 200]** will produce a serialized blob of size **[209, 216]**, all of which exceed the declared `max_size` of 208. When `add_deltas` tries to insert such a key into the `StableBTreeMap`, the stable-structures library panics, trapping the update message. Because the registry sync is a periodic timer-driven operation, repeated traps would permanently stall the node-rewards canister's ability to ingest new registry data, breaking reward calculation for all nodes on the subnet.

---

### Likelihood Explanation

The comment on `MAX_REGISTRY_KEY_SIZE` states it is "2 times the max key size present in the registry", implying current keys are ≤ 100 bytes. However, the bound is declared as a hard contract for the stable B-tree. If the NNS governance ever adds a registry key whose string length exceeds 192 bytes (still within the declared 200-byte limit), the node-rewards canister will trap on every sync attempt. A governance participant submitting a legitimate registry mutation with a long key name is a realistic trigger; no malicious intent is required. [5](#0-4) 

---

### Recommendation

Change the `max_size` to account for **both** `u64` fields:

```rust
const BOUND: Bound = Bound::Bounded {
    max_size: MAX_REGISTRY_KEY_SIZE + 2 * size_of::<u64>() as u32,
    is_fixed_size: false,
};
```

This raises the declared maximum from 208 to 216 bytes, matching the actual worst-case output of `to_bytes()`.

---

### Proof of Concept

```rust
use ic_stable_structures::Storable;

let key = StorableRegistryKey {
    key: "a".repeat(193),          // 193 bytes — within the 200-byte limit
    version: u64::MAX,
    timestamp_nanoseconds: u64::MAX,
};

let bytes = key.to_bytes();
assert_eq!(bytes.len(), 193 + 8 + 8); // = 209 bytes

// Declared max_size = 200 + 8 = 208 bytes
// 209 > 208  →  StableBTreeMap panics on insert → canister traps
``` [6](#0-5) [7](#0-6)

### Citations

**File:** rs/registry/canister-client/src/stable_memory.rs (L22-27)
```rust
#[derive(Clone, Ord, PartialOrd, Eq, PartialEq, Default)]
pub struct StorableRegistryKey {
    pub key: String,
    pub version: u64,
    pub timestamp_nanoseconds: u64,
}
```

**File:** rs/registry/canister-client/src/stable_memory.rs (L39-40)
```rust
// This value is set as 2 times the max key size present in the registry
const MAX_REGISTRY_KEY_SIZE: u32 = 200;
```

**File:** rs/registry/canister-client/src/stable_memory.rs (L42-76)
```rust
impl Storable for StorableRegistryKey {
    fn to_bytes(&self) -> Cow<'_, [u8]> {
        let mut storable_key = vec![];
        let key_b = self.key.as_bytes().to_vec();
        let version_b = self.version.to_be_bytes().to_vec();
        let timestamp_b = self.timestamp_nanoseconds.to_be_bytes().to_vec();

        storable_key.extend_from_slice(&key_b);
        storable_key.extend_from_slice(&version_b);
        storable_key.extend_from_slice(&timestamp_b);

        Cow::Owned(storable_key)
    }

    fn from_bytes(bytes: Cow<[u8]>) -> Self {
        let bytes = bytes.as_ref();
        let len = bytes.len();
        let (remaining_bytes, timestamp_bytes) = bytes.split_at(len - 8);
        let (key_bytes, version_bytes) = remaining_bytes.split_at(len - 16);

        let key = String::from_utf8(key_bytes.to_vec()).expect("Invalid UTF-8 in key");
        let version = u64::from_be_bytes(version_bytes.try_into().expect("Invalid version bytes"));
        let timestamp_nanoseconds =
            u64::from_be_bytes(timestamp_bytes.try_into().expect("Invalid timestamp bytes"));

        Self {
            key,
            version,
            timestamp_nanoseconds,
        }
    }
    const BOUND: Bound = Bound::Bounded {
        max_size: MAX_REGISTRY_KEY_SIZE + size_of::<u64>() as u32,
        is_fixed_size: false,
    };
```

**File:** rs/registry/canister-client/src/stable_canister_client.rs (L56-95)
```rust
    fn add_deltas(&self, deltas: Vec<RegistryDelta>) -> Result<(), String> {
        for delta in deltas {
            let string_key = std::str::from_utf8(&delta.key[..]).map_err(|e| format!("{e:?}"))?;
            let mut highest_version_inserted = self.get_latest_version();

            S::with_registry_map_mut(|local_registry| {
                for v in delta.values {
                    let registry_version = RegistryVersion::from(v.version);
                    highest_version_inserted =
                        std::cmp::max(highest_version_inserted, registry_version);

                    let key = StorableRegistryKey {
                        key: string_key.to_string(),
                        version: registry_version.get(),
                        timestamp_nanoseconds: v.timestamp_nanoseconds,
                    };
                    let value = StorableRegistryValue(if v.deletion_marker {
                        None
                    } else {
                        Some(v.value)
                    });

                    local_registry.insert(key, value);

                    self.timestamp_to_versions_map
                        .write()
                        .unwrap()
                        .entry(v.timestamp_nanoseconds)
                        .or_default()
                        .insert(registry_version);
                }
            });
            // Update the latest version if the inserted version is higher than the current one.
            if highest_version_inserted > self.get_latest_version() {
                self.latest_version
                    .store(highest_version_inserted.get(), AtomicOrdering::SeqCst);
            }
        }
        Ok(())
    }
```

**File:** rs/node_rewards/canister/src/storage.rs (L51-54)
```rust
    static REGISTRY_DATA_STORE_BTREE_MAP: RefCell<StableBTreeMap<StorableRegistryKey, StorableRegistryValue, VM>>
        = RefCell::new(MEMORY_MANAGER.with_borrow(|mm|
            StableBTreeMap::init(mm.get(REGISTRY_STORE_MEMORY_ID))
        ));
```
