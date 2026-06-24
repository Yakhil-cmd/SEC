Audit Report

## Title
Permissionless NNS Neuron Stake Refresh Allows Any Caller to Dilute Victim Neuron's Age Bonus - (`rs/nns/governance/src/governance.rs`)

## Summary
The NNS Governance canister's `refresh_neuron_by_id_or_subaccount` and `refresh_neuron` functions perform no authorization check on the caller, allowing any principal to trigger a stake refresh on any neuron. Because the ICP ledger permits anyone to transfer ICP to a neuron's governance subaccount, an attacker can force `update_stake_adjust_age` to execute on a victim neuron, permanently diluting its age and reducing its age-bonus voting power without the owner's consent.

## Finding Description
`refresh_neuron_by_id_or_subaccount` (L5875–5896) accepts no `caller` parameter and performs no ownership or hotkey check before delegating to `refresh_neuron` (L5900–5962). Inside `refresh_neuron`, the only guard is a minimum-balance check (L5924–5935); there is no check that the ingress sender is the neuron's controller or a registered hotkey. When the ledger balance exceeds the cached stake (`Ordering::Less` branch, L5950–5951), `update_stake_adjust_age` is called unconditionally.

`update_stake_adjust_age` (L999–1040 in `rs/nns/governance/src/neuron/types.rs`) computes a weighted-average age via `combine_aged_stakes`, treating the newly detected ICP as having age 0. This permanently reduces `aging_since_timestamp_seconds`, diluting the neuron's age bonus.

The exploit path:
1. Attacker computes the victim's neuron subaccount (deterministic, public).
2. Attacker calls `icrc1_transfer` on the ICP ledger, sending any amount ≥ 1 e8s to `AccountIdentifier::new(GOVERNANCE_CANISTER_ID, Some(victim_subaccount))`.
3. Attacker calls `manage_neuron` with `ClaimOrRefresh { by: Some(By::NeuronIdOrSubaccount(Empty {})) }` targeting the victim's neuron ID.
4. `refresh_neuron` detects the balance increase and calls `update_stake_adjust_age`, diluting the victim's age.
5. Steps 2–4 are repeatable, asymptotically driving the age toward zero.

The test suite at L5014 explicitly documents this as intentional: *"Tests that a neuron can be refreshed by subaccount, and that anyone can do it."* However, the `test_refresh_neuron_by_subaccount_by_proxy` test (L5025–5029) mistakenly sets `caller == owner` (both `TEST_NEURON_1_OWNER_PRINCIPAL`), so it does not actually exercise the third-party attacker path despite the comment's claim.

## Impact Explanation
The NNS age bonus contributes up to 25% additional voting power for neurons aged ≥ 4 years. Voting power directly determines the share of NNS voting rewards. An attacker who repeatedly sends small ICP amounts and triggers refreshes continuously erodes the victim's age bonus. The age dilution is irreversible without the victim staking additional ICP and waiting years for the age to recover. This constitutes unauthorized manipulation of a governance asset (a neuron's age and voting power) with a direct, lasting financial impact on the victim's reward stream. This matches the High impact class: *"Unauthorized access to neurons, governance assets, wallets, identities, ledgers, or canister-controlled funds where exploitation requires meaningful per-target work or other constraints."*

## Likelihood Explanation
The attack is reachable by any unprivileged ingress sender with ICP. Neuron subaccounts are deterministic and publicly derivable from on-chain state. The attacker's ICP is locked into the victim's neuron (not returned), making this a griefing attack with a direct cost to the attacker. For high-value neurons with large stakes, a meaningful age dilution requires proportionally more ICP, keeping the practical likelihood medium for large neurons and higher for smaller ones. The attack is repeatable with no protocol-level rate limit.

## Recommendation
Add an authorization check in `refresh_neuron` (or at the `refresh_neuron_by_id_or_subaccount` call site) requiring the caller to be the neuron's controller or a registered hotkey before allowing the stake refresh to proceed when invoked via `By::NeuronIdOrSubaccount`. The `By::MemoAndController` path can remain open since knowledge of the memo and controller is effectively self-service. Alternatively, restrict `By::NeuronIdOrSubaccount` refreshes to authorized callers only, preserving the permissionless discovery use case (neuron ID lookup) while blocking the age-dilution attack vector.

## Proof of Concept
Minimal unit test plan (extending the existing test harness in `rs/nns/governance/tests/governance.rs`):

1. Create a neuron owned by `ALICE` with 1,000 ICP staked and advance time by 4 years so `age_seconds == 4 * ONE_YEAR_SECONDS`.
2. Record `alice_neuron.aging_since_timestamp_seconds` (baseline).
3. As `BOB` (a completely different principal, e.g., `TEST_NEURON_2_OWNER_PRINCIPAL`), call `driver.add_funds_to_account(alice_subaccount, 10 ICP)` to simulate a ledger transfer.
4. As `BOB`, call `gov.manage_neuron(&BOB, ManageNeuron { neuron_id_or_subaccount: Some(NeuronIdOrSubaccount::NeuronId(alice_nid)), command: Some(Command::ClaimOrRefresh(ClaimOrRefresh { by: Some(By::NeuronIdOrSubaccount(Empty {})) })) })`.
5. Assert the call succeeds (no authorization error).
6. Assert `alice_neuron.aging_since_timestamp_seconds > baseline` (age has been diluted forward in time, reducing effective age).
7. Assert `alice_neuron.cached_neuron_stake_e8s == 1_010 ICP` (stake increased).
8. Repeat steps 3–6 ten times and assert the age continues to decrease monotonically toward zero.