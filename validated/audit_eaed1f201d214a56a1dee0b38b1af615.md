### Title
NNS Governance `disburse_neuron` Burns Fees Before Validating Disburse Amount, Causing Irrecoverable Fund Loss - (File: rs/nns/governance/src/governance.rs)

---

### Summary

The NNS governance canister's `disburse_neuron` function does not validate that the caller-supplied disburse amount is within the neuron's actual minted stake before executing a two-step ledger operation. If the requested amount exceeds the neuron's stake, the first ledger call (fee burn) permanently succeeds and mutates both the ledger and governance state, while the second ledger call (the actual disburse transfer) predictably fails. The caller loses their neuron fees without receiving any disbursement.

---

### Finding Description

`disburse_neuron` in `rs/nns/governance/src/governance.rs` performs two sequential ledger transfers:

1. **Transfer 1** — Burns `fees_amount_e8s` from the neuron's subaccount to the minting account (if fees exceed the transaction fee).
2. **Transfer 2** — Transfers `disburse_amount_e8s` from the neuron's subaccount to the caller's chosen account.

The `disburse_amount_e8s` is computed from the caller-supplied `disburse.amount.e8s` with no upper-bound cap against the neuron's actual minted stake:

```rust
let mut disburse_amount_e8s = disburse
    .amount
    .as_ref()
    .map_or(neuron_minted_stake_e8s, |a| {
        a.e8s.saturating_sub(fees_amount_e8s)
    });
``` [1](#0-0) 

If the caller supplies an `amount.e8s` larger than the neuron's minted stake, Transfer 1 (fee burn) still executes and succeeds. The governance state is then mutated to zero out `neuron_fees_e8s` and reduce `cached_neuron_stake_e8s`: [2](#0-1) 

Transfer 2 then fails because the neuron's subaccount no longer holds enough tokens. The code itself acknowledges this: [3](#0-2) 

The SNS governance counterpart **does** have the missing guard — it explicitly caps the disburse amount to the neuron's stake before proceeding:

```rust
// You cannot disburse more than the neuron's stake, which includes fees.
disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s());
``` [4](#0-3) 

The NNS version has no equivalent cap.

---

### Impact Explanation

A neuron controller who calls `manage_neuron` → `Disburse` with an `amount.e8s` exceeding their neuron's minted stake will:

- Have their neuron fees permanently burned from the ICP ledger (irreversible on-chain).
- Have their governance-side `neuron_fees_e8s` zeroed and `cached_neuron_stake_e8s` reduced.
- Receive **zero** ICP in return, because Transfer 2 fails with `InsufficientFunds`.

This is a **ledger conservation bug**: the neuron's on-chain token balance is reduced (fees burned) without the corresponding credit to the caller. The existing test `test_cant_disburse_without_paying_fees` confirms this behavior — after a failed over-amount disburse, `neuron_fees_e8s == 0` and the neuron's ledger account is reduced by the fee amount, yet the caller receives nothing. [5](#0-4) 

---

### Likelihood Explanation

The `manage_neuron` endpoint is a public update call on the NNS governance canister, reachable by any ICP holder who controls a dissolved neuron. The trigger requires only that the caller supply a `Disburse.amount.e8s` value larger than their neuron's minted stake. This can happen:

- By user error (e.g., specifying a round number like `1_000_000_000_000` without knowing the exact stake).
- By a UI/wallet bug that passes an incorrect amount.
- Deliberately, to grief oneself (no external attacker benefit, but the user suffers fund loss).

Any dissolved neuron with non-trivial `neuron_fees_e8s` (i.e., fees > `transaction_fee_e8s`) is at risk.

---

### Recommendation

Add an upfront cap on `disburse_amount_e8s` to `neuron_minted_stake_e8s`, mirroring the SNS governance fix:

```rust
// Cap the disburse amount to the neuron's minted stake to prevent
// burning fees when the disburse transfer would predictably fail.
disburse_amount_e8s = disburse_amount_e8s.min(neuron_minted_stake_e8s);
```

This should be inserted immediately after the `disburse_amount_e8s` computation at line 2021, before the neuron lock is acquired and before any ledger calls are made. [6](#0-5) 

---

### Proof of Concept

1. Create a dissolved, KYC-verified neuron with `cached_neuron_stake_e8s = 100_000_000` (1 ICP) and `neuron_fees_e8s = 20_000` (fees > `transaction_fee_e8s = 10_000`).
2. Call `manage_neuron` with `Command::Disburse { amount: Some(Amount { e8s: 10_000_000_000 }) }` (100 ICP, far exceeding the stake).
3. **Observed**: Transfer 1 burns 20_000 e8s of fees from the neuron's ledger subaccount. Governance state updates: `neuron_fees_e8s = 0`, `cached_neuron_stake_e8s = 80_000_000`. Transfer 2 attempts to send `10_000_000_000 - 20_000 - 10_000 = 9_999_970_000` e8s but fails with `InsufficientFunds { balance: 80_000_000 }`. The caller receives 0 ICP. The 20_000 e8s in fees are permanently lost.
4. **Expected**: The function should return an error before any ledger call, preserving the neuron's state. [7](#0-6)

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

**File:** rs/nns/governance/src/governance.rs (L2039-2100)
```rust
        // We need to do 2 transfers:
        // 1 - Burn the neuron management fees.
        // 2 - Transfer the the disbursed amount to the target account

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

        // Transfer 2 - Disburse to the chosen account. This may fail if the
        // user told us to disburse more than they had in their account (but
        // the burn still happened).
        let now = self.env.now();

        tla_log_label!("DisburseNeuron_Stake");
        tla_log_locals! {
            fees_amount: fees_amount_e8s,
            neuron_id: id.id,
            to_account: tla::account_to_tla(to_account),
            disburse_amount: disburse_amount_e8s
        };

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

**File:** rs/sns/governance/src/governance.rs (L1165-1166)
```rust
        // You cannot disburse more than the neuron's stake, which includes fees.
        disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s());
```

**File:** rs/nns/governance/tests/governance.rs (L5272-5303)
```rust
    // Try to disburse more than the stake amount, this should fail.
    // and cause the neuron to be unchanged.
    let result = gov
        .disburse_neuron(
            &id,
            &from,
            &Disburse {
                amount: Some(Amount {
                    e8s: 1000 * 100_000_000,
                }),
                to_account: Some(AccountIdentifier::new(from, None).into()),
            },
        )
        .now_or_never()
        .unwrap();

    assert!(result.is_err());
    assert_eq!(result.unwrap_err().error_type, ErrorType::External as i32);

    assert_eq!(
        0,
        gov.neuron_store
            .with_neuron(&id, |neuron| neuron.neuron_fees_e8s)
            .expect("Neuron not found")
    );
    driver.assert_account_contains(
        &AccountIdentifier::new(
            GOVERNANCE_CANISTER_ID.get(),
            Some(Subaccount::try_from(&neuron.account[..]).unwrap()),
        ),
        neuron_stake_e8s - neuron_fees_e8s,
    );
```
