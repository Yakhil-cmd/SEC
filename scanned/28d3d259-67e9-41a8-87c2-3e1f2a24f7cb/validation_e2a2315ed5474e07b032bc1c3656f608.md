### Title
NNS Governance Neuron Management Fees Cleared Without Burning When Below Transaction Fee Threshold - (`rs/nns/governance/src/governance.rs`)

### Summary

In `NNS Governance`, `disburse_neuron` unconditionally resets `neuron.neuron_fees_e8s = 0` even when the actual ledger burn is skipped because `fees_amount_e8s <= transaction_fee_e8s`. This means ICP tokens representing proposal-rejection penalties are silently forgiven rather than burned, breaking ledger conservation. The SNS governance counterpart correctly preserves `neuron_fees_e8s` when the burn is skipped, confirming the NNS behavior is a bug.

### Finding Description

In `rs/nns/governance/src/governance.rs`, `disburse_neuron` performs two steps:

**Step 1 – Conditional burn:**
```rust
if fees_amount_e8s > transaction_fee_e8s {
    self.ledger.transfer_funds(fees_amount_e8s, 0, ..., governance_minting_account(), ...).await?;
}
```
The burn is skipped when `fees_amount_e8s <= transaction_fee_e8s` (10,000 e8s = 0.0001 ICP), because the ICP ledger rejects burns below the minimum burn amount.

**Step 2 – Unconditional state update:**
```rust
self.with_neuron_mut(id, |neuron| {
    if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
        neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
    } else {
        neuron.cached_neuron_stake_e8s = 0;
    }
    neuron.neuron_fees_e8s = 0;  // ← always executed, even when burn was skipped
})
```

`neuron.neuron_fees_e8s = 0` is executed unconditionally regardless of whether the burn actually occurred. [1](#0-0) 

The result: when `fees_amount_e8s <= transaction_fee_e8s`, the governance state records the fees as burned (clears them and reduces `cached_neuron_stake_e8s`), but the ICP ledger balance of the neuron's subaccount is never reduced. The `fees_amount_e8s` tokens remain stranded in the neuron's subaccount — not burned, not accessible to the owner (governance thinks stake is 0).

**Contrast with SNS governance**, which correctly preserves `neuron_fees_e8s` when the burn is skipped:
```rust
if max_burnable_fee > transaction_fee_e8s {
    // ... burn via ledger ...
    neuron.neuron_fees_e8s = neuron.neuron_fees_e8s.saturating_sub(max_burnable_fee);
    // Only updated if burn actually happened
}
``` [2](#0-1) 

The SNS governance changelog explicitly notes this was a bug fix: *"Fees are now only recorded as burned when they exceed the transaction fee threshold and are actually burned."* [3](#0-2) 

**Attacker-controlled entry path:**

Any neuron controller whose `neuron_fees_e8s <= transaction_fee_e8s` (10,000 e8s) calls `disburse_neuron` via the NNS governance canister's `manage_neuron` ingress endpoint. No privileged access is required — any dissolved neuron owner can trigger this path. [4](#0-3) 

### Impact Explanation

**Ledger conservation bug**: ICP tokens that should be burned (as penalty for rejected proposals) are not burned. The ICP total supply is higher than it should be by the sum of all such unburned fees. The tokens are stranded in neuron subaccounts — governance state says stake is 0, but the ledger balance is non-zero by `fees_amount_e8s`.

The practical impact is bounded by `transaction_fee_e8s` (10,000 e8s = 0.0001 ICP) per affected neuron. With the current NNS `reject_cost_e8s` of 1 ICP (100,000,000 e8s), this path is rarely triggered under normal conditions. However, if governance parameters are ever changed to allow smaller reject costs, or if fees accumulate in small increments through other mechanisms, the impact scales.

### Likelihood Explanation

**Low-to-medium**. The current NNS `reject_cost_e8s` (1 ICP) far exceeds `transaction_fee_e8s` (0.0001 ICP), so the condition `fees_amount_e8s <= transaction_fee_e8s` is rarely met in practice. However:
- The bug is reachable by any neuron controller without any special privileges
- It is triggered automatically (not requiring deliberate exploitation) whenever the condition holds
- If governance parameters change, the impact could increase

### Recommendation

Mirror the SNS governance fix: only clear `neuron_fees_e8s` when the burn actually succeeds. Change the unconditional `neuron.neuron_fees_e8s = 0` to a conditional update:

```rust
self.with_neuron_mut(id, |neuron| {
    if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
        neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
    } else {
        neuron.cached_neuron_stake_e8s = 0;
    }
-   neuron.neuron_fees_e8s = 0;
+   if fees_amount_e8s > transaction_fee_e8s {
+       neuron.neuron_fees_e8s = 0;
+   }
    // If fees were too small to burn, preserve neuron_fees_e8s for future burning
})
```

This aligns NNS governance with the SNS governance behavior and ensures `neuron_fees_e8s` is only cleared when the corresponding ledger burn actually occurred. [5](#0-4) 

### Proof of Concept

1. Create a neuron with `neuron_fees_e8s = 5_000` (e.g., from a rejected proposal with a small `reject_cost_e8s`).
2. Dissolve the neuron and call `disburse_neuron`.
3. Since `5_000 <= transaction_fee_e8s (10_000)`, the burn is skipped.
4. Observe: `neuron.neuron_fees_e8s` is set to 0 in governance state, but the neuron's ICP ledger subaccount balance still contains the 5,000 e8s (not burned).
5. The ICP total supply is 5,000 e8s higher than it should be.

The existing test `test_disburse_neuron_small_fees_not_burned` in SNS governance confirms the correct behavior (fees preserved), while no equivalent test exists for NNS governance to catch this regression. [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance.rs (L1944-1968)
```rust
    pub async fn disburse_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse: &manage_neuron::Disburse,
    ) -> Result<u64, GovernanceError> {
        let transaction_fee_e8s = self.transaction_fee();

        let (
            is_neuron_controlled_by_caller,
            neuron_state,
            is_neuron_kyc_verified,
            neuron_subaccount,
            fees_amount_e8s,
            neuron_minted_stake_e8s,
        ) = self.with_neuron(id, |neuron| {
            (
                neuron.is_controlled_by(caller),
                neuron.state(self.env.now()),
                neuron.kyc_verified,
                neuron.subaccount(),
                neuron.neuron_fees_e8s,
                neuron.minted_stake_e8s(),
            )
        })?;
```

**File:** rs/nns/governance/src/governance.rs (L2046-2075)
```rust
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
```

**File:** rs/sns/governance/src/governance.rs (L1181-1208)
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
```

**File:** rs/sns/governance/CHANGELOG.md (L91-93)
```markdown
- Fixed a bug that could allow an SNS Neuron to burn fees that would have been refunded after proposal acceptance.
- Fees are now only recorded as burned when they exceed the transaction fee threshold and are actually burned.
- Added comprehensive tests to ensure the correct behavior in the future.
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
