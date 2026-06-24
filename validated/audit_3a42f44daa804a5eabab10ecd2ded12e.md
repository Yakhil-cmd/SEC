Audit Report

## Title
SNS Governance `maybe_finalize_disburse_maturity` Double-Mint After Successful Transfer — (File: rs/sns/governance/src/governance.rs)

## Summary
The SNS governance canister's `maybe_finalize_disburse_maturity` function mints SNS tokens via an async inter-canister call to the ledger, then removes the `DisburseMaturityInProgress` entry only after the mint succeeds. If the callback traps after a successful mint (or if `get_neuron_result_mut` returns `Err` post-await), the governance state is rolled back while the ledger mint is already committed, leaving the disbursement entry in the queue for re-execution. The NNS governance equivalent explicitly avoids this by popping the entry before the ledger call and pushing it back only on failure.

## Finding Description
In `rs/sns/governance/src/governance.rs`, `maybe_finalize_disburse_maturity` (lines 4920–5083) iterates over neurons with ready disbursements. For each entry:

1. It acquires a neuron lock (line 5006).
2. It calls `self.ledger.transfer_funds(...)` with memo `self.env.now()` — an async minting call (lines 5037–5046).
3. On `Ok(block_index)`, it calls `self.get_neuron_result_mut(&neuron_id)` (line 5056). If this returns `Err`, the code hits `continue` at line 5066, skipping `remove(0)` entirely — the mint succeeded but the entry is never removed.
4. Only if both the ledger call and the re-borrow succeed does `neuron.disburse_maturity_in_progress.remove(0)` execute at line 5069.

The disbursement entry is never removed before the ledger call. Two concrete failure modes leave it in the queue after a successful mint:

**Mode A:** `get_neuron_result_mut` returns `Err` after the async call returns (e.g., neuron store inconsistency). The `continue` at line 5066 skips `remove(0)`.

**Mode B:** The callback traps after `transfer_funds` returns `Ok` but before `remove(0)` executes (e.g., `remove(0)` panics on an unexpectedly empty list, or any other post-await trap). Per the IC execution model, the governance canister's state changes from that callback are rolled back; the ledger mint is already committed in a separate canister's state.

The memo is `self.env.now()` (line 5044), which changes on each periodic task invocation, so the ledger has no deduplication window to reject the duplicate mint.

By contrast, `rs/nns/governance/src/governance/disburse_maturity.rs` lines 615–623 pops the disbursement entry **before** calling the ledger, and lines 652–656 push it back only on ledger failure — the correct pattern that SNS governance lacks.

## Impact Explanation
Each re-execution of the same `DisburseMaturityInProgress` entry mints a fresh batch of SNS tokens from the governance minting account to the neuron owner's account. The neuron's `maturity_e8s_equivalent` was already decremented when the disbursement was initiated (lines 1693–1695), so the ledger's total supply grows by the disbursement amount on every duplicate mint with no corresponding maturity deduction. This constitutes illegal minting of SNS tokens and unbounded inflation of the SNS token supply, matching the **High** impact class: "Significant SNS security impact with concrete user or protocol harm" — specifically unauthorized token creation that dilutes all SNS token holders and can drain the SNS treasury's economic integrity.

## Likelihood Explanation
The most realistic trigger is Mode A: `get_neuron_result_mut` returning `Err` after the async call. While uncommon in normal operation, any transient inconsistency in the neuron store (e.g., concurrent message processing that removes or replaces the neuron entry between the await point and the re-borrow) triggers it without any attacker action. Mode B (callback trap) requires a post-mint panic, which is possible if `disburse_maturity_in_progress` is concurrently emptied before `remove(0)`. Both modes require no special privileges — they are triggered by normal SNS operation. The condition is repeatable: every subsequent periodic task invocation re-mints until the entry is manually cleared or the neuron is otherwise modified.

## Recommendation
Adopt the pattern used by NNS governance's `try_finalize_maturity_disbursement`:

1. **Pop the disbursement entry before calling the ledger.** Remove the entry from `disburse_maturity_in_progress` atomically before the `transfer_funds` await.
2. **Push it back only on ledger failure.** If `transfer_funds` returns `Err`, re-insert the entry at the front of the list so it is retried on the next periodic task.
3. **Retain the neuron lock if the push-back itself fails**, preventing the entry from being re-processed in an inconsistent state.
4. Use a stable idempotency key (e.g., a hash of `(neuron_id, disbursement_timestamp, amount)`) as the ledger memo instead of `self.env.now()`, so that any duplicate mint attempt falls within the ledger's deduplication window and is rejected.

## Proof of Concept
1. SNS neuron owner calls `manage_neuron` → `DisburseMaturity { percentage_to_disburse: 100 }`. `maturity_e8s_equivalent` is decremented and a `DisburseMaturityInProgress` entry is pushed (lines 1693–1698).
2. After the disbursement delay, the SNS governance heartbeat calls `maybe_finalize_disburse_maturity`. The entry's `finalize_disbursement_timestamp_seconds` is in the past.
3. `transfer_funds` is called and the ledger commits the mint, returning `Ok(block_index)`.
4. After the `Ok` match, `get_neuron_result_mut` returns `Err` (e.g., inject a neuron store fault in a test, or arrange for the neuron to be absent at the re-borrow point). The `continue` at line 5066 is hit; `remove(0)` is never called.
5. The next heartbeat finds the same entry still present (timestamp still in the past) and mints again.
6. A deterministic integration test or PocketIC test can reproduce this by: (a) stubbing `get_neuron_result_mut` to return `Err` on the second call within the callback, (b) verifying the ledger balance increases by 2× the disbursement amount, and (c) verifying the `disburse_maturity_in_progress` list is non-empty after the first periodic task run.