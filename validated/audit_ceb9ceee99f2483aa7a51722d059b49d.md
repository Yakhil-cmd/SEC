Audit Report

## Title
`randomly_pick_swap_start` Silently Falls Back to Fixed Constant When RNG Uninitialized, Producing Deterministic SNS Swap Start Time — (`rs/nns/governance/src/governance.rs`)

## Summary

`CanisterRandomnessGenerator` is constructed with `rng: None` and is seeded only after an async `raw_rand` round trip completes via `SeedingTask`. During the window between canister init/upgrade and that first async response, `randomly_pick_swap_start` silently substitutes `10_000` for the missing random value via `unwrap_or(10_000)`, producing a deterministic swap start time of 2:45:00 UTC for any `CreateServiceNervousSystem` proposal executed in that window.

## Finding Description

`CanisterRandomnessGenerator::new()` explicitly sets `rng: None`: [1](#0-0) 

`random_u64()` returns `Err(RngError::RngNotInitialized)` when `rng` is `None`: [2](#0-1) 

`SeedingTask` seeds the RNG only after the async `raw_rand` call returns. Its `initial_delay()` is `Duration::from_secs(0)`, but on the IC, timer callbacks execute after the current message completes and require at least one async round trip, guaranteeing a window of at least one IC round post-init/upgrade where `rng` remains `None`: [3](#0-2) 

`randomly_pick_swap_start` swallows the error with `unwrap_or(10_000)`: [4](#0-3) 

`10_000 % 86400 = 10000`; rounded down to the nearest 15-minute boundary: `10000 - (10000 % 900) = 9900` seconds = **2:45:00 UTC**. This is called unconditionally in `do_create_service_nervous_system`: [5](#0-4) 

The result is used as `swap_start_timestamp_seconds` only when the proposal omits an explicit `start_time`; when `start_time` is `None`, `random_swap_start_time` is substituted directly: [6](#0-5) 

No guard in `do_create_service_nervous_system` checks whether the RNG is initialized before proceeding. [7](#0-6) 

## Impact Explanation

Any `CreateServiceNervousSystem` proposal executed during the post-upgrade uninitialized window (e.g., a proposal adopted just before an upgrade that is queued for execution) will produce an SNS swap with a publicly known, deterministic start time of 2:45:00 UTC. For oversubscribed SNS swaps that close early upon reaching maximum participation, an observer who knows this fixed start time in advance can position themselves to participate at the exact opening moment, gaining priority over other participants. This constitutes a concrete fairness and integrity impact on the SNS launch mechanism, falling under **High: Significant NNS/SNS security impact with concrete user or protocol harm**. The severity is constrained to the lower end of High (or upper Medium) because exploitation requires the coincidence of a governance upgrade and a queued CSNS proposal without an explicit `start_time`.

## Likelihood Explanation

The uninitialized window exists after **every governance canister upgrade**, a routine operational event. A CSNS proposal adopted just before an upgrade and queued for execution is a realistic scenario. The IC's deterministic execution model makes the window reproducible and predictable. If `raw_rand` fails, `RETRY_SEEDING_INTERVAL` is 30 seconds, extending the window significantly. [8](#0-7) 

No attacker action is required to trigger the fallback — it is a passive consequence of the execution timing. Any chain observer can detect the condition and exploit the known start time.

## Recommendation

`randomly_pick_swap_start` should propagate the `RngNotInitialized` error rather than silently substituting a hardcoded constant. The function signature should return `Result<GlobalTimeOfDay, GovernanceError>`, and `do_create_service_nervous_system` should return an error if the RNG is not yet seeded, allowing the proposal execution to be retried once the RNG is available. Alternatively, the `SeedingTask` should be completed synchronously during `canister_init` and `canister_post_upgrade` before any proposal execution is permitted.

## Proof of Concept

1. Deploy or upgrade the NNS governance canister.
2. In the same IC round (before `SeedingTask` completes its first `raw_rand` call), execute a `CreateServiceNervousSystem` proposal that omits `swap_parameters.start_time` (e.g., a proposal adopted just before the upgrade).
3. `randomly_pick_swap_start` is called with `rng = None`.
4. `random_u64()` returns `Err(RngNotInitialized)`.
5. `unwrap_or(10_000)` substitutes `10_000`.
6. `10_000 % 86400 = 10000`; `10000 - (10000 % 900) = 9900` seconds after midnight = 2:45:00 UTC.
7. `swap_start_timestamp_seconds` is set to this deterministic value.
8. A participant monitoring the chain observes the fixed start time and participates at exactly 2:45:00 UTC, ahead of uninformed participants in an oversubscribed swap.

A deterministic integration test using PocketIC can reproduce this by: (a) deploying governance, (b) submitting and adopting a CSNS proposal, (c) triggering an upgrade before `SeedingTask` fires, and (d) asserting that `swap_start_timestamp_seconds` equals `9900`.

### Citations

**File:** rs/nns/governance/src/canister_state.rs (L135-138)
```rust
impl CanisterRandomnessGenerator {
    pub fn new() -> Self {
        CanisterRandomnessGenerator { rng: None }
    }
```

**File:** rs/nns/governance/src/canister_state.rs (L141-147)
```rust
impl RandomnessGenerator for CanisterRandomnessGenerator {
    fn random_u64(&mut self) -> Result<u64, RngError> {
        match self.rng.as_mut() {
            Some(rand) => Ok(rand.next_u64()),
            None => Err(RngError::RngNotInitialized),
        }
    }
```

**File:** rs/nns/governance/src/timer_tasks/seeding.rs (L21-22)
```rust
const SEEDING_INTERVAL: Duration = Duration::from_secs(3600);
const RETRY_SEEDING_INTERVAL: Duration = Duration::from_secs(30);
```

**File:** rs/nns/governance/src/timer_tasks/seeding.rs (L31-50)
```rust
        let result: Result<Vec<u8>, (Option<i32>, String)> = env
            .call_canister_method(IC_00, "raw_rand", Encode!().unwrap())
            .await;

        let next_delay = match result {
            Ok(bytes) => {
                let seed = Decode!(&bytes, [u8; 32]).unwrap();
                self.governance.with_borrow_mut(|governance| {
                    governance.seed_rng(seed);
                });
                SEEDING_INTERVAL
            }
            Err((code, msg)) => {
                println!(
                    "{}Error seeding RNG. Error Code: {:?}. Error Message: {}",
                    LOG_PREFIX, code, msg
                );
                RETRY_SEEDING_INTERVAL
            }
        };
```

**File:** rs/nns/governance/src/governance.rs (L4401-4446)
```rust
    async fn do_create_service_nervous_system(
        &mut self,
        proposal_id: u64,
        create_service_nervous_system: &CreateServiceNervousSystem,
    ) -> Result<(), GovernanceError> {
        // Get the current time of proposal execution.
        let current_timestamp_seconds = self.env.now();

        let swap_parameters = create_service_nervous_system
            .swap_parameters
            .as_ref()
            .ok_or(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "missing field swap_parameters",
            ))?;

        let proposal_id = ProposalId { id: proposal_id };
        let (initial_neurons_fund_participation_snapshot, neurons_fund_participation_constraints) =
            if swap_parameters.neurons_fund_participation.unwrap_or(false) {
                let (
                    initial_neurons_fund_participation_snapshot,
                    neurons_fund_participation_constraints,
                ) = self
                    .draw_maturity_from_neurons_fund(&proposal_id, create_service_nervous_system)?;
                (
                    initial_neurons_fund_participation_snapshot,
                    Some(neurons_fund_participation_constraints),
                )
            } else {
                self.record_neurons_fund_participation_not_requested(&proposal_id)?;
                (NeuronsFundSnapshot::empty(), None)
            };

        let random_swap_start_time = self.randomly_pick_swap_start();
        let create_service_nervous_system = create_service_nervous_system.clone();

        self.execute_create_service_nervous_system_proposal(
            create_service_nervous_system,
            neurons_fund_participation_constraints,
            current_timestamp_seconds,
            proposal_id,
            random_swap_start_time,
            initial_neurons_fund_participation_snapshot,
        )
        .await
    }
```

**File:** rs/nns/governance/src/governance.rs (L4456-4472)
```rust
        let (swap_start_timestamp_seconds, swap_due_timestamp_seconds) = {
            let start_time = create_service_nervous_system
                .swap_parameters
                .as_ref()
                .and_then(|swap_parameters| swap_parameters.start_time);

            let duration = create_service_nervous_system
                .swap_parameters
                .as_ref()
                .and_then(|swap_parameters| swap_parameters.duration);

            CreateServiceNervousSystem::swap_start_and_due_timestamps(
                start_time.unwrap_or(random_swap_start_time),
                duration.unwrap_or_default(),
                current_timestamp_seconds,
            )
        }?;
```

**File:** rs/nns/governance/src/governance.rs (L7885-7888)
```rust
    pub fn randomly_pick_swap_start(&mut self) -> GlobalTimeOfDay {
        // It's not critical that we have perfect randomness here, so we can default to a fixed value
        // for the edge case where the RNG is not initialized (which should never happen in practice).
        let time_of_day_seconds = self.randomness.random_u64().unwrap_or(10_000) % ONE_DAY_SECONDS;
```
