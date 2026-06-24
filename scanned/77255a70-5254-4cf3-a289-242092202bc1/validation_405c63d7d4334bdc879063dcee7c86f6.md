### Title
Missing Anonymous-Principal Validation in SNS Swap `Init::validate()` Allows Permanently Broken Swap Deployment - (File: rs/sns/swap/src/types.rs)

### Summary
The `validate_canister_id` and `validate_principal` helper functions used in `Init::validate()` for the SNS Swap canister only check that a string is parseable as a `PrincipalId`, but do not reject the anonymous principal (`2vxsx-fae`). Because the `Init` struct is explicitly documented as immutable after canister creation, a swap deployed with the anonymous principal in any critical field is permanently misconfigured with no recovery path.

### Finding Description
`validate_canister_id` in `rs/sns/swap/src/types.rs` performs only a parse check:

```rust
pub fn validate_canister_id(p: &str) -> Result<(), String> {
    let _pp = PrincipalId::from_str(p).map_err(|x| { ... })?;
    Ok(())
}
``` [1](#0-0) 

`Init::validate()` calls this for all five critical canister IDs:

```rust
validate_canister_id(&self.nns_governance_canister_id)?;
validate_canister_id(&self.sns_governance_canister_id)?;
validate_canister_id(&self.sns_ledger_canister_id)?;
validate_canister_id(&self.icp_ledger_canister_id)?;
validate_canister_id(&self.sns_root_canister_id)?;
``` [2](#0-1) 

The anonymous principal string `"2vxsx-fae"` is a valid `PrincipalId`, so it passes this check silently. The same gap exists in `validate_principal`, used for `fallback_controller_principal_ids`:

```rust
pub fn validate_principal(p: &str) -> Result<(), String> {
    let _ = PrincipalId::from_str(p).map_err(|x| { ... })?;
    Ok(())
}
``` [3](#0-2) 

The `Init` struct's own documentation confirms immutability:

> "canister creation, and cannot be modified afterwards. If the initialization parameters are incorrect, the swap will immediately be aborted." [4](#0-3) 

`Swap::new()` panics only if `init.validate()` returns `Err`, but since anonymous principals pass validation, the canister initializes successfully in a permanently broken state: [5](#0-4) 

The existing integration test confirms this is reachable — the swap canister is successfully installed with all five canister IDs set to `Principal::anonymous()`: [6](#0-5) 

A parallel broken validation exists in SNS Governance. `validate_canister_id_field` is explicitly acknowledged as a no-op:

```rust
fn validate_canister_id_field(name: &str, principal_id: PrincipalId) -> Result<(), String> {
    // TODO(NNS1-1992) – CanisterId::try_from always returns `Ok(_)` so this
    // check does nothing.
    match CanisterId::try_from(principal_id) { Ok(_) => Ok(()), ... }
}
``` [7](#0-6) 

This means `root_canister_id`, `ledger_canister_id`, and `swap_canister_id` in SNS Governance also accept the anonymous principal without error. [8](#0-7) 

### Impact Explanation
**Scenario A — Permanently broken swap:** A developer (canister caller/developer attacker class) deploys the SNS Swap canister directly with any of the five critical canister IDs set to the anonymous principal. `Init::validate()` accepts the configuration. All subsequent cross-canister calls to governance, ledger, and root canisters target the anonymous principal, which is not a real canister. Every `open_swap`, `finalize_swap`, and `abort_swap` operation fails permanently. Because `Init` is immutable, there is no recovery path.

**Scenario B — Anonymous principal as fallback controller:** If `fallback_controller_principal_ids` includes `"2vxsx-fae"` and the swap is aborted, the SNS Root canister calls `set_dapp_controllers` with the anonymous principal as one of the controllers. On the IC, the anonymous principal is the sender of every unauthenticated ingress message. Any user sending an unsigned request to the management canister's `update_settings` would be authorized to stop, delete, or upgrade the dapp canisters, constituting a full canister takeover.

### Likelihood Explanation
The normal SNS deployment path through SNS-W uses real canister IDs derived from freshly created canisters, so the risk in that flow is low. However, the vulnerability is directly reachable by any developer who deploys the SNS Swap canister outside the SNS-W flow (a supported and documented operation), or who constructs a `SwapInit` payload manually. The integration test suite itself demonstrates the path is exercised with anonymous principals. There is no runtime guard, no post-deployment setter, and no upgrade path to correct the misconfiguration.

### Recommendation
1. In `validate_canister_id` (`rs/sns/swap/src/types.rs`), add an explicit rejection of the anonymous principal:
   ```rust
   if _pp.is_anonymous() {
       return Err(format!("CanisterId \"{p}\" must not be the anonymous principal."));
   }
   ```
2. Apply the same check in `validate_principal` for `fallback_controller_principal_ids`.
3. Fix `validate_canister_id_field` in `rs/sns/governance/src/governance.rs` (tracked as TODO NNS1-1992) to use `CanisterId::try_from_principal_id` instead of `CanisterId::try_from`, and add an anonymous-principal check.
4. Add unit tests asserting that `Init::validate()` and `ValidGovernanceProto::try_from()` reject the anonymous principal for all canister ID fields.

### Proof of Concept
The existing integration test at `rs/sns/integration_tests/src/swap.rs` already demonstrates the issue — the swap canister installs successfully with every canister ID set to `Principal::anonymous()`:

```rust
let args = Encode!(&Init {
    nns_governance_canister_id: Principal::anonymous().to_string(),
    sns_governance_canister_id: Principal::anonymous().to_string(),
    sns_ledger_canister_id: Principal::anonymous().to_string(),
    icp_ledger_canister_id: Principal::anonymous().to_string(),
    sns_root_canister_id: Principal::anonymous().to_string(),
    fallback_controller_principal_ids: vec![Principal::anonymous().to_string()],
    ...
}).unwrap();
let canister_id = state_machine.install_canister(wasm.clone(), args, None).unwrap();
// Succeeds — no validation error is raised
``` [9](#0-8)

### Citations

**File:** rs/sns/swap/src/types.rs (L28-35)
```rust
pub fn validate_principal(p: &str) -> Result<(), String> {
    let _ = PrincipalId::from_str(p).map_err(|x| {
        format!(
            "Couldn't validate PrincipalId. String \"{p}\" could not be converted to PrincipalId: {x}"
        )
    })?;
    Ok(())
}
```

**File:** rs/sns/swap/src/types.rs (L37-44)
```rust
pub fn validate_canister_id(p: &str) -> Result<(), String> {
    let _pp = PrincipalId::from_str(p).map_err(|x| {
        format!(
            "Couldn't validate CanisterId. String \"{p}\" could not be converted to PrincipalId: {x}"
        )
    })?;
    Ok(())
}
```

**File:** rs/sns/swap/src/types.rs (L282-295)
```rust
    pub fn validate(&self) -> Result<(), String> {
        validate_canister_id(&self.nns_governance_canister_id)?;
        validate_canister_id(&self.sns_governance_canister_id)?;
        validate_canister_id(&self.sns_ledger_canister_id)?;
        validate_canister_id(&self.icp_ledger_canister_id)?;
        validate_canister_id(&self.sns_root_canister_id)?;

        if self.fallback_controller_principal_ids.is_empty() {
            return Err("at least one fallback controller required".to_string());
        }
        for fc in &self.fallback_controller_principal_ids {
            validate_principal(fc)?;
        }

```

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L244-248)
```rust
/// canister creation, and cannot be modified afterwards.
///
/// If the initialization parameters are incorrect, the swap will
/// immediately be aborted.
#[derive(
```

**File:** rs/sns/swap/src/swap.rs (L401-404)
```rust
    pub fn new(init: Init) -> Self {
        if let Err(e) = init.validate() {
            panic!("Invalid init arg, reason: {e}\nArg: {init:#?}\n");
        }
```

**File:** rs/sns/integration_tests/src/swap.rs (L14-47)
```rust
    let args = Encode!(&Init {
        nns_governance_canister_id: Principal::anonymous().to_string(),
        sns_governance_canister_id: Principal::anonymous().to_string(),
        sns_ledger_canister_id: Principal::anonymous().to_string(),
        icp_ledger_canister_id: Principal::anonymous().to_string(),
        sns_root_canister_id: Principal::anonymous().to_string(),
        fallback_controller_principal_ids: vec![Principal::anonymous().to_string()],
        transaction_fee_e8s: Some(10_000),
        neuron_minimum_stake_e8s: Some(1_000_000),
        confirmation_text: None,
        restricted_countries: None,
        min_participants: Some(5),
        min_icp_e8s: None,
        max_icp_e8s: None,
        min_direct_participation_icp_e8s: Some(12_300_000_000),
        max_direct_participation_icp_e8s: Some(65_000_000_000),
        min_participant_icp_e8s: Some(6_500_000_000),
        max_participant_icp_e8s: Some(65_000_000_000),
        swap_start_timestamp_seconds: Some(0),
        swap_due_timestamp_seconds: Some(u64::MAX),
        sns_token_e8s: Some(10_000_000),
        neuron_basket_construction_parameters: Some(NeuronBasketConstructionParameters {
            count: 5,
            dissolve_delay_interval_seconds: 10_001,
        }),
        nns_proposal_id: Some(10),
        should_auto_finalize: Some(true),
        neurons_fund_participation_constraints: None,
        neurons_fund_participation: Some(false),
    })
    .unwrap();
    let canister_id = state_machine
        .install_canister(wasm.clone(), args, None)
        .unwrap();
```

**File:** rs/sns/governance/src/governance.rs (L499-508)
```rust
    fn validate_canister_id_field(name: &str, principal_id: PrincipalId) -> Result<(), String> {
        // TODO(NNS1-1992) – CanisterId::try_from always returns `Ok(_)` so this
        // check does nothing.
        match CanisterId::try_from(principal_id) {
            Ok(_) => Ok(()),
            Err(err) => Err(format!(
                "Unable to convert {name} PrincipalId to CanisterId: {err:#?}",
            )),
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L525-527)
```rust
        Self::validate_canister_id_field("root", root_canister_id)?;
        Self::validate_canister_id_field("ledger", ledger_canister_id)?;
        Self::validate_canister_id_field("swap", swap_canister_id)?;
```
