Audit Report

## Title
NNS Governance `disburse_neuron` Unconditionally Zeros `neuron_fees_e8s` Even When Fees Are Not Burned on Ledger — (File: rs/nns/governance/src/governance.rs)

## Summary
In `disburse_neuron`, the governance state update that zeros `neuron_fees_e8s` and reduces `cached_neuron_stake_e8s` by `fees_amount_e8s` executes unconditionally, even when the fee burn is skipped because `fees_amount_e8s ≤ transaction_fee_e8s`. This causes the governance's accounting to permanently diverge from the actual ledger balance, stranding up to 9,999 e8s in the neuron's subaccount. The SNS governance canister was explicitly patched for this identical issue.

## Finding Description
In `rs/nns/governance/src/governance.rs`, the fee burn is correctly gated:

```rust
// Line 2046
if fees_amount_e8s > transaction_fee_e8s {
    self.ledger.transfer_funds(fees_amount_e8s, 0, ...).await?;
}
```

Immediately after, the governance state update runs **unconditionally** outside that block:

```rust
// Lines 2067-2076
self.with_neuron_mut(id, |neuron| {
    if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
        neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
    } else {
        neuron.cached_neuron_stake_e8s = 0;
    }
    neuron.neuron_fees_e8s = 0;  // ← always zeroed
})
```

When `fees_amount_e8s ≤ transaction_fee_e8s`, no burn occurs on the ledger, yet governance subtracts `fees_amount_e8s` from `cached_neuron_stake_e8s` and sets `neuron_fees_e8s = 0`. The subsequent disburse transfer (lines 2091–2108) then sends `cached_stake - fees - tx_fee` to the user. After the transfer, the ledger subaccount retains exactly `fees_amount_e8s` tokens that were never burned, while governance records `cached_neuron_stake_e8s = 0` and `neuron_fees_e8s = 0`.

The SNS governance fix (lines 1193–1208 of `rs/sns/governance/src/governance.rs`) explicitly moves the state update inside the conditional block with the comment: *"We only update the cached_neuron_stake_e8s and neuron_fees_e8s if we actually burn fees, otherwise this leads to ledger and governance getting out of sync."* The SNS CHANGELOG (lines 89–93) confirms this was a deliberate bug fix.

## Impact Explanation
This is a **Medium** severity finding. Any neuron owner whose `neuron_fees_e8s` is between 1 and 9,999 e8s at disbursal time permanently loses up to 9,999 e8s (≈ 0.0001 ICP) in their neuron's subaccount. The governance-ledger invariant (`cached_neuron_stake_e8s` reflects actual ledger balance) is violated. The stranded tokens are unrecoverable: `refresh_neuron` would update `cached_neuron_stake_e8s` to the residual, but a follow-up disburse would compute a transfer amount of `residual - tx_fee` which underflows via `saturating_sub` to 0, making the tokens permanently inaccessible. This constitutes concrete, permanent loss of user funds in NNS governance, an in-scope component.

## Likelihood Explanation
The `neuron_management_fee_per_proposal_e8s` in NNS economics is 1,000 e8s. Any neuron that has accumulated between 1 and 9 rejected proposals has fees between 1,000 and 9,000 e8s — all below the 10,000 e8s burn threshold. This is a realistic scenario for active governance participants. The bug is triggered by the neuron owner's own normal disburse action with no special privileges, no victim mistakes, and no attacker involvement required. It is self-inflicted by the protocol on any qualifying neuron at disbursal time.

## Recommendation
Mirror the SNS governance fix: move the `neuron_fees_e8s = 0` and `cached_neuron_stake_e8s -= fees_amount_e8s` assignments inside the `if fees_amount_e8s > transaction_fee_e8s` block in `rs/nns/governance/src/governance.rs`. When fees are too small to burn, they should remain in `neuron_fees_e8s` and `cached_neuron_stake_e8s` unchanged, and the disburse amount should be computed as `minted_stake_e8s() - transaction_fee_e8s`, which is already the default path when no explicit amount is specified.

## Proof of Concept
**Precondition:** Neuron with `cached_neuron_stake_e8s = 1_000_000_000`, `neuron_fees_e8s = 5_000`, `transaction_fee_e8s = 10_000`.

1. `fees_amount_e8s = 5_000`, `neuron_minted_stake_e8s = 999_995_000`
2. `disburse_amount_e8s = 999_995_000 - 10_000 = 999_985_000`
3. Fee burn skipped (`5_000 ≤ 10_000`)
4. Unconditional state update: `cached_neuron_stake_e8s = 999_995_000`, `neuron_fees_e8s = 0`
5. Ledger transfer: `999_985_000` sent to user, `10_000` tx fee deducted
   - Ledger subaccount balance: `1_000_000_000 - 999_985_000 - 10_000 = 5_000`
6. Post-transfer state update: `999_995_000 - (999_985_000 + 10_000) = 0`

**Result:** Governance: `cached_neuron_stake_e8s = 0`, `neuron_fees_e8s = 0`. Ledger subaccount: `5_000` e8s permanently stranded.

A unit test can be written against the existing mock ledger infrastructure in `rs/nns/governance/src/governance_tests/` by constructing a neuron with `neuron_fees_e8s = 5_000`, calling `disburse_neuron`, and asserting that the mock ledger subaccount balance equals `5_000` while governance records `cached_neuron_stake_e8s = 0` — demonstrating the invariant violation directly.