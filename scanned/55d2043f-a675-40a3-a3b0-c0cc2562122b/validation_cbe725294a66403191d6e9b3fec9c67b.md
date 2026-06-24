### Title
Any Unprivileged Principal Can Trigger SNS Neuron Claim for an Arbitrary Controller - (`rs/sns/governance/src/governance.rs`)

### Summary
The SNS Governance `manage_neuron` / `ClaimOrRefresh` endpoint allows any unprivileged caller to supply an arbitrary `controller` principal in the `MemoAndController` field. The neuron is then created with that controller as its owner, not the caller. Because the neuron ID is deterministically derived from `(controller, memo)`, an observer who sees a pending staking transfer can race to call `manage_neuron` with the victim's principal and the observed memo, claiming the neuron first. The neuron is correctly assigned to the intended controller, but the attacker can exploit the race to grief the victim (e.g., by consuming the neuron slot before the victim's own claim arrives, or by triggering the claim at an unfavorable time).

### Finding Description
The SNS Governance canister exposes `manage_neuron` as an open update call. When the command is `ClaimOrRefresh { by: MemoAndController { memo, controller: Some(victim) } }`, the governance canister:

1. Computes the neuron ID deterministically as `NeuronId::from(compute_neuron_staking_subaccount_bytes(controller, memo))`.
2. If no neuron with that ID exists, calls `claim_neuron(nid, &controller)`.
3. Creates the neuron with `controller` as the owner and grants it `neuron_claimer_permissions`.

There is **no check that the caller equals the controller**. Any principal can trigger the claim for any other principal's staking subaccount.

The relevant code path in `rs/sns/governance/src/governance.rs`:

```rust
async fn claim_or_refresh_neuron_by_memo_and_controller(
    &mut self,
    caller: &PrincipalId,
    memo_and_controller: &MemoAndController,
) -> Result<(), GovernanceError> {
    let controller = memo_and_controller.controller.unwrap_or(*caller);
    // caller is never checked against controller for new claims
    let nid = NeuronId::from(ledger::compute_neuron_staking_subaccount_bytes(
        controller, memo,
    ));
    match self.get_neuron_result(&nid) {
        Ok(neuron) => self.refresh_neuron(&nid).await,
        Err(_) => self.claim_neuron(nid, &controller).await,
    }
}
```

The NNS Governance canister has the same open-claim design but explicitly tests and documents that "a non-controller can claim a neuron for the controller" (`test_claim_neuron_memo_and_controller_by_proxy`). The SNS Governance canister inherits this design.

The staking subaccount is public information: it is `SHA256(0x0c || "neuron-stake" || controller_bytes || memo_be_bytes)`. An attacker who observes a ledger transfer to a governance subaccount (all ledger transactions are public on-chain) can immediately compute the `(controller, memo)` pair and race to call `manage_neuron` with `ClaimOrRefresh { by: MemoAndController { memo, controller: victim } }` before the victim does.

### Impact Explanation
- **Neuron creation timing manipulation**: The attacker can claim the neuron at a moment of their choosing (e.g., immediately after the ledger transfer confirms, before the victim's own claim), setting `aging_since_timestamp_seconds` and `created_timestamp_seconds` to an attacker-chosen block time. This affects the neuron's age-based voting power bonus.
- **Dissolve delay reset**: The neuron is created with `DissolveDelaySeconds(0)`. If the victim intended to immediately set a dissolve delay after claiming, the attacker's early claim forces the victim to issue a separate transaction.
- **Griefing / DoS on claim**: If the attacker's claim transaction is processed first, the victim's own `ClaimOrRefresh` call will hit the `refresh_neuron` path instead of `claim_neuron`, which is benign in the normal case but can be used to force a ledger balance query at an attacker-chosen time.
- **No fund loss**: The neuron is always assigned to the correct `controller`; the attacker cannot steal the staked tokens or take ownership.

### Likelihood Explanation
- All ledger transfers are publicly visible on-chain; the `(controller, memo)` pair is trivially computable from the transfer destination subaccount.
- The `manage_neuron` endpoint is open to any ingress sender with no rate limit on `ClaimOrRefresh`.
- The IC does not have a mempool in the Ethereum sense, but an attacker monitoring the ledger canister's certified state can detect a completed transfer and submit a claim in the next round, racing the victim's own claim.
- The attack requires no privileged access, no key compromise, and no governance majority.
- Likelihood is **medium**: it requires active monitoring and precise timing, but is fully within the capability of an unprivileged on-chain observer.

### Recommendation
1. **Require caller == controller for new claims**: In `claim_or_refresh_neuron_by_memo_and_controller`, when the neuron does not yet exist (i.e., the `claim_neuron` branch), verify that `caller == controller`. Proxy claims (where `caller != controller`) should only be permitted for `refresh_neuron` (stake top-up), not for initial creation.
2. **Alternatively, allow proxy claims but document the design**: If proxy claiming is intentional (as it is in NNS Governance), document it explicitly and ensure users understand that anyone can trigger their neuron claim once the staking transfer is visible on-chain.

### Proof of Concept
1. Victim `V` transfers `N` SNS tokens to `governance_canister[subaccount = SHA256("neuron-stake" || V || memo)]`.
2. Attacker `A` observes the ledger transfer (public certified state).
3. `A` calls SNS Governance `manage_neuron` with:
   ```
   ManageNeuron {
     subaccount: <victim's subaccount>,
     command: ClaimOrRefresh {
       by: MemoAndController { memo: <victim's memo>, controller: Some(<V>) }
     }
   }
   ```
4. SNS Governance executes `claim_neuron(nid, &V)` — neuron is created with `V` as controller, `created_timestamp_seconds = now_attacker_chosen`.
5. Victim `V` later calls the same `manage_neuron`; it now hits `refresh_neuron` instead of `claim_neuron`.

The neuron belongs to `V`, but its creation timestamp and initial dissolve delay were set at attacker-chosen timing. The test `test_claim_neuron_memo_and_controller_by_proxy` in `rs/sns/integration_tests/src/neuron.rs` explicitly demonstrates that `user2` can successfully claim a neuron on behalf of `user1`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4210-4227)
```rust
    async fn claim_or_refresh_neuron_by_memo_and_controller(
        &mut self,
        caller: &PrincipalId,
        memo_and_controller: &MemoAndController,
    ) -> Result<(), GovernanceError> {
        let controller = memo_and_controller.controller.unwrap_or(*caller);
        let memo = memo_and_controller.memo;
        let nid = NeuronId::from(ledger::compute_neuron_staking_subaccount_bytes(
            controller, memo,
        ));
        match self.get_neuron_result(&nid) {
            Ok(neuron) => {
                let nid = neuron.id.as_ref().expect("Neuron must have an id").clone();
                self.refresh_neuron(&nid).await
            }
            Err(_) => self.claim_neuron(nid, &controller).await,
        }
    }
```

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

**File:** rs/sns/integration_tests/src/neuron.rs (L1741-1778)
```rust
        // user2 claims the neuron that user1 staked
        let claim_response: ManageNeuronResponse = sns_canisters
            .governance
            .update_from_sender(
                "manage_neuron",
                candid_one,
                ManageNeuron {
                    subaccount: to_subaccount.to_vec(),
                    command: Some(Command::ClaimOrRefresh(ClaimOrRefresh {
                        by: Some(By::MemoAndController(MemoAndController {
                            memo: nonce,
                            controller: Some(user1.get_principal_id()),
                        })),
                    })),
                },
                &user2,
            )
            .await
            .expect("Error calling the manage_neuron api.");

        let neuron_id = match claim_response.command.unwrap() {
            CommandResponse::ClaimOrRefresh(response) => response.refreshed_neuron_id.unwrap(),
            CommandResponse::Error(error) => panic!(
                "Unexpected error when claiming neuron for user {}: {}",
                user1.get_principal_id(),
                error
            ),
            _ => panic!(
                "Unexpected command response when claiming neuron for user {}.",
                user1.get_principal_id()
            ),
        };

        let neuron = sns_canisters.get_neuron(&neuron_id).await;

        let expected_permissions = vec![NeuronPermission::new(
            &user1.get_principal_id(),
            NeuronPermissionType::all(),
```

**File:** rs/nervous_system/common/src/ledger.rs (L4-14)
```rust
/// Computes the bytes of the subaccount to which neuron staking transfers are made. This
/// function must be kept in sync with the Nervous System UI equivalent.
pub fn compute_neuron_staking_subaccount_bytes(controller: PrincipalId, nonce: u64) -> [u8; 32] {
    compute_neuron_domain_subaccount_bytes(controller, b"neuron-stake", nonce)
}

/// Computes the subaccount to which neuron staking transfers are made. This
/// function must be kept in sync with the Nervous System UI equivalent.
pub fn compute_neuron_staking_subaccount(controller: PrincipalId, nonce: u64) -> IcpSubaccount {
    IcpSubaccount(compute_neuron_staking_subaccount_bytes(controller, nonce))
}
```

**File:** rs/nns/governance/tests/governance.rs (L4818-4826)
```rust
/// Tests that a non-controller can claim a neuron for the controller (the
/// principal whose id was used to build the subaccount).
#[test]
#[cfg_attr(feature = "tla", with_tla_trace_check)]
fn test_claim_neuron_memo_and_controller_by_proxy() {
    let owner = *TEST_NEURON_1_OWNER_PRINCIPAL;
    let caller = *TEST_NEURON_2_OWNER_PRINCIPAL;
    do_test_claim_neuron_by_memo_and_controller(owner, caller);
}
```
