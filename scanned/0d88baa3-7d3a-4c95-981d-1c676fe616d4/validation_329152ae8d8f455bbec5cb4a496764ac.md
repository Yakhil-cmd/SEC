### Title
NNS Governance `disburse_neuron` Unconditionally Zeroes `neuron_fees_e8s` Without Burning When Fees ≤ Transaction Fee - (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

In the NNS governance canister's `disburse_neuron` function, the ICP ledger burn of accumulated neuron management fees is **conditional** (only fires when `fees_amount_e8s > transaction_fee_e8s`), but the governance state update that zeroes `neuron_fees_e8s` and decrements `cached_neuron_stake_e8s` is **unconditional**. When a neuron's fees are at or below the ledger transaction fee threshold, the burn is silently skipped while governance records it as complete, stranding ICP tokens in the neuron's subaccount permanently.

---

### Finding Description

In `rs/nns/governance/src/governance.rs`, the `disburse_neuron` function performs two sequential operations:

**Step 1 — Conditional burn (lines 2046–2065):**

```rust
if fees_amount_e8s > transaction_fee_e8s {
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
```

**Step 2 — Unconditional state update (lines 2067–2076):**

```rust
self.with_neuron_mut(id, |neuron| {
    // Update the stake and the fees to reflect the burning above.
    if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
        neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
    } else {
        neuron.cached_neuron_stake_e8s = 0;
    }
    neuron.neuron_fees_e8s = 0;   // ← always executed
})
.expect("Expected the parent neuron to exist");
``` [1](#0-0) [2](#0-1) 

The comment on line 2068 says *"Update the stake and the fees to reflect the burning above"*, but the block executes unconditionally — even when no burn occurred because `fees_amount_e8s <= transaction_fee_e8s`.

**Concrete token accounting trace** (fees F ≤ transaction_fee T, stake S):

| Step | Ledger subaccount balance | Governance `cached_neuron_stake_e8s` | Governance `neuron_fees_e8s` |
|---|---|---|---|
| Initial | S | S | F |
| After conditional burn (skipped) | S | S | F |
| After unconditional state update | S | S − F | 0 |
| After disburse transfer (S−F−T sent, T fee) | S − (S−F) = **F** | 0 | 0 |

After the full operation, **F ICP tokens remain in the neuron's subaccount** on the ledger, but governance records `cached_neuron_stake_e8s = 0` and `neuron_fees_e8s = 0`. Those F tokens are permanently stranded — no governance operation can recover or burn them because the neuron's recorded stake is zero.

---

### Impact Explanation

**Vulnerability class:** Ledger conservation bug.

ICP tokens that the protocol intends to destroy (burn) as neuron management fees are instead left in the neuron's subaccount, unaccounted for in governance state. The governance canister's view of the neuron's balance diverges from the actual ICP ledger balance by exactly `fees_amount_e8s`. These tokens are permanently inaccessible: governance will never issue another burn or transfer for them because it believes the neuron is fully disbursed. The ICP supply is inflated relative to what the protocol intends.

**Impact: Medium** — Per-neuron loss is bounded by `transaction_fee_e8s` (10,000 e8s = 0.0001 ICP), but the bug is triggered on every qualifying disbursal and accumulates across all affected neurons over time. Additionally, the governance/ledger state divergence is a correctness violation that could affect downstream tooling and accounting.

---

### Likelihood Explanation

**Likelihood: High** — The trigger condition (`neuron_fees_e8s <= transaction_fee_e8s`) is realistic and common:

1. A neuron submits a proposal with a `reject_cost_e8s` ≤ 10,000 e8s.
2. The proposal is rejected, adding those fees to `neuron_fees_e8s`.
3. The neuron dissolves and the controller calls `disburse_neuron`.
4. The bug fires silently — no error is returned, the disbursal succeeds, but the fee tokens are not burned.

Any unprivileged neuron controller can reach this path via the standard `manage_neuron` ingress endpoint with a `Disburse` command. No special access is required.

---

### Recommendation

Move the governance state update inside the conditional burn block, mirroring the correct pattern used in the SNS governance implementation (`rs/sns/governance/src/governance.rs` lines 1193–1208):

```diff
-       if fees_amount_e8s > transaction_fee_e8s {
+       if fees_amount_e8s > transaction_fee_e8s {
            let _result = self.ledger.transfer_funds(
                fees_amount_e8s, 0, Some(neuron_subaccount),
                governance_minting_account(), now,
            ).await?;
-       }
 
-       self.with_neuron_mut(id, |neuron| {
-           // Update the stake and the fees to reflect the burning above.
-           if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
-               neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
-           } else {
-               neuron.cached_neuron_stake_e8s = 0;
-           }
-           neuron.neuron_fees_e8s = 0;
-       })
-       .expect("Expected the parent neuron to exist");
+           self.with_neuron_mut(id, |neuron| {
+               if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
+                   neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
+               } else {
+                   neuron.cached_neuron_stake_e8s = 0;
+               }
+               neuron.neuron_fees_e8s = 0;
+           })
+           .expect("Expected the parent neuron to exist");
+       }
```

The SNS governance already applies this correct pattern: it only updates `neuron_fees_e8s` and `cached_neuron_stake_e8s` inside the `if max_burnable_fee > transaction_fee_e8s` block. [3](#0-2) 

---

### Proof of Concept

**Entry path:** Unprivileged ingress call to NNS governance `manage_neuron` with `Command::Disburse`.

**Preconditions:**
- Neuron is dissolved.
- `neuron.neuron_fees_e8s` is in range `(0, transaction_fee_e8s]` (e.g., 5,000 e8s from a rejected proposal with a small reject cost).

**Steps:**
1. Neuron controller calls `manage_neuron` → `Disburse` on a dissolved neuron with `neuron_fees_e8s = 5_000`.
2. `fees_amount_e8s = 5_000 <= transaction_fee_e8s = 10_000` → the `if` at line 2046 is false, no ledger burn is issued.
3. The unconditional block at lines 2067–2076 executes: `cached_neuron_stake_e8s -= 5_000`, `neuron_fees_e8s = 0`.
4. The disburse transfer succeeds.
5. **Result:** 5,000 e8s remain in the neuron's ICP subaccount on the ledger, but governance records the neuron as fully disbursed with zero stake and zero fees. The 5,000 e8s are permanently stranded and not burned. [4](#0-3)

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
