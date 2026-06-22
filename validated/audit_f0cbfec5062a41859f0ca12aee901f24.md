### Title
Unprivileged SNS Token Holder Can Exhaust `max_number_of_neurons` Quota, DOSing All Neuron Creation — (`rs/sns/governance/src/governance.rs`)

---

### Summary

SNS Governance enforces a hard cap on the total number of neurons via `max_number_of_neurons`. Unlike NNS Governance, SNS Governance has **no rate limiter** on neuron creation. Any unprivileged principal holding SNS tokens can repeatedly call `manage_neuron` → `ClaimOrRefresh` (or `Split`) with the minimum stake to fill the neuron population to the cap, permanently blocking all other users from creating neurons until a governance proposal raises the limit — which itself requires existing neurons to vote.

---

### Finding Description

**Root cause — `check_neuron_population_can_grow`:** [1](#0-0) 

Every call to `add_neuron` (invoked by both `claim_neuron` and `split`) passes through this check. When `self.proto.neurons.len() + 1 > max_number_of_neurons`, the call returns `PreconditionFailed` and no new neuron can be created.

**Root cause — `claim_neuron` has no rate limiter:** [2](#0-1) 

Compare this to NNS Governance's `claim_neuron`, which calls `self.rate_limiter.try_reserve(...)` before proceeding: [3](#0-2) 

NNS enforces `MAX_SUSTAINED_NEURONS_PER_HOUR = 15` and `MAX_NEURON_CREATION_SPIKE = 300`: [4](#0-3) 

SNS has no equivalent. The SNS `claim_neuron` path goes directly from ledger balance check to `add_neuron` with no temporal throttle.

**The global cap:** [5](#0-4) 

The default `max_number_of_neurons` is 200,000 and `neuron_minimum_stake_e8s` defaults to 1 governance token (1e8 e8s): [6](#0-5) 

An SNS can configure `max_number_of_neurons` to a much smaller value (minimum: 1), making the attack cheaper. The existing integration test confirms the DOS is real and reproducible: [7](#0-6) 

---

### Impact Explanation

Once the neuron population cap is reached:
- No new principal can stake and claim an SNS neuron.
- No existing neuron can be split into a child neuron.
- New participants cannot obtain voting power, submit proposals, or participate in governance.
- The only recovery path is a governance proposal to raise `max_number_of_neurons`, but that proposal requires existing neurons to vote — a quorum that may be difficult to reach if the attacker's dust neurons dilute voting power or if legitimate participation is already impaired.

This is a governance-participation DOS matching the external report's impact class: griefing with no profit motive, damage to users and the protocol.

---

### Likelihood Explanation

**Attack cost:** `max_number_of_neurons × neuron_minimum_stake_e8s` SNS tokens. For an SNS that sets `max_number_of_neurons` to a small value (e.g., 1,000) and has a low token price, the cost is trivial. Even at the default 200,000-neuron cap, the attacker's tokens are not burned — they are locked in neurons and can be dissolved after the dissolve delay, making the net cost only opportunity cost and transaction fees.

**No rate limiter:** Unlike NNS, SNS governance imposes no per-hour or per-spike limit on neuron creation. An attacker can fill the cap in a single burst of `manage_neuron` calls.

**Entry path:** Any unprivileged ingress sender holding SNS tokens can call `manage_neuron` with `Command::ClaimOrRefresh`. No special role, hot key, or governance majority is required.

---

### Recommendation

1. **Add a rate limiter to SNS `claim_neuron`** analogous to the NNS `NEURON_RATE_LIMITER_KEY` mechanism, capping the number of neurons that can be created per hour per principal or globally.
2. **Enforce a per-principal neuron count limit** so a single principal cannot hold an unbounded fraction of `max_number_of_neurons`.
3. **Raise the minimum stake floor** relative to the token's market value to increase the economic cost of a dust-neuron attack.

---

### Proof of Concept

**Attacker-controlled entry path:**

```
Attacker (any principal with SNS tokens)
  → manage_neuron { ClaimOrRefresh { MemoAndController { memo: N, controller: attacker } } }
  → SNS Governance::claim_or_refresh_neuron_by_memo_and_controller
  → SNS Governance::claim_neuron          ← no rate limiter here
  → add_neuron
  → check_neuron_population_can_grow      ← increments counter toward cap
```

Repeat with memo = 0, 1, 2, … until `neurons.len() == max_number_of_neurons`.

**Victim's subsequent call:**

```
Victim (any principal)
  → manage_neuron { ClaimOrRefresh { … } }
  → claim_neuron → add_neuron → check_neuron_population_can_grow
  → Err(PreconditionFailed, "Cannot add neuron. Max number of neurons reached.")
```

The existing test at `rs/sns/integration_tests/src/neuron.rs:436` (`test_claim_neuron_fails_when_max_number_of_neurons_is_reached`) already demonstrates this exact failure mode with `max_number_of_neurons = 1`. Scaling to any configured cap requires only proportionally more tokens and `manage_neuron` calls, with no rate-limiting obstacle in the SNS path. [8](#0-7) [1](#0-0) [3](#0-2) [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4319-4360)
```rust
    async fn claim_neuron(
        &mut self,
        neuron_id: NeuronId,
        principal_id: &PrincipalId,
    ) -> Result<(), GovernanceError> {
        let now = self.env.now();

        // We need to create the neuron before checking the balance so that we record
        // the neuron and add it to the set of neurons with ongoing operations. This
        // avoids a race where a user calls this method a second time before the first
        // time responds. If we store the neuron and lock it before we make the call,
        // we know that any concurrent call to mutate the same neuron will need to wait
        // for this one to finish before proceeding.
        let neuron = Neuron {
            id: Some(neuron_id.clone()),
            permissions: vec![NeuronPermission::new(
                principal_id,
                self.neuron_claimer_permissions_or_panic().permissions,
            )],
            cached_neuron_stake_e8s: 0,
            neuron_fees_e8s: 0,
            created_timestamp_seconds: now,
            aging_since_timestamp_seconds: now,
            followees: self.default_followees_or_panic().followees,
            topic_followees: Some(TopicFollowees {
                topic_id_to_followees: btreemap! {},
            }),
            maturity_e8s_equivalent: 0,
            dissolve_state: Some(DissolveState::DissolveDelaySeconds(0)),
            // A neuron created through the `claim_or_refresh` ManageNeuron command will
            // have the default voting power multiplier applied.
            voting_power_percentage_multiplier: DEFAULT_VOTING_POWER_PERCENTAGE_MULTIPLIER,
            source_nns_neuron_id: None,
            staked_maturity_e8s_equivalent: None,
            auto_stake_maturity: None,
            vesting_period_seconds: None,
            disburse_maturity_in_progress: vec![],
        };

        // This also verifies that there are not too many neurons already.
        self.add_neuron(neuron.clone())?;

```

**File:** rs/sns/governance/src/governance.rs (L6363-6379)
```rust
    /// Checks whether new neurons can be added or whether the maximum number of neurons,
    /// as defined in the nervous system parameters, has already been reached.
    fn check_neuron_population_can_grow(&self) -> Result<(), GovernanceError> {
        let max_number_of_neurons = self
            .nervous_system_parameters_or_panic()
            .max_number_of_neurons
            .expect("NervousSystemParameters must have max_number_of_neurons");

        if (self.proto.neurons.len() as u64) + 1 > max_number_of_neurons {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Cannot add neuron. Max number of neurons reached.",
            ));
        }

        Ok(())
    }
```

**File:** rs/nns/governance/src/governance.rs (L228-241)
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

**File:** rs/nns/governance/src/governance.rs (L5985-5996)
```rust
    #[cfg_attr(feature = "tla", tla_update_method(CLAIM_NEURON_DESC.clone(), tla_snapshotter!()))]
    async fn claim_neuron(
        &mut self,
        subaccount: Subaccount,
        controller: PrincipalId,
        claim_or_refresh: &ClaimOrRefresh,
    ) -> Result<NeuronId, GovernanceError> {
        let neuron_limit_reservation = self.rate_limiter.try_reserve(
            self.env.now_system_time(),
            NEURON_RATE_LIMITER_KEY.to_string(),
            1,
        )?;
```

**File:** rs/sns/governance/src/types.rs (L383-386)
```rust
    /// This is an upper bound for `max_number_of_neurons`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    /// See also: `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`.
    pub const MAX_NUMBER_OF_NEURONS_CEILING: u64 = 200_000;
```

**File:** rs/sns/governance/src/types.rs (L469-479)
```rust
    pub fn with_default_values() -> Self {
        Self {
            reject_cost_e8s: Some(E8S_PER_TOKEN), // 1 governance token
            neuron_minimum_stake_e8s: Some(E8S_PER_TOKEN), // 1 governance token
            transaction_fee_e8s: Some(DEFAULT_TRANSFER_FEE.get_e8s()),
            max_proposals_to_keep_per_action: Some(100),
            initial_voting_period_seconds: Some(4 * ONE_DAY_SECONDS), // 4d
            wait_for_quiet_deadline_increase_seconds: Some(ONE_DAY_SECONDS), // 1d
            default_followees: Some(DefaultFollowees::default()),
            max_number_of_neurons: Some(200_000),
            neuron_minimum_dissolve_delay_to_vote_seconds: Some(6 * ONE_MONTH_SECONDS), // 6m
```

**File:** rs/sns/integration_tests/src/neuron.rs (L435-503)
```rust
#[test]
fn test_claim_neuron_fails_when_max_number_of_neurons_is_reached() {
    local_test_on_sns_subnet(|runtime| async move {
        // Set up an SNS with a ledger account for two users
        let user1 = UserInfo::new(Sender::from_keypair(&TEST_USER1_KEYPAIR));
        let user2 = UserInfo::new(Sender::from_keypair(&TEST_USER2_KEYPAIR));
        let alloc = Tokens::from_tokens(1000).unwrap();

        let sys_params = NervousSystemParameters {
            neuron_claimer_permissions: Some(NeuronPermissionList {
                permissions: NeuronPermissionType::all(),
            }),
            max_number_of_neurons: Some(1),
            ..NervousSystemParameters::with_default_values()
        };

        let sns_init_payload = SnsTestsInitPayloadBuilder::new()
            .with_ledger_account(user1.sender.get_principal_id().0.into(), alloc)
            .with_ledger_account(user2.sender.get_principal_id().0.into(), alloc)
            .with_nervous_system_parameters(sys_params.clone())
            .build();

        let sns_canisters = SnsCanisters::set_up(&runtime, sns_init_payload).await;

        // Successfully STAKE and CLAIM user1's neuron, reaching the configured maximum number of
        // neurons allowed in the system
        sns_canisters
            .stake_and_claim_neuron(&user1.sender, Some(ONE_YEAR_SECONDS as u32))
            .await;

        // Only STAKE for user2.
        sns_canisters
            .stake_neuron_account(&user2.sender, &user2.subaccount, 1)
            .await;

        let manage_neuron_command = ManageNeuron {
            subaccount: user2.subaccount.to_vec(),
            command: Some(Command::ClaimOrRefresh(ClaimOrRefresh {
                by: Some(By::MemoAndController(MemoAndController {
                    memo: NONCE,
                    controller: None,
                })),
            })),
        };

        // Try claiming the Neuron for user2 (via memo and controller). This should fail due to
        // the max_number_of_neurons being reached.
        let response: ManageNeuronResponse = sns_canisters
            .governance
            .update_from_sender(
                "manage_neuron",
                candid_one,
                manage_neuron_command.clone(),
                &user2.sender,
            )
            .await
            .expect("Error calling the manage_neuron api.");

        // assert that the error_type is PreconditionFailed.
        let error = match response.command.unwrap() {
            CommandResponse::Error(error) => error,
            CommandResponse::ClaimOrRefresh(_) => {
                panic!(
                    "User should not have been able to claim a neuron due to reaching max_number_of_neurons"
                )
            }
            _ => panic!("Unexpected command response when claiming neuron."),
        };
        assert_eq!(error.error_type, ErrorType::PreconditionFailed as i32);
```
