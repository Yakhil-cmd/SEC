### Title
Fee Inconsistency in SNS `disburse_neuron` Between Fee Burn and Stake Transfer - (`File: rs/sns/governance/src/governance.rs`)

### Summary
The SNS governance `disburse_neuron` function reads `transaction_fee_e8s` once at the start of execution, but the SNS ledger's `transfer_fee` can be changed by a `ManageLedgerParameters` proposal between the two async await points inside the function. If the fee changes between the neuron-management-fee burn and the stake disbursement transfer, the second ledger call fails with `BadFee`, leaving the user's management fees permanently burned while their stake is never returned.

### Finding Description

`disburse_neuron` in `rs/sns/governance/src/governance.rs` is an `async` function with two sequential inter-canister calls to the SNS ledger:

1. **Transfer 1** – burn neuron management fees (line ~1182–1191)
2. **Transfer 2** – disburse stake to the user (line ~1210–1220)

The `transaction_fee_e8s` used for both computations and for the fee argument of Transfer 2 is captured once at the top of the function:

```rust
let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();
``` [1](#0-0) 

This value is then used to compute `disburse_amount_e8s` and is passed directly as the fee to the second ledger call:

```rust
let block_height = self
    .ledger
    .transfer_funds(
        disburse_amount_e8s,
        transaction_fee_e8s,   // ← stale if fee changed
        Some(from_subaccount),
        to_account,
        self.env.now(),
    )
    .await?;
``` [2](#0-1) 

The SNS ledger's `transfer_fee` is mutable. It is changed by executing a `ManageLedgerParameters` governance proposal, which upgrades the ledger canister with a new `transfer_fee`:

```rust
async fn perform_manage_ledger_parameters(
    &mut self,
    proposal_id: u64,
    manage_ledger_parameters: ManageLedgerParameters,
) -> Result<(), GovernanceError> {
``` [3](#0-2) 

The `ManageLedgerParameters` action carries an optional `transfer_fee` field:

```rust
pub struct ManageLedgerParameters {
    pub transfer_fee: Option<u64>,
    ...
}
``` [4](#0-3) 

This is confirmed to work end-to-end by the integration test `test_manage_ledger_parameters_change_transfer_fee`, which verifies that the ledger fee and governance's cached `transaction_fee_e8s` are both updated when the proposal executes:

```rust
assert_eq!(
    nervous_system_parameters_with_new_fee.transaction_fee_e8s,
    Some(new_fee)
);
``` [5](#0-4) 

Because `disburse_neuron` is async and suspends at the first `await` (Transfer 1), the governance canister can process other messages during that suspension — including completing the execution of a `ManageLedgerParameters` proposal that changes the ledger fee. When `disburse_neuron` resumes and issues Transfer 2 with the now-stale `transaction_fee_e8s`, the ledger rejects it with `BadFee`.

### Impact Explanation

**Impact: Medium**

The neuron management fees are burned in Transfer 1 (which succeeds), but the stake disbursement in Transfer 2 fails. The user permanently loses their neuron management fees without receiving their staked tokens. The neuron's `cached_neuron_stake_e8s` and `neuron_fees_e8s` are updated to reflect the burned fees before Transfer 2 is attempted:

```rust
neuron.cached_neuron_stake_e8s = neuron
    .cached_neuron_stake_e8s
    .saturating_sub(max_burnable_fee);
neuron.neuron_fees_e8s = neuron.neuron_fees_e8s.saturating_sub(max_burnable_fee);
``` [6](#0-5) 

After the failure, the neuron's on-chain ledger balance is reduced by the burned fees, but the user receives nothing. This is a ledger conservation violation: tokens are destroyed without a corresponding credit to any user account.

### Likelihood Explanation

**Likelihood: Low-Medium**

The window requires a `ManageLedgerParameters` proposal to complete execution between the two ledger calls inside a single `disburse_neuron` invocation. Both operations are async and involve inter-canister calls, so the IC scheduler can interleave them. No privileged access is required beyond normal SNS governance participation (submitting and voting on proposals). The scenario is not directly attacker-controlled but can occur naturally during normal SNS operation whenever a fee-change proposal is active.

### Recommendation

Capture `transaction_fee_e8s` immediately before each ledger call rather than once at the top of the function, or re-read the current fee from governance state before issuing Transfer 2. Alternatively, store the fee used for Transfer 1 in the neuron's in-flight command record so that Transfer 2 uses the same value regardless of subsequent state changes — analogous to how the external report recommends locking fee values at request time.

### Proof of Concept

1. Alice calls `disburse_neuron` on her dissolved SNS neuron. Governance reads `transaction_fee_e8s = 10_000` and computes `disburse_amount_e8s = stake - 10_000`.
2. Governance issues Transfer 1 (burn management fees) and suspends at `await`.
3. While suspended, a `ManageLedgerParameters` proposal completes execution, upgrading the SNS ledger with `transfer_fee = 20_000`. Governance's `NervousSystemParameters.transaction_fee_e8s` is also updated to `20_000`.
4. Governance resumes and issues Transfer 2: `transfer_funds(disburse_amount_e8s, 10_000, ...)`. The ledger now expects fee `20_000` and returns `BadFee`.
5. `disburse_neuron` returns an error. Alice's management fees are gone (burned in step 2), her neuron's cached stake is reduced, but her staked tokens were never transferred to her account.

### Citations

**File:** rs/sns/governance/src/governance.rs (L1138-1138)
```rust
        let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();
```

**File:** rs/sns/governance/src/governance.rs (L1181-1209)
```rust
        if max_burnable_fee > transaction_fee_e8s {
            let _result = self
                .ledger
                .transfer_funds(
                    max_burnable_fee,
                    0, // Burning transfers don't pay a fee.
                    Some(from_subaccount),
                    self.governance_minting_account(),
                    self.env.now(),
                )
                .await?;

            // We only update the cached_neuron_stake_e8s and neuron_fees_e8s if we actually
            // burn fees, otherwise this leads to ledger and governance getting out of sync.
            let nid = id.to_string();
            let neuron = self
                .proto
                .neurons
                .get_mut(&nid)
                .expect("Expected the parent neuron to exist");

            // Update the neuron's stake and management fees to reflect the burning
            // above.
            neuron.cached_neuron_stake_e8s = neuron
                .cached_neuron_stake_e8s
                .saturating_sub(max_burnable_fee);

            neuron.neuron_fees_e8s = neuron.neuron_fees_e8s.saturating_sub(max_burnable_fee);
        }
```

**File:** rs/sns/governance/src/governance.rs (L3090-3095)
```rust
    async fn perform_manage_ledger_parameters(
        &mut self,
        proposal_id: u64,
        manage_ledger_parameters: ManageLedgerParameters,
    ) -> Result<(), GovernanceError> {
        self.check_no_upgrades_in_progress(Some(proposal_id))?;
```

**File:** rs/sns/governance/api/src/ic_sns_governance.pb.v1.rs (L455-463)
```rust
/// A proposal function that changes the ledger's parameters.
/// Fields with None values will remain unchanged.
#[derive(Default, candid::CandidType, candid::Deserialize, Debug, Clone, PartialEq)]
pub struct ManageLedgerParameters {
    pub transfer_fee: Option<u64>,
    pub token_name: Option<String>,
    pub token_symbol: Option<String>,
    pub token_logo: Option<String>,
}
```

**File:** rs/sns/integration_tests/src/manage_ledger_parameters.rs (L132-135)
```rust
    assert_eq!(
        nervous_system_parameters_with_new_fee.transaction_fee_e8s,
        Some(new_fee)
    );
```
