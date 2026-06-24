### Title
SNS Swap Finalization DoS via Deleted/Frozen Dapp Canister in `set_dapp_controllers` Pre-flight Loop — (File: `rs/sns/root/src/lib.rs`)

### Summary
The `set_dapp_controllers` function in the SNS root canister iterates over all registered dapp canisters in a sequential loop and calls `canister_status` on each. If any single dapp canister has been deleted (e.g., due to running out of cycles), the loop unconditionally `panic!`s, trapping the entire call. Because `set_dapp_controllers` is invoked by the swap canister during SNS swap finalization, a single unavailable dapp canister permanently blocks finalization for **all** swap participants — preventing distribution of SNS tokens on success or return of ICP on failure.

### Finding Description

In `SnsRootCanister::set_dapp_controllers`, a pre-flight loop checks that SNS root still controls every dapp canister before transferring controllers:

```rust
for dapp_canister_id in &dapp_canister_ids {
    let canister_status = match management_canister_client
        .canister_status(dapp_canister_id.into())
        .await
    {
        Err(_) => {
            // TODO(NNS1-1993): Remove this panic and return an error type instead.
            panic!(
                "Could not get the status of canister: {dapp_canister_id}.  \
                 Root may not be a controller."
            )
        }
        Ok(status) => status,
    };
    ...
    assert!(
        is_controllee,
        "Operation aborted due to an error; no changes have been made: \
         Unable to determine whether this canister (SNS root) is the controller \
         of a registered dapp canister ({dapp_canister_id}). This may be due to \
         the canister having been deleted, which may be due to it running out \
         of cycles."
    );
}
``` [1](#0-0) 

A `panic!` in a canister context is a Wasm trap. The entire `set_dapp_controllers` update call is rejected, no state changes are committed, and the swap canister's finalization call receives a reject response. Because the swap canister calls `set_dapp_controllers` as part of its finalization flow, and because there is no mechanism to remove a deleted dapp canister from the registered list or to skip it, the swap is permanently stuck.

The developers themselves acknowledge the risk in the assert message: *"This may be due to the canister having been deleted, which may be due to it running out of cycles."* A `TODO(NNS1-1993)` comment marks the `panic!` as known-bad but unfixed. [2](#0-1) 

### Impact Explanation

**High.** Once a dapp canister is deleted, `set_dapp_controllers` can never succeed. The SNS swap finalization is permanently blocked for every participant:
- On a successful swap: SNS tokens cannot be distributed to buyers.
- On a failed/aborted swap: ICP cannot be returned to buyers.

The entire registered dapp canister list is always processed; there is no mechanism to skip a deleted entry or remove it from the list before finalization.

### Likelihood Explanation

**Low.** Dapp canisters are registered by SNS governance and SNS root becomes their sole controller, so the original developer cannot directly delete them. However, the IC automatically deletes canisters whose cycle balance drops to zero after a grace period. An attacker who can make many calls to a dapp canister (consuming its cycles) can cause it to be deleted. Alternatively, if no one tops up the canister's cycles, it will be deleted by the IC naturally. The registered dapp canister list is public, making targeting straightforward.

### Recommendation

1. Replace the `panic!` and `assert!` with graceful error returns (the existing `TODO(NNS1-1993)` tracks this).
2. Add a governance-callable function to deregister a dapp canister from the SNS root's list, analogous to the MultiRewards recommendation to allow governance to remove a reward token.
3. Emit a critical error metric and skip the deleted canister rather than aborting the entire loop, so the remaining dapp canisters can still have their controllers updated.

### Proof of Concept

1. An SNS is created with one or more dapp canisters registered via `register_dapp_canisters`.
2. An attacker repeatedly calls an expensive method on a registered dapp canister, draining its cycle balance. Alternatively, the canister's cycles are simply not replenished.
3. The IC deletes the dapp canister after its cycle balance reaches zero.
4. The SNS swap period ends; the swap canister calls `set_dapp_controllers` on SNS root to finalize.
5. The pre-flight loop reaches the deleted canister; `management_canister_client.canister_status(deleted_id)` returns an error.
6. The `panic!` at line 809 traps the call; the swap canister receives a reject.
7. Finalization cannot proceed. All swap participants are permanently locked out of their SNS tokens or ICP refunds. [3](#0-2) [1](#0-0)

### Citations

**File:** rs/sns/root/src/lib.rs (L762-790)
```rust
    pub async fn set_dapp_controllers<'a>(
        self_ref: &'static LocalKey<RefCell<Self>>,
        management_canister_client: &'a impl ManagementCanisterClient,
        own_canister_id: PrincipalId,
        caller: PrincipalId,
        request: &'a SetDappControllersRequest,
    ) -> SetDappControllersResponse {
        let is_authorized = self_ref.with(|self_ref| {
            caller == self_ref.borrow().swap_canister_id()
                || caller == self_ref.borrow().governance_canister_id()
        });
        // TODO(NNS1-1993): Remove this assertion and return an error type instead.
        assert!(is_authorized, "Caller ({caller}) is not authorized.");

        // Grab a snapshot of canisters to operate on.
        let dapp_canister_ids = match &request.canister_ids {
            Some(canister_ids) => canister_ids.canister_ids.clone(),
            // If no canister list is specified, we take all the canisters controlled by root.
            None => {
                let is_authorized_to_set_all_controllers =
                    self_ref.with(|self_ref| caller == self_ref.borrow().swap_canister_id());
                if is_authorized_to_set_all_controllers {
                    self_ref.with(|self_ref| self_ref.borrow().dapp_canister_ids.clone())
                } else {
                    // TODO(NNS1-1993): Remove this panic and return an error type instead.
                    panic!("Only the swap canister is authorized to set all dapp controllers")
                }
            }
        };
```

**File:** rs/sns/root/src/lib.rs (L796-825)
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
```
