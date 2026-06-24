Audit Report

## Title
Permissionless `ClaimOrRefresh` via `NeuronIdOrSubaccount` Allows Any Caller to Permanently Dilute a Neuron's Age Bonus - (File: `rs/nns/governance/src/governance.rs`)

## Summary
The NNS governance `manage_neuron_internal` function routes `ClaimOrRefresh { by: NeuronIdOrSubaccount }` directly to `refresh_neuron_by_id_or_subaccount` without any caller authorization check. An attacker can transfer ICP to a victim's neuron subaccount and then invoke this path to trigger `update_stake_adjust_age`, permanently diluting the victim's earned age bonus and reducing their voting power and reward share in all future NNS reward periods.

## Finding Description
In `rs/nns/governance/src/governance.rs`, `manage_neuron_internal` handles `ClaimOrRefresh` before any neuron ownership check and returns early:

```rust
Some(By::NeuronIdOrSubaccount(_)) => {
    let id = mgmt.get_neuron_id_or_subaccount()?.ok_or_else(|| { ... })?;
    self.refresh_neuron_by_id_or_subaccount(id, claim_or_refresh)
        .await
        .map(ManageNeuronResponse::claim_or_refresh_neuron_response)
}
```

`refresh_neuron_by_id_or_subaccount` (L5875–5896) performs no caller identity check — it only resolves the neuron ID/subaccount and calls `refresh_neuron`. Inside `refresh_neuron` (L5936–5958), when the ledger balance exceeds the cached stake, `update_stake_adjust_age` is called unconditionally:

```rust
Ordering::Less => {
    neuron.update_stake_adjust_age(balance.get_e8s(), now);
}
```

`update_stake_adjust_age` (L999–1039 in `rs/nns/governance/src/neuron/types.rs`) computes a weighted-average age: new stake is treated as having age 0, so the effective age is diluted proportionally to the ratio of old stake to new total stake. For a non-dissolving neuron, `aging_since_timestamp_seconds` is advanced forward, permanently reducing the age bonus.

The test helper `refresh_neuron_by_id_or_subaccount` in `rs/nns/governance/tests/governance.rs` (L4936–4997) explicitly demonstrates a different `caller` successfully refreshing `owner`'s neuron via `By::NeuronIdOrSubaccount`, confirming the permissionless path is exercised and succeeds.

The exploit path is:
1. Attacker reads victim's neuron subaccount (public via `list_neurons`).
2. Attacker transfers X ICP to that subaccount via the ICP ledger.
3. Attacker calls `manage_neuron` with `ClaimOrRefresh { by: NeuronIdOrSubaccount({}) }` targeting the victim's neuron ID.
4. `refresh_neuron` queries the ledger, finds balance > cached stake, calls `update_stake_adjust_age(new_balance, now)`.
5. Victim's `aging_since_timestamp_seconds` is advanced, permanently reducing effective age.

No existing guard prevents this: the `ClaimOrRefresh` branch returns before the standard neuron ownership check at L6150, and neither `refresh_neuron_by_id_or_subaccount` nor `refresh_neuron` checks the caller against the neuron's controller or hotkeys.

## Impact Explanation
A victim neuron that has accumulated years of age bonus (up to 25% additional voting power for neurons aged ≥ 4 years) can have that bonus permanently diluted. Because NNS voting rewards are distributed proportionally to `deciding_voting_power`, diluting the age bonus reduces the victim's reward share in every future reward period for the remaining life of the neuron. This constitutes unauthorized modification of NNS governance parameters with concrete, irreversible harm to the victim's governance influence and reward income. This matches the **Medium** impact tier: a meaningful NNS security impact requiring attacker cost and targeting specific conditions (small, highly-aged neurons), with concrete user harm.

## Likelihood Explanation
The attacker must spend real ICP equal to the victim's current stake to halve the age bonus; that ICP is credited to the victim's neuron (not destroyed). The cost-to-harm ratio is unfavorable for large neurons but feasible for small, highly-aged neurons (e.g., a neuron with 1 ICP staked and 4 years of age can have its age halved for a cost of 1 ICP). The attack requires no privileged access, no victim cooperation, and is fully permissionless. It is repeatable: the attacker can continue sending small amounts to progressively erode the age further. Likelihood is **low to medium** given the economic cost, but the attack is technically trivial and requires only knowledge of the victim's neuron ID (public).

## Recommendation
Add an authorization check in `refresh_neuron_by_id_or_subaccount` (or at the top of `refresh_neuron`) requiring the caller to be the neuron's controller or a registered hotkey before calling `update_stake_adjust_age`. Alternatively, decouple the age-adjustment logic from permissionless stake refreshes: allow any caller to trigger a balance sync (updating `cached_neuron_stake_e8s`), but only update `aging_since_timestamp_seconds` when the refresh is initiated by an authorized principal (controller or hotkey).

## Proof of Concept
The existing test infrastructure in `rs/nns/governance/tests/governance.rs` already demonstrates the permissionless path. A minimal reproducing test:

1. Create a neuron owned by `owner` with 10 ICP staked and advance time by 4 years to accumulate maximum age bonus.
2. Record `aging_since_timestamp_seconds` (should be ~4 years in the past).
3. As `attacker` (a different principal), add 10 ICP to the neuron's subaccount via `driver.add_funds_to_account`.
4. As `attacker`, call `gov.manage_neuron(&attacker, &ManageNeuron { neuron_id_or_subaccount: Some(NeuronIdOrSubaccount::NeuronId(nid)), command: Some(Command::ClaimOrRefresh(ClaimOrRefresh { by: Some(By::NeuronIdOrSubaccount(Empty {})) })) })`.
5. Assert that `neuron.cached_neuron_stake_e8s == 20 ICP` and that `aging_since_timestamp_seconds` has advanced by ~2 years (age halved), confirming the dilution.

This follows the same pattern as the existing `refresh_neuron_by_id_or_subaccount` test helper (L4936–4997) which already passes with a non-owner caller.