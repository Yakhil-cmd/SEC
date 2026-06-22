### Title
NNS Governance `disburse_neuron` Unconditionally Zeros `neuron_fees_e8s` Even When Fees Are Not Burned on Ledger — (`File: rs/nns/governance/src/governance.rs`)

---

### Summary

In NNS governance's `disburse_neuron`, when a neuron's accumulated fees (`neuron_fees_e8s`) are at or below the ledger's minimum burn threshold (`transaction_fee_e8s`), the fee burn is correctly skipped. However, the governance state update that zeros `neuron_fees_e8s` and reduces `cached_neuron_stake_e8s` runs **unconditionally**, regardless of whether the burn actually occurred. This causes the governance's accounting to diverge from the actual ledger balance, permanently stranding the unburned fee tokens in the neuron's subaccount. The SNS governance canister was explicitly patched for this identical issue.

---

### Finding Description

In `rs/nns/governance/src/governance.rs`, `disburse_neuron` performs two ledger operations: first burning fees, then transferring the stake. The fee burn is gated on a minimum-amount check:

```rust
if fees_amount_e8s > transaction_fee_e8s {
    // burn fees on ledger
    self.ledger.transfer_funds(fees_amount_e8s, 0, ...).await?;
}
``` [1](#0-0) 

Immediately after, the governance state is updated **unconditionally**:

```rust
self.with_neuron_mut(id, |neuron| {
    if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
        neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
    } else {
        neuron.cached_neuron_stake_e8s = 0;
    }
    neuron.neuron_fees_e8s = 0;   // ← always zeroed
})
``` [2](#0-1) 

When `fees_amount_e8s ≤ transaction_fee_e8s`, no burn occurs on the ledger, yet governance:
1. Subtracts `fees_amount_e8s` from `cached_neuron_stake_e8s`
2. Sets `neuron_fees_e8s = 0`

The disburse transfer then sends `cached_stake - fees - tx_fee` to the user. After the transfer completes, the ledger subaccount retains exactly `fees_amount_e8s` tokens (never burned), while governance records `cached_neuron_stake_e8s = 0` and `neuron_fees_e8s = 0`. These residual tokens are stranded: governance believes the neuron is empty, and the ledger minimum-burn constraint prevents recovering them via a subsequent disburse.

The SNS governance canister was explicitly fixed for this exact pattern. Its `disburse_neuron` only updates `cached_neuron_stake_e8s` and `neuron_fees_e8s` inside the conditional burn block:

```rust
if max_burnable_fee > transaction_fee_e8s {
    // burn on ledger ...
    // We only update ... if we actually burn fees, otherwise this leads to
    // ledger and governance getting out of sync.
    neuron.cached_neuron_stake_e8s = neuron.cached_neuron_stake_e8s.saturating_sub(max_burnable_fee);
    neuron.neuron_fees_e8s = neuron.neuron_fees_e8s.saturating_sub(max_burnable_fee);
}
``` [3](#0-2) 

The SNS CHANGELOG explicitly documents this fix:

> "Fees are now only recorded as burned when they exceed the transaction fee threshold and are actually burned." [4](#0-3) 

The NNS governance does not have this protection.

---

### Impact Explanation

**Ledger conservation bug.** For any NNS neuron whose `neuron_fees_e8s ≤ transaction_fee_e8s` (10,000 e8s = 0.0001 ICP) at the time of disbursal, up to 10,000 e8s of ICP are permanently stranded in the neuron's governance subaccount. The governance canister records the neuron as fully emptied, but the ICP ledger retains the fee amount. These tokens cannot be recovered: a subsequent `refresh_neuron` would update `cached_neuron_stake_e8s` to the residual balance, but a follow-up disburse would fail because the residual amount is ≤ `transaction_fee_e8s` and cannot cover the ledger transfer fee. The governance-ledger invariant (`cached_neuron_stake_e8s` reflects actual ledger balance) is violated.

---

### Likelihood Explanation

The `neuron_management_fee_per_proposal_e8s` in NNS economics is 1,000 e8s. A neuron that has submitted between 1 and 9 rejected proposals accumulates between 1,000 and 9,000 e8s in fees — all below the 10,000 e8s burn threshold. This is a common, realistic scenario for active governance participants. Any such neuron owner who dissolves and disburses their neuron triggers the bug without any special action. [5](#0-4) 

---

### Recommendation

Mirror the SNS governance fix in NNS governance's `disburse_neuron`: move the `neuron_fees_e8s = 0` and `cached_neuron_stake_e8s -= fees_amount_e8s` assignments inside the `if fees_amount_e8s > transaction_fee_e8s` block, so governance state is only updated when the ledger burn actually occurs. When fees are too small to burn, they should remain in `neuron_fees_e8s` and `cached_neuron_stake_e8s` unchanged, and the disburse amount should be computed as `cached_neuron_stake_e8s - neuron_fees_e8s - transaction_fee_e8s` (i.e., `minted_stake_e8s() - transaction_fee_e8s`), which is already the default path.

---

### Proof of Concept

**Precondition:** Neuron with `cached_neuron_stake_e8s = 1_000_000_000` (10 ICP), `neuron_fees_e8s = 5_000` (below `transaction_fee_e8s = 10_000`).

**Trace through `disburse_neuron`:**

1. `fees_amount_e8s = 5_000`, `neuron_minted_stake_e8s = 999_995_000`
2. `disburse_amount_e8s = 999_995_000 - 10_000 = 999_985_000`
3. Fee burn skipped (`5_000 ≤ 10_000`)
4. Governance state updated unconditionally:
   - `cached_neuron_stake_e8s = 1_000_000_000 - 5_000 = 999_995_000`
   - `neuron_fees_e8s = 0`
5. Ledger transfer: `999_985_000` sent to user, `10_000` tx fee deducted
   - Ledger subaccount balance: `1_000_000_000 - 999_985_000 - 10_000 = 5_000`
6. Governance cached stake updated: `999_995_000 - (999_985_000 + 10_000) = 0`

**Result:**
- Governance: `cached_neuron_stake_e8s = 0`, `neuron_fees_e8s = 0` ✓ (appears empty)
- Ledger subaccount: `5_000` e8s remaining ✗ (stranded, unrecoverable)

The 5,000 e8s are permanently inaccessible. `refresh_neuron` would set `cached_neuron_stake_e8s = 5_000`, but a subsequent disburse would attempt to transfer `5_000 - 10_000` which underflows to 0 (via `saturating_sub`), and the ledger transfer of 0 tokens would fail or be a no-op. [6](#0-5) [7](#0-6)

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

**File:** rs/nns/governance/src/governance.rs (L2046-2065)
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
```

**File:** rs/nns/governance/src/governance.rs (L2067-2076)
```rust
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

**File:** rs/nns/governance/src/governance.rs (L2102-2108)
```rust
        self.with_neuron_mut(id, |neuron| {
            let to_deduct = disburse_amount_e8s + transaction_fee_e8s;
            // The transfer was successful we can change the stake of the neuron.
            neuron.cached_neuron_stake_e8s =
                neuron.cached_neuron_stake_e8s.saturating_sub(to_deduct);
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

**File:** rs/sns/governance/CHANGELOG.md (L89-93)
```markdown
Fixed multiple issues in `disburse_neuron` functionality:

- Fixed a bug that could allow an SNS Neuron to burn fees that would have been refunded after proposal acceptance.
- Fees are now only recorded as burned when they exceed the transaction fee threshold and are actually burned.
- Added comprehensive tests to ensure the correct behavior in the future.
```

**File:** rs/nns/governance/canister/governance.did (L693-698)
```text
type NetworkEconomics = record {
  neuron_minimum_stake_e8s : nat64;
  max_proposals_to_keep_per_topic : nat32;
  neuron_management_fee_per_proposal_e8s : nat64;
  reject_cost_e8s : nat64;
  transaction_fee_e8s : nat64;
```
