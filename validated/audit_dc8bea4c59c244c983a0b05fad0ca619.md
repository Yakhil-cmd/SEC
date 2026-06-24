Audit Report

## Title
Unbounded Neuron Iteration in SNS `compute_ballots_for_new_proposal` Can Exhaust Instruction Limit, Permanently Blocking Proposal Submission - (File: rs/sns/governance/src/governance.rs)

## Summary
The SNS Governance canister's `compute_ballots_for_new_proposal` function iterates synchronously over every neuron in `self.proto.neurons` with no instruction-limit guard. When the neuron count is sufficiently large, every call to `make_proposal` will trap with `CanisterInstructionLimitExceeded`, permanently preventing any new governance proposals from being submitted to that SNS instance. No privileged access is required to trigger this condition.

## Finding Description
`make_proposal` unconditionally calls `compute_ballots_for_new_proposal` before inserting any proposal: [1](#0-0) 

`compute_ballots_for_new_proposal` contains a plain `for` loop over the entire neuron map with no instruction budget check: [2](#0-1) 

Per iteration the function reads the neuron's dissolve state, computes a multi-factor voting-power bonus (`dissolve_delay_seconds`, `voting_power` with age and delay bonuses), and performs a `BTreeMap::insert`. A grep search for `instruction_counter`, `over_soft_message_limit`, and `noop_self_call_if_over_instructions` across the entire `rs/sns/governance/src/governance.rs` file returns **zero matches** — confirming there is no instruction-limit guard anywhere in this code path. [3](#0-2) 

By contrast, the NNS Governance canister defines explicit hard and soft instruction limits: [4](#0-3) 

And uses `noop_self_call_if_over_instructions` to yield before the limit is hit: [5](#0-4) 

The SNS has neither the instruction-limit constants nor the yield mechanism. The NNS comment at line 23 states the hard limit of 750 billion instructions "leaves room for 750 thousand neurons with complex following." The IC update-message instruction limit on application subnets (where SNS canisters run) is 20 × 10⁹ instructions. Even assuming the per-neuron work in `compute_ballots_for_new_proposal` is simpler than the NNS voting cascade, the budget is exhausted well within the `MAX_NUMBER_OF_NEURONS` ceiling defined in `rs/sns/governance/src/types.rs`. [6](#0-5) 

## Impact Explanation
Once the neuron count crosses the instruction-budget threshold, every call to `make_proposal` traps. No new governance proposals can be submitted to the affected SNS — including any remediation proposal that would itself require `make_proposal` to succeed. This is a complete, self-reinforcing denial-of-service on SNS governance proposal submission. This matches the allowed High impact: **"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS"** and **"Significant SNS security impact with concrete user or protocol harm."** Severity: **High**.

## Likelihood Explanation
Any SNS with organic growth in its neuron population approaches the threshold over time. An adversary can accelerate this by splitting existing neurons or staking small amounts to create many neurons — operations available to any token holder with `SubmitProposal` permission and sufficient stake to cover `reject_cost_e8s`. No privileged access, no governance majority, no subnet corruption, and no external oracle is required. The attack is repeatable and permanent once triggered.

## Recommendation
1. **Adopt the NNS snapshot pattern**: replace the live neuron iteration with a pre-computed, periodically refreshed voting-power snapshot (analogous to `compute_voting_power_snapshot_for_standard_proposal` in the NNS neuron store), updated in a timer job where instruction overruns can be handled incrementally.
2. **Add an instruction-limit guard**: if live iteration must be kept, add an `ic_cdk::api::instruction_counter()` check inside the loop and return a graceful error (not a trap) when the soft limit is approached, mirroring the NNS `SOFT_VOTING_INSTRUCTIONS_LIMIT` / `HARD_VOTING_INSTRUCTIONS_LIMIT` pattern.
3. **Enforce a tighter neuron cap**: lower `MAX_NUMBER_OF_NEURONS` to a value provably safe given the per-neuron instruction cost, and add a benchmark test that verifies `compute_ballots_for_new_proposal` completes within budget at the cap.

## Proof of Concept
1. Deploy an SNS with default `NervousSystemParameters`.
2. Create N neurons (by staking and claiming) where N × (per-neuron instruction cost for dissolve-delay read + voting-power computation + `BTreeMap::insert`) exceeds the application-subnet update-message instruction limit (20 × 10⁹). Based on the NNS benchmark data (≈1 M instructions/neuron for more complex following logic), this threshold is reachable at tens of thousands of neurons, well within the documented `MAX_NUMBER_OF_NEURONS` ceiling.
3. Call `manage_neuron` → `MakeProposal` from any neuron with `SubmitProposal` permission.
4. Observe the call trap with `CanisterInstructionLimitExceeded`.
5. Confirm that no subsequent `make_proposal` call succeeds regardless of the proposer, because the neuron map size is unchanged.
6. A deterministic integration test using PocketIC can inject the required number of neurons and assert that `make_proposal` returns an instruction-limit error, providing a reproducible, safe proof without touching mainnet.

### Citations

**File:** rs/sns/governance/src/governance.rs (L3557-3559)
```rust
        let (_, electoral_roll) = self
            .compute_ballots_for_new_proposal()
            .map_err(|err| GovernanceError::new_with_message(ErrorType::PreconditionFailed, err))?;
```

**File:** rs/sns/governance/src/governance.rs (L5225-5226)
```rust
    /// Computes the total potential voting power of the governance canister and ballots.
    fn compute_ballots_for_new_proposal(&self) -> Result<(u64, BTreeMap<String, Ballot>), String> {
```

**File:** rs/sns/governance/src/governance.rs (L5255-5280)
```rust
        for (k, v) in self.proto.neurons.iter() {
            // If this neuron is eligible to vote, record its
            // voting power at the time of proposal creation (now).
            if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
                // Not eligible due to dissolve delay.
                continue;
            }

            let voting_power = v.voting_power(
                now_seconds,
                max_dissolve_delay,
                max_age_bonus,
                max_dissolve_delay_bonus_percentage,
                max_age_bonus_percentage,
            );

            total_power += voting_power as u128;
            electoral_roll.insert(
                k.clone(),
                Ballot {
                    vote: Vote::Unspecified as i32,
                    voting_power,
                    cast_timestamp_seconds: 0,
                },
            );
        }
```

**File:** rs/nns/governance/src/voting.rs (L22-31)
```rust
/// The hard limit for the number of instructions that can be executed in a single call context.
/// This leaves room for 750 thousand neurons with complex following.
const HARD_VOTING_INSTRUCTIONS_LIMIT: u64 = 750 * BILLION;
// For production, we want this higher so that we can process more votes, but without affecting
// the overall responsiveness of the canister. 1 Billion seems like a reasonable compromise.
const SOFT_VOTING_INSTRUCTIONS_LIMIT: u64 = if cfg!(feature = "test") {
    1_000_000
} else {
    BILLION
};
```

**File:** rs/nns/governance/src/voting.rs (L163-175)
```rust
            if let Err(e) = noop_self_call_if_over_instructions(
                SOFT_VOTING_INSTRUCTIONS_LIMIT,
                Some(HARD_VOTING_INSTRUCTIONS_LIMIT),
            )
            .await
            {
                println!(
                    "Error in cast_vote_and_cascade_follow, \
                        voting will be processed in timers: {}",
                    e
                );
                break;
            }
```

**File:** rs/sns/governance/src/types.rs (L1-70)
```rust
use crate::{
    following::TOPICS,
    governance::{Governance, NERVOUS_SYSTEM_FUNCTION_DELETION_MARKER, TimeWarp},
    logs::INFO,
    pb::{
        sns_root_types::{
            ManageDappCanisterSettingsRequest, RegisterDappCanistersRequest,
            SetDappControllersRequest, set_dapp_controllers_request::CanisterIds,
        },
        v1::{
            ChunkedCanisterWasm, ClaimSwapNeuronsError, ClaimSwapNeuronsResponse,
            ClaimedSwapNeuronStatus, CustomProposalCriticality, DefaultFollowees,
            DeregisterDappCanisters, Empty, ExecuteGenericNervousSystemFunction, Followee,
            GovernanceError, ManageDappCanisterSettings, ManageLedgerParameters,
            ManageNeuronResponse, ManageSnsMetadata, MintSnsTokens, Motion, NervousSystemFunction,
            NervousSystemParameters, Neuron, NeuronId, NeuronIds, NeuronPermission,
            NeuronPermissionList, NeuronPermissionType, ProposalId, RegisterDappCanisters,
            RewardEvent, SnsVersion, TransferSnsTreasuryFunds, UpgradeSnsControlledCanister,
            UpgradeSnsToNextVersion, Vote, VotingRewardsParameters,
            claim_swap_neurons_request::{
                NeuronRecipe, NeuronRecipes,
                neuron_recipe::{self, Participant},
            },
            claim_swap_neurons_response::{ClaimSwapNeuronsResult, ClaimedSwapNeurons, SwapNeuron},
            get_neuron_response,
            governance::{
                self, Mode, SnsMetadata, Version,
                neuron_in_flight_command::{self, SyncCommand},
            },
            governance_error::ErrorType,
            manage_neuron,
            manage_neuron_response::{
                self, DisburseMaturityResponse, MergeMaturityResponse, StakeMaturityResponse,
            },
            nervous_system_function::FunctionType,
            neuron::{FolloweesForTopic, TopicFollowees},
            proposal::Action,
        },
    },
    proposal::ValidGenericNervousSystemFunction,
    topics::topic_descriptions,
};
use async_trait::async_trait;
use candid::{Decode, Encode};
use ic_base_types::CanisterId;
use ic_canister_log::log;
use ic_crypto_sha2::Sha256;
use ic_icrc1_ledger::UpgradeArgs as LedgerUpgradeArgs;
use ic_ledger_core::tokens::TOKEN_SUBDIVIDABLE_BY;
use ic_management_canister_types_private::{
    CanisterIdRecord, CanisterInstallModeError, StoredChunksReply,
};
use ic_nervous_system_common::{
    DEFAULT_TRANSFER_FEE, NervousSystemError, ONE_DAY_SECONDS, ONE_MONTH_SECONDS, ONE_YEAR_SECONDS,
    hash_to_hex_string, ledger_validation::MAX_LOGO_LENGTH,
};
use ic_nervous_system_common_validation::validate_url;
use ic_nervous_system_proto::pb::v1::{Duration as PbDuration, Percentage};
use ic_sns_governance_api::format_full_hash;
use ic_sns_governance_proposal_criticality::{ProposalCriticality, VotingDurationParameters};
use icrc_ledger_types::icrc::generic_metadata_value::MetadataValue;
use icrc_ledger_types::icrc::metadata_key::MetadataKey;
use itertools::{Either, Itertools};
use lazy_static::lazy_static;
use std::{
    collections::{BTreeMap, BTreeSet, HashSet},
    convert::TryFrom,
    fmt,
};
use strum::IntoEnumIterator;
```
