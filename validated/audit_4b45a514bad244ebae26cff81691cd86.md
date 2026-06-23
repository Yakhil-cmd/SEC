### Title
`LinearMap::new` Panics When `clear_following_after_seconds = 0` Is Set via Governance, Locking NNS Voting Power Computation - (`rs/nns/governance/src/network_economics.rs`)

---

### Summary

`VotingPowerEconomics.validate()` explicitly permits `clear_following_after_seconds = 0`, but the downstream computation in `deciding_voting_power_adjustment_factor_function()` constructs a `LinearMap` whose `from` range has equal start and end, triggering an unconditional `assert!` panic in `LinearMap::new`. After a `ManageNetworkEconomics` proposal sets this field to zero, every NNS governance call that computes deciding voting power panics, effectively locking proposal creation, voting, and all related operations.

---

### Finding Description

`VotingPowerEconomics.validate()` explicitly states that fields **are allowed to be set to 0**: [1](#0-0) 

The `ManageNetworkEconomics` proposal action can therefore set `clear_following_after_seconds = Some(0)` and pass validation without error.

After that proposal executes, `deciding_voting_power_adjustment_factor_function()` is called on every voting power computation: [2](#0-1) 

When `clear_following_after_seconds = 0`, `get_clear_following_after_seconds()` returns `0`, so:

```
end = begin.saturating_add(0) = begin
from_range = begin..begin   // zero-length range
```

`LinearMap::new` enforces a hard `assert!` that the range is non-zero: [3](#0-2) 

This `assert!` fires unconditionally, panicking the canister message. The panic propagates through `deciding_voting_power_adjustment_factor()`: [4](#0-3) 

…into `potential_and_deciding_voting_power()`: [5](#0-4) 

…which is called by every proposal creation, vote cast, and ballot computation in the NNS governance canister. [6](#0-5) 

---

### Impact Explanation

After a `ManageNetworkEconomics` proposal sets `clear_following_after_seconds = 0`, **every** NNS governance ingress call that touches voting power computation traps. This includes:

- `make_proposal` (ballots are computed at proposal creation)
- `register_vote`
- Any query or update that calls `deciding_voting_power` or `potential_and_deciding_voting_power`

The NNS governance canister is effectively locked for all governance operations until a recovery proposal (setting `clear_following_after_seconds` back to a non-zero value) can be passed — but passing a proposal itself requires voting power computation, creating a deadlock. Recovery would require a subnet upgrade or hotfix.

This is directly analogous to the Fluid DEX bug: a flag/parameter that is permitted to be zero by the validation layer causes a division-by-zero / panic in the computation layer, locking the system. [1](#0-0) 

---

### Likelihood Explanation

A `ManageNetworkEconomics` proposal is a standard NNS governance action reachable by any neuron with sufficient dissolve delay. The bug is triggered by a **legitimate, non-malicious** governance configuration change — e.g., an attempt to "disable" the voting power decay feature by setting the decay window to zero. The `validate()` function explicitly documents that zero is allowed, making this a plausible governance mistake. Likelihood is **medium**: it requires a governance proposal to pass, but the validation explicitly invites the dangerous value.

---

### Recommendation

`VotingPowerEconomics.validate()` must reject `clear_following_after_seconds = 0`. The comment "They are allowed to be set to 0 though" is incorrect with respect to `clear_following_after_seconds` because a zero value makes `begin == end` in the `LinearMap` range, triggering the assertion. Add a lower-bound check:

```rust
if let Some(0) = self.clear_following_after_seconds {
    defects.push(
        "clear_following_after_seconds must be greater than zero.".to_string()
    );
}
```

Alternatively, `deciding_voting_power_adjustment_factor_function()` should guard against the zero case and return a degenerate map or a constant factor of 0 instead of constructing a `LinearMap` with an empty range.

---

### Proof of Concept

1. Submit a `ManageNetworkEconomics` NNS proposal with `clear_following_after_seconds = Some(0)`.
2. The proposal passes `VotingPowerEconomics.validate()` without error (the comment explicitly says 0 is allowed).
3. After the proposal executes, call any NNS governance method that creates a proposal or casts a vote.
4. `deciding_voting_power_adjustment_factor_function()` constructs `LinearMap::new(begin..begin, 1..0)`.
5. `assert!(from.end != from.start)` fires → canister traps.
6. All subsequent governance operations that compute voting power trap identically, locking the NNS governance canister. [7](#0-6) [2](#0-1)

### Citations

**File:** rs/nns/governance/src/network_economics.rs (L315-322)
```rust
    pub fn deciding_voting_power_adjustment_factor(
        &self,
        time_since_last_voting_power_refreshed: Duration,
    ) -> Decimal {
        self.deciding_voting_power_adjustment_factor_function()
            .apply(time_since_last_voting_power_refreshed.as_secs())
            .clamp(Decimal::from(0), Decimal::from(1))
    }
```

**File:** rs/nns/governance/src/network_economics.rs (L324-336)
```rust
    fn deciding_voting_power_adjustment_factor_function(&self) -> LinearMap {
        let from_range = {
            let begin = self.get_start_reducing_voting_power_after_seconds();
            let end = begin.saturating_add(self.get_clear_following_after_seconds());

            begin..end
        };

        #[allow(clippy::reversed_empty_ranges)]
        let to_range = 1..0;

        LinearMap::new(from_range, to_range)
    }
```

**File:** rs/nns/governance/src/network_economics.rs (L348-352)
```rust
    /// This just validates that all fields are set.
    ///
    /// They are allowed to be set to 0 though.
    ///
    /// In practice, we would never see None in any fields, because
```

**File:** rs/nervous_system/linear_map/src/lib.rs (L26-38)
```rust
impl LinearMap {
    /// The ends of from must be different.
    pub fn new<N1, N2>(from: Range<N1>, to: Range<N2>) -> Self
    where
        Decimal: From<N1> + From<N2>,
    {
        let from = Decimal::from(from.start)..Decimal::from(from.end);
        let to = Decimal::from(to.start)..Decimal::from(to.end);

        // from must have nonzero length.
        assert!(from.end != from.start, "{from:#?}");
        Self { from, to }
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L371-378)
```rust
    pub fn potential_and_deciding_voting_power(
        &self,
        voting_power_economics: &VotingPowerEconomics,
        now_seconds: u64,
    ) -> (u64, u64) {
        let stake_e8s = self.stake_e8s();
        let boost = dissolve_delay_bonus_multiplier(self.dissolve_delay_seconds(now_seconds))
            * age_bonus_multiplier(self.age_seconds(now_seconds));
```

**File:** rs/nns/governance/src/neuron/types.rs (L390-397)
```rust
        let adjustment_factor: Decimal = {
            let time_since_last_refreshed = Duration::from_secs(
                now_seconds.saturating_sub(self.voting_power_refreshed_timestamp_seconds),
            );

            voting_power_economics
                .deciding_voting_power_adjustment_factor(time_since_last_refreshed)
        };
```
