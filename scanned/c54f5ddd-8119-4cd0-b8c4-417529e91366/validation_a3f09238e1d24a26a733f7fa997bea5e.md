### Title
SNS Governance: `neuron_minimum_dissolve_delay_to_vote_seconds` Not Bounded Against Maximum Voting Period, Enabling Flash-Loan-Style Governance Attacks - (File: `rs/sns/governance/src/types.rs`)

### Summary
The SNS `NervousSystemParameters` validation does not enforce that `neuron_minimum_dissolve_delay_to_vote_seconds` is at least as large as the maximum possible proposal voting period (`initial_voting_period_seconds + 2 * wait_for_quiet_deadline_increase_seconds`). This allows an SNS to be configured — or governance-attacked into a configuration — where an attacker can borrow SNS tokens, stake them, vote on a critical proposal, and fully disburse the tokens before the proposal even closes, with zero long-term skin in the game.

### Finding Description

The SNS `NervousSystemParameters::validate_neuron_minimum_dissolve_delay_to_vote_seconds` function imposes only one constraint: the value must not exceed `max_dissolve_delay_seconds`. [1](#0-0) 

There is no floor check and no cross-validation against `initial_voting_period_seconds` or `wait_for_quiet_deadline_increase_seconds`. The value can legally be set to `0`. [2](#0-1) 

The `validate_initial_voting_period_seconds` and `validate_wait_for_quiet_deadline_increase_seconds` are validated independently with their own floor/ceiling bounds, but neither references `neuron_minimum_dissolve_delay_to_vote_seconds`. [3](#0-2) 

The maximum possible voting period for an SNS proposal is `initial_voting_period_seconds + 2 * wait_for_quiet_deadline_increase_seconds`, as documented in the proto and code. [4](#0-3) 

The NNS governance, by contrast, enforces a hard lower bound of 14 days on `neuron_minimum_dissolve_delay_to_vote_seconds`, which exceeds the NNS voting period. [5](#0-4) 

The NNS code even comments that this bound exists precisely to prevent dissolved neurons from voting, acknowledging the implicit dependency. [5](#0-4) 

No equivalent protection exists in the SNS parameter validation.

### Impact Explanation

An attacker targeting an SNS with `neuron_minimum_dissolve_delay_to_vote_seconds` set below the maximum voting period can:

1. Acquire SNS tokens from a DEX or lending market (no long-term commitment required).
2. Stake them in a neuron with dissolve delay equal to `neuron_minimum_dissolve_delay_to_vote_seconds`.
3. Cast a decisive vote on a malicious `UpgradeSnsControlledCanister`, `TransferSnsTreasuryFunds`, or `ManageNervousSystemParameters` proposal.
4. Immediately start dissolving the neuron.
5. After `neuron_minimum_dissolve_delay_to_vote_seconds` seconds — which can be 0 — disburse the tokens and return them to the lender.

The attacker bears no lasting economic exposure. The proposal may still be open and executing while the attacker has already exited. This enables a complete SNS governance takeover or treasury drain with only transient capital.

### Likelihood Explanation

**Medium.** Any SNS that sets `neuron_minimum_dissolve_delay_to_vote_seconds` to a value shorter than its maximum voting period is immediately vulnerable. This misconfiguration is not prevented by the protocol. An SNS community may set it low intentionally (to lower the barrier to participation) without understanding the flash-loan attack surface. Additionally, a malicious `ManageNervousSystemParameters` proposal — itself passable if the attacker already holds transient voting power — could reduce the parameter to 0, bootstrapping the attack.

### Recommendation

Add a cross-field validation in `validate_neuron_minimum_dissolve_delay_to_vote_seconds` that enforces:

```
neuron_minimum_dissolve_delay_to_vote_seconds
    >= initial_voting_period_seconds + 2 * wait_for_quiet_deadline_increase_seconds
```

This mirrors the protection the NNS applies via `NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_BOUNDS` and is the direct analog of the Audius fix (`decreaseStakeLockupDuration >= votingPeriod + executionDelay`). [1](#0-0) 

### Proof of Concept

**Configuration (valid per current validation):**
```
neuron_minimum_dissolve_delay_to_vote_seconds = 0
initial_voting_period_seconds = INITIAL_VOTING_PERIOD_SECONDS_FLOOR  (e.g., 1 day)
wait_for_quiet_deadline_increase_seconds = 1
```

This passes `NervousSystemParameters::validate()` because:
- `0 <= max_dissolve_delay_seconds` ✓
- `initial_voting_period_seconds` is within `[FLOOR, CEILING]` ✓
- `wait_for_quiet_deadline_increase_seconds >= 1` ✓ [6](#0-5) 

**Attack sequence:**
1. Attacker borrows 10M SNS tokens from a lending protocol.
2. Calls `claim_or_refresh_neuron` to stake them; neuron dissolve delay = 0 (already dissolved).
3. Submits or votes `Yes` on a `TransferSnsTreasuryFunds` proposal draining the SNS treasury to attacker's account.
4. Immediately calls `disburse` (neuron is already dissolved, no wait required).
5. Returns 10M tokens to lender. Proposal executes within `initial_voting_period_seconds`.

The SNS treasury is drained; the attacker's net cost is only the loan fee and transaction fees. [1](#0-0) [7](#0-6)

### Citations

**File:** rs/sns/governance/src/types.rs (L570-594)
```rust
    /// This validates that the `NervousSystemParameters` are well-formed.
    pub fn validate(&self) -> Result<(), String> {
        self.validate_reject_cost_e8s()?;
        self.validate_neuron_minimum_stake_e8s()?;
        self.validate_transaction_fee_e8s()?;
        self.validate_max_proposals_to_keep_per_action()?;
        self.validate_initial_voting_period_seconds()?;
        self.validate_wait_for_quiet_deadline_increase_seconds()?;
        self.validate_default_followees()?;
        self.validate_max_number_of_neurons()?;
        self.validate_neuron_minimum_dissolve_delay_to_vote_seconds()?;
        self.validate_max_followees_per_function()?;
        self.validate_max_dissolve_delay_seconds()?;
        self.validate_max_neuron_age_for_age_bonus()?;
        self.validate_max_number_of_proposals_with_ballots()?;
        self.validate_neuron_claimer_permissions()?;
        self.validate_neuron_grantable_permissions()?;
        self.validate_max_number_of_principals_per_neuron()?;
        self.validate_voting_rewards_parameters()?;
        self.validate_max_dissolve_delay_bonus_percentage()?;
        self.validate_max_age_bonus_percentage()?;
        self.validate_additional_critical_native_action_ids()?;

        Ok(())
    }
```

**File:** rs/sns/governance/src/types.rs (L653-673)
```rust
    /// Validates that the nervous system parameter initial_voting_period_seconds is well-formed.
    fn validate_initial_voting_period_seconds(&self) -> Result<(), String> {
        let initial_voting_period_seconds =
            self.initial_voting_period_seconds.ok_or_else(|| {
                "NervousSystemParameters.initial_voting_period_seconds must be set".to_string()
            })?;

        if initial_voting_period_seconds < Self::INITIAL_VOTING_PERIOD_SECONDS_FLOOR {
            Err(format!(
                "NervousSystemParameters.initial_voting_period_seconds must be greater than {}",
                Self::INITIAL_VOTING_PERIOD_SECONDS_FLOOR
            ))
        } else if initial_voting_period_seconds > Self::INITIAL_VOTING_PERIOD_SECONDS_CEILING {
            Err(format!(
                "NervousSystemParameters.initial_voting_period_seconds must be less than {}",
                Self::INITIAL_VOTING_PERIOD_SECONDS_CEILING
            ))
        } else {
            Ok(())
        }
    }
```

**File:** rs/sns/governance/src/types.rs (L675-713)
```rust
    /// Validates that the nervous system parameter wait_for_quiet_deadline_increase_seconds is well-formed.
    fn validate_wait_for_quiet_deadline_increase_seconds(&self) -> Result<(), String> {
        let initial_voting_period_seconds =
            self.initial_voting_period_seconds.ok_or_else(|| {
                "NervousSystemParameters.initial_voting_period_seconds must be set".to_string()
            })?;
        let wait_for_quiet_deadline_increase_seconds = self
            .wait_for_quiet_deadline_increase_seconds
            .ok_or_else(|| {
                "NervousSystemParameters.wait_for_quiet_deadline_increase_seconds must be set"
                    .to_string()
            })?;

        if wait_for_quiet_deadline_increase_seconds
            < Self::WAIT_FOR_QUIET_DEADLINE_INCREASE_SECONDS_FLOOR
        {
            Err(format!(
                "NervousSystemParameters.wait_for_quiet_deadline_increase_seconds must be greater than or equal to {}",
                Self::WAIT_FOR_QUIET_DEADLINE_INCREASE_SECONDS_FLOOR
            ))
        } else if wait_for_quiet_deadline_increase_seconds
            > Self::WAIT_FOR_QUIET_DEADLINE_INCREASE_SECONDS_CEILING
        {
            Err(format!(
                "NervousSystemParameters.wait_for_quiet_deadline_increase_seconds must be less than or equal to {}",
                Self::WAIT_FOR_QUIET_DEADLINE_INCREASE_SECONDS_CEILING
            ))
        // If `wait_for_quiet_deadline_increase_seconds > initial_voting_period_seconds / 2`, any flip (including an initial `yes` vote)
        // will always cause the deadline to be increased. That seems like unreasonable behavior, so we prevent that from being
        // the case.
        } else if wait_for_quiet_deadline_increase_seconds > initial_voting_period_seconds / 2 {
            Err(format!(
                "NervousSystemParameters.wait_for_quiet_deadline_increase_seconds is {}, but must be less than or equal to half the initial voting period, {}",
                initial_voting_period_seconds,
                initial_voting_period_seconds / 2
            ))
        } else {
            Ok(())
        }
```

**File:** rs/sns/governance/src/types.rs (L752-772)
```rust
    /// Validates that the nervous system parameter
    /// neuron_minimum_dissolve_delay_to_vote_seconds is well-formed.
    fn validate_neuron_minimum_dissolve_delay_to_vote_seconds(&self) -> Result<(), String> {
        let max_dissolve_delay_seconds = self.validate_max_dissolve_delay_seconds()?;

        let neuron_minimum_dissolve_delay_to_vote_seconds = self
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .ok_or_else(|| {
                "NervousSystemParameters.neuron_minimum_dissolve_delay_to_vote_seconds must be set"
                    .to_string()
            })?;

        if neuron_minimum_dissolve_delay_to_vote_seconds > max_dissolve_delay_seconds {
            Err(format!(
                "The minimum dissolve delay to vote ({neuron_minimum_dissolve_delay_to_vote_seconds}) cannot be greater than the max \
                dissolve delay ({max_dissolve_delay_seconds})"
            ))
        } else {
            Ok(())
        }
    }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1152-1162)
```text
  // The wait for quiet algorithm extends the voting period of a proposal when
  // there is a flip in the majority vote during the proposal's voting period.
  // This parameter determines the maximum time period that the voting period
  // may be extended after a flip. If there is a flip at the very end of the
  // original proposal deadline, the remaining time will be set to this parameter.
  // If there is a flip before or after the original deadline, the deadline will be
  // extended by somewhat less than this parameter.
  // The maximum total voting period extension is 2 * wait_for_quiet_deadline_increase_seconds.
  // For more information, see the wiki page on the wait-for-quiet algorithm:
  // https://internetcomputer.org/how-it-works/network-nervous-system-nns/#voting-rules
  optional uint64 wait_for_quiet_deadline_increase_seconds = 18;
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1182-1185)
```text
  // The minimum dissolve delay a neuron must have to be eligible to vote.
  //
  // The chosen value must be smaller than max_dissolve_delay_seconds.
  optional uint64 neuron_minimum_dissolve_delay_to_vote_seconds = 8;
```

**File:** rs/nns/governance/src/network_economics.rs (L285-294)
```rust
    /// A proposal to set `VotingPowerEconomics.min_dissolve_delay_seconds` must specify a value
    /// for this field that falls within this range. Changing the lower bound of this parameter
    /// requires manually checking how it might interact with other aspects of the NNS.
    /// In particular, it is not currently possible for a dissolved neuron to cast a vote, as
    /// the minimal dissolve delay to be eligible for voting exceeds the maximal voting period.
    /// Thus, there may be implicit dependencies of the NNS itself or its clients on this aspect,
    /// which originate from the time when the minimum dissolve delay to vote was an internal NNS
    /// constant.
    pub const NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_BOUNDS: RangeInclusive<u64> =
        (14 * ONE_DAY_SECONDS)..=(6 * ONE_MONTH_SECONDS);
```
