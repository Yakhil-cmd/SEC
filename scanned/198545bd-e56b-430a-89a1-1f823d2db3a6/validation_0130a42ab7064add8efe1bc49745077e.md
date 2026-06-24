### Title
Silent Cap Instead of Error When Disburse Amount Exceeds Neuron Stake - (`File: rs/sns/governance/src/governance.rs`)

### Summary
The SNS Governance `disburse_neuron` function silently caps the disbursement amount to the neuron's available stake when the caller requests more than is available, returning `Ok(block_height)` with no indication that less was disbursed. This is a direct analog to the MochiVault borrow-to-max-cf bug: instead of rejecting an over-limit request with an error, the function silently substitutes a lower amount and succeeds.

### Finding Description
In `rs/sns/governance/src/governance.rs`, the `disburse_neuron` function computes the disbursement amount as follows:

```rust
let mut disburse_amount_e8s = disburse
    .amount
    .as_ref()
    .map_or(neuron.stake_e8s(), |a| a.e8s);   // use caller-supplied amount

// You cannot disburse more than the neuron's stake, which includes fees.
disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s()); // silent cap
``` [1](#0-0) 

When `disburse.amount.e8s > neuron.stake_e8s()`, the requested amount is silently replaced with `neuron.stake_e8s()` and the function proceeds to execute the transfer and return `Ok(block_height)`. The caller receives no indication that the actual disbursed amount differs from what was requested.

The function's own doc-comment contradicts this behavior, stating:

> "Note that we don't enforce that 'amount' is actually smaller than or equal to the neuron's stake … The ledger canister still guarantees that a transaction cannot transfer, i.e., disburse, more than what was in the neuron's account on the ledger." [2](#0-1) 

The stated design intent was to let the ledger reject over-limit transfers, but the implementation silently caps instead. The behavior is confirmed by the unit test `test_disburse_neuron_caps_to_maximum_available_stake`, which explicitly asserts that requesting 500M e8s when only ~499.95M is available succeeds and disburses the maximum: [3](#0-2) 

The return type of `disburse_neuron` is `Result<u64, GovernanceError>` where the `u64` is only the ledger block height — there is no field carrying the actual disbursed amount back to the caller. [4](#0-3) 

### Impact Explanation
A caller who specifies an explicit `amount` to `Disburse` (e.g., to cover a precise financial obligation) will receive less than requested with no error. The call returns `Ok`, so the caller's error-handling path is never triggered. The caller must independently query their ledger balance to discover the shortfall. In automated or programmatic flows (e.g., a canister that disburses a neuron and then immediately uses the proceeds), this silent reduction can cause downstream failures or loss of expected funds without any on-chain error signal.

Additionally, because the fee-burn step (Transfer 1) executes before the stake-disburse step (Transfer 2), a scenario where the burn succeeds but the capped disburse amount is still insufficient to cover the ledger transfer fee results in the burn being irreversible while the disburse fails — a partial state mutation. [5](#0-4) 

### Likelihood Explanation
Any SNS neuron holder with `NeuronPermissionType::Disburse` permission can trigger this via the standard `manage_neuron` ingress endpoint. No privileged access is required. The scenario arises naturally when a user's neuron stake has decreased (e.g., due to fee deductions from rejected proposals) since they last checked, and they request the previously-known amount. It also arises in any automated disbursal flow that does not pre-validate the stake.

### Recommendation
Return an explicit error when the caller-supplied `amount.e8s` exceeds `neuron.stake_e8s()`, consistent with the stated design intent in the doc-comment and consistent with how `disburse_to_neuron` handles over-limit amounts:

```rust
if let Some(ref a) = disburse.amount {
    if a.e8s > neuron.stake_e8s() {
        return Err(GovernanceError::new_with_message(
            ErrorType::InsufficientFunds,
            format!(
                "Requested disbursal of {} e8s exceeds neuron stake of {} e8s.",
                a.e8s, neuron.stake_e8s()
            ),
        ));
    }
}
```

Alternatively, if silent capping is intentional, the return type should be changed to carry the actual disbursed amount so callers can detect the discrepancy.

### Proof of Concept
1. Create a dissolved SNS neuron with `cached_neuron_stake_e8s = 100_000_000` (1 ICP) and `neuron_fees_e8s = 0`.
2. Call `manage_neuron` with `Command::Disburse { amount: Some(Amount { e8s: 500_000_000 }), to_account: None }`.
3. Observe: the call returns `Ok` with a block height.
4. Observe: the caller's ledger account receives only `100_000_000 - transaction_fee_e8s`, not `500_000_000 - transaction_fee_e8s`.
5. No error is returned; the caller has no way to detect the shortfall from the return value alone.

This is directly confirmed by the existing unit test: [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1099-1109)
```rust
    /// Note that we don't enforce that 'amount' is actually smaller
    /// than or equal to the neuron's stake.
    /// This will allow a user to still disburse funds if:
    /// - Someone transferred more funds to the neuron's subaccount after the
    ///   the initial neuron claim that we didn't know about.
    /// - The transfer of funds previously failed for some reason (e.g. the
    ///   ledger was unavailable or broken).
    ///
    /// The ledger canister still guarantees that a transaction cannot
    /// transfer, i.e., disburse, more than what was in the neuron's account
    /// on the ledger.
```

**File:** rs/sns/governance/src/governance.rs (L1119-1124)
```rust
    pub async fn disburse_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse: &manage_neuron::Disburse,
    ) -> Result<u64, GovernanceError> {
```

**File:** rs/sns/governance/src/governance.rs (L1160-1166)
```rust
        let mut disburse_amount_e8s = disburse
            .amount
            .as_ref()
            .map_or(neuron.stake_e8s(), |a| a.e8s);

        // You cannot disburse more than the neuron's stake, which includes fees.
        disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s());
```

**File:** rs/sns/governance/src/governance.rs (L1181-1223)
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

        // Transfer 2 - Disburse to the chosen account. This may fail if the
        // user told us to disburse more than they had in their account (but
        // the burn still happened).
        let block_height = self
            .ledger
            .transfer_funds(
                disburse_amount_e8s,
                transaction_fee_e8s,
                Some(from_subaccount),
                to_account,
                self.env.now(),
            )
            .await?;
```

**File:** rs/sns/governance/src/governance/disburse_neuron_tests.rs (L688-714)
```rust
fn test_disburse_neuron_caps_to_maximum_available_stake() {
    // Test that disburse_neuron disburses maximum possible when requested amount exceeds available
    let (mut governance, neuron_id, ledger) =
        setup_disburse_neuron_test(DissolveState::WhenDissolvedTimestampSeconds(0), 50_000);

    // Create an open proposal with reject cost of 30,000 e8s (makes 30K non-burnable)
    let proposal_data = proposal_data_with_reject_cost(1, neuron_id.clone(), 30_000, 0);

    governance.proto.proposals.insert(1, proposal_data);

    // Available for disbursal: stake_e8s = cached_stake - fees = 500M - 50K = 499.95M
    // Max disburse considering tx fee: 499.95M - 10K = 499.94M
    // Try to disburse 500M (more than available, should disburse max possible)
    let disburse = manage_neuron::Disburse {
        amount: Some(manage_neuron::disburse::Amount {
            e8s: 500 * E8, // 500M e8s (more than available, should get capped)
        }),
        to_account: None,
    };

    let result = governance
        .disburse_neuron(&neuron_id, &A_NEURON_PRINCIPAL_ID, &disburse)
        .now_or_never()
        .unwrap();

    // Should succeed and disburse the maximum possible amount
    assert_eq!(result, Ok(1)); // Mock ledger returns block height 1
```
