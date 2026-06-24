### Title
Unrestricted `transfer_fee` Increase via `ManageLedgerParameters` Breaks `neuron_minimum_stake_e8s` Invariant, Freezing Neuron Operations - (File: `rs/sns/governance/src/proposal.rs`, `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance `ManageLedgerParameters` proposal action allows any SNS token holder with sufficient voting power to raise the ledger `transfer_fee` to an arbitrarily large value (up to `u64::MAX`) with no upper-bound validation. When the new fee exceeds the existing `neuron_minimum_stake_e8s`, the invariant `neuron_minimum_stake_e8s > transaction_fee_e8s` — enforced at SNS initialization and during `ManageNervousSystemParameters` proposals — is silently violated. This breaks neuron staking, splitting, and disbursement for all existing and future neuron holders without any on-chain protection.

---

### Finding Description

The `validate_and_render_manage_ledger_parameters` function in `rs/sns/governance/src/proposal.rs` performs no upper-bound check on the proposed `transfer_fee`:

```rust
if let Some(transfer_fee) = transfer_fee {
    render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
    change = true;
}
```

No comparison against `neuron_minimum_stake_e8s` is made at proposal submission time. [1](#0-0) 

When the proposal executes, `perform_manage_ledger_parameters` upgrades the ledger with the new fee and then unconditionally writes the new fee into `nervous_system_parameters.transaction_fee_e8s`:

```rust
if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
    && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
{
    nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
}
``` [2](#0-1) 

This write bypasses the `NervousSystemParameters::validate()` path entirely. That validator enforces `neuron_minimum_stake_e8s > transaction_fee_e8s`:

```rust
if neuron_minimum_stake_e8s <= transaction_fee_e8s {
    Err(...)
}
``` [3](#0-2) 

The `ManageLedgerParameters` struct accepts any `u64` value for `transfer_fee` with no ceiling: [4](#0-3) 

By contrast, `ManageNervousSystemParameters` proposals go through `new_params.validate()` before being applied, which would catch a fee exceeding the minimum stake. `ManageLedgerParameters` has no equivalent cross-parameter check. [5](#0-4) 

---

### Impact Explanation

Once `transfer_fee` is raised above `neuron_minimum_stake_e8s`:

1. **Neuron staking is broken**: Any attempt to stake a new neuron will fail because the staked amount cannot satisfy `neuron_minimum_stake_e8s` after the fee is deducted.
2. **Neuron splitting is broken**: `split_neuron` checks that the child neuron will have at least `neuron_minimum_stake_e8s` after the fee is deducted; with a fee larger than the minimum stake, this is impossible.
3. **Neuron disbursement is broken**: Disbursing a neuron requires a ledger transfer; if the fee exceeds the neuron's balance, the transfer fails.
4. **Governance participation is degraded**: Existing neuron holders cannot create new neurons or split existing ones, effectively freezing governance participation for the SNS.
5. **The invariant violation is permanent** unless a subsequent `ManageNervousSystemParameters` proposal raises `neuron_minimum_stake_e8s` above the new fee, or a `ManageLedgerParameters` proposal lowers the fee — both of which require governance participation that may itself be impaired.

The impact is a **governance authorization/ledger conservation bug**: critical SNS token holder operations are permanently disrupted for all users of the affected SNS.

---

### Likelihood Explanation

The entry path is a standard SNS governance proposal, submittable by any principal holding sufficient SNS voting power (i.e., a neuron with enough stake). This is an unprivileged ingress path — no admin key, no threshold corruption, and no external oracle is required. A malicious or negligent SNS token holder with majority voting power can trigger this in a single proposal. The SNS governance system is designed to allow token holders to pass `ManageLedgerParameters` proposals, and the missing validation is a straightforward oversight. The likelihood is **medium-high** for any SNS where voting power is concentrated or where a malicious actor acquires a majority stake.

---

### Recommendation

In `validate_and_render_manage_ledger_parameters` (`rs/sns/governance/src/proposal.rs`), add a check that the proposed `transfer_fee` does not exceed (or equal) the current `neuron_minimum_stake_e8s` stored in `NervousSystemParameters`. This requires passing the current parameters into the validation function. Alternatively, in `perform_manage_ledger_parameters` (`rs/sns/governance/src/governance.rs`), before writing the new `transaction_fee_e8s`, validate that the resulting `NervousSystemParameters` (with the new fee) still satisfies `NervousSystemParameters::validate()`, and reject the proposal execution if not.

---

### Proof of Concept

1. Deploy an SNS with default parameters: `neuron_minimum_stake_e8s = 1_000_000` (1 governance token), `transaction_fee_e8s = 10_000`.
2. Acquire majority voting power in the SNS.
3. Submit and pass a `ManageLedgerParameters` proposal with `transfer_fee = Some(2_000_000)` (greater than `neuron_minimum_stake_e8s`).
4. `validate_and_render_manage_ledger_parameters` accepts this — no upper-bound check exists. [1](#0-0) 
5. `perform_manage_ledger_parameters` upgrades the ledger with `transfer_fee = 2_000_000` and writes `nervous_system_parameters.transaction_fee_e8s = Some(2_000_000)`. [2](#0-1) 
6. Now `transaction_fee_e8s (2_000_000) > neuron_minimum_stake_e8s (1_000_000)`.
7. Any attempt to stake a new neuron with `1_000_000` e8s fails: after the fee is deducted, the resulting stake is negative/zero, violating the minimum stake requirement.
8. Any attempt to split a neuron fails for the same reason.
9. The SNS governance is effectively frozen for new participants; existing neuron holders cannot split or disburse neurons whose balance is below the new fee.

### Citations

**File:** rs/sns/governance/src/proposal.rs (L1773-1776)
```rust
    if let Some(transfer_fee) = transfer_fee {
        render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
        change = true;
    }
```

**File:** rs/sns/governance/src/governance.rs (L2587-2597)
```rust
        let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());

        log!(
            INFO,
            "Setting Governance nervous system params to: {:?}",
            &new_params
        );

        match new_params.validate() {
            Ok(()) => {
                self.proto.parameters = Some(new_params);
```

**File:** rs/sns/governance/src/governance.rs (L3191-3195)
```rust
                    if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
                        && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
                    {
                        nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
                    }
```

**File:** rs/sns/governance/src/types.rs (L610-617)
```rust
        if neuron_minimum_stake_e8s <= transaction_fee_e8s {
            Err(format!(
                "NervousSystemParameters.neuron_minimum_stake_e8s ({neuron_minimum_stake_e8s}) must be greater than \
                NervousSystemParameters.transaction_fee_e8s ({neuron_minimum_stake_e8s})"
            ))
        } else {
            Ok(())
        }
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L558-567)
```rust
pub struct ManageLedgerParameters {
    #[prost(uint64, optional, tag = "1")]
    pub transfer_fee: ::core::option::Option<u64>,
    #[prost(string, optional, tag = "2")]
    pub token_name: ::core::option::Option<::prost::alloc::string::String>,
    #[prost(string, optional, tag = "3")]
    pub token_symbol: ::core::option::Option<::prost::alloc::string::String>,
    #[prost(string, optional, tag = "4")]
    pub token_logo: ::core::option::Option<::prost::alloc::string::String>,
}
```
