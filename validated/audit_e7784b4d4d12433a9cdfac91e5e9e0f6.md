Audit Report

## Title
Permissionless `ClaimOrRefresh` Enables Forced Neuron Age Dilution via Unsolicited Stake Top-Up - (File: `rs/nns/governance/src/governance.rs`)

## Summary
Any unprivileged caller can transfer ICP to a victim's neuron subaccount and then invoke `manage_neuron` with `ClaimOrRefresh { by: NeuronIdOrSubaccount }` without any authorization check. This triggers `refresh_neuron`, which unconditionally calls `update_stake_adjust_age` on the inflow, permanently diluting the neuron's `aging_since_timestamp_seconds` via a weighted average where the injected stake carries age 0. The result is an irreversible reduction in the victim's age bonus and proportional share of all future NNS voting rewards.

## Finding Description

In `manage_neuron_internal` (`rs/nns/governance/src/governance.rs`, lines 6104–6147), the `ClaimOrRefresh` command is dispatched **before** any authorization check. The `By::NeuronIdOrSubaccount` branch calls `refresh_neuron_by_id_or_subaccount` with no caller identity validation:

```rust
// Lines 6104-6147
if let Some(Command::ClaimOrRefresh(claim_or_refresh)) = &mgmt.command {
    return match &claim_or_refresh.by {
        Some(By::NeuronIdOrSubaccount(_)) => {
            self.refresh_neuron_by_id_or_subaccount(id, claim_or_refresh)
                .await
                ...
        }
    };
}
```

`refresh_neuron_by_id_or_subaccount` (lines 5873–5896) resolves the neuron and calls `refresh_neuron` with no authorization check. Inside `refresh_neuron` (lines 5936–5959), when the ledger balance exceeds the cached stake (`Ordering::Less`), `update_stake_adjust_age` is called unconditionally:

```rust
Ordering::Less => {
    neuron.update_stake_adjust_age(balance.get_e8s(), now);
}
```

`update_stake_adjust_age` (`rs/nns/governance/src/neuron/types.rs`, lines 999–1038) calls `combine_aged_stakes` where the injected delta carries age 0:

```rust
let (new_stake_e8s, new_age_seconds) = combine_aged_stakes(
    self.cached_neuron_stake_e8s,
    self.age_seconds(now),
    updated_stake_e8s.saturating_sub(self.cached_neuron_stake_e8s),
    0,  // injected stake has age 0
);
```

`combine_aged_stakes` (`rs/nns/governance/src/neuron/mod.rs`, lines 22–46) computes:

```
new_age = (old_stake * old_age + injected_stake * 0) / (old_stake + injected_stake)
        = old_stake * old_age / (old_stake + injected_stake)
```

This advances `aging_since_timestamp_seconds` forward, permanently reducing the neuron's age. The age bonus multiplier (`rs/nns/governance/src/neuron/voting_power.rs`, lines 23–31) is `0.25 * t + 1` where `t = age / MAX_NEURON_AGE_FOR_AGE_BONUS`, providing up to 25% bonus at maximum age. This feeds directly into `potential_and_deciding_voting_power` (`rs/nns/governance/src/neuron/types.rs`, lines 377–379):

```rust
let boost = dissolve_delay_bonus_multiplier(...) * age_bonus_multiplier(self.age_seconds(now_seconds));
let mut potential_voting_power = Decimal::from(stake_e8s) * boost;
```

The same structural vulnerability exists in SNS Governance (`rs/sns/governance/src/governance.rs`, lines 4274–4295), where `refresh_neuron` also calls `neuron.update_stake` unconditionally on any inflow with no authorization check.

There are no existing guards: the neuron lock only prevents concurrent operations on the same neuron; it does not restrict who may initiate the refresh. The `By::NeuronIdOrSubaccount` path has no caller-identity check anywhere in its call chain.

## Impact Explanation

This is a **High** severity finding matching the allowed impact: *"Significant NNS, SNS, or infrastructure security impact with concrete user or protocol harm."*

A neuron aged ≥ 4 years receives a 25% age bonus on voting power. Voting rewards are distributed proportionally to `deciding_voting_power`, which includes this bonus. By injecting an amount equal to the victim's current stake, the attacker halves the neuron's age, cutting the age bonus roughly in half. The dilution is permanent — the age cannot be restored without a governance-level migration (as evidenced by the historical `ResetAging`/`RestoreAging` audit events in `rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto`, lines 2617–2643). For high-value neurons with years of accumulated age, this represents a permanent, irreversible reduction in voting reward income for the lifetime of the neuron.

## Likelihood Explanation

The attack is fully permissionless and requires only two standard on-chain operations available to any principal:
1. An ICP ledger transfer to the victim's neuron subaccount (publicly derivable from the neuron's controller and memo, both visible in public neuron data).
2. A `manage_neuron` call with `ClaimOrRefresh { by: NeuronIdOrSubaccount }` targeting the victim's neuron ID.

There is no rate limit or cooldown on `ClaimOrRefresh`. The victim has no mechanism to refuse the top-up or prevent the subsequent refresh. The attacker's cost is the ICP injected (which accrues to the victim's stake, not the attacker), plus the ledger fee. For targeted attacks against known high-value neurons (e.g., named neurons with years of accumulated age), the cost is proportional to the desired dilution magnitude. The attack is repeatable.

## Recommendation

1. **Require authorization for `ClaimOrRefresh` on existing neurons**: When `By::NeuronIdOrSubaccount` targets an already-existing neuron, require the caller to be the neuron's controller or a registered hot key. New-neuron claiming (where the neuron does not yet exist) can remain permissionless.
2. **Separate claiming from refreshing**: Split `ClaimOrRefresh` into two distinct commands — `Claim` (new neuron, open) and `Refresh` (existing neuron, requires owner authorization).
3. **Alternatively, decouple age adjustment from permissionless refresh**: Only update `aging_since_timestamp_seconds` when the refresh is initiated by an authorized caller; permissionless refreshes update only `cached_neuron_stake_e8s` without touching the age.

## Proof of Concept

```rust
// Attacker knows victim's neuron ID = V and its subaccount = S
// (derivable from public neuron data)

// Step 1: Send ICP to victim's neuron subaccount
icp_ledger.transfer({
    to: governance_canister_subaccount(S),
    amount: victim_stake_e8s,  // inject equal to victim's stake to halve age
    fee: 10_000,
    memo: 0,
});

// Step 2: Trigger permissionless refresh — no authorization required
governance.manage_neuron({
    neuron_id_or_subaccount: NeuronId(V),
    command: ClaimOrRefresh { by: NeuronIdOrSubaccount {} }
});

// Result:
// refresh_neuron reads balance = old_stake + injected_stake
// update_stake_adjust_age called: new_age = old_stake * old_age / (old_stake + injected_stake)
// aging_since_timestamp_seconds advanced forward permanently
// Victim's age bonus and voting reward share permanently reduced
```

A deterministic integration test can be written using PocketIC: create a neuron, advance time to accumulate age, perform the two-step attack from a different principal, and assert that `aging_since_timestamp_seconds` has advanced forward (age decreased) without any authorized action by the neuron owner.