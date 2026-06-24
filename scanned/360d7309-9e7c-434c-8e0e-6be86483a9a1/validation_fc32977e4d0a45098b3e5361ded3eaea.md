### Title
Double-Subtraction of `neuron_fees_e8s` in `disburse_neuron` Causes Under-Disbursement to Neuron Controller - (File: rs/nns/governance/src/governance.rs)

### Summary

`disburse_neuron` in NNS governance subtracts `neuron_fees_e8s` from the caller-supplied disburse amount **and** burns those same fees in a separate ledger transfer. A neuron controller who supplies an explicit amount equal to `minted_stake_e8s()` — the value naturally returned by the public "available balance" helper — receives `neuron_fees_e8s` fewer ICP than expected, with the shortfall silently stranded in the neuron's subaccount.

### Finding Description

`minted_stake_e8s()` is defined as:

```rust
pub fn minted_stake_e8s(&self) -> u64 {
    self.cached_neuron_stake_e8s
        .saturating_sub(self.neuron_fees_e8s)
}
``` [1](#0-0) 

It already subtracts `neuron_fees_e8s` from the raw on-chain balance.

`disburse_neuron` then computes the transfer amount as:

```rust
let mut disburse_amount_e8s = disburse
    .amount
    .as_ref()
    .map_or(neuron_minted_stake_e8s, |a| {
        a.e8s.saturating_sub(fees_amount_e8s)   // ← subtracts fees from caller amount
    });
``` [2](#0-1) 

Immediately after, the same `fees_amount_e8s` is burned in Transfer 1:

```rust
if fees_amount_e8s > transaction_fee_e8s {
    self.ledger.transfer_funds(fees_amount_e8s, 0, …).await?;
}
``` [3](#0-2) 

**Concrete scenario** — neuron with `cached_neuron_stake_e8s = S`, `neuron_fees_e8s = F`, `minted_stake_e8s() = S − F`:

| Step | Expected | Actual |
|---|---|---|
| Caller supplies `amount = S − F` (the "available" balance) | `disburse_amount = (S−F) − tx_fee` | `disburse_amount = (S−F) − F − tx_fee = S − 2F − tx_fee` |
| Transfer 1 burns fees | F burned | F burned |
| Transfer 2 sends to caller | `S − F − tx_fee` ICP | `S − 2F − tx_fee` ICP |
| Residual in neuron subaccount | 0 | F (stranded) |

The caller is short-changed by exactly `F` ICP. The stranded `F` tokens remain in the neuron's subaccount with `cached_neuron_stake_e8s` updated to `F`, requiring a second disburse call (and paying a second `transaction_fee_e8s`).

The SNS governance implementation does **not** have this flaw — it caps at `stake_e8s()` without subtracting fees from the caller-supplied amount:

```rust
let mut disburse_amount_e8s = disburse.amount.as_ref().map_or(neuron.stake_e8s(), |a| a.e8s);
disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s());
``` [4](#0-3) 

### Impact Explanation

A neuron controller who queries `minted_stake_e8s()` (the natural "how much can I disburse?" value) and passes it as the explicit `amount` in a `Disburse` command receives `neuron_fees_e8s` fewer ICP than the full net stake. The shortfall is not permanently destroyed — it remains in the neuron's governance subaccount — but it is inaccessible until the controller issues a second disburse call, paying an additional `transaction_fee_e8s`. For neurons with large accumulated fees (e.g., from many rejected proposals), the under-disbursement can be significant. This is a **ledger conservation / accounting correctness bug**: ICP that should flow to the controller is silently retained.

### Likelihood Explanation

Any dissolved neuron controller is an unprivileged ingress sender who can trigger this path. The trigger condition — supplying `amount = minted_stake_e8s()` — is the most natural thing a client or dapp would do after querying the neuron's available balance. No special privileges, timing, or coordination are required. The NNS governance canister is one of the highest-value canisters on the IC, making even low-severity accounting discrepancies worth addressing.

### Recommendation

Align NNS governance with the SNS implementation: do not subtract `fees_amount_e8s` from the caller-supplied amount. Instead, cap `disburse_amount_e8s` at `neuron_minted_stake_e8s` (the net-of-fees value), mirroring the SNS pattern:

```rust
let mut disburse_amount_e8s = disburse
    .amount
    .as_ref()
    .map_or(neuron_minted_stake_e8s, |a| a.e8s);

// Cap at the net stake (fees will be burned separately).
disburse_amount_e8s = disburse_amount_e8s.min(neuron_minted_stake_e8s);
```

This ensures that a caller who supplies `amount = minted_stake_e8s()` receives exactly `minted_stake_e8s() − transaction_fee_e8s` ICP, consistent with the no-amount (`None`) path.

### Proof of Concept

The existing test `test_nns1_520` inadvertently documents the double-subtraction: it supplies `amount = neuron_stake_e8s` (the **gross** stake) and asserts the caller receives `neuron_stake_e8s − neuron_fees_e8s − transaction_fee_e8s`. [5](#0-4) 

A complementary test demonstrating the bug:

```rust
// Neuron: cached_stake = 1_000_000_000, fees = 50_000_000
// minted_stake = 950_000_000
gov.disburse_neuron(
    &id, &from,
    &Disburse {
        amount: Some(Amount { e8s: 950_000_000 }), // = minted_stake_e8s()
        to_account: None,
    },
).now_or_never().unwrap().unwrap();

// Actual: caller receives 950_000_000 - 50_000_000 - tx_fee = 900_000_000 - tx_fee
// Expected: caller receives 950_000_000 - tx_fee
// Shortfall: 50_000_000 e8s (= neuron_fees_e8s) stranded in neuron subaccount
```

The root cause is at `rs/nns/governance/src/governance.rs` line 2020 (`a.e8s.saturating_sub(fees_amount_e8s)`), where `fees_amount_e8s` is subtracted from the caller-supplied amount despite being burned independently in Transfer 1 at lines 2046–2064. [6](#0-5)

### Citations

**File:** rs/nns/governance/src/neuron/types.rs (L983-986)
```rust
    pub fn minted_stake_e8s(&self) -> u64 {
        self.cached_neuron_stake_e8s
            .saturating_sub(self.neuron_fees_e8s)
    }
```

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

**File:** rs/sns/governance/src/governance.rs (L1160-1166)
```rust
        let mut disburse_amount_e8s = disburse
            .amount
            .as_ref()
            .map_or(neuron.stake_e8s(), |a| a.e8s);

        // You cannot disburse more than the neuron's stake, which includes fees.
        disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s());
```

**File:** rs/nns/governance/tests/governance.rs (L4585-4617)
```rust
    gov.disburse_neuron(
        &id,
        &from,
        &Disburse {
            amount: Some(Amount { e8s: 100000000 }),
            to_account: Some(AccountIdentifier::new(from, Some(to_subaccount)).into()),
        },
    )
    .now_or_never()
    .unwrap()
    .unwrap();

    // The user's account should now have the amount
    driver.assert_account_contains(
        &AccountIdentifier::new(from, Some(to_subaccount)),
        // In the end, the user's account should have the stake - fees
        // - transaction fees.
        neuron_stake_e8s
            - neuron_fees_e8s
            - gov
                .heap_data
                .economics
                .as_ref()
                .unwrap()
                .transaction_fee_e8s,
    );

    assert_eq!(
        gov.neuron_store
            .with_neuron(&id, |n| n.cached_neuron_stake_e8s)
            .expect("Neuron not found"),
        0
    );
```
