### Title
Global Neuron Creation Rate Limiter Exhaustion via Unrestricted `ClaimOrRefresh` with `MemoAndController` - (File: rs/nns/governance/src/governance.rs)

### Summary
The NNS Governance canister uses a single global neuron creation rate limiter keyed on `"ADD_NEURON"`, shared across all callers and all neuron-creation paths. Any unprivileged ingress sender can exhaust this global burst capacity (`MAX_NEURON_CREATION_SPIKE = 300`) by pre-funding subaccounts and repeatedly calling `manage_neuron` with `ClaimOrRefresh { by: MemoAndController { controller: None, memo: i } }`, temporarily throttling all users to at most 1 new neuron per 240 seconds.

### Finding Description
The rate limiter is configured as follows:

```
add_capacity_amount:    1
add_capacity_interval:  240 s  (= 3600 / MAX_SUSTAINED_NEURONS_PER_HOUR)
max_capacity:           300    (= MAX_SUSTAINED_NEURONS_PER_HOUR * 20 = 15 * 20)
``` [1](#0-0) 

The rate limiter is instantiated with a single shared key: [2](#0-1) [3](#0-2) 

Every neuron-creation path — `claim_neuron`, `split_neuron`, `disburse_to_neuron`, and `create_neuron` — calls `try_reserve` against this same key before doing any caller-identity check: [4](#0-3) [5](#0-4) [6](#0-5) 

The `ClaimOrRefresh` command with `MemoAndController` is explicitly designed to allow **any caller** to claim a neuron on behalf of another principal — no ownership check is performed before the rate-limiter reservation is taken: [7](#0-6) 

The reservation is only committed (i.e., capacity is only permanently consumed) when the neuron is successfully created. A failed claim (e.g., insufficient ledger balance) drops the reservation without consuming capacity. Therefore, the attacker **must** actually fund the subaccounts with the minimum stake to exhaust the limiter.

### Impact Explanation
**Impact: Medium.**  
After the attacker exhausts all 300 burst slots, every user — including legitimate ones — is throttled to at most 1 new neuron per 240 seconds (15/hour). This is not a complete denial of service, but it is a severe throughput reduction during high-demand periods (e.g., immediately after a major ICP price move or governance event). The attacker can repeat the attack indefinitely by dissolving and re-staking the neurons. The error returned to blocked users is:

> "Reached maximum number of neurons that can be created in this hour. Please wait and try again later." [8](#0-7) 

### Likelihood Explanation
**Likelihood: Low.**  
The attacker must pre-fund 300 subaccounts with the NNS minimum neuron stake (~1 ICP each = ~300 ICP total). The ICP is not destroyed — it is locked in neurons and can be recovered after the dissolve delay — but the opportunity cost and setup effort are non-trivial. A well-funded adversary (e.g., a competing protocol or nation-state actor) could execute this repeatedly.

### Recommendation
1. **Per-principal sub-limit**: Track neuron-creation counts per caller within the rate-limiter window and reject requests that exceed a per-principal quota before consuming global capacity.
2. **Restrict proxy claiming**: Require that `ClaimOrRefresh` with `MemoAndController` only succeeds when `controller == caller`, or impose a separate, lower per-caller burst limit for proxy claims.
3. **Increase minimum stake**: A higher minimum stake raises the capital cost of the attack proportionally.

### Proof of Concept
```
// Attacker (any ingress principal P) pre-funds 300 subaccounts:
for memo in 0..300:
    transfer(1_ICP, governance_subaccount(P, memo))

// Attacker exhausts the global rate limiter:
for memo in 0..300:
    manage_neuron(ClaimOrRefresh {
        by: MemoAndController { memo, controller: None }
    })
// → 300 reservations committed; global capacity = 0

// Now any other user calling manage_neuron(ClaimOrRefresh{...}) receives:
// GovernanceError { Unavailable, "Reached maximum number of neurons that can
//   be created in this hour. Please wait and try again later." }
// until capacity replenishes at 1 slot per 240 s.
```

The test `test_rate_limiting_neuron_creation` in `rs/nns/governance/tests/governance.rs` already demonstrates that exhausting `MAX_NEURON_CREATION_SPIKE` slots blocks subsequent callers with exactly this error, confirming the mechanism is reachable from ingress. [9](#0-8)

### Citations

**File:** rs/nns/governance/src/governance.rs (L228-240)
```rust
/// The maximum number of neurons supported.
pub const MAX_NUMBER_OF_NEURONS: usize = 500_000;

// Spawning is exempted from rate limiting, so we don't need large of a limit here.
pub const MAX_SUSTAINED_NEURONS_PER_HOUR: u64 = 15;

pub const MINIMUM_SECONDS_BETWEEN_ALLOWANCE_INCREASE: u64 = 3600 / MAX_SUSTAINED_NEURONS_PER_HOUR;

/// The maximum number of neurons that can be created in a spike. Note that such rate of neuron
/// creation is not sustainable as the allowance will be exhausted after creating this many neurons
/// in a short period of time, and the allowance will only be increased according to
/// `MINIMUM_SECONDS_BETWEEN_ALLOWANCE_INCREASE`.
pub const MAX_NEURON_CREATION_SPIKE: u64 = MAX_SUSTAINED_NEURONS_PER_HOUR * 20;
```

**File:** rs/nns/governance/src/governance.rs (L290-292)
```rust
/// A key for the neuron rate limiter, to make sure all add_neuron operations are limited
/// in the same limit.
const NEURON_RATE_LIMITER_KEY: &str = "ADD_NEURON";
```

**File:** rs/nns/governance/src/governance.rs (L346-365)
```rust
impl From<RateLimiterError> for GovernanceError {
    fn from(value: RateLimiterError) -> Self {
        let message = match value {
            RateLimiterError::NotEnoughCapacity => {
                "Reached maximum number of neurons that can be created in this hour. \
                    Please wait and try again later."
                    .to_string()
            }
            RateLimiterError::InvalidArguments(e) => format!("Rate Limit Error: {e}"),
            RateLimiterError::MaxReservationsReached => {
                "Reached maximum number of neuron creation reservations.  This should not happen."
                    .to_string()
            }
            RateLimiterError::ReservationNotFound => "Rate limit reservation could not be \
                committed because rate limiter has no record of it."
                .to_string(),
        };

        GovernanceError::new_with_message(ErrorType::Unavailable, message)
    }
```

**File:** rs/nns/governance/src/governance.rs (L1261-1271)
```rust
fn new_rate_limiter() -> InMemoryRateLimiter<String> {
    RateLimiter::new_in_memory(RateLimiterConfig {
        add_capacity_amount: 1,
        add_capacity_interval: Duration::from_secs(MINIMUM_SECONDS_BETWEEN_ALLOWANCE_INCREASE),
        max_capacity: MAX_NEURON_CREATION_SPIKE,
        // It should not be possible to have more than MAX_NEURON_CREATION_SPIKE_RESERVATIONS
        // because there is only one reservation space being used.
        // But we don't want to hit that error, so we add an extra one.
        max_reservations: MAX_NEURON_CREATION_SPIKE + 1,
    })
}
```

**File:** rs/nns/governance/src/governance.rs (L2143-2147)
```rust
        let neuron_limit_reservation = self.rate_limiter.try_reserve(
            self.env.now_system_time(),
            NEURON_RATE_LIMITER_KEY.to_string(),
            1,
        )?;
```

**File:** rs/nns/governance/src/governance.rs (L5852-5870)
```rust
    async fn claim_or_refresh_neuron_by_memo_and_controller(
        &mut self,
        caller: &PrincipalId,
        memo_and_controller: MemoAndController,
        claim_or_refresh: &ClaimOrRefresh,
    ) -> Result<NeuronId, GovernanceError> {
        let controller = memo_and_controller.controller.unwrap_or(*caller);
        let memo = memo_and_controller.memo;
        let subaccount = ledger::compute_neuron_staking_subaccount(controller, memo);
        match self.neuron_store.get_neuron_id_for_subaccount(subaccount) {
            Some(neuron_id) => {
                self.refresh_neuron(neuron_id, subaccount, claim_or_refresh)
                    .await
            }
            None => {
                self.claim_neuron(subaccount, controller, claim_or_refresh)
                    .await
            }
        }
```

**File:** rs/nns/governance/src/governance.rs (L5992-5996)
```rust
        let neuron_limit_reservation = self.rate_limiter.try_reserve(
            self.env.now_system_time(),
            NEURON_RATE_LIMITER_KEY.to_string(),
            1,
        )?;
```

**File:** rs/nns/governance/src/governance/create_neuron.rs (L91-97)
```rust
        let (neuron_limit_reservation, neuron_subaccount, neuron_id) =
            governance.with_borrow_mut(|governance| {
                let neuron_limit_reservation = governance.rate_limiter.try_reserve(
                    governance.env.now_system_time(),
                    NEURON_RATE_LIMITER_KEY.to_string(),
                    1,
                )?;
```

**File:** rs/nns/governance/tests/governance.rs (L5099-5210)
```rust
#[test]
fn test_rate_limiting_neuron_creation() {
    let current_peak = MAX_NEURON_CREATION_SPIKE;
    let minimum_wait_for_capacity_increase = MINIMUM_SECONDS_BETWEEN_ALLOWANCE_INCREASE;

    // Some neurons with maturity and stake so we can spawn and split
    let staked_neurons = (1..=(current_peak - 1))
        .map(|i| {
            let controller = PrincipalId::new_user_test_id(i);
            let nonce = 1234_u64;
            api::Neuron {
                id: Some(NeuronId::from_u64(i)),
                account: ledger::compute_neuron_staking_subaccount(controller, nonce).into(),
                controller: Some(controller),
                cached_neuron_stake_e8s: 10 * E8,
                neuron_fees_e8s: 0,
                created_timestamp_seconds: 0,
                aging_since_timestamp_seconds: 0,
                maturity_e8s_equivalent: 10 * E8,
                staked_maturity_e8s_equivalent: Some(10 * E8),
                dissolve_state: Some(api::neuron::DissolveState::DissolveDelaySeconds(
                    VotingPowerEconomics::DEFAULT_NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS,
                )),
                ..Default::default()
            }
        })
        .collect::<Vec<_>>();

    let (driver, mut gov) = governance_with_neurons(&staked_neurons);

    for i in 1..=(current_peak - 1) {
        let neuron_id = NeuronId::from_u64(i);
        // Split
        let controller = gov
            .neuron_store
            .with_neuron(&neuron_id, |neuron| neuron.controller())
            .unwrap();
        gov.split_neuron(
            &neuron_id,
            &controller,
            &Split {
                amount_e8s: 5 * E8,
                memo: None,
            },
        )
        .now_or_never()
        .unwrap()
        .unwrap();

        // spawn should not be rate limited
        let controller = gov
            .neuron_store
            .with_neuron(&neuron_id, |neuron| neuron.controller())
            .unwrap();
        gov.spawn_neuron(
            &neuron_id,
            &controller,
            &Spawn {
                new_controller: Some(PrincipalId::new_user_test_id(i * 2)),
                nonce: None,
                percentage_to_spawn: Some(40),
            },
        )
        .expect(
            "We unexpectedly hit the rate limit, which means spawn is not exempt from the limits. \
                This is an error.",
        );
    }

    // Claim our first neuron... should affect limits
    let controller = PrincipalId::new_user_test_id(100);
    let nonce = 0;
    let amount_e8s = 10 * E8;
    let new_neuron_subaccount = ledger::compute_neuron_staking_subaccount(controller, nonce);
    let driver = driver.with_ledger_accounts(vec![fake::FakeAccount {
        id: AccountIdentifier::new(
            ic_base_types::PrincipalId::from(GOVERNANCE_CANISTER_ID),
            Some(new_neuron_subaccount),
        ),
        amount_e8s,
    }]);

    let result: ManageNeuronResponse = claim_neuron_by_memo(&mut gov, controller, nonce);
    result.panic_if_error("Could not claim neuron!");

    // Claim another neuron, which should then fail
    let controller = PrincipalId::new_user_test_id(101);
    let nonce = 1;
    let new_neuron_subaccount = ledger::compute_neuron_staking_subaccount(controller, nonce);
    let mut driver = driver.with_ledger_accounts(vec![fake::FakeAccount {
        id: AccountIdentifier::new(
            ic_base_types::PrincipalId::from(GOVERNANCE_CANISTER_ID),
            Some(new_neuron_subaccount),
        ),
        amount_e8s,
    }]);

    let result: ManageNeuronResponse = claim_neuron_by_memo(&mut gov, controller, nonce);

    match result.command {
        Some(CommandResponse::Error(e)) => {
            assert_eq!(
                e,
                api::GovernanceError::new_with_message(
                    api::governance_error::ErrorType::Unavailable,
                    "Reached maximum number of neurons that can be created in this hour. \
                        Please wait and try again later."
                )
            )
        }
        r => panic!("We did not get a rate limited response!, {r:?}"),
    }
```
