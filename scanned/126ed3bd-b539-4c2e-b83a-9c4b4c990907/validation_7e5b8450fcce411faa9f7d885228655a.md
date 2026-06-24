### Title
Canister-Controlled Neurons Enable Tokenized Voting Power Accumulation Without Restriction - (File: rs/nns/governance/src/governance/create_neuron.rs)

---

### Summary

The NNS governance canister imposes no restriction on the type of principal that can control a neuron. Any principal — including a canister — can stake ICP, claim a neuron, and accumulate governance voting power. Because a canister's behavior is fully programmable, a malicious canister can issue wrapper tokens to depositors, making the locked ICP effectively liquid while concentrating voting power under a single operator's control. This is the direct IC analog of the AuraLocker M-10 finding.

---

### Finding Description

The `create_neuron` function in `rs/nns/governance/src/governance/create_neuron.rs` accepts an arbitrary `PrincipalId` as the neuron controller with no check on whether it is a user (self-authenticating) or a canister (opaque) principal:

```rust
let controller = controller.unwrap_or(caller);
``` [1](#0-0) 

The same is true of the legacy `claim_neuron` path in `rs/nns/governance/src/governance.rs`, which accepts any `PrincipalId controller` argument and passes it directly to `NeuronBuilder::new`:

```rust
async fn claim_neuron(
    &mut self,
    subaccount: Subaccount,
    controller: PrincipalId,
    ...
) -> Result<NeuronId, GovernanceError> {
    ...
    let neuron = NeuronBuilder::new(nid, subaccount, controller, ...)
``` [2](#0-1) 

This was an explicit design change made in June 2024, removing the prior `is_self_authenticating` requirement. The test that documents this change confirms the current behavior:

```rust
/// It used to be that controllers must be self-authenticating. Later (Jun, 2024) we got rid of that
/// requirement. That is, the controller can be any type of principal (including canister).
``` [3](#0-2) 

There is no whitelist, blacklist, or any other mechanism in either `create_neuron` or `claim_neuron` to restrict canister principals from becoming neuron controllers. The `NeuronBuilder` and `add_neuron` path used by `create_neuron` perform no such check either. [4](#0-3) 

A neuron's controller has full authority over it: it can vote, set following, add hotkeys, and disburse. The `is_authorized_to_vote` check only verifies that the caller is the controller or a registered hotkey — it does not distinguish between canister and user principals: [5](#0-4) 

---

### Impact Explanation

A malicious canister operator can:

1. Deploy a "wrapper" canister that accepts ICP deposits from users.
2. Have the canister call `create_neuron` (or `claim_neuron`) with itself as the controller, locking the ICP in NNS neurons.
3. Issue ICRC-1 wrapper tokens (e.g., "wICP") to depositors, representing their proportional share of the locked ICP. These tokens are freely tradeable on DEXes, making the locked ICP effectively liquid.
4. Attract additional users by offering better short-term incentives (liquidity, yield, etc.) compared to direct NNS staking.
5. Accumulate a disproportionate share of NNS voting power.
6. Unilaterally direct the canister to vote on all NNS proposals, dictating protocol upgrades, treasury decisions, and subnet configuration.

The locked ICP itself is not stolen, but the governance integrity of the NNS is compromised: a single operator controls a large bloc of voting power that was contributed by many independent users who believed they were participating in decentralized governance.

---

### Likelihood Explanation

The attack is realistic and requires no privileged access:

- Any unprivileged developer can deploy a canister on the IC.
- The IC explicitly permits canisters to be neuron controllers (the restriction was removed in June 2024).
- ICRC-1 token issuance is a standard, well-documented pattern on the IC.
- Users seeking liquidity for locked ICP have a clear economic incentive to use such a wrapper.
- There is no on-chain mechanism (whitelist, blacklist, or EOA check) to prevent this.

The only mitigating factor is that users must voluntarily choose to deposit through the malicious canister rather than staking directly, which is why the severity is medium rather than high — matching the AuraLocker finding's final severity.

---

### Recommendation

Introduce a restriction on the type of principal that can control a neuron, analogous to the mitigation applied to AuraLocker. Concretely:

1. **Preferred**: Require that neuron controllers be self-authenticating principals (i.e., user keys). Canister principals (`is_self_authenticating()` returns `false` for opaque/canister IDs) would be rejected at `create_neuron` and `claim_neuron` time.
2. **Alternative**: Implement a governance-controlled whitelist of canister principals that are explicitly permitted to hold neurons (e.g., the NNS root canister, the Neurons' Fund canister, etc.), rejecting all other canister principals.

The check should be added in `create_neuron.rs` immediately after the controller is resolved:

```rust
let controller = controller.unwrap_or(caller);
// Reject canister controllers unless whitelisted.
if !controller.is_self_authenticating() && !is_whitelisted_canister(controller) {
    return Err(GovernanceError::new_with_message(
        ErrorType::NotAuthorized,
        "Neuron controller must be a self-authenticating principal or a whitelisted canister.",
    ));
}
``` [6](#0-5) 

The same guard should be applied in `claim_neuron`: [7](#0-6) 

---

### Proof of Concept

1. Attacker deploys `WrapperCanister` on the IC. The canister exposes:
   - `deposit(amount_e8s)` — accepts ICP via ICRC-2 `transfer_from` and mints wrapper tokens to the caller.
   - `vote(proposal_id, vote)` — callable only by the canister's controller; directs all held neurons to vote.

2. `WrapperCanister` calls `create_neuron` on the NNS governance canister with `controller = WrapperCanister.principal_id()` and the deposited ICP as stake. This succeeds because `create_neuron` imposes no restriction on the controller type. [8](#0-7) 

3. `WrapperCanister` issues ICRC-1 wrapper tokens to each depositor proportional to their ICP contribution. These tokens trade freely on DEXes, giving depositors liquidity while their ICP remains locked.

4. Attacker advertises higher APY (e.g., from MEV or protocol fees) to attract large ICP deposits. Over time, `WrapperCanister` accumulates neurons representing a significant fraction of total NNS voting power.

5. Attacker calls `WrapperCanister.vote(proposal_id, Yes)` to cast votes on all NNS proposals with the accumulated voting power, effectively controlling NNS governance outcomes — including protocol upgrades, subnet additions, and treasury disbursements.

### Citations

**File:** rs/nns/governance/src/governance/create_neuron.rs (L23-27)
```rust
    pub async fn create_neuron(
        governance: &'static LocalKey<RefCell<Self>>,
        caller: PrincipalId,
        request: CreateNeuronRequest,
    ) -> Result<CreatedNeuron, GovernanceError> {
```

**File:** rs/nns/governance/src/governance/create_neuron.rs (L143-152)
```rust
        let controller = controller.unwrap_or(caller);
        if amount_e8s < neuron_minimum_stake_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Amount {amount_e8s} e8s is less than the minimum stake \
                    {neuron_minimum_stake_e8s} e8s for a neuron"
                ),
            ));
        }
```

**File:** rs/nns/governance/src/governance/create_neuron.rs (L186-199)
```rust
        // Step 1: Add new neuron with 0 stake. We do this before the transfer to ensure that a
        // neuron can be created.
        let neuron = NeuronBuilder::new(
            neuron_id,
            neuron_subaccount,
            controller,
            dissolve_state_and_age,
            now_seconds,
        )
        .with_followees(followees)
        .with_kyc_verified(true)
        .with_auto_stake_maturity(auto_stake_maturity)
        .build();
        governance.with_borrow_mut(|g| g.add_neuron(neuron.id().id, neuron.clone()))?;
```

**File:** rs/nns/governance/src/governance.rs (L5857-5870)
```rust
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

**File:** rs/nns/governance/src/governance.rs (L5985-6012)
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

        let nid = self.neuron_store.new_neuron_id(&mut *self.randomness)?;
        let now = self.env.now();
        let neuron = NeuronBuilder::new(
            nid,
            subaccount,
            controller,
            DissolveStateAndAge::NotDissolving {
                dissolve_delay_seconds: INITIAL_NEURON_DISSOLVE_DELAY,
                aging_since_timestamp_seconds: now,
            },
            now,
        )
        .with_followees(self.heap_data.default_followees.clone())
        .with_kyc_verified(true)
        .build();
```

**File:** rs/nns/governance/tests/governance.rs (L6503-6570)
```rust
/// It used to be that controllers must be self-authenticating. Later (Jun, 2024) we got rid of that
/// requirement. That is, the controller can be any type of principal (including canister).
/// Discussed here:
/// https://forum.dfinity.org/t/reevaluating-neuron-control-restrictions/28597
#[tokio::test]
async fn test_neuron_with_non_self_authenticating_controller_is_now_allowed() {
    // Step 1: Prepare the world.

    let controller = PrincipalId::new_user_test_id(42);
    assert!(!controller.is_self_authenticating(), "{controller:?}");

    let memo = 43;
    let neuron_subaccount = Subaccount(compute_neuron_staking_subaccount_bytes(controller, memo));

    let amount_e8s = 10 * E8;

    // Step 1.1: Initialize ledger with 10 ICP in the (governance) subaccount where
    // (non-self-authenticating) controller will claim new a neuron.
    let driver = fake::FakeDriver::default()
        .at(56)
        .with_ledger_accounts(vec![fake::FakeAccount {
            id: AccountIdentifier::new(
                ic_base_types::PrincipalId::from(GOVERNANCE_CANISTER_ID),
                Some(neuron_subaccount),
            ),
            amount_e8s,
        }])
        .with_supply(Tokens::from_tokens(400_000_000).unwrap());

    // Step 1.2: Construct Governance.
    let mut gov = Governance::new(
        empty_fixture(),
        driver.get_fake_env(),
        driver.get_fake_ledger(),
        driver.get_fake_cmc(),
        driver.get_fake_randomness_generator(),
    );

    // Step 2: Call code under test.

    let claim_or_refresh = manage_neuron::Command::ClaimOrRefresh(ClaimOrRefresh {
        by: Some(By::Memo(memo)),
    });
    let manage_neuron = ManageNeuron {
        id: None,
        neuron_id_or_subaccount: None,
        command: Some(claim_or_refresh),
    };
    let caller = controller;
    let result: ManageNeuronResponse = gov.manage_neuron(&caller, &manage_neuron).await;

    // Step 3: Inspect result(s).

    // Step 3.1: Assert that a plausible neuron ID was returned.
    let manage_neuron_response::Command::ClaimOrRefresh(manage_neuron_response) =
        result.command.as_ref().unwrap()
    else {
        panic!("{result:#?}");
    };
    let Some(neuron_id) = manage_neuron_response.refreshed_neuron_id else {
        panic!("{result:#?}");
    };
    assert!(neuron_id.id > 0, "{result:#?}");

    // Step 3.2: Inspect the new neuron's controller.
    let neuron = gov.get_full_neuron(&neuron_id, &caller).unwrap();
    assert_eq!(neuron.controller.unwrap(), controller, "{:#?}", neuron);
}
```

**File:** rs/nns/governance/src/neuron/types.rs (L239-256)
```rust
    /// Returns true if and only if `principal` is authorized to
    /// perform non-privileged operations, like vote and follow,
    /// on behalf of this neuron, i.e., if `principal` is either the
    /// controller or one of the authorized hot keys.
    pub(crate) fn is_authorized_to_vote(&self, principal: &PrincipalId) -> bool {
        self.is_hotkey_or_controller(principal)
    }

    /// Returns true if and only if `principal` is authorized to
    /// call simulate_manage_neuron requests on this neuron
    pub(crate) fn is_authorized_to_simulate_manage_neuron(&self, principal: &PrincipalId) -> bool {
        self.is_hotkey_or_controller(principal)
    }

    /// Returns true if and only if `principal` is either the controller or a hotkey
    fn is_hotkey_or_controller(&self, principal: &PrincipalId) -> bool {
        self.is_controlled_by(principal) || self.hot_keys.contains(principal)
    }
```
