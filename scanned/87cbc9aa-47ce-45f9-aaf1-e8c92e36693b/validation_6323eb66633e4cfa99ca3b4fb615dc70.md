### Title
Incorrect Fee Deduction from User-Specified Disbursement Amount in NNS Governance `disburse_neuron` - (File: `rs/nns/governance/src/governance.rs`)

### Summary
When a neuron controller calls `disburse_neuron` with an explicit `amount = Some(X)`, the NNS governance canister subtracts `neuron_fees_e8s` (accumulated governance penalty fees) from the user-specified amount before computing the disbursement. This causes the user to receive `X - fees - tx_fee` instead of the expected `X - tx_fee`. The fees are burned from the neuron's account, but they are also incorrectly deducted from the user-specified disbursement amount, reducing what the user receives. This is inconsistent with the SNS governance implementation of the same operation.

### Finding Description
In `disburse_neuron` at lines 2016–2021 of `rs/nns/governance/src/governance.rs`, when the caller provides an explicit `disburse.amount = Some(a)`, the code computes:

```rust
let mut disburse_amount_e8s = disburse
    .amount
    .as_ref()
    .map_or(neuron_minted_stake_e8s, |a| {
        a.e8s.saturating_sub(fees_amount_e8s)   // ← fees subtracted from user-specified amount
    });
``` [1](#0-0) 

Then `transaction_fee_e8s` is also subtracted: [2](#0-1) 

Separately, the fees are burned from the neuron's subaccount: [3](#0-2) 

And the disbursement transfer is made: [4](#0-3) 

The total deducted from the neuron's on-chain account is `fees_amount_e8s + disburse_amount_e8s + transaction_fee_e8s = fees + (X - fees) + tx_fee = X + tx_fee`. The user receives only `X - fees - tx_fee`.

The comment at line 2012–2015 claims "there is symmetry here," but the symmetry is false: when `amount = None`, the default `neuron_minted_stake_e8s` already equals `cached_stake - fees`, so the total taken from the neuron is `cached_stake + tx_fee` and the user receives `cached_stake - fees - tx_fee`. When `amount = Some(minted_stake)` (i.e., `Some(cached_stake - fees)`), the total taken from the neuron is only `minted_stake + tx_fee = cached_stake - fees + tx_fee`, and the user receives `minted_stake - fees - tx_fee = cached_stake - 2·fees - tx_fee` — `fees` fewer tokens than the `amount = None` path for the same logical amount. [5](#0-4) 

The SNS governance implementation of the same operation does **not** subtract fees from the user-specified amount:

```rust
let mut disburse_amount_e8s = disburse
    .amount
    .as_ref()
    .map_or(neuron.stake_e8s(), |a| a.e8s);   // ← no fee subtraction
disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s());
``` [6](#0-5) 

This confirms the NNS behavior is inconsistent and incorrect.

### Impact Explanation
Any NNS neuron controller who has accumulated `neuron_fees_e8s > 0` (from rejected governance proposals) and calls `disburse_neuron` with an explicit `amount` will receive `neuron_fees_e8s` fewer ICP than they specified. The shortfall is burned (not stolen), but the user suffers a direct, unannounced token loss. For neurons with large accumulated fees, this can be a material amount of ICP. The governance canister's own cached stake accounting is also left inconsistent: `cached_neuron_stake_e8s` is reduced by `fees + disburse_amount + tx_fee`, but the user only received `disburse_amount` worth of value. [7](#0-6) 

### Likelihood Explanation
Medium.

### Citations

**File:** rs/nns/governance/src/governance.rs (L2008-2021)
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
```

**File:** rs/nns/governance/src/governance.rs (L2023-2027)
```rust
        // Subtract the transaction fee from the amount to disburse since it'll
        // be deducted from the source (the neuron's) account.
        if disburse_amount_e8s > transaction_fee_e8s {
            disburse_amount_e8s -= transaction_fee_e8s
        }
```

**File:** rs/nns/governance/src/governance.rs (L2046-2064)
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

**File:** rs/nns/governance/src/governance.rs (L2102-2107)
```rust
        self.with_neuron_mut(id, |neuron| {
            let to_deduct = disburse_amount_e8s + transaction_fee_e8s;
            // The transfer was successful we can change the stake of the neuron.
            neuron.cached_neuron_stake_e8s =
                neuron.cached_neuron_stake_e8s.saturating_sub(to_deduct);
        })
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
