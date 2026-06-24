### Title
`disburse_neuron()` Always Zeros `neuron_fees_e8s` Even When Fee Burn Is Skipped, Locking Tokens in Neuron Subaccount — (`File: rs/nns/governance/src/governance.rs`)

### Summary
In NNS governance, `disburse_neuron()` unconditionally sets `neuron.neuron_fees_e8s = 0` and reduces `cached_neuron_stake_e8s` by the fee amount even when the fee-burn ledger transfer is skipped (because the fee is below the minimum burn threshold). This causes the fee tokens to remain permanently locked in the neuron's subaccount on the ICP ledger while governance believes they no longer exist.

### Finding Description

In `rs/nns/governance/src/governance.rs`, `disburse_neuron()` performs two ledger transfers: first burning `neuron_fees_e8s` (if above the transaction fee threshold), then transferring the stake to the user. [1](#0-0) 

The fee burn is conditionally skipped when `fees_amount_e8s <= transaction_fee_e8s`:

```rust
if fees_amount_e8s > transaction_fee_e8s {
    // burn fees via ledger transfer
    let _result = self.ledger.transfer_funds(fees_amount_e8s, 0, Some(neuron_subaccount), governance_minting_account(), now).await?;
}

// This block runs UNCONDITIONALLY — even when the burn above was skipped
self.with_neuron_mut(id, |neuron| {
    if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
        neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
    } else {
        neuron.cached_neuron_stake_e8s = 0;
    }
    neuron.neuron_fees_e8s = 0;  // ← always zeroed, even without a burn
})
``` [2](#0-1) 

When the burn is skipped:
- `disburse_amount_e8s = (cached_neuron_stake_e8s - fees_amount_e8s) - transaction_fee_e8s` is transferred to the user (Transfer 2)
- After Transfer 2, the neuron's ledger subaccount still holds `fees_amount_e8s` tokens
- But governance records `cached_neuron_stake_e8s = 0` and `neuron_fees_e8s = 0`
- Those `fees_amount_e8s` tokens are permanently stuck in the neuron's subaccount with no governance record to recover them

By contrast, the SNS governance correctly guards the state update inside the same conditional as the burn: [3](#0-2) 

The SNS test explicitly verifies that small fees are **not** zeroed when the burn is skipped: [4](#0-3) 

### Impact Explanation

**Ledger conservation bug.** Tokens held in a neuron's governance subaccount on the ICP ledger are permanently inaccessible after disbursal when `neuron_fees_e8s ≤ transaction_fee_e8s`. The governance canister's accounting diverges from the actual ledger state: governance reports zero stake and zero fees, but the ledger subaccount retains `fees_amount_e8s` tokens with no mechanism to recover them. The maximum stuck amount per neuron is `transaction_fee_e8s - 1 = 9,999 e8s ≈ 0.0001 ICP`.

### Likelihood Explanation

**Low in practice for NNS.** The NNS minimum reject cost for proposals is 1 ICP = 100,000,000 e8s, which is far above the transaction fee of 10,000 e8s. Therefore, any neuron that has accumulated fees will almost certainly have `neuron_fees_e8s > transaction_fee_e8s`, meaning the burn path is taken and the bug is not triggered. The scenario requires a neuron with fees strictly between 1 and 9,999 e8s, which cannot arise from normal NNS proposal rejection. The bug is structurally present and diverges from the SNS implementation, but practical exploitability under current NNS parameters is negligible.

### Recommendation

Mirror the SNS implementation: only update `neuron_fees_e8s` and `cached_neuron_stake_e8s` when the burn actually occurs.

```diff
 if fees_amount_e8s > transaction_fee_e8s {
     let _result = self.ledger.transfer_funds(fees_amount_e8s, 0, Some(neuron_subaccount), governance_minting_account(), now).await?;
+    self.with_neuron_mut(id, |neuron| {
+        if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
+            neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
+        } else {
+            neuron.cached_neuron_stake_e8s = 0;
+        }
+        neuron.neuron_fees_e8s = 0;
+    }).expect("Expected the parent neuron to exist");
 }
-
-self.with_neuron_mut(id, |neuron| {
-    if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
-        neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
-    } else {
-        neuron.cached_neuron_stake_e8s = 0;
-    }
-    neuron.neuron_fees_e8s = 0;
-}).expect("Expected the parent neuron to exist");
```

### Proof of Concept

1. Create an NNS neuron with `neuron_fees_e8s` in the range `(0, transaction_fee_e8s]` (e.g., 5,000 e8s). This is not achievable via normal proposal rejection today, but the code path is reachable if fees are set directly or if the transaction fee is raised.
2. Dissolve the neuron and call `disburse_neuron`.
3. Observe that the fee burn is skipped (condition `fees_amount_e8s > transaction_fee_e8s` is false).
4. Observe that governance sets `neuron_fees_e8s = 0` and reduces `cached_neuron_stake_e8s` by 5,000 e8s.
5. Query the neuron's ledger subaccount: it retains 5,000 e8s that governance no longer tracks.
6. Confirm no subsequent governance operation can recover those tokens. [1](#0-0) [5](#0-4)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2046-2076)
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

**File:** rs/sns/governance/src/governance/disburse_neuron_tests.rs (L540-579)
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
```
