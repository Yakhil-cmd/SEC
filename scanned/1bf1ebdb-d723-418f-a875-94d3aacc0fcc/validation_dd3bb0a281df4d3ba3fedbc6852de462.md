### Title
SNS Governance `reserved_canister_targets` Hardcoded Blacklist Omits Index, Archive, and Extension Canisters - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The `reserved_canister_targets()` function in SNS governance returns a hardcoded list of canisters that cannot be targeted by `GenericNervousSystemFunction` proposals. This list omits the SNS index canister, ledger archive canisters, and extension canisters — all of which are registered core SNS canisters that hold value or manage critical state. A malicious SNS governance majority can submit an `AddGenericNervousSystemFunction` proposal targeting any of these omitted canisters, pass validation, and then execute arbitrary inter-canister calls against them via `ExecuteGenericNervousSystemFunction`.

---

### Finding Description

`reserved_canister_targets()` in `rs/sns/governance/src/governance.rs` returns a hardcoded list of six canister IDs that are blocked from being targeted by `GenericNervousSystemFunction`s:

```rust
pub fn reserved_canister_targets(&self) -> Vec<CanisterId> {
    vec![
        self.env.canister_id(),                   // governance
        self.proto.root_canister_id_or_panic(),   // root
        self.proto.ledger_canister_id_or_panic(), // ledger
        self.proto.swap_canister_id_or_panic(),   // swap
        NNS_LEDGER_CANISTER_ID,
        CanisterId::ic_00(),
    ]
}
``` [1](#0-0) 

This list is consumed by `perform_add_generic_nervous_system_function()` to reject proposals that target reserved canisters: [2](#0-1) 

However, the SNS system registers additional core canisters beyond those six. The `SnsRootCanister` state struct tracks:
- `index_canister_id` — the SNS index canister (tracks token balances)
- `archive_canister_ids` — ledger archive canisters (hold historical ledger blocks)
- `extensions.extension_canister_ids` — extension canisters [3](#0-2) 

None of these appear in `reserved_canister_targets()`. The governance canister has no knowledge of `index_canister_id`, `archive_canister_ids`, or `extension_canister_ids` when building the reserved list — it only reads from its own proto fields, which do not include those IDs. [4](#0-3) 

The validation path for `AddGenericNervousSystemFunction` proposals also uses this same `disallowed_target_canister_ids` set: [5](#0-4) 

Because the index, archive, and extension canisters are absent from the reserved list, a proposal to register a `GenericNervousSystemFunction` targeting any of them passes validation without error.

---

### Impact Explanation

A malicious SNS governance majority can:

1. Submit an `AddGenericNervousSystemFunction` proposal with `target_canister_id` set to the SNS index canister or any archive canister. This passes the `reserved_canister_targets()` check because those IDs are not in the list.
2. Once adopted, submit `ExecuteGenericNervousSystemFunction` proposals to call arbitrary methods on those canisters from the governance canister's principal.
3. The SNS index canister tracks token balances; arbitrary calls to it could corrupt balance state. Archive canisters hold immutable ledger history; calls to their admin methods (if callable from governance) could corrupt the historical record. Extension canisters are newly registered and their exposure surface is unbounded.

This is a governance authorization bypass: the blacklist is structurally incomplete and cannot be extended without upgrading the governance canister itself, mirroring the exact class of bug in the Rocket Pool report.

---

### Likelihood Explanation

Medium. The attack requires an SNS governance majority (enough voting power to pass proposals). However, SNS governance is open to any token holder, and a coordinated group of malicious token holders — or a single actor who accumulates sufficient stake — can exploit this without any privileged access. The `AddGenericNervousSystemFunction` proposal type is a standard, publicly available governance action reachable by any SNS neuron holder.

---

### Recommendation

- Extend `reserved_canister_targets()` to dynamically query the SNS root canister for the full list of registered SNS canisters (including index, archives, and extensions) before building the reserved set, rather than relying on a hardcoded list derived only from governance's own proto fields.
- Alternatively, at proposal submission time, cross-check the target canister ID against the live `list_sns_canisters` response from root.
- Document which canisters are and are not targetable by `GenericNervousSystemFunction`s, and add a process to verify this list whenever new canister types are added to the SNS framework.

---

### Proof of Concept

1. Deploy a full SNS (governance, root, ledger, index, swap).
2. As an SNS neuron holder, submit an `AddGenericNervousSystemFunction` proposal:
   - `target_canister_id` = the SNS index canister ID (obtained from `list_sns_canisters`)
   - `target_method_name` = any publicly callable method on the index canister
   - `validator_canister_id` = any non-reserved canister
3. Observe that `validate_and_render_add_generic_nervous_system_function` succeeds — the index canister ID is not in `disallowed_target_canister_ids` because `reserved_canister_targets()` does not include it. [6](#0-5) 

4. After the proposal is adopted, submit `ExecuteGenericNervousSystemFunction` referencing the newly registered function ID. The governance canister will call the index canister's method directly, with the proposal payload as the argument, from the governance canister's principal — bypassing any assumption that only root controls the index canister.

### Citations

**File:** rs/sns/governance/src/governance.rs (L807-817)
```rust
    // Returns the ids of canisters that cannot be targeted by GenericNervousSystemFunctions.
    pub fn reserved_canister_targets(&self) -> Vec<CanisterId> {
        vec![
            self.env.canister_id(),
            self.proto.root_canister_id_or_panic(),
            self.proto.ledger_canister_id_or_panic(),
            self.proto.swap_canister_id_or_panic(),
            NNS_LEDGER_CANISTER_ID,
            CanisterId::ic_00(),
        ]
    }
```

**File:** rs/sns/governance/src/governance.rs (L2271-2285)
```rust
        // This validates that it is well-formed, but not the canister targets.
        match ValidGenericNervousSystemFunction::try_from(&nervous_system_function) {
            Ok(valid_function) => {
                let reserved_canisters = self.reserved_canister_targets();
                let target_canister_id = valid_function.target_canister_id;
                let validator_canister_id = valid_function.validator_canister_id;

                if reserved_canisters.contains(&target_canister_id)
                    || reserved_canisters.contains(&validator_canister_id)
                {
                    return Err(GovernanceError::new_with_message(
                        ErrorType::PreconditionFailed,
                        "Cannot add generic nervous system functions that targets sns core canisters, the NNS ledger, or ic00",
                    ));
                }
```

**File:** rs/sns/governance/src/sns_root_types.rs (L15-54)
```rust
pub struct SnsRootCanister {
    /// Required.
    ///
    /// The SNS root canister is supposed to be able to control this canister.  The
    /// governance canister sends the SNS root canister change_governance_canister
    /// update method calls (and possibly other things).
    #[prost(message, optional, tag = "1")]
    pub governance_canister_id: ::core::option::Option<::ic_base_types::PrincipalId>,
    /// Required.
    ///
    /// The SNS Ledger canister ID
    #[prost(message, optional, tag = "2")]
    pub ledger_canister_id: ::core::option::Option<::ic_base_types::PrincipalId>,
    /// Dapp canister IDs.
    #[prost(message, repeated, tag = "3")]
    pub dapp_canister_ids: ::prost::alloc::vec::Vec<::ic_base_types::PrincipalId>,
    /// Extension canister IDs.
    #[prost(message, optional, tag = "11")]
    pub extensions: ::core::option::Option<Extensions>,
    /// Required.
    ///
    /// The swap canister ID.
    #[prost(message, optional, tag = "4")]
    pub swap_canister_id: ::core::option::Option<::ic_base_types::PrincipalId>,
    /// CanisterIds of the archives of the SNS Ledger blocks.
    #[prost(message, repeated, tag = "5")]
    pub archive_canister_ids: ::prost::alloc::vec::Vec<::ic_base_types::PrincipalId>,
    /// Required.
    ///
    /// The SNS Index canister ID
    #[prost(message, optional, tag = "7")]
    pub index_canister_id: ::core::option::Option<::ic_base_types::PrincipalId>,
    /// True if the SNS is running in testflight mode. Then additional
    /// controllers beyond SNS root are allowed when registering a dapp.
    #[prost(bool, tag = "8")]
    pub testflight: bool,
    /// Information about the timers that perform periodic tasks of this Root canister.
    #[prost(message, optional, tag = "10")]
    pub timers: ::core::option::Option<::ic_nervous_system_proto::pb::v1::Timers>,
}
```

**File:** rs/sns/governance/src/proposal.rs (L1373-1393)
```rust
pub fn validate_and_render_add_generic_nervous_system_function(
    disallowed_target_canister_ids: &HashSet<CanisterId>,
    add: &NervousSystemFunction,
    existing_functions: &BTreeMap<u64, NervousSystemFunction>,
) -> Result<String, String> {
    let validated_function = ValidGenericNervousSystemFunction::try_from(add)?;
    if existing_functions.contains_key(&validated_function.id) {
        return Err(format!(
            "There is already a NervousSystemFunction with id: {}",
            validated_function.id
        ));
    }

    let target_canister_id = validated_function.target_canister_id;
    let validator_canister_id = validated_function.validator_canister_id;

    if disallowed_target_canister_ids.contains(&target_canister_id)
        || disallowed_target_canister_ids.contains(&validator_canister_id)
    {
        return Err("Function targets a reserved canister.".to_string());
    }
```
