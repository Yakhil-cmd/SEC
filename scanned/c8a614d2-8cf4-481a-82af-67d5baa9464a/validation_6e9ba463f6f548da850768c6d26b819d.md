### Title
`NeuronInfo.stake_e8s` Understates Neuron Value by Omitting Staked Maturity - (`File: rs/nns/governance/src/neuron/types.rs`)

---

### Summary

The `get_neuron_info` method in the NNS Governance canister populates the `NeuronInfo.stake_e8s` field using `minted_stake_e8s()`, which excludes `staked_maturity_e8s_equivalent`. This contradicts the Candid interface documentation, which explicitly states the field should equal `cached_neuron_stake_e8s - neuron_fees_e8s + staked_maturity_e8s_equivalent`. Any neuron with staked maturity will have its total locked value understated in the publicly queryable `get_neuron_info` response.

---

### Finding Description

In `rs/nns/governance/src/neuron/types.rs`, the `get_neuron_info` function constructs a `NeuronInfo` struct:

```rust
NeuronInfo {
    ...
    stake_e8s: self.minted_stake_e8s(),   // line 950 — WRONG
    ...
    staked_maturity_e8s_equivalent: self.staked_maturity_e8s_equivalent,
}
``` [1](#0-0) 

`minted_stake_e8s()` is defined as only `cached_neuron_stake_e8s - neuron_fees_e8s`, explicitly documented as **not counting staked maturity**:

```rust
pub fn minted_stake_e8s(&self) -> u64 {
    self.cached_neuron_stake_e8s
        .saturating_sub(self.neuron_fees_e8s)
}
``` [2](#0-1) 

By contrast, `stake_e8s()` — the correct full-value function — includes staked maturity:

```rust
fn neuron_stake_e8s(...) -> u64 {
    cached_neuron_stake_e8s
        .saturating_sub(neuron_fees_e8s)
        .saturating_add(staked_maturity_e8s_equivalent.unwrap_or(0))
}
``` [3](#0-2) 

The Candid interface documentation for `NeuronInfo.stake_e8s` explicitly states the correct formula:

```
// The amount of ICP (and staked maturity) locked in this neuron.
// cached_neuron_stake_e8s - neuron_fees_e8s + staked_maturity_e8s_equivalent
stake_e8s : nat64;
``` [4](#0-3) 

The voting power calculation itself correctly uses `stake_e8s()` (which includes staked maturity), so voting power is computed correctly — only the reported `stake_e8s` value in `NeuronInfo` is wrong. [5](#0-4) 

The Rosetta API propagates this incorrect value directly to external consumers:

```rust
Ok(NeuronInfoResponse {
    ...
    stake_e8s: res.stake_e8s,  // carries the understated value
})
``` [6](#0-5) 

---

### Impact Explanation

Any caller of the public `get_neuron_info` or `get_neuron_info_by_id_or_subaccount` query endpoints receives a `NeuronInfo.stake_e8s` that is lower than the true locked value for neurons with non-zero `staked_maturity_e8s_equivalent`. The magnitude of the understatement equals the neuron's entire staked maturity balance. Downstream systems (dashboards, Rosetta API clients, analytics tools, and any protocol logic relying on `NeuronInfo.stake_e8s`) will systematically undercount the true economic value locked in such neurons. This is an incorrect value reporting bug in a publicly accessible, unauthenticated query endpoint.

---

### Likelihood Explanation

The `staked_maturity_e8s_equivalent` feature is actively used: neurons with `auto_stake_maturity = true` accumulate staked maturity with every reward event, and users can explicitly call `stake_maturity` to move maturity into staked form. Any neuron that has ever staked maturity will trigger this discrepancy. The `get_neuron_info` endpoint is callable by any unprivileged principal with no authentication required, making this trivially reachable.

---

### Recommendation

In `get_neuron_info`, replace `self.minted_stake_e8s()` with `self.stake_e8s()` for the `stake_e8s` field of the returned `NeuronInfo`, so that the reported value matches the documented formula and includes `staked_maturity_e8s_equivalent`.

---

### Proof of Concept

1. Create a neuron with `cached_neuron_stake_e8s = 100`, `neuron_fees_e8s = 0`, `staked_maturity_e8s_equivalent = Some(50)`.
2. Call `get_neuron_info` on that neuron from any principal.
3. Observe `NeuronInfo.stake_e8s = 100` (from `minted_stake_e8s()`).
4. The documented and correct value should be `150` (`cached_neuron_stake_e8s - neuron_fees_e8s + staked_maturity_e8s_equivalent = 100 - 0 + 50`).
5. The `potential_voting_power` field in the same response will correctly reflect the full `150`-based stake, creating an observable internal inconsistency between `stake_e8s` and `potential_voting_power`. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/neuron/types.rs (L373-379)
```rust
        voting_power_economics: &VotingPowerEconomics,
        now_seconds: u64,
    ) -> (u64, u64) {
        let stake_e8s = self.stake_e8s();
        let boost = dissolve_delay_bonus_multiplier(self.dissolve_delay_seconds(now_seconds))
            * age_bonus_multiplier(self.age_seconds(now_seconds));
        let mut potential_voting_power = Decimal::from(stake_e8s) * boost;
```

**File:** rs/nns/governance/src/neuron/types.rs (L942-963)
```rust
        NeuronInfo {
            id: Some(self.id()),
            retrieved_at_timestamp_seconds: now_seconds,
            state: self.state(now_seconds) as i32,
            age_seconds: self.age_seconds(now_seconds),
            dissolve_delay_seconds: self.dissolve_delay_seconds(now_seconds),
            recent_ballots,
            created_timestamp_seconds: self.created_timestamp_seconds,
            stake_e8s: self.minted_stake_e8s(),
            joined_community_fund_timestamp_seconds,
            known_neuron_data,
            neuron_type: self.neuron_type,
            visibility,
            voting_power_refreshed_timestamp_seconds: Some(
                self.voting_power_refreshed_timestamp_seconds,
            ),
            deciding_voting_power: Some(deciding_voting_power),
            potential_voting_power: Some(potential_voting_power),
            voting_power: potential_voting_power,
            eight_year_gang_bonus_base_e8s: Some(self.eight_year_gang_bonus_base_e8s),
            staked_maturity_e8s_equivalent: self.staked_maturity_e8s_equivalent,
        }
```

**File:** rs/nns/governance/src/neuron/types.rs (L973-979)
```rust
    pub fn stake_e8s(&self) -> u64 {
        neuron_stake_e8s(
            self.cached_neuron_stake_e8s,
            self.neuron_fees_e8s,
            self.staked_maturity_e8s_equivalent,
        )
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L981-986)
```rust
    /// Returns the current `minted` stake of the neuron, i.e. the ICP backing the
    /// neuron, minus the fees. This does not count staked maturity.
    pub fn minted_stake_e8s(&self) -> u64 {
        self.cached_neuron_stake_e8s
            .saturating_sub(self.neuron_fees_e8s)
    }
```

**File:** rs/nns/governance/src/neuron/mod.rs (L10-18)
```rust
fn neuron_stake_e8s(
    cached_neuron_stake_e8s: u64,
    neuron_fees_e8s: u64,
    staked_maturity_e8s_equivalent: Option<u64>,
) -> u64 {
    cached_neuron_stake_e8s
        .saturating_sub(neuron_fees_e8s)
        .saturating_add(staked_maturity_e8s_equivalent.unwrap_or(0))
}
```

**File:** rs/nns/governance/canister/governance.did (L897-902)
```text
  // The amount of ICP (and staked maturity) locked in this neuron.
  //
  // This is the foundation of the neuron's voting power.
  //
  // cached_neuron_stake_e8s - neuron_fees_e8s + staked_maturity_e8s_equivalent
  stake_e8s : nat64;
```

**File:** rs/rosetta-api/icp/src/request_handler.rs (L922-931)
```rust
        Ok(NeuronInfoResponse {
            verified_query: verified,
            retrieved_at_timestamp_seconds: res.retrieved_at_timestamp_seconds,
            state,
            age_seconds: res.age_seconds,
            dissolve_delay_seconds: res.dissolve_delay_seconds,
            voting_power: res.voting_power,
            created_timestamp_seconds: res.created_timestamp_seconds,
            stake_e8s: res.stake_e8s,
        })
```
