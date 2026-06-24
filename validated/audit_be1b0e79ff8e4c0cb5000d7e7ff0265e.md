Audit Report

## Title
Any Caller Can Permanently Lock ICP in a Non-KYC-Verified Neuron via Unauthenticated `ClaimOrRefresh` - (File: rs/nns/governance/src/governance.rs)

## Summary

`refresh_neuron` accepts no `caller` parameter and performs no ownership or KYC check before updating `cached_neuron_stake_e8s`. Because `disburse_neuron` and `disburse_to_neuron` both gate disbursement on `kyc_verified == true`, any unprivileged ingress caller can send ICP to a non-KYC-verified genesis neuron's subaccount and call `ClaimOrRefresh` to permanently lock those funds — the neuron owner cannot disburse them, and no other recovery path exists.

## Finding Description

`manage_neuron_internal` handles `ClaimOrRefresh` before any neuron-existence or authorization checks and returns early, bypassing all downstream guards (lines 6104–6148). Both `By::MemoAndController` and `By::NeuronIdOrSubaccount` variants route unconditionally to `refresh_neuron` (lines 5852–5896), which takes no `caller` argument and contains no ownership check and no `kyc_verified` check (lines 5900–5962). It reads the ledger balance and, if higher than the cached stake, calls `neuron.update_stake_adjust_age(balance.get_e8s(), now)` — permanently recording the inflated stake. In contrast, `disburse_neuron` (lines 1990–1995) and `disburse_to_neuron` (lines 2949–2954) both hard-reject with `PreconditionFailed` when `kyc_verified == false`. The test `test_refresh_neuron_by_memo_by_proxy` (lines 4922–4928) explicitly validates that a caller different from the neuron owner can successfully refresh and double the stake, confirming this is reachable by any principal. New neurons are created with `kyc_verified = true` (lines 6010–6012), so only genesis neurons initialized with `kyc_verified = false` are affected.

## Impact Explanation

An attacker transfers ICP to the target neuron's subaccount and calls `ClaimOrRefresh`. `refresh_neuron` updates `cached_neuron_stake_e8s` to the new balance. The neuron owner — already unable to disburse due to `kyc_verified = false` — now has additional ICP permanently locked in the neuron with no disbursement path and no protocol-level recovery mechanism. The attacker's ICP is destroyed; the victim's neuron accumulates irrecoverable funds. This constitutes a permanent, irreversible loss of ICP assets caused by a missing authorization/KYC gate in production NNS Governance code. This matches the **High** impact category: significant NNS governance/ledger security impact with concrete user funds harm, where exploitation requires per-target work (identifying a non-KYC-verified neuron and funding the attack).

## Likelihood Explanation

A neuron's subaccount is deterministically derivable from the controller principal and memo, both of which are observable from on-chain ledger history. The `ClaimOrRefresh` command requires no special privilege — any ingress sender can call `manage_neuron`. The only cost is the ICP the attacker sends to the subaccount. The affected population is genesis neurons with `kyc_verified = false`; these are a finite, identifiable set. The attack is repeatable against any such neuron.

## Recommendation

**Option 1 (preferred):** Pass `caller` into `refresh_neuron` and reject calls where `caller` is not the neuron's controller, consistent with the ownership enforcement already present in `disburse_neuron` and `disburse_to_neuron`.

**Option 2:** Add a `kyc_verified` check inside `refresh_neuron` before updating `cached_neuron_stake_e8s`. If the neuron is not KYC-verified, reject the stake increase with `PreconditionFailed`, mirroring the gate on all fund-movement operations.

Option 1 is the stronger fix and aligns with the principle of least privilege already applied to all other sensitive neuron operations.

## Proof of Concept

The existing test `test_refresh_neuron_by_memo_by_proxy` (lines 4922–4928 of `rs/nns/governance/tests/governance.rs`) already proves the proxy-refresh path succeeds. A targeted PoC extends this:

1. Initialize a neuron with `kyc_verified = false` (as genesis neurons are).
2. As a different principal (attacker), add funds to the neuron's subaccount via `driver.add_funds_to_account`.
3. Call `gov.manage_neuron(&attacker, &ManageNeuron { command: Some(Command::ClaimOrRefresh(...By::NeuronIdOrSubaccount...)) })`.
4. Assert `cached_neuron_stake_e8s` increased — confirming the stake was updated without any KYC or ownership check.
5. Attempt `disburse_neuron` as the neuron owner — assert it fails with `PreconditionFailed: Neuron is not kyc verified`.
6. Confirm no other code path exists to recover the funds from the neuron subaccount.