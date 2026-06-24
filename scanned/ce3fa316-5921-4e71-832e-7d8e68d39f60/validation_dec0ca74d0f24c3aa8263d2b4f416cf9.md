### Title
Off-by-One Boundary Check in `disburse_neuron` Allows User to Receive Excess Tokens When Stake Equals Transaction Fee - (File: rs/nns/governance/src/governance.rs)

---

### Summary

In `disburse_neuron`, the condition used to subtract the transaction fee from the disbursement amount uses a strict `>` comparison instead of `>=`. When `disburse_amount_e8s` equals exactly `transaction_fee_e8s`, the fee is not subtracted, and the user receives `transaction_fee_e8s` tokens instead of zero. The identical bug exists in the SNS governance implementation.

---

### Finding Description

In `rs/nns/governance/src/governance.rs`, the function `disburse_neuron` computes the amount to transfer to the user and then attempts to subtract the ledger transaction fee: [1](#0-0) 

```rust
// Subtract the transaction fee from the amount to disburse since it'll
// be deducted from the source (the neuron's) account.
if disburse_amount_e8s > transaction_fee_e8s {
    disburse_amount_e8s -= transaction_fee_e8s
}
```

When `disburse_amount_e8s == transaction_fee_e8s`, the condition `>` is false, so the fee is **not** subtracted. The code then proceeds to call: [2](#0-1) 

```rust
let block_height = self
    .ledger
    .transfer_funds(
        disburse_amount_e8s,       // = transaction_fee_e8s
        transaction_fee_e8s,
        Some(neuron_subaccount),
        to_account,
        now,
    )
    .await?;
```

This transfers `transaction_fee_e8s` tokens to the user and charges `transaction_fee_e8s` as the ledger fee — debiting `2 * transaction_fee_e8s` from the neuron account. The user receives tokens they should not receive; the correct disbursement amount when `disburse_amount_e8s == transaction_fee_e8s` is zero (the entire amount is consumed by the fee).

The identical bug exists in the SNS governance implementation: [3](#0-2) 

```rust
// Subtract the transaction fee from the amount to disburse since it will
// be deducted from the source (the neuron's) account.
if disburse_amount_e8s > transaction_fee_e8s {
    disburse_amount_e8s -= transaction_fee_e8s
}
```

---

### Impact Explanation

A neuron controller can receive `transaction_fee_e8s` tokens (10,000 e8s = 0.0001 ICP for NNS) more than they are entitled to when disbursing a neuron whose minted stake equals exactly `transaction_fee_e8s`. This is a **ledger conservation bug**: tokens are created from the neuron account beyond what the accounting model permits. The neuron account is debited `2 * transaction_fee_e8s` while the user receives `transaction_fee_e8s` and the ledger burns `transaction_fee_e8s` — but the user's net gain is `transaction_fee_e8s` tokens that should have been zero.

---

### Likelihood Explanation

The condition `disburse_amount_e8s == transaction_fee_e8s` is reachable by any neuron controller. A concrete path:

- NNS `transaction_fee_e8s` = 10,000 e8s.
- Create a neuron with `cached_neuron_stake_e8s = 20,000` and accumulate `neuron_fees_e8s = 10,000` (e.g., via a rejected proposal whose `reject_cost_e8s = 10,000`).
- `neuron_minted_stake_e8s = 20,000 − 10,000 = 10,000 = transaction_fee_e8s`.
- The fee-burn guard `if fees_amount_e8s > transaction_fee_e8s` is also false (10,000 is not > 10,000), so fees are not burned and the neuron account retains `20,000` e8s — enough to cover `transfer_funds(10,000, 10,000, ...)`.
- The transfer succeeds and the user receives 10,000 e8s.

The attacker controls the neuron stake and fee accumulation entirely through normal governance participation. No privileged access is required.

---

### Recommendation

Change the strict `>` to `>=` in both files so that when `disburse_amount_e8s` equals `transaction_fee_e8s`, the result is zero and no transfer is issued:

**`rs/nns/governance/src/governance.rs` line 2025:**
```rust
// Before
if disburse_amount_e8s > transaction_fee_e8s {
// After
if disburse_amount_e8s >= transaction_fee_e8s {
```

**`rs/sns/governance/src/governance.rs` line 1170:**
```rust
// Before
if disburse_amount_e8s > transaction_fee_e8s {
// After
if disburse_amount_e8s >= transaction_fee_e8s {
```

---

### Proof of Concept

**NNS Governance — `disburse_neuron`:**

1. Create a dissolved neuron with `cached_neuron_stake_e8s = 20_000` and `neuron_fees_e8s = 10_000` (NNS `transaction_fee_e8s = 10_000`).
2. Call `disburse_neuron` with no explicit amount.
3. `neuron_minted_stake_e8s = 20_000 − 10_000 = 10_000`.
4. `disburse_amount_e8s = 10_000`.
5. Guard: `10_000 > 10_000` → **false** → fee not subtracted; `disburse_amount_e8s` stays `10_000`.
6. Fee-burn guard: `10_000 > 10_000` → **false** → fees not burned; neuron account retains `20_000` e8s.
7. `transfer_funds(10_000, 10_000, neuron_subaccount, to_account, now)` succeeds.
8. User receives **10,000 e8s** (0.0001 ICP); correct amount is **0**. [4](#0-3) [5](#0-4)

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

**File:** rs/nns/governance/src/governance.rs (L2091-2100)
```rust
        let block_height = self
            .ledger
            .transfer_funds(
                disburse_amount_e8s,
                transaction_fee_e8s,
                Some(neuron_subaccount),
                to_account,
                now,
            )
            .await?;
```

**File:** rs/sns/governance/src/governance.rs (L1160-1172)
```rust
        let mut disburse_amount_e8s = disburse
            .amount
            .as_ref()
            .map_or(neuron.stake_e8s(), |a| a.e8s);

        // You cannot disburse more than the neuron's stake, which includes fees.
        disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s());

        // Subtract the transaction fee from the amount to disburse since it will
        // be deducted from the source (the neuron's) account.
        if disburse_amount_e8s > transaction_fee_e8s {
            disburse_amount_e8s -= transaction_fee_e8s
        }
```
