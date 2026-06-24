### Title
Stale `dapp_canister_ids` Entry After Canister Deletion Causes Panic in `set_dapp_controllers`, Permanently Blocking SNS Swap Finalization - (File: `rs/sns/root/src/lib.rs`)

---

### Summary

`SnsRootCanister::set_dapp_controllers` performs a pre-flight `canister_status` check on every canister in `dapp_canister_ids`. If any registered dapp canister has been deleted (e.g., ran out of cycles), the management canister returns `CanisterNotFound`, and the code unconditionally **panics**. Because `dapp_canister_ids` is never cleaned up on deletion, the stale entry permanently blocks every future call to `set_dapp_controllers`, including the critical swap-finalization path that returns dapp canisters to their original owners.

---

### Finding Description

`SnsRootCanister` maintains a persistent list `dapp_canister_ids` of registered dapp canister principals. When `set_dapp_controllers` is called (by the swap or governance canister), it iterates over this list and calls `management_canister_client.canister_status(...)` on each entry as a pre-flight ownership check:

```rust
// rs/sns/root/src/lib.rs  lines 796–812
for dapp_canister_id in &dapp_canister_ids {
    let dapp_canister_id = CanisterId::try_from(*dapp_canister_id)...;
    let canister_status = match management_canister_client
        .canister_status(dapp_canister_id.into())
        .await
    {
        Err(_) => {
            // TODO(NNS1-1993): Remove this panic and return an error type instead.
            panic!(
                "Could not get the status of canister: {dapp_canister_id}.  Root may not be a controller."
            )
        }
        Ok(status) => status,
    };
```

If a dapp canister has been deleted (its cycles were exhausted and the IC garbage-collected it, or it was explicitly deleted), `canister_status` returns an error with `CanisterNotFound`. The code panics unconditionally. The stale `PrincipalId` remains in `dapp_canister_ids` across upgrades (it is part of the persisted `SnsRootCanister` proto state), so every subsequent call to `set_dapp_controllers` that touches that canister ID will also panic. There is no recovery path.

The `dapp_canister_ids` field is populated by `register_dapp_canister` (line 743) and is only removed when `set_dapp_controllers` successfully completes (line 875). Because the panic fires before any removal, the stale entry is never cleaned up.

---

### Impact Explanation

`set_dapp_controllers` is the mechanism by which:
1. The **swap canister** returns dapp canisters to their original owners after a completed or failed SNS token swap.
2. The **governance canister** executes `DeregisterDappCanisters` proposals.

If any single registered dapp canister is deleted, both of these critical operations are permanently bricked for the entire SNS instance. Dapp canisters that are still alive remain locked under SNS root control with no way to transfer them back. The SNS swap cannot finalize. This is a denial-of-service against the SNS governance and swap lifecycle.

---

### Likelihood Explanation

Dapp canisters registered with SNS root can run out of cycles through normal operation (insufficient top-up, high traffic, or deliberate neglect). The IC automatically deletes canisters that remain below the freezing threshold for a grace period. This is a realistic, non-adversarial scenario. An adversary who can call any cycles-consuming public method on a dapp canister can also accelerate the depletion. No privileged access is required to trigger the condition; only the natural lifecycle of a canister running out of cycles is needed.

---

### Recommendation

In the pre-flight loop of `set_dapp_controllers`, handle `CanisterNotFound` (or any `canister_status` error) gracefully instead of panicking. Options:

1. **Remove the stale entry** from `dapp_canister_ids` when `canister_status` returns an error indicating the canister no longer exists, and continue processing the remaining canisters.
2. **Return a structured error** (as the existing `TODO(NNS1-1993)` comment already acknowledges) instead of panicking, so callers can observe and handle the failure.
3. **Add a canister-existence check** before calling `canister_status`, analogous to the EVM fix of checking `code.length > 0` before calling `owner()`.

The analogous fix in the referenced EVM report was:
```solidity
if (address(currentProxy) != address(0) && currentProxy.code.length > 0 && currentProxy.owner() == owner)
```

The IC analog would be to treat a `CanisterNotFound` error from `canister_status` as a signal to prune the stale entry rather than panic.

---

### Proof of Concept

**Setup:**
1. Deploy an SNS. Register a dapp canister `D` with SNS root via `register_dapp_canisters`. `D` is added to `dapp_canister_ids`.
2. Canister `D` runs out of cycles and is deleted by the IC (or is explicitly deleted via a governance proposal).
3. `dapp_canister_ids` still contains `D`'s principal ID (persisted in stable state).

**Trigger:**
4. The swap canister calls `set_dapp_controllers` to return all dapp canisters to original owners after swap finalization.
5. The pre-flight loop reaches `D`, calls `management_canister_client.canister_status(D)`.
6. The management canister returns `CanisterNotFound` (reject code `DestinationInvalid`).
7. The `Err(_)` arm fires: `panic!("Could not get the status of canister: D. Root may not be a controller.")`.
8. The SNS root canister traps. No controller changes are made. `dapp_canister_ids` is unchanged.
9. Every future call to `set_dapp_controllers` (including retry attempts) hits the same panic.

**Result:** The SNS swap cannot finalize. All surviving dapp canisters remain permanently locked under SNS root control. The `DeregisterDappCanisters` governance proposal path is also blocked. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/sns/root/src/lib.rs (L740-744)
```rust
        // Add canister_to_register to self.dapp_canister_ids.
        self_ref.with_borrow_mut(|state| {
            let canister_to_register = PrincipalId::from(canister_to_register);
            state.dapp_canister_ids.push(canister_to_register);
        });
```

**File:** rs/sns/root/src/lib.rs (L796-826)
```rust
        for dapp_canister_id in &dapp_canister_ids {
            let dapp_canister_id = CanisterId::try_from(*dapp_canister_id).unwrap_or_else(|err| {
                panic!(
                    "Unable to convert principal ID ({dapp_canister_id}) of a dapp into a \
                     canister ID: {err:#?}"
                )
            });
            let canister_status = match management_canister_client
                .canister_status(dapp_canister_id.into())
                .await
            {
                Err(_) => {
                    // TODO(NNS1-1993): Remove this panic and return an error type instead.
                    panic!(
                        "Could not get the status of canister: {dapp_canister_id}.  Root may not be a controller."
                    )
                }
                Ok(status) => status,
            };
            let is_controllee = canister_status.controllers().contains(&own_canister_id);

            // TODO(NNS1-1993): Remove this assertion and return an error type instead.
            assert!(
                is_controllee,
                "Operation aborted due to an error; no changes have been made: \
                 Unable to determine whether this canister (SNS root) is the controller \
                 of a registered dapp canister ({dapp_canister_id}). This may be due to \
                 the canister having been deleted, which may be due to it running out \
                 of cycles."
            );
        }
```

**File:** rs/sns/root/src/lib.rs (L872-880)
```rust
            // If necessary, remove dapp_canister_id from self_ref.
            if !still_controlled_by_this_canister {
                self_ref.with(|self_ref| {
                    swap_remove_if(&mut self_ref.borrow_mut().dapp_canister_ids, |element| {
                        element == dapp_canister_id
                    })
                });
            }
        }
```

**File:** rs/sns/root/src/gen/ic_sns_root.pb.v1.rs (L29-31)
```rust
    #[prost(message, repeated, tag = "3")]
    pub dapp_canister_ids: ::prost::alloc::vec::Vec<::ic_base_types::PrincipalId>,
    /// Extension canister IDs.
```
