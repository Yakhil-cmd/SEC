The code ordering is confirmed. Let me verify the exact error message content and whether `maturity_e8s_equivalent` is exposed anywhere publicly.

### Title
Authorization Check Ordered After State-Dependent Check Leaks Private Neuron Maturity to Unauthorized Callers — (`rs/nns/governance/src/governance/disburse_maturity.rs`)

---

### Summary

In `initiate_maturity_disbursement`, the `DisbursementTooSmall` guard executes at line 293 **before** the `CallerIsNotNeuronController` guard at line 303. An unprivileged ingress caller can probe any neuron's private `maturity_e8s_equivalent` by observing which error is returned, and the error message itself embeds the exact computed disbursement amount.

---

### Finding Description

The check ordering in `initiate_maturity_disbursement` is:

1. `InvalidPercentage` (line 261) — input validation, fine before auth
2. `InvalidDestination` (line 265) — input validation, fine before auth
3. Neuron state read (lines 271–289) — reads `maturity_e8s_equivalent`, `is_neuron_controlled_by_caller`, etc.
4. **`DisbursementTooSmall` (line 293)** — uses private neuron state, **before auth**
5. `NeuronSpawning` (line 300) — also before auth
6. **`CallerIsNotNeuronController` (line 303)** — auth check, too late [1](#0-0) 

The `DisbursementTooSmall` variant carries `disbursement_maturity_e8s` directly in its error message:

```
"Disbursement ({disbursement_maturity_e8s}) is too small. The amount should be at least: {minimum_disbursement_e8s} e8s"
``` [2](#0-1) 

`disbursement_maturity_e8s` is computed as `maturity_e8s_equivalent * percentage / 100`. With `percentage=1`, this equals `maturity / 100`. The error fires only when this value is below `MINIMUM_DISBURSEMENT_E8S` (= `E8` = 100,000,000 e8s = 1 ICP). [3](#0-2) 

---

### Impact Explanation

`maturity_e8s_equivalent` is **not** included in the public `NeuronInfo` struct returned by `get_neuron_info` (which is callable by anyone). The public struct exposes `stake_e8s`, `staked_maturity_e8s_equivalent` (only for controllers/hotkeys or public neurons), but **not** liquid maturity. [4](#0-3) [5](#0-4) 

An unprivileged caller submitting `manage_neuron → DisburseMaturity(percentage=1)` on a target neuron with maturity < 10,000,000,000 e8s (100 ICP) receives `DisbursementTooSmall` with the exact `disbursement_maturity_e8s` embedded in the error string, from which they can recover `maturity ≈ disbursement_maturity_e8s × 100`. For neurons with maturity ≥ 100 ICP, the caller receives `CallerIsNotNeuronController`, leaking the binary threshold crossing. By varying `percentage` from 1–100, the attacker can narrow the maturity range further.

Additionally, the `NeuronSpawning` check at line 300 also precedes the auth check, leaking spawning state to unauthorized callers. [6](#0-5) 

---

### Likelihood Explanation

The attack requires only a valid ingress call to the NNS governance canister's `manage_neuron` endpoint with a `DisburseMaturity` command — no special privileges, no keys, no social engineering. The neuron ID is the only required input. The call is update (not query), so it goes through consensus, but the error response is deterministic and observable by the caller. This is trivially executable against any neuron on mainnet.

---

### Recommendation

Move the `CallerIsNotNeuronController` check (and `NeuronSpawning` check) to execute **before** any check that reads or computes from private neuron state. The corrected order should be:

1. Input validation (`InvalidPercentage`, `InvalidDestination`)
2. Neuron existence check
3. **Authorization: `CallerIsNotNeuronController`**
4. **State guard: `NeuronSpawning`**
5. Size check: `DisbursementTooSmall`
6. Capacity check: `TooManyDisbursements`

---

### Proof of Concept

```rust
// Neuron N has maturity_e8s_equivalent = 9_900_000_000 (99 ICP), controller = Alice

// Attacker (Bob, non-controller) calls:
let result = initiate_maturity_disbursement(
    &mut neuron_store,
    &bob,           // non-controller
    &neuron_id_N,
    &DisburseMaturity { percentage_to_disburse: 1, to_account: None, to_account_identifier: None },
    now_seconds,
);

// Bob receives:
// Err(DisbursementTooSmall { disbursement_maturity_e8s: 99_000_000, minimum_disbursement_e8s: 100_000_000 })
// Bob now knows: maturity ≈ 99_000_000 * 100 = 9_900_000_000 e8s = 99 ICP

// Now raise maturity to 10_100_000_000 (101 ICP):
// Bob calls again with percentage=1:
// Err(CallerIsNotNeuronController)
// Bob now knows: maturity crossed the 100 ICP threshold
```

This matches the existing test `test_initiate_maturity_disbursement_disbursement_too_small` which uses a controller — the same error is returned regardless of caller identity when maturity is below threshold. [7](#0-6)

### Citations

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L44-45)
```rust
/// behavior (which maturity disbursement is designed to replace).
pub const MINIMUM_DISBURSEMENT_E8S: u64 = E8;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L108-117)
```rust
            InitiateMaturityDisbursementError::DisbursementTooSmall {
                disbursement_maturity_e8s,
                minimum_disbursement_e8s,
            } => GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Disbursement ({disbursement_maturity_e8s}) is too small. The amount \
                    should be at least: {minimum_disbursement_e8s} e8s",
                ),
            ),
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L291-305)
```rust
    let disbursement_maturity_e8s =
        percentage_of_maturity(maturity_e8s_equivalent, *percentage_to_disburse)?;
    if disbursement_maturity_e8s < MINIMUM_DISBURSEMENT_E8S {
        return Err(InitiateMaturityDisbursementError::DisbursementTooSmall {
            disbursement_maturity_e8s,
            minimum_disbursement_e8s: MINIMUM_DISBURSEMENT_E8S,
        });
    }

    if is_neuron_spawning {
        return Err(InitiateMaturityDisbursementError::NeuronSpawning);
    }
    if !is_neuron_controlled_by_caller {
        return Err(InitiateMaturityDisbursementError::CallerIsNotNeuronController);
    }
```

**File:** rs/nns/governance/api/src/types.rs (L70-135)
```rust
pub struct NeuronInfo {
    /// The unique identifier of the neuron.
    pub id: Option<NeuronId>,
    /// The exact time at which this data was computed. This means, for
    /// example, that the exact time that this neuron will enter the
    /// dissolved state, assuming it is currently dissolving, is given
    /// by `retrieved_at_timestamp_seconds+dissolve_delay_seconds`.
    pub retrieved_at_timestamp_seconds: u64,
    /// The current state of the neuron. See \[NeuronState\] for a
    /// description of the different states.
    pub state: i32,
    /// The current age of the neuron. See \[Neuron::age_seconds\]
    /// for details on how it is computed.
    pub age_seconds: u64,
    /// The current dissolve delay of the neuron. See
    /// \[Neuron::dissolve_delay_seconds\] for details on how it is
    /// computed.
    pub dissolve_delay_seconds: u64,
    /// See \[Neuron::recent_ballots\] for a description.
    pub recent_ballots: Vec<BallotInfo>,
    /// Current voting power of the neuron.
    pub voting_power: u64,
    /// When the Neuron was created. A neuron can only vote on proposals
    /// submitted after its creation date.
    pub created_timestamp_seconds: u64,
    /// Current stake of the neuron, in e8s.
    pub stake_e8s: u64,
    /// Timestamp when this neuron joined the community fund.
    pub joined_community_fund_timestamp_seconds: Option<u64>,
    /// If this neuron is a known neuron, this is data associated
    /// with it, including the neuron's name and (optionally) a description.
    pub known_neuron_data: Option<KnownNeuronData>,
    /// The type of the Neuron. See \[NeuronType\] for a description
    /// of the different states.
    pub neuron_type: Option<i32>,
    /// See the Visibility enum.
    pub visibility: Option<i32>,
    /// The last time that voting power was "refreshed". There are two ways to
    /// refresh the voting power of a neuron: set following, or vote directly. In
    /// the future, there will be a dedicated API for refreshing. Note that direct
    /// voting implies that refresh also occurs when a proposal is created, because
    /// direct voting is part of proposal creation.
    ///
    /// Effect: When this becomes > 6 months ago, the amount of voting power that
    /// this neuron can exercise decreases linearly down to 0 over the course of 1
    /// month. After that, following is cleared, except for ManageNeuron proposals.
    ///
    /// This will always be populated. If the underlying neuron was never
    /// refreshed, this will be set to 2024-11-05T00:00:01 UTC (1730764801 seconds
    /// after the UNIX epoch).
    pub voting_power_refreshed_timestamp_seconds: ::core::option::Option<u64>,
    /// See analogous field in Neuron.
    pub deciding_voting_power: Option<u64>,
    /// See analogous field in Neuron.
    pub potential_voting_power: Option<u64>,

    /// Base value (in e8s) used for the "8-year gang" dissolve delay bonus.
    /// For neurons that had the maximum dissolve delay of 8 years before the
    /// maximum dissolve delay was reduced, this is set to the total staked value
    /// net of fees (including staked maturity) captured at the time of migration.
    /// For all other neurons, this is 0.
    pub eight_year_gang_bonus_base_e8s: Option<u64>,

    /// See analogous field in Neuron.
    pub staked_maturity_e8s_equivalent: Option<u64>,
}
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

**File:** rs/nns/governance/src/governance/disburse_maturity_tests.rs (L441-466)
```rust
#[test]
fn test_initiate_maturity_disbursement_disbursement_too_small() {
    let mut neuron_store = NeuronStore::new(BTreeMap::new());
    let neuron = create_neuron_builder()
        .with_maturity_e8s_equivalent(9_900_000_000)
        .build();
    neuron_store.add_neuron(neuron).unwrap();

    assert_eq!(
        initiate_maturity_disbursement(
            &mut neuron_store,
            &CONTROLLER,
            &NeuronId { id: 1 },
            &DisburseMaturity {
                percentage_to_disburse: 1,
                to_account: None,
                to_account_identifier: None,
            },
            NOW_SECONDS,
        ),
        Err(InitiateMaturityDisbursementError::DisbursementTooSmall {
            disbursement_maturity_e8s: 99_000_000,
            minimum_disbursement_e8s: 100_000_000,
        })
    );
}
```
