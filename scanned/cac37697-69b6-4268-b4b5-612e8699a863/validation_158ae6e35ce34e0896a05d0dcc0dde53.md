### Title
Silent Under-Disbursement in SNS Governance `disburse_neuron` When Requested Amount Exceeds Available Stake - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance `disburse_neuron` function silently caps the disbursement amount to the neuron's available stake when the caller requests more than is available, returning `Ok(block_height)` without any indication that the user received less than they requested. This is a direct analog to the AFiBase under-disbursement pattern: instead of reverting or returning an error, the function silently transfers a lesser amount.

### Finding Description
In `rs/sns/governance/src/governance.rs`, the `disburse_neuron` function computes the disbursement amount and then applies a silent cap:

```rust
let mut disburse_amount_e8s = disburse
    .amount
    .as_ref()
    .map_or(neuron.stake_e8s(), |a| a.e8s);   // user-supplied amount

// You cannot disburse more than the neuron's stake, which includes fees.
disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s()); // silent cap
``` [1](#0-0) 

After the cap, the function subtracts the transaction fee and proceeds to transfer the reduced amount, returning `Ok(block_height)` on success:

```rust
let block_height = self
    .ledger
    .transfer_funds(
        disburse_amount_e8s,   // silently reduced amount
        transaction_fee_e8s,
        Some(from_subaccount),
        to_account,
        self.env.now(),
    )
    .await?;
``` [2](#0-1) 

The caller receives no signal that the disbursed amount differs from the requested amount. The function's own comment acknowledges the asymmetry with the NNS governance implementation:

> "Note that we don't enforce that 'amount' is actually smaller than or equal to the neuron's stake." [3](#0-2) 

The NNS governance `disburse_neuron` deliberately does **not** cap the amount — it passes the user-requested value directly to the ledger, which returns an `InsufficientFunds` error if the balance is too low: [4](#0-3) 

The SNS governance diverges from this safe pattern by silently capping instead of propagating an error.

A unit test explicitly confirms and validates this silent-cap behavior as intentional:

```rust
// Try to disburse 500M (more than available, should disburse max possible)
let disburse = manage_neuron::Disburse {
    amount: Some(manage_neuron::disburse::Amount { e8s: 500 * E8 }),
    ...
};
// Should succeed and disburse the maximum possible amount
assert_eq!(result, Ok(1));
disburse_call.assert_amount_and_fee(499_940_000, 10_000); // 499.94M, not 500M
``` [5](#0-4) 

### Impact Explanation
A neuron controller who calls `manage_neuron { Disburse { amount: Some(X) } }` expecting to receive exactly `X - tx_fee` tokens will silently receive less — up to `neuron.stake_e8s() - tx_fee` — with no error returned. The caller has no programmatic way to detect the shortfall from the return value alone. If the caller is a smart contract or automated system relying on the exact disbursed amount (e.g., to fund a downstream payment), the silent under-disbursement causes a conservation failure: tokens the user believed were disbursed remain locked in the neuron subaccount, inaccessible without a subsequent disburse call.

**Vulnerability class**: Ledger conservation bug — asset under-disbursement without caller notification.

### Likelihood Explanation
The condition is reachable by any SNS neuron controller via a standard unprivileged ingress `manage_neuron` call. It triggers whenever the caller specifies an `amount` that exceeds `neuron.stake_e8s()`, which can happen naturally due to fee deductions reducing the effective stake below what the user calculated off-chain. No privileged access, key compromise, or majority attack is required.

### Recommendation
Replace the silent cap with an explicit error when the requested amount exceeds the available stake:

```rust
if let Some(requested) = disburse.amount.as_ref() {
    if requested.e8s > neuron.stake_e8s() {
        return Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!(
                "Requested disbursal of {} e8s exceeds available stake of {} e8s.",
                requested.e8s,
                neuron.stake_e8s()
            ),
        ));
    }
}
```

This aligns SNS governance with the NNS governance behavior and with the fix applied to the AFiBase contract: revert (error) rather than silently disburse less.

### Proof of Concept
1. Create an SNS neuron with `cached_neuron_stake_e8s = 500_000_000` and `neuron_fees_e8s = 50_000`. Effective `stake_e8s() = 499_950_000`.
2. Dissolve the neuron.
3. Call `manage_neuron` with `Disburse { amount: Some(Amount { e8s: 500_000_000 }) }`.
4. Observe: the call returns `Ok(block_height)` — no error.
5. Check the destination account balance: it received `499_940_000` e8s (499.95M − 10K tx fee), not the requested `499_990_000` e8s (500M − 10K tx fee).
6. The shortfall of `50_000` e8s (the fee amount) remains in the neuron subaccount with no notification to the caller.

The existing test `test_disburse_neuron_caps_to_maximum_available_stake` in `rs/sns/governance/src/governance/disburse_neuron_tests.rs` reproduces this exact scenario. [6](#0-5)

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

**File:** rs/sns/governance/src/governance.rs (L1160-1166)
```rust
        let mut disburse_amount_e8s = disburse
            .amount
            .as_ref()
            .map_or(neuron.stake_e8s(), |a| a.e8s);

        // You cannot disburse more than the neuron's stake, which includes fees.
        disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s());
```

**File:** rs/sns/governance/src/governance.rs (L1214-1223)
```rust
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

**File:** rs/nns/governance/src/governance.rs (L1929-1935)
```rust
    /// Note that we don't enforce that 'amount' is actually smaller
    /// than or equal to the cached stake in the neuron.
    /// This will allow a user to still disburse funds if:
    /// - Someone transferred more funds to the neuron's subaccount after the
    ///   the initial neuron claim that we didn't know about.
    /// - The transfer of funds previously failed for some reason (e.g. the
    ///   ledger was unavailable or broken).
```

**File:** rs/sns/governance/src/governance/disburse_neuron_tests.rs (L687-745)
```rust
#[test]
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

    // Verify that the ledger calls were made correctly
    let transfer_calls = ledger.get_transfer_calls();
    assert_eq!(transfer_calls.len(), 2); // One burn, one disburse

    // Check burn call (first transfer)
    let burn_call = &transfer_calls[0];
    assert!(burn_call.is_burn());
    burn_call.assert_amount_and_fee(20_000, 0); // amount burned (50K - 30K non-burnable)

    // Check disburse call (second transfer)
    let disburse_call = &transfer_calls[1];
    assert!(disburse_call.is_transfer());
    // Max disburse: 499.95M available - 10K tx_fee = 499,940,000
    disburse_call.assert_amount_and_fee(499_940_000, 10_000);

    // Check final neuron state after max disbursal
    let updated_neuron = governance
        .proto
        .neurons
        .get(&neuron_id.to_string())
        .unwrap();

    // Should have 30K fees remaining (tied to open proposal)
    assert_eq!(updated_neuron.neuron_fees_e8s, 30_000);

    // Check cached_neuron_stake_e8s: 500M - 20K burned - 499.94M disbursed - 10K tx_fee = 30K
    assert_eq!(updated_neuron.cached_neuron_stake_e8s, 30_000);

    // Remaining stake: 30K cached - 30K fees = 0 (neuron fully disbursed)
    assert_eq!(updated_neuron.stake_e8s(), 0);
```
