### Title
NNS Governance `disburse_neuron` Unconditionally Zeros `neuron_fees_e8s` Without Burning Fees When Fee Amount Is Below Transaction Fee Threshold — (File: rs/nns/governance/src/governance.rs)

---

### Summary

In `disburse_neuron`, the NNS governance canister skips the ledger burn of `neuron_fees_e8s` when the fee amount does not exceed the transaction fee, but **unconditionally zeros `neuron_fees_e8s`** in the neuron's cached state regardless. This mirrors the GMX pattern exactly: a "debt" (unburned fees) is silently forgotten, leaving tokens permanently locked in the neuron's subaccount while governance believes they have been burned.

---

### Finding Description

The `disburse_neuron` function in `rs/nns/governance/src/governance.rs` performs two ledger operations:

1. **Burn fees** — only if `fees_amount_e8s > transaction_fee_e8s`
2. **Transfer stake** — always

After the conditional burn, the neuron state is updated unconditionally:

```rust
// Transfer 1 - burn the fees, but only if the value exceeds the cost of
// a transaction fee, as the ledger doesn't support burn transfers for
// an amount less than the transaction fee.
if fees_amount_e8s > transaction_fee_e8s {
    let _result = self
        .ledger
        .transfer_funds(fees_amount_e8s, 0, Some(neuron_subaccount), governance_minting_account(), now)
        .await?;
}

self.with_neuron_mut(id, |neuron| {
    if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
        neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
    } else {
        neuron.cached_neuron_stake_e8s = 0;
    }
    neuron.neuron_fees_e8s = 0;   // ← ALWAYS zeroed, even when no burn occurred
})
``` [1](#0-0) 

When `fees_amount_e8s <= transaction_fee_e8s`:

- The ledger burn is **skipped** — the fee tokens remain in the neuron's subaccount on the ICP ledger.
- `cached_neuron_stake_e8s` is **reduced** by `fees_amount_e8s` (the disburse amount was already computed net of fees).
- `neuron_fees_e8s` is **zeroed** — governance now believes the fees were burned.

The result: `fees_amount_e8s` tokens are permanently stranded in the neuron's subaccount. Governance has no record of them, and the disburse transfer already excluded them from the payout. The "debt" is forgotten.

**Contrast with SNS governance**, which correctly conditions the state update on whether the burn actually occurred:

```rust
if max_burnable_fee > transaction_fee_e8s {
    // ... ledger burn ...
    neuron.cached_neuron_stake_e8s = neuron.cached_neuron_stake_e8s.saturating_sub(max_burnable_fee);
    neuron.neuron_fees_e8s = neuron.neuron_fees_e8s.saturating_sub(max_burnable_fee);
    // neuron_fees_e8s is NOT touched if burn was skipped
}
``` [2](#0-1) 

The SNS test `test_disburse_neuron_small_fees_not_burned` explicitly validates that `neuron_fees_e8s` is **preserved** when fees are too small to burn: [3](#0-2) 

The NNS governance has no equivalent guard.

---

### Impact Explanation

**Ledger conservation bug**: When `neuron_fees_e8s ≤ transaction_fee_e8s`, the neuron owner's disburse payout is reduced by `fees_amount_e8s` (as if the fees were burned), but the tokens are never actually burned. They remain in the neuron's ICP ledger subaccount, permanently inaccessible through normal governance flows. Governance state diverges from ledger state: governance records 0 fees and 0 stake, while the ledger holds `fees_amount_e8s` unclaimed tokens.

The stranded tokens cannot be recovered through normal disburse flows because governance believes the neuron is fully emptied. Recovery would require the owner to re-stake into the same subaccount and call `claim_or_refresh_neuron_from_account` — a non-obvious path unknown to most users.

The per-incident token loss is bounded by `transaction_fee_e8s` (currently 10,000 e8s = 0.0001 ICP), making individual impact small. However, the governance–ledger state divergence is a correctness violation in the token accounting invariant.

---

### Likelihood Explanation

The trigger condition is `neuron.neuron_fees_e8s ≤ transaction_fee_e8s` (≤ 10,000 e8s). In the NNS today, the default `reject_cost_e8s` is 1 ICP (100,000,000 e8s), making a single rejected proposal accumulate fees far above the threshold. However:

- The `NetworkEconomics` parameters are governance-adjustable; a future proposal could lower `reject_cost_e8s`.
- A neuron that has had fees partially refunded (e.g., via proposal adoption) could end up with a residual `neuron_fees_e8s` below the threshold.
- The code path is reachable by any dissolved neuron controller calling `disburse_neuron` — no privileged access required. [4](#0-3) 

---

### Recommendation

Move the neuron state update inside the conditional burn block, mirroring the SNS governance pattern:

```rust
if fees_amount_e8s > transaction_fee_e8s {
    self.ledger.transfer_funds(fees_amount_e8s, 0, ...).await?;
    self.with_neuron_mut(id, |neuron| {
        if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
            neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
        } else {
            neuron.cached_neuron_stake_e8s = 0;
        }
        neuron.neuron_fees_e8s = 0;
    })?;
} else {
    // Fees too small to burn; leave neuron_fees_e8s unchanged.
    // The disburse amount was already computed net of fees, so
    // cached_neuron_stake_e8s will be reduced by the stake transfer below.
}
```

This ensures `neuron_fees_e8s` is only cleared when the corresponding ledger burn has actually executed, keeping governance state consistent with the ledger.

---

### Proof of Concept

**Setup**: Create an NNS neuron. Submit a proposal with a `reject_cost_e8s` that results in `neuron_fees_e8s = 5_000` (below `transaction_fee_e8s = 10_000`). Dissolve the neuron and call `disburse_neuron`.

**Expected (correct) behavior**: Fees of 5,000 e8s are not burned (too small), `neuron_fees_e8s` remains 5,000, and the disburse payout equals `cached_neuron_stake_e8s - 5_000 - 10_000`.

**Actual behavior**:
1. Burn is skipped (`5_000 ≤ 10_000`).
2. `cached_neuron_stake_e8s -= 5_000`.
3. `neuron_fees_e8s = 0` — governance forgets the 5,000 e8s debt.
4. Disburse transfer sends `cached_neuron_stake_e8s - 5_000 - 10_000` tokens.
5. ICP ledger subaccount retains 5,000 e8s permanently.
6. Governance records 0 stake, 0 fees — diverged from ledger truth.

The stranded 5,000 e8s are neither burned nor disbursed, violating the token accounting invariant. [5](#0-4)

### Citations

**File:** rs/nns/governance/src/governance.rs (L1944-1950)
```rust
    pub async fn disburse_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse: &manage_neuron::Disburse,
    ) -> Result<u64, GovernanceError> {
        let transaction_fee_e8s = self.transaction_fee();
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
