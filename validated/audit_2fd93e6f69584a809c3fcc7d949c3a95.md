### Title
Missing State Migration Implementation for `StateV2` in Cycles Minting Canister — (`File: rs/nns/cmc/src/main.rs`)

### Summary
The Cycles Minting Canister (CMC) defines a versioned state type (`StateV2`) and a `State::decode()` function that explicitly documents where a migration from `StateV1` to `StateV2` should be implemented. However, the migration code is entirely absent — replaced only by a comment skeleton. If the canister is ever upgraded from a version that stored `StateV1` on-chain, the `post_upgrade` hook will call `State::decode()`, detect `stored_state_version < current_state_version`, and immediately return an `Err(...)`, causing `unwrap()` to panic and bricking the canister permanently.

### Finding Description
In `rs/nns/cmc/src/main.rs`, the current state type is aliased as `StateV2` with version number `2`. [1](#0-0) 

The `State::decode()` function handles the case where the stored version is less than the current version with only a comment placeholder — no actual migration function exists: [2](#0-1) 

The `post_upgrade` hook calls `State::decode(&bytes).unwrap()`, which will panic if `decode` returns `Err`: [3](#0-2) 

`StateV2` contains two non-optional, non-`#[serde(default)]` fields that were added relative to any prior state version — `subnet_rental_cycles_limit: Cycles` and `subnet_rental_canister_limiter: limiter::Limiter` — which are required fields with no fallback: [4](#0-3) 

The code comment at line 171 explicitly acknowledges the intended migration path (`v = v_s + 1` → decode as `StateVLast` and migrate), but the `Ordering::Less` branch returns an error instead of executing any migration: [5](#0-4) 

### Impact Explanation
If the NNS governance canister submits a proposal to upgrade the CMC from a version that stored `StateV1` (version `1`) to the current wasm (version `2`), the `post_upgrade` hook will panic. On the Internet Computer, a panicking `post_upgrade` causes the upgrade to be rolled back — but if the pre-upgrade state was already committed and the new wasm is the only available wasm, the canister becomes unupgradeable until a hotfix wasm is deployed via NNS governance. During this window, the CMC — which is the sole canister authorized to mint cycles on the IC — is bricked. No user can create canisters via the CMC, no cycles can be minted, and the Subnet Rental Canister cannot top up subnets. This is a **cycles/resource accounting bug** with direct denial-of-service impact on the entire IC ecosystem.

### Likelihood Explanation
The CMC is a long-lived NNS system canister. The `StateV2` type and its version number `2` imply a prior `StateV1` existed. Any NNS upgrade proposal that moves from a `StateV1`-storing wasm to the current wasm will trigger this panic. The NNS upgrade process is a standard governance operation executable by any neuron with sufficient voting power — no privileged key or admin access beyond normal NNS governance participation is required. The likelihood is **medium**: it depends on whether a `StateV1`-storing wasm is still in the upgrade path, but the code explicitly documents this as a supported scenario and provides no implementation.

### Recommendation
1. Define a `StateV1` struct capturing the fields that existed before `subnet_rental_cycles_limit` and `subnet_rental_canister_limiter` were added.
2. Implement a `migrate_v1_to_v2(state: StateV1) -> StateV2` function that populates the new fields with their default values (e.g., `subnet_rental_cycles_limit: Cycles::from(SUBNET_RENTAL_DEFAULT_CYCLES_LIMIT)` and a fresh `Limiter`).
3. Replace the `Ordering::Less` branch in `State::decode()` with the actual migration call:
   ```rust
   if stored_state_version == StateVersion(1) {
       let state = deserializer.get_value::<StateV1>().unwrap();
       deserializer.done().unwrap();
       return Ok(migrate_v1_to_v2(state));
   }
   ```
4. Add a query method (e.g., `get_state_version`) to allow operators to verify the migrated state version after upgrade.
5. Add a unit test that serializes a `StateV1`, decodes it as `StateV2`, and asserts the migrated fields have correct default values.

### Proof of Concept
1. Serialize a `StateV1` (without `subnet_rental_cycles_limit` / `subnet_rental_canister_limiter`) using `Encode!(&StateVersion(1), &state_v1)`.
2. Write those bytes to stable memory via `stable_utils::stable_set`.
3. Upgrade the CMC canister to the current wasm.
4. `post_upgrade` calls `State::decode(&bytes)`.
5. `decode` reads `stored_state_version = StateVersion(1)`, compares to `StateVersion(2)`, enters `Ordering::Less`, and returns `Err("[cycles] ERROR: stored state version ... is lesser than the current state version ...")`.
6. `State::decode(&bytes).unwrap()` panics.
7. The upgrade fails; the CMC is left in its pre-upgrade state but the new wasm cannot be installed without a separate hotfix NNS proposal. [6](#0-5) [3](#0-2)

### Citations

**File:** rs/nns/cmc/src/main.rs (L157-180)
```rust
/// Version of the State type.
///
/// Each generation of the State type has an associated version.
/// The version of the State type currently stored in stable storage
/// is also stored in stable storage as a candid encoded number
/// just before the candid encoded State value itself.
///
/// Let
///   v         = version of the current (expected) State
///   State     = current State type
///   StateVn   = State type of version n
///   v_s       = version stored in stable storage, the next argument in stable storage
///               should then contain the candid encoded StateVv_s
///
/// If v = v_s + 1 then decode the stable storage as StateVv_s and migrate it to State
/// If v = v_s     then decode the stable storage as State
/// If v = v_s - 1 then it means a rollback probably happened because the stored version
///                is one bigger than the expected version.
///                To be safe we don't support this and will panic.
///                Instead a hotfix should be performed.
#[derive(
    Copy, Clone, Eq, PartialEq, Ord, PartialOrd, Hash, Debug, CandidType, Deserialize, Serialize,
)]
struct StateVersion(u64);
```

**File:** rs/nns/cmc/src/main.rs (L182-196)
```rust
/// Current state type.
///
/// IMPORTANT: when changing the state type in a backwards incompatible way make sure to:
///
/// * Introduce a new StateV(n+1) type where n is the version of the current State type.
///
/// * Set the State type alias to StateV(n+1).
///
/// * Introduce a migration function from StateVn -> StateV(n+1).
///
/// * Perform this migration in State::decode(...).
///
/// * Optionally remove older State types (StateVm where m < n)
///   because they are no longer needed.
type State = StateV2;
```

**File:** rs/nns/cmc/src/main.rs (L235-243)
```rust
    /// How many cycles are allowed to be minted by the Subnet Rental Canister in a month.
    pub subnet_rental_cycles_limit: Cycles,

    /// Maintain a count of how many cycles have been minted in the last hour.
    pub base_limiter: limiter::Limiter,

    /// Maintain a count of how many cycles have been minted by the Subnet Rental Canister
    /// in the last month.
    pub subnet_rental_canister_limiter: limiter::Limiter,
```

**File:** rs/nns/cmc/src/main.rs (L300-335)
```rust
    fn decode(bytes: &[u8]) -> Result<Self, String> {
        let mut deserializer = candid::de::IDLDeserialize::new(bytes).unwrap();
        let stored_state_version: StateVersion =
            deserializer.get_value().expect("state version is missing");
        let current_state_version: StateVersion = Self::state_version();

        match stored_state_version.cmp(&current_state_version) {
            Ordering::Greater => {
                return Err(format!(
                    "[cycles] ERROR: stored state version {stored_state_version:?} is greater than the current state \
                     version {current_state_version:?}!  This likely means a rollback happened. This is not supported. \
                     Please upgrade to a hotfix instead."
                ));
            }
            Ordering::Less => {
                // This is where you would put a function to do the migration, which would look something like this:
                // if stored_state_version == StateVersion(*last_state_version*) {
                //   let state = deserializer.get_value::<StateVLast>().unwrap();
                //   deserializer.done().unwrap();
                //   return Ok(migrate_last_to_current(state));
                // }
                // Migrations should be deleted after execution to keep the codebase tidy.
                return Err(format!(
                    "[cycles] ERROR: stored state version {stored_state_version:?} is lesser than the current state \
                     version {current_state_version:?}! Did you forget to migrate the old to the current type?"
                ));
            }
            Ordering::Equal => print(format!(
                "[cycles] INFO: stored state version {stored_state_version:?} equals the current state version {current_state_version:?}. \
                Continuing to decode the stable storage ... ",
            )),
        };
        let state = deserializer.get_value::<State>().unwrap();
        deserializer.done().unwrap();
        Ok(state)
    }
```

**File:** rs/nns/cmc/src/main.rs (L2374-2395)
```rust
#[post_upgrade]
fn post_upgrade(maybe_args: Option<CyclesCanisterInitPayload>) {
    let bytes = stable_utils::stable_get().expect("Could not read data from stable memory");
    print(format!(
        "[cycles] deserializing state after upgrade ({} bytes)",
        bytes.len(),
    ));

    let mut new_state = State::decode(&bytes).unwrap();
    if new_state.subnet_types_to_subnets.is_none() {
        new_state.subnet_types_to_subnets = Some(BTreeMap::new());
    }

    if let Some(args) = maybe_args {
        if let Some(xrc_flag) = args.exchange_rate_canister {
            new_state.exchange_rate_canister_id = xrc_flag.extract_exchange_rate_canister_id();
        }
        new_state.cycles_ledger_canister_id = args.cycles_ledger_canister_id;
    }

    STATE.with(|state| state.replace(Some(new_state)));
}
```
