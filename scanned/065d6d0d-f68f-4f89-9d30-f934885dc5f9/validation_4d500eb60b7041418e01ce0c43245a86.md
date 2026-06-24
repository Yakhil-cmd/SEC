### Title
NNS Governance `disburse_neuron` Incorrectly Deducts `neuron_fees_e8s` from User-Specified Disburse Amount — (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

In `disburse_neuron()`, when a caller supplies an explicit `amount` in the `Disburse` command, the NNS governance canister silently subtracts `neuron_fees_e8s` from that amount before computing the ledger transfer. The caller therefore receives fewer ICP than they specified. The shortfall remains stranded in the neuron's subaccount until a subsequent disburse call. This is a ledger conservation bug directly analogous to the BountyV1.sol finding where `_takerFee` was incorrectly deducted from the stake return.

---

### Finding Description

In `rs/nns/governance/src/governance.rs`, `disburse_neuron` computes the transfer amount as follows:

```rust
let mut disburse_amount_e8s = disburse
    .amount
    .as_ref()
    .map_or(neuron_minted_stake_e8s, |a| {
        a.e8s.saturating_sub(fees_amount_e8s)   // ← fees incorrectly subtracted
    });

if disburse_amount_e8s > transaction_fee_e8s {
    disburse_amount_e8s -= transaction_fee_e8s
}
``` [1](#0-0) 

When `amount = None`, the caller correctly receives `minted_stake − tx_fee`, where `minted_stake = cached_neuron_stake_e8s − neuron_fees_e8s`. When `amount = Some(X)`, however, the caller receives `(X − neuron_fees_e8s) − tx_fee` instead of the expected `X − tx_fee`. The `neuron_fees_e8s` is subtracted a second time from the caller-specified value, even though those fees are already burned in Transfer 1 before the stake transfer occurs.

The inline comment attempts to justify this as "symmetry":

> *"Note that the implementation of `minted_stake_e8s()` is effectively `cached_neuron_stake_e8s.saturating_sub(neuron_fees_e8s)`. So there is symmetry here in that we are subtracting `fees_amount_e8s` from both sides of this `map_or`."* [2](#0-1) 

This reasoning is incorrect. After Transfer 1 burns the fees, the neuron's ledger balance is already `cached_stake − fees`. The user-specified amount `X` should be transferred directly (minus the ledger tx_fee), not reduced by `fees_amount_e8s` again.

**Contrast with SNS governance**, which handles this correctly — it does not subtract fees from the user-specified amount:

```rust
let mut disburse_amount_e8s = disburse
    .amount
    .as_ref()
    .map_or(neuron.stake_e8s(), |a| a.e8s);   // ← no fee subtraction

disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s());
``` [3](#0-2) 

---

### Impact Explanation

A neuron controller who specifies an explicit `amount` in a `Disburse` command receives `neuron_fees_e8s` fewer ICP than they requested. The shortfall is not burned — it remains in the neuron's subaccount — but the caller must issue an additional disburse call to recover it. In the worst case (e.g., `neuron_fees_e8s = reject_cost_e8s = 1 ICP` on mainnet), the caller silently loses 1 ICP per disburse call relative to their stated intent. This is a ledger conservation bug: the user's specified stake return is incorrectly reduced by a fee that should not apply to the user-specified amount.

---

### Likelihood Explanation

Any NNS neuron that has accumulated `neuron_fees_e8s` (from rejected governance proposals) and whose controller specifies an explicit `amount` in a `Disburse` command is affected. This is a standard, unprivileged ingress call available to any neuron controller. The `reject_cost_e8s` on mainnet is 1 ICP, so any neuron that has had at least one rejected proposal will have non-zero `neuron_fees_e8s` at the time of disbursal (unless fees were already burned in a prior disburse attempt). Likelihood is **medium** — the condition requires both non-zero fees and an explicit amount argument, but both are common in practice.

---

### Recommendation

When the caller supplies an explicit `amount`, do not subtract `neuron_fees_e8s` from it. The fees are already handled by Transfer 1 (the burn). The disburse amount should be:

```rust
let mut disburse_amount_e8s = disburse
    .amount
    .as_ref()
    .map_or(neuron_minted_stake_e8s, |a| a.e8s);

// Cap at minted stake to prevent over-disbursal
disburse_amount_e8s = disburse_amount_e8s.min(neuron_minted_stake_e8s);
```

This aligns with the SNS governance implementation and ensures the caller receives exactly what they specified (minus the ledger transaction fee).

---

### Proof of Concept

**Setup:**
- Neuron: `cached_neuron_stake_e8s = 200_000_000` (2 ICP), `neuron_fees_e8s = 100_000_000` (1 ICP, from one rejected proposal)
- `minted_stake = 100_000_000` (1 ICP)
- `transaction_fee_e8s = 10_000`

**Caller action:** `manage_neuron(Disburse { amount: Some(Amount { e8s: 100_000_000 }), ... })`

**Expected:** Caller receives `100_000_000 − 10_000 = 99_990_000` e8s (1 ICP minus tx fee).

**Actual (current code):**
1. Transfer 1: burn `100_000_000` fees → neuron ledger balance = `100_000_000`, `cached_stake = 100_000_000`, `fees = 0`
2. `disburse_amount_e8s = 100_000_000.saturating_sub(100_000_000) = 0`
3. Since `0 ≤ transaction_fee_e8s`, no subtraction; `disburse_amount_e8s = 0`
4. Transfer 2: `0` ICP sent to caller — caller receives **nothing**

Even in a less extreme case (e.g., `amount = 150_000_000`, `fees = 100_000_000`):
- `disburse_amount_e8s = 150_000_000 − 100_000_000 − 10_000 = 49_990_000`
- Caller receives `49_990_000` instead of `149_990_000` — a shortfall of exactly `neuron_fees_e8s = 100_000_000`.

The root cause is at: [4](#0-3)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2008-2027)
```rust
        // Calculate the amount to transfer, and adjust the cached stake,
        // accordingly. Make sure no matter what the user disburses we still
        // take the fees into account.
        //
        // Note that the implementation of minted_stake_e8s() is effectively:
        //   neuron.cached_neuron_stake_e8s.saturating_sub(neuron.neuron_fees_e8s)
        // So there is symmetry here in that we are subtracting
        // fees_amount_e8s from both sides of this `map_or`.
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

**File:** rs/sns/governance/src/governance.rs (L1160-1166)
```rust
        let mut disburse_amount_e8s = disburse
            .amount
            .as_ref()
            .map_or(neuron.stake_e8s(), |a| a.e8s);

        // You cannot disburse more than the neuron's stake, which includes fees.
        disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s());
```
