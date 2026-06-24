### Title
NNS Governance `disburse_neuron` Unconditionally Zeroes `neuron_fees_e8s` Even When Fee Burn Is Skipped, Causing Permanent Ledger/Governance Accounting Divergence - (`File: rs/nns/governance/src/governance.rs`)

---

### Summary

In `rs/nns/governance/src/governance.rs`, the `disburse_neuron` function unconditionally sets `neuron.neuron_fees_e8s = 0` and decrements `neuron.cached_neuron_stake_e8s` by `fees_amount_e8s` regardless of whether the fee-burn ledger transfer actually executed. When `fees_amount_e8s <= transaction_fee_e8s`, the burn is intentionally skipped (the ICP ledger rejects burns below the transaction fee), but the governance accounting variables are still updated as if the burn succeeded. This permanently diverges the governance neuron state from the actual ICP ledger balance.

---

### Finding Description

`disburse_neuron` performs two ledger transfers: (1) burn the neuron's accumulated management fees, and (2) transfer the disbursed stake to the recipient. The burn is gated:

```rust
// rs/nns/governance/src/governance.rs:2046-2065
if fees_amount_e8s > transaction_fee_e8s {
    let _result = self.ledger.transfer_funds(
        fees_amount_e8s, 0,
        Some(neuron_subaccount), governance_minting_account(), now,
    ).await?;
}
```

Immediately after — unconditionally, outside the `if` block — the neuron's accounting fields are updated:

```rust
// rs/nns/governance/src/governance.rs:2067-2076
self.with_neuron_mut(id, |neuron| {
    if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
        neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
    } else {
        neuron.cached_neuron_stake_e8s = 0;
    }
    neuron.neuron_fees_e8s = 0;   // ← always executed
})
.expect("Expected the parent neuron to exist");
```

When `fees_amount_e8s <= transaction_fee_e8s` (burn skipped):
- The ICP ledger subaccount still holds `fees_amount_e8s` tokens (no burn occurred).
- Governance sets `neuron_fees_e8s = 0` and reduces `cached_neuron_stake_e8s` by `fees_amount_e8s`.
- After the subsequent disburse transfer (lines 2091–2107), governance believes `cached_neuron_stake_e8s = 0`, but the ledger subaccount retains `fees_amount_e8s` tokens permanently with no recovery path.

The SNS governance canister already identified and fixed this exact pattern. Its `disburse_neuron` places the neuron-state update **inside** the burn-conditional block and carries an explicit comment:

```rust
// rs/sns/governance/src/governance.rs:1193-1194
// We only update the cached_neuron_stake_e8s and neuron_fees_e8s if we actually
// burn fees, otherwise this leads to ledger and governance getting out of sync.
```

The NNS governance retains the unfixed pattern.

---

### Impact Explanation

Every dissolved NNS neuron with `0 < neuron_fees_e8s <= transaction_fee_e8s` (up to 9 999 e8s ≈ 0.0001 ICP) that is disbursed will permanently strand those tokens in the neuron's ICP ledger subaccount. Governance records the neuron as fully emptied (`cached_neuron_stake_e8s = 0`, `neuron_fees_e8s = 0`), but the ledger disagrees. The discrepancy is irreversible: no governance command can recover the stranded balance because governance believes the neuron has zero stake. Over time, as many neurons accumulate small reject-cost fees and are subsequently dissolved and disbursed, the aggregate stranded ICP grows. Additionally, any tooling or monitoring that relies on `cached_neuron_stake_e8s` as a faithful mirror of the ledger balance will produce incorrect results.

---

### Likelihood Explanation

The condition `0 < neuron_fees_e8s <= transaction_fee_e8s` is reachable by any NNS neuron owner. A neuron accumulates `neuron_fees_e8s` equal to `reject_cost_e8s` each time one of its proposals is rejected. The NNS `reject_cost_e8s` is currently set to `transaction_fee_e8s` (10 000 e8s), so a neuron with exactly one rejected proposal has `neuron_fees_e8s == transaction_fee_e8s`, which does **not** satisfy `> transaction_fee_e8s` and therefore skips the burn. Any neuron owner who submits a proposal that is rejected and then dissolves and disburses their neuron triggers this path. This is a normal, unprivileged user flow.

---

### Recommendation

Mirror the SNS governance fix: move the `neuron_fees_e8s` and `cached_neuron_stake_e8s` update inside the `if fees_amount_e8s > transaction_fee_e8s` block, so accounting is only adjusted when the burn actually executes. When the burn is skipped, leave `neuron_fees_e8s` unchanged (or explicitly document and accept the write-off, but do not reduce `cached_neuron_stake_e8s` without a corresponding ledger debit).

```rust
if fees_amount_e8s > transaction_fee_e8s {
    self.ledger.transfer_funds(...).await?;

    // Only update accounting when the burn actually happened.
    self.with_neuron_mut(id, |neuron| {
        neuron.cached_neuron_stake_e8s =
            neuron.cached_neuron_stake_e8s.saturating_sub(fees_amount_e8s);
        neuron.neuron_fees_e8s = 0;
    }).expect("Expected the parent neuron to exist");
}
```

---

### Proof of Concept

**Trigger condition:** neuron with `neuron_fees_e8s = 5_000` (< `transaction_fee_e8s = 10_000`), dissolved, disbursed in full.

**Step-by-step:**

1. `fees_amount_e8s = 5_000`, `transaction_fee_e8s = 10_000`.
2. Condition `fees_amount_e8s > transaction_fee_e8s` is **false** → burn skipped; ICP ledger subaccount still holds 5 000 e8s.
3. Lines 2067–2076 execute unconditionally: `cached_neuron_stake_e8s -= 5_000`; `neuron_fees_e8s = 0`.
4. Disburse transfer (lines 2091–2107) moves `disburse_amount_e8s + transaction_fee_e8s` out of the subaccount; governance then sets `cached_neuron_stake_e8s = 0`.
5. **Result:** ledger subaccount balance = 5 000 e8s; governance `cached_neuron_stake_e8s = 0`, `neuron_fees_e8s = 0`. The 5 000 e8s are permanently stranded.

The SNS governance unit test `test_disburse_neuron_small_fees_not_burned` (in `rs/sns/governance/src/governance/disburse_neuron_tests.rs:540`) explicitly validates the correct behaviour — fees are preserved in `neuron_fees_e8s` when the burn is skipped — confirming that the NNS governance code diverges from the correct pattern. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

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

**File:** rs/nns/governance/tla/Disburse_Neuron.tla (L98-105)
```text
                if(fees_amount > TRANSACTION_FEE) {
                    send_request(self, transfer(neuron[neuron_id].account, Minting_Account_Id, fees_amount, 0));
                }
                else {
                    update_fees(neuron_id, fees_amount);
                    send_request(self, transfer(neuron[neuron_id].account, to_account, disburse_amount, TRANSACTION_FEE));
                    goto DisburseNeuron_Stake_WaitForTransfer;
                };
```
