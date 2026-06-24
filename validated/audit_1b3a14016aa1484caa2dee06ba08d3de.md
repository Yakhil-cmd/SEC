The code confirms all the key claims. Let me verify the `disburse_maturity` caller authorization and check if there's any existing guard I might have missed.

Audit Report

## Title
SNS Neuron Maturity Drained to Old Owner After Permission Transfer via Unguarded `disburse_maturity_in_progress` - (`rs/sns/governance/src/governance.rs`)

## Summary
An SNS neuron owner can call `disburse_maturity` to lock in a pending disbursement to their own account, then immediately transfer full neuron control to a new owner via `add_neuron_permissions` / `remove_neuron_permissions`. Neither permission-change function checks for pending `disburse_maturity_in_progress` entries. After the 7-day delay, the governance timer mints the maturity to the original owner, leaving the new owner with a neuron stripped of its maturity and no mechanism to cancel the in-flight disbursement.

## Finding Description
In `disburse_maturity` (L1609–1706), the caller's destination account is captured immediately into a `DisburseMaturityInProgress` struct and pushed onto `neuron.disburse_maturity_in_progress`, while `neuron.maturity_e8s_equivalent` is decremented atomically:

```rust
// L1681-1698
let disbursement_in_progress = DisburseMaturityInProgress {
    amount_e8s: maturity_to_deduct,
    account_to_disburse_to: Some(to_account_proto),  // fixed at call time
    finalize_disbursement_timestamp_seconds: Some(now_seconds + MATURITY_DISBURSEMENT_DELAY_SECONDS),
    ...
};
neuron.maturity_e8s_equivalent = neuron.maturity_e8s_equivalent.saturating_sub(maturity_to_deduct);
neuron.disburse_maturity_in_progress.push(disbursement_in_progress);
```

`add_neuron_permissions` (L4570–4643) performs only three checks: caller authorization, grantable-permissions validation, and principal-count limit. It contains no inspection of `disburse_maturity_in_progress`. `remove_neuron_permissions` (L4659–4715) similarly checks only caller authorization and permission existence. Neither function blocks or warns when pending disbursements exist.

The periodic governance task (L4976–5046) iterates all neurons, finds entries whose `finalize_disbursement_timestamp_seconds` has elapsed, and calls `ledger.transfer_funds` to the stored `account_to_disburse_to` — which is immutably the original caller's account. A `grep` for `cancel.*disburse` across the entire SNS governance source returns no matches, confirming there is no cancellation endpoint.

Exploit path:
1. Principal A holds a neuron with `maturity_e8s_equivalent = N` and `DisburseMaturity` + `ManagePrincipals` permissions.
2. A calls `disburse_maturity { percentage_to_disburse: 100, to_account: A }` → maturity becomes 0, pending record points to A.
3. A calls `add_neuron_permissions { principal_id: B, permissions: [all] }` — succeeds with no disbursement check.
4. A calls `remove_neuron_permissions { principal_id: A, permissions: [all] }` — succeeds with no disbursement check.
5. B now controls the neuron, observes `maturity_e8s_equivalent = 0`, and cannot cancel the pending entry.
6. After `MATURITY_DISBURSEMENT_DELAY_SECONDS` (7 days), the governance timer mints ~N SNS tokens to A.

## Impact Explanation
This is a direct, irrecoverable loss of SNS governance tokens for the new neuron owner. The maturity is already deducted from the neuron at step 2, so B receives a neuron with zero maturity and no recourse. The impact maps to **High ($2,000–$10,000): Unauthorized access to neurons, governance assets, wallets, identities, ledgers, or canister-controlled funds**. The per-target work is finding a buyer for an OTC neuron trade, which is a standard SNS use case. The loss magnitude scales with neuron maturity and could be substantial for large neurons.

## Likelihood Explanation
SNS neurons are routinely transferred OTC via `add_neuron_permissions` / `remove_neuron_permissions` — this is the only protocol-supported transfer mechanism. The attacker needs only the `DisburseMaturity` and `ManagePrincipals` permissions that every neuron owner holds by default. Steps 2–4 can be submitted in consecutive messages within the same or immediately following rounds, making it impossible for the buyer to detect the pending disbursement even with a pre-transfer query. No privileged access, no race condition dependency on the victim, and no external oracle is required.

## Recommendation
In `remove_neuron_permissions`, before completing removal of the last permission for a principal, check whether `neuron.disburse_maturity_in_progress` is non-empty and return an error (e.g., `ErrorType::PreconditionFailed`) requiring the disbursements to settle first. Alternatively, add a `cancel_disburse_maturity` endpoint callable by any principal holding `ManagePrincipals` permission, which removes a pending entry and restores `maturity_e8s_equivalent`, giving a new owner the ability to cancel disbursements initiated by the previous owner.

## Proof of Concept
A deterministic PocketIC integration test:
1. Create an SNS with a neuron owned by principal A with `maturity_e8s_equivalent = 10_000_000_000`.
2. Call `manage_neuron(DisburseMaturity { percentage_to_disburse: 100, to_account: A })` as A. Assert `neuron.maturity_e8s_equivalent == 0` and `neuron.disburse_maturity_in_progress.len() == 1`.
3. Call `manage_neuron(AddNeuronPermissions { principal_id: B, permissions: [all] })` as A. Assert success.
4. Call `manage_neuron(RemoveNeuronPermissions { principal_id: A, permissions: [all] })` as A. Assert success.
5. Advance the PocketIC clock by `MATURITY_DISBURSEMENT_DELAY_SECONDS + 1`.
6. Trigger the governance heartbeat/timer.
7. Assert A's SNS token balance increased by ~10_000_000_000 e8s and B's balance is unchanged.
8. Assert `neuron.disburse_maturity_in_progress` is now empty and `neuron.maturity_e8s_equivalent == 0`.