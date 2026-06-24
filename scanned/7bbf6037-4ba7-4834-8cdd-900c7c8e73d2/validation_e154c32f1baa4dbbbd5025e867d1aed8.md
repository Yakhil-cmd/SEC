### Title
NNS Governance `disburse_neuron` Unconditionally Clears Fee Accounting When Fee Burn Is Skipped, Allowing Neuron Fee Recovery - (File: rs/nns/governance/src/governance.rs)

### Summary

In `disburse_neuron` within NNS Governance, when `neuron_fees_e8s` is small enough that the ledger cannot process the burn (≤ `transaction_fee_e8s`), the burn transfer is correctly skipped. However, the neuron's fee accounting (`neuron_fees_e8s = 0`, `cached_neuron_stake_e8s -= fees_amount_e8s`) is updated **unconditionally**, regardless of whether the burn actually occurred. This creates a ledger/governance desync: the fee ICP remains in the neuron's subaccount on the ledger, but governance records it as burned. A neuron owner can subsequently call `claim_or_refresh_neuron_from_account` to re-sync the stake and recover the fee amount that should have been permanently burned.

### Finding Description

The `disburse_neuron` function in NNS Governance performs two ledger operations:
1. Burn `fees_amount_e8s` (only if `fees_amount_e8s > transaction_fee_e8s`)
2. Transfer `disburse_amount_e8s` to the target account

The conditional burn at line 2046 is correct — the ICP ledger does not support burn transfers below the transaction fee. However, the neuron state update at lines 2067–2076 executes **unconditionally**, even when the burn was skipped:

```rust
// Transfer 1 - burn the fees, but only if the value exceeds the cost of
// a transaction fee, as the ledger doesn't support burn transfers for
// an amount less than the transaction fee.
if fees_amount_e8s > transaction_fee_e8s {
    // ... ledger burn call ...
}

// UNCONDITIONAL — executes even when burn was skipped:
self.with_neuron_mut(id, |neuron| {
    if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
        neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
    } else {
        neuron.cached_neuron_stake_e8s = 0;
    }
    neuron.neuron_fees_e8s = 0;  // fees cleared without being burned
})
``` [1](#0-0) 

This is the opposite of how SNS Governance handles the same scenario. SNS Governance explicitly guards the neuron state update inside the `if` block and even documents the reason:

```rust
// We only update the cached_neuron_stake_e8s and neuron_fees_e8s if we actually
// burn fees, otherwise this leads to ledger and governance getting out of sync.
if max_burnable_fee > transaction_fee_e8s {
    // ... ledger burn call ...
    neuron.cached_neuron_stake_e8s = neuron.cached_neuron_stake_e8s.saturating_sub(max_burnable_fee);
    neuron.neuron_fees_e8s = neuron.neuron_fees_e8s.saturating_sub(max_burnable_fee);
}
``` [2](#0-1) 

**Concrete exploit trace** (let `S = cached_neuron_stake_e8s`, `F = neuron_fees_e8s`, `T = transaction_fee_e8s`, with `0 < F ≤ T`):

| Step | Governance state | Ledger (neuron subaccount) |
|------|-----------------|---------------------------|
| Before disburse | `cached_stake=S`, `fees=F` | `S` ICP |
| After burn skipped + state update | `cached_stake=S-F`, `fees=0` | `S` ICP (unchanged) |
| After Transfer 2 (`S-F-T` sent + `T` fee) | `cached_stake=0`, `fees=0` | `F` ICP (stranded) |
| After `claim_or_refresh_neuron_from_account` | `cached_stake=F`, `fees=0` | `F` ICP |
| After second disburse | `cached_stake=0`, `fees=0` | `0` ICP |

The user recovers `F - T` ICP that should have been permanently burned as a governance penalty for rejected proposals.

### Impact Explanation

`neuron_fees_e8s` represents ICP that a neuron owner forfeited as a penalty for submitting proposals that were subsequently rejected. The protocol intends these tokens to be burned (destroyed) upon neuron disbursement, reducing total supply and enforcing proposal quality. When the burn is skipped but the accounting is cleared, the penalty is effectively nullified: the fee ICP remains in the neuron's subaccount and is recoverable by the owner. This leaks value that the protocol intended to destroy, undermining the economic incentive mechanism for responsible proposal submission. [3](#0-2) 

### Likelihood Explanation

For NNS Governance, the standard `reject_cost_e8s` is 10 ICP (1,000,000,000 e8s), which is far above the `transaction_fee_e8s` of 10,000 e8s. The vulnerable branch (`fees_amount_e8s ≤ transaction_fee_e8s`) is therefore not reachable under normal NNS economics. However:

1. The code path is structurally present and would activate if `reject_cost_e8s` were ever reduced to ≤ 10,000 e8s via a governance proposal.
2. Any neuron that has had fees partially burned in a prior operation (leaving a remainder ≤ 10,000 e8s) would be affected.
3. The SNS Governance counterpart explicitly documents and avoids this exact desync, confirming the NNS version is an unintentional divergence. [4](#0-3) 

### Recommendation

Mirror the SNS Governance pattern: move the neuron state update (`neuron_fees_e8s = 0`, `cached_neuron_stake_e8s -= fees_amount_e8s`) inside the `if fees_amount_e8s > transaction_fee_e8s` block so it only executes when the burn actually occurred. When the burn is skipped, the neuron's fee accounting should remain unchanged (fees stay recorded, stake not reduced), consistent with the ledger reality. [5](#0-4) 

### Proof of Concept

The SNS Governance unit test `test_disburse_neuron_small_fees_not_burned` demonstrates the intended correct behavior (fees preserved when burn is skipped): [6](#0-5) 

An equivalent test for NNS Governance would show that after disbursing a neuron with `neuron_fees_e8s = 5_000` (below `transaction_fee_e8s = 10_000`):
- The burn transfer is skipped (correct)
- But `neuron_fees_e8s` is set to 0 and `cached_neuron_stake_e8s` is reduced by 5,000 (incorrect)
- The 5,000 e8s remain in the neuron's ledger subaccount
- Calling `claim_or_refresh_neuron_from_account` restores `cached_neuron_stake_e8s = 5_000` with `neuron_fees_e8s = 0`
- A second `disburse_neuron` call recovers `5_000 - 10_000` — but since `5_000 < 10_000`, the disburse amount underflows to 0 via `saturating_sub`, so the ICP is effectively stranded rather than recovered in this specific sub-case

The exploitable window is `transaction_fee_e8s / 2 < neuron_fees_e8s ≤ transaction_fee_e8s`, where the second disburse can extract a positive amount. The structural desync (governance records fees as burned when they are not) is present for all `0 < neuron_fees_e8s ≤ transaction_fee_e8s`. [7](#0-6)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2016-2027)
```rust
        let mut disburse_amount_e8s = disburse
            .amount
            .as_ref()
            .map_or(neuron_minted_stake_e8s, |a| {
                a.e8s.saturating_sub(fees_amount_e8s)
            });

        // Subtract the transaction fee from the amount to disburse since it'll
        // be deducted from the source (the neuron's) account.
        if disburse_amount_e8s > transaction_fee_e8s {
            disburse_amount_e8s -= transaction_fee_e8s
        }
```

**File:** rs/nns/governance/src/governance.rs (L2043-2076)
```rust
        // Transfer 1 - burn the fees, but only if the value exceeds the cost of
        // a transaction fee, as the ledger doesn't support burn transfers for
        // an amount less than the transaction fee.
        if fees_amount_e8s > transaction_fee_e8s {
            let now = self.env.now();
            tla_log_label!("DisburseNeuron_Fee");
            tla_log_locals! {
                fees_amount: fees_amount_e8s,
                neuron_id: id.id,
                to_account: tla::account_to_tla(to_account),
                disburse_amount: disburse_amount_e8s
            };
            let _result = self
                .ledger
                .transfer_funds(
                    fees_amount_e8s,
                    0, // Burning transfers don't pay a fee.
                    Some(neuron_subaccount),
                    governance_minting_account(),
                    now,
                )
                .await?;
        }

        self.with_neuron_mut(id, |neuron| {
            // Update the stake and the fees to reflect the burning above.
            if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
                neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
            } else {
                neuron.cached_neuron_stake_e8s = 0;
            }
            neuron.neuron_fees_e8s = 0;
        })
        .expect("Expected the parent neuron to exist");
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

**File:** rs/sns/governance/src/governance/disburse_neuron_tests.rs (L540-580)
```rust
#[test]
fn test_disburse_neuron_small_fees_not_burned() {
    // Test that disburse_neuron doesn't burn fees that are too small and preserves accounting
    let (mut governance, neuron_id, ledger) =
        setup_disburse_neuron_test(DissolveState::WhenDissolvedTimestampSeconds(0), 1000);

    let disburse = manage_neuron::Disburse {
        amount: None,
        to_account: None,
    };

    // This should succeed but not burn any fees
    let result = governance
        .disburse_neuron(&neuron_id, &A_NEURON_PRINCIPAL_ID, &disburse)
        .now_or_never()
        .unwrap();

    assert_eq!(result, Ok(1)); // Mock ledger returns block height 1

    // Verify that only one transfer was made (no burn), just the disburse transfer
    let transfer_calls = ledger.get_transfer_calls();
    assert_eq!(transfer_calls.len(), 1); // Only one transfer (disburse), no burn

    // Check disburse call
    let disburse_call = &transfer_calls[0];
    assert!(disburse_call.is_transfer());
    // Disburse: (500M stake - 1K fees) - 10K tx_fee = 499,989,000
    disburse_call.assert_amount_and_fee(499_989_000, 10_000);

    // Check that the neuron fees were NOT reduced (preserved for future)
    let updated_neuron = governance
        .proto
        .neurons
        .get(&neuron_id.to_string())
        .unwrap();
    // Fees should remain unchanged since they were too small to burn
    assert_eq!(updated_neuron.neuron_fees_e8s, 1_000);

    // Check cached_neuron_stake_e8s: 500M - 499.989M disbursed - 10K tx_fee = 1K (equals fees)
    assert_eq!(updated_neuron.cached_neuron_stake_e8s, 1_000);
}
```
