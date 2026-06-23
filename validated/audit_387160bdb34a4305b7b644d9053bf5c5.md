### Title
Neuron Claiming Allows Specifying Arbitrary Canister as Controller Without Capability Verification, Permanently Locking Staked ICP — (File: `rs/nns/governance/src/governance.rs`)

### Summary

The NNS (and SNS) governance canister's `ClaimOrRefresh` command accepts an optional `controller` field in `MemoAndController`. Any unprivileged ingress sender can specify an arbitrary `PrincipalId` — including a canister — as the neuron controller. Since June 2024, the self-authenticating restriction on neuron controllers was explicitly removed. There is no check that the specified controller canister is capable of calling `manage_neuron` to dissolve or disburse the neuron. If a neuron is claimed with a canister as controller that does not implement the governance management interface, the staked ICP is permanently locked with no recovery path.

### Finding Description

In `rs/nns/governance/src/governance.rs`, the function `claim_or_refresh_neuron_by_memo_and_controller` accepts a caller-supplied `controller` field:

```rust
let controller = memo_and_controller.controller.unwrap_or(*caller);
``` [1](#0-0) 

When the subaccount has no existing neuron, `claim_neuron` is called with the caller-supplied `controller` directly:

```rust
None => {
    self.claim_neuron(subaccount, controller, claim_or_refresh)
        .await
}
``` [2](#0-1) 

Inside `claim_neuron`, the neuron is built with the supplied `controller` without any validation that the controller is capable of managing the neuron:

```rust
let neuron = NeuronBuilder::new(
    nid,
    subaccount,
    controller,
    ...
)
``` [3](#0-2) 

The codebase explicitly documents that since June 2024, canisters are permitted as neuron controllers:

> "It used to be that controllers must be self-authenticating. Later (Jun, 2024) we got rid of that requirement. That is, the controller can be any type of principal (including canister)." [4](#0-3) 

The `manage_neuron` entrypoint passes the `MemoAndController` directly to `claim_or_refresh_neuron_by_memo_and_controller` without any restriction on who can specify the `controller` field: [5](#0-4) 

The same pattern exists in SNS governance: [6](#0-5) 

The proto definition confirms the `controller` field is fully optional and caller-supplied: [7](#0-6) 

A test explicitly confirms that any third party can claim a neuron on behalf of an arbitrary controller: [8](#0-7) 

### Impact Explanation

If a neuron is claimed with a canister as controller that does not implement the `manage_neuron` interface (i.e., cannot call `Disburse`, `StartDissolving`, or any other neuron management command), the staked ICP is permanently locked inside the governance canister's subaccount. The neuron can never be dissolved or disbursed. The ICP is effectively burned. This is a ledger conservation violation: ICP enters the governance canister but can never exit.

### Likelihood Explanation

The likelihood is low-to-medium. Two realistic paths exist:

1. **User error**: A developer intending to stake ICP for a canister-controlled neuron sends ICP to `governance_subaccount(canister_id, memo)` and claims the neuron, but the canister does not implement `manage_neuron`. The ICP is permanently locked.

2. **Griefing**: An attacker sends ICP to `governance_subaccount(victim_canister_id, memo)` and calls `ClaimOrRefresh { controller: Some(victim_canister_id), memo }`. The neuron is created with the victim canister as controller. If the victim canister cannot manage neurons, the ICP is locked. The attacker loses the ICP they sent but successfully locks it.

Path 1 is increasingly likely as canister-controlled neurons become a common pattern (e.g., DAO treasuries, protocol-owned liquidity), especially since the self-authenticating restriction was removed in June 2024.

### Recommendation

1. **Validate controller capability**: Before creating a neuron with a canister as controller, verify that the canister exposes a neuron management interface (e.g., via a canister metadata check or a registry of governance-capable canisters).
2. **Alternatively, require explicit opt-in**: Require the controller canister to have previously registered itself as capable of managing neurons, or require the controller to co-sign the claim.
3. **At minimum, document the risk**: Add a clear warning in the `ClaimOrRefresh` API documentation that specifying a canister as controller without ensuring it implements `manage_neuron` will permanently lock the staked ICP.
4. **Apply the same fix to SNS governance**: The identical pattern exists in `rs/sns/governance/src/governance.rs`.

### Proof of Concept

1. Deploy a canister `C` that does **not** implement `manage_neuron`.
2. Transfer at least `neuron_minimum_stake_e8s` ICP to `AccountIdentifier::new(GOVERNANCE_CANISTER_ID, Some(compute_neuron_staking_subaccount(C.principal, memo)))`.
3. Call `manage_neuron` on the NNS governance canister from any principal with:
   ```
   ClaimOrRefresh {
     by: MemoAndController {
       memo: <memo>,
       controller: Some(C.principal)
     }
   }
   ```
4. The governance canister creates a neuron with `C` as controller and the staked ICP as stake.
5. Since `C` cannot call `manage_neuron`, the neuron can never be dissolved or disbursed. The ICP is permanently locked.

### Citations

**File:** rs/nns/governance/src/governance.rs (L5852-5870)
```rust
    async fn claim_or_refresh_neuron_by_memo_and_controller(
        &mut self,
        caller: &PrincipalId,
        memo_and_controller: MemoAndController,
        claim_or_refresh: &ClaimOrRefresh,
    ) -> Result<NeuronId, GovernanceError> {
        let controller = memo_and_controller.controller.unwrap_or(*caller);
        let memo = memo_and_controller.memo;
        let subaccount = ledger::compute_neuron_staking_subaccount(controller, memo);
        match self.neuron_store.get_neuron_id_for_subaccount(subaccount) {
            Some(neuron_id) => {
                self.refresh_neuron(neuron_id, subaccount, claim_or_refresh)
                    .await
            }
            None => {
                self.claim_neuron(subaccount, controller, claim_or_refresh)
                    .await
            }
        }
```

**File:** rs/nns/governance/src/governance.rs (L6000-6012)
```rust
        let neuron = NeuronBuilder::new(
            nid,
            subaccount,
            controller,
            DissolveStateAndAge::NotDissolving {
                dissolve_delay_seconds: INITIAL_NEURON_DISSOLVE_DELAY,
                aging_since_timestamp_seconds: now,
            },
            now,
        )
        .with_followees(self.heap_data.default_followees.clone())
        .with_kyc_verified(true)
        .build();
```

**File:** rs/nns/governance/src/governance.rs (L6123-6130)
```rust
                Some(By::MemoAndController(memo_and_controller)) => self
                    .claim_or_refresh_neuron_by_memo_and_controller(
                        caller,
                        memo_and_controller.clone(),
                        claim_or_refresh,
                    )
                    .await
                    .map(ManageNeuronResponse::claim_or_refresh_neuron_response),
```

**File:** rs/nns/governance/tests/governance.rs (L4818-4826)
```rust
/// Tests that a non-controller can claim a neuron for the controller (the
/// principal whose id was used to build the subaccount).
#[test]
#[cfg_attr(feature = "tla", with_tla_trace_check)]
fn test_claim_neuron_memo_and_controller_by_proxy() {
    let owner = *TEST_NEURON_1_OWNER_PRINCIPAL;
    let caller = *TEST_NEURON_2_OWNER_PRINCIPAL;
    do_test_claim_neuron_by_memo_and_controller(owner, caller);
}
```

**File:** rs/nns/governance/tests/governance.rs (L6503-6506)
```rust
/// It used to be that controllers must be self-authenticating. Later (Jun, 2024) we got rid of that
/// requirement. That is, the controller can be any type of principal (including canister).
/// Discussed here:
/// https://forum.dfinity.org/t/reevaluating-neuron-control-restrictions/28597
```

**File:** rs/sns/governance/src/governance.rs (L4210-4227)
```rust
    async fn claim_or_refresh_neuron_by_memo_and_controller(
        &mut self,
        caller: &PrincipalId,
        memo_and_controller: &MemoAndController,
    ) -> Result<(), GovernanceError> {
        let controller = memo_and_controller.controller.unwrap_or(*caller);
        let memo = memo_and_controller.memo;
        let nid = NeuronId::from(ledger::compute_neuron_staking_subaccount_bytes(
            controller, memo,
        ));
        match self.get_neuron_result(&nid) {
            Ok(neuron) => {
                let nid = neuron.id.as_ref().expect("Neuron must have an id").clone();
                self.refresh_neuron(&nid).await
            }
            Err(_) => self.claim_neuron(nid, &controller).await,
        }
    }
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L944-966)
```text
  message ClaimOrRefresh {
    message MemoAndController {
      uint64 memo = 1;
      ic_base_types.pb.v1.PrincipalId controller = 2;
    }

    oneof by {
      // DEPRECATED: Use MemoAndController and omit the controller.
      uint64 memo = 1;

      // Claim or refresh a neuron, by providing the memo used in the
      // staking transfer and 'controller' as the principal id used to
      // calculate the subaccount to which the transfer was made. If
      // 'controller' is omitted, the principal id of the caller is
      // used.
      MemoAndController memo_and_controller = 2;

      // This just serves as a tag to indicate that the neuron should be
      // refreshed by it's id or subaccount. This does not work to claim
      // new neurons.
      Empty neuron_id_or_subaccount = 3;
    }
  }
```
