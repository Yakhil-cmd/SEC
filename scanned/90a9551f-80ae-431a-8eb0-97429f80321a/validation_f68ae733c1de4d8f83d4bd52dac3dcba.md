### Title
NNS Governance `disburse_neuron` Unconditionally Clears Neuron Fees Without Burning Them on the Ledger - (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

In `rs/nns/governance/src/governance.rs`, the `disburse_neuron` function unconditionally zeroes `neuron_fees_e8s` and reduces `cached_neuron_stake_e8s` in governance state even when the fee-burn ledger transfer was **skipped** (because `fees_amount_e8s <= transaction_fee_e8s`). This creates a permanent divergence between the governance canister's neuron state and the actual ICP ledger balance, constituting a ledger conservation bug: tokens that should be burned are not burned, and the governance state falsely records them as burned.

---

### Finding Description

`disburse_neuron` performs two ledger operations:
1. Burn `neuron_fees_e8s` (only if `fees_amount_e8s > transaction_fee_e8s`)
2. Transfer the disbursed stake to the recipient

The fee-burn is correctly gated:

```rust
if fees_amount_e8s > transaction_fee_e8s {
    let _result = self.ledger.transfer_funds(
        fees_amount_e8s, 0, Some(neuron_subaccount),
        governance_minting_account(), now,
    ).await?;
}
```

However, the state update that follows is **unconditional** — it runs regardless of whether the burn happened:

```rust
self.with_neuron_mut(id, |neuron| {
    if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
        neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
    } else {
        neuron.cached_neuron_stake_e8s = 0;
    }
    neuron.neuron_fees_e8s = 0;   // ← always zeroed
})
.expect("Expected the parent neuron to exist");
``` [1](#0-0) 

When `fees_amount_e8s <= transaction_fee_e8s` (fees too small to burn):
- The ledger burn is **skipped** — the fee tokens remain in the neuron's subaccount
- Governance still sets `neuron_fees_e8s = 0` and decrements `cached_neuron_stake_e8s` by `fees_amount_e8s`
- Result: governance believes the fees were burned; the ledger still holds them

The SNS governance's `disburse_neuron` correctly avoids this by placing the state update **inside** the conditional burn block:

```rust
if max_burnable_fee > transaction_fee_e8s {
    self.ledger.transfer_funds(max_burnable_fee, ...).await?;
    // state update only here, inside the if
    neuron.cached_neuron_stake_e8s = neuron.cached_neuron_stake_e8s.saturating_sub(max_burnable_fee);
    neuron.neuron_fees_e8s = neuron.neuron_fees_e8s.saturating_sub(max_burnable_fee);
}
``` [2](#0-1) 

The NNS code's own comment at line 2068 says "Update the stake and the fees to reflect the burning above," implying the update should only occur when a burn actually happened. [3](#0-2) 

---

### Impact Explanation

**Ledger conservation bug:** ICP tokens that should be destroyed (burned) are not burned. They remain permanently locked in the neuron's governance-controlled subaccount. The governance canister's neuron record shows `neuron_fees_e8s = 0` and a reduced `cached_neuron_stake_e8s`, but the actual ICP ledger balance of the subaccount is higher by `fees_amount_e8s`. These tokens cannot be recovered through any normal governance operation (the neuron owner's subsequent disburse will only transfer up to the governance-tracked amount), so they are effectively stranded — inflating the circulating ICP supply relative to what the governance protocol intends.

---

### Likelihood Explanation

Any NNS neuron whose accumulated `neuron_fees_e8s` is nonzero but does not exceed the ICP ledger transaction fee (currently 10,000 e8s = 0.0001 ICP) will trigger this bug on every `disburse_neuron` call. A neuron accumulates fees from rejected governance proposals; a neuron that submitted one or more proposals that were rejected by a small margin could have fees in this range. The trigger is a standard unprivileged ingress call (`manage_neuron` → `Disburse`) by the neuron's controller.

---

### Recommendation

Move the `with_neuron_mut` state update inside the `if fees_amount_e8s > transaction_fee_e8s` block, mirroring the SNS governance implementation. When the burn is skipped, `neuron_fees_e8s` and `cached_neuron_stake_e8s` must not be modified to reflect a burn that did not occur. [4](#0-3) 

---

### Proof of Concept

1. Create an NNS neuron with `neuron_fees_e8s = F` where `0 < F <= transaction_fee_e8s` (e.g., F = 1,000 e8s, transaction_fee = 10,000 e8s). This can occur naturally when a proposal is rejected and the reject cost is small.
2. Dissolve the neuron and wait for it to reach `Dissolved` state.
3. Call `manage_neuron` → `Disburse` (no amount specified) as the neuron controller.
4. **Observed:** The ledger burn for `F` e8s is skipped (correct), but governance sets `neuron_fees_e8s = 0` and `cached_neuron_stake_e8s -= F` (incorrect). The neuron's ICP subaccount on the ledger retains `F` extra e8s that governance no longer tracks.
5. **Expected:** When the burn is skipped, governance state should remain unchanged for `neuron_fees_e8s` and `cached_neuron_stake_e8s`.

The stranded `F` e8s in the subaccount are unrecoverable through any governance operation, constituting a permanent ledger conservation violation. [5](#0-4)

### Citations

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
