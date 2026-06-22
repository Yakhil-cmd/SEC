### Title
Any Caller Can Increase Stake of a Non-KYC-Verified Neuron, Permanently Freezing Funds - (File: rs/nns/governance/src/governance.rs)

---

### Summary

The NNS Governance canister's `ClaimOrRefresh` command allows any unprivileged ingress caller to increase the staked ICP of a neuron they do not own, without checking the neuron owner's KYC verification status. Because `disburse_neuron` and `disburse_to_neuron` both gate disbursement on `kyc_verified == true`, a third party can permanently lock additional ICP inside a non-KYC-verified neuron by sending ICP to its subaccount and calling `ClaimOrRefresh`.

---

### Finding Description

The NNS Governance canister enforces asymmetric authorization across neuron operations. Sensitive operations that move funds out of a neuron — `disburse_neuron` and `disburse_to_neuron` — require the caller to be the neuron controller **and** require `kyc_verified == true` on the neuron: [1](#0-0) [2](#0-1) 

However, the `refresh_neuron` function — which increases a neuron's `cached_neuron_stake_e8s` by reading the on-chain ledger balance — accepts **no `caller` parameter** and performs **no ownership check and no KYC check**: [3](#0-2) 

This function is reachable by any ingress caller through `manage_neuron_internal` via the `ClaimOrRefresh` command, which is explicitly handled before any neuron-existence or authorization checks: [4](#0-3) 

Both `By::MemoAndController` and `By::NeuronIdOrSubaccount` variants route to `refresh_neuron` without any caller validation: [5](#0-4) 

The existing test suite explicitly documents and validates this behavior as intentional — a proxy caller (different from the neuron owner) can successfully refresh and increase the stake of another principal's neuron: [6](#0-5) [7](#0-6) 

---

### Impact Explanation

A neuron with `kyc_verified = false` (applicable to unverified genesis neurons) cannot have its stake disbursed: [8](#0-7) 

An attacker who sends ICP to such a neuron's subaccount and calls `ClaimOrRefresh` causes `refresh_neuron` to update `cached_neuron_stake_e8s` to the new higher balance. Those additional ICP are then permanently frozen: the neuron owner cannot disburse them (blocked by the KYC check), and no other mechanism exists to recover funds from a neuron subaccount once the stake is recorded. The attacker loses the ICP they sent, but the victim's neuron accumulates irrecoverable locked funds.

---

### Likelihood Explanation

The neuron subaccount is deterministically computable from the controller principal and memo nonce — both of which are public information derivable from on-chain transaction history. Any ingress sender can call `manage_neuron` with `ClaimOrRefresh` without any special privilege. The only cost to the attacker is the ICP transferred to the neuron subaccount (which is lost). Genesis neurons with `kyc_verified = false` are the affected population; newly claimed neurons are created with `kyc_verified = true` by default: [9](#0-8) 

---

### Recommendation

Two mitigations mirror the options in the external report:

1. **Restrict `ClaimOrRefresh` (refresh path) to the neuron controller only.** Pass `caller` into `refresh_neuron` and reject calls where `caller` is not the neuron's controller, consistent with how `disburse_neuron` and `disburse_to_neuron` enforce ownership.

2. **Check `kyc_verified` inside `refresh_neuron` before updating the cached stake.** If the neuron is not KYC-verified, reject the stake increase. This mirrors the KYC gate already present on all fund-movement operations.

Option 1 is the stronger fix and aligns with the principle of least privilege already applied to all other sensitive neuron operations.

---

### Proof of Concept

```
// Attacker knows: neuron_controller (public), memo (public from ledger history)
// Step 1: Compute the neuron's subaccount deterministically
let subaccount = ledger::compute_neuron_staking_subaccount(neuron_controller, memo);
let account = neuron_subaccount(subaccount);

// Step 2: Transfer ICP to the neuron's subaccount from attacker's account
// (standard ICP ledger transfer — no special permission required)
icp_ledger.transfer(attacker_account, account, amount_e8s).await;

// Step 3: Call manage_neuron as any principal (attacker != neuron_controller)
governance.manage_neuron(
    &attacker_principal,
    &ManageNeuron {
        neuron_id_or_subaccount: Some(NeuronIdOrSubaccount::NeuronId(target_neuron_id)),
        id: None,
        command: Some(Command::ClaimOrRefresh(ClaimOrRefresh {
            by: Some(By::NeuronIdOrSubaccount(Empty {})),
        })),
    },
).await;
// refresh_neuron() is called with no caller check and no kyc_verified check.
// cached_neuron_stake_e8s is now increased.
// The neuron owner (kyc_verified=false) still cannot disburse — funds are frozen.
``` [3](#0-2) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance.rs (L1970-1995)
```rust
        if !is_neuron_controlled_by_caller {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                format!(
                    "Caller '{:?}' is not authorized to control neuron '{}'.",
                    caller, id.id
                ),
            ));
        }

        if neuron_state != NeuronState::Dissolved {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Neuron {} has NOT been dissolved. It is in state {:?}",
                    id.id, neuron_state
                ),
            ));
        }

        if !is_neuron_kyc_verified {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Neuron {} is not kyc verified.", id.id),
            ));
        }
```

**File:** rs/nns/governance/src/governance.rs (L2900-2954)
```rust
        if !parent_neuron.is_controlled_by(caller) {
            return Err(GovernanceError::new(ErrorType::NotAuthorized));
        }

        let min_stake = economics.neuron_minimum_stake_e8s;
        if disburse_to_neuron.amount_e8s < min_stake + transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Called `disburse_to_neuron` with `amount` argument {} e8s. This is too little: \
                      at the minimum, one needs the minimum neuron stake, which is {} e8s, \
                      plus the transaction fee, which is {}. Hence the minimum disburse amount is {}.",
                    disburse_to_neuron.amount_e8s,
                    min_stake,
                    transaction_fee_e8s,
                    min_stake + transaction_fee_e8s
                ),
            ));
        }

        if parent_neuron.minted_stake_e8s()
            < economics.neuron_minimum_stake_e8s + disburse_to_neuron.amount_e8s
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Trying to disburse {} e8s out of neuron {}. \
                     This is not allowed, because the parent has stake {} e8s. \
                     If the requested amount was subtracted from it, there would be less than \
                     the minimum allowed stake, which is {} e8s. ",
                    disburse_to_neuron.amount_e8s,
                    parent_nid.id,
                    parent_neuron.minted_stake_e8s(),
                    min_stake
                ),
            ));
        }

        let state = parent_neuron.state(created_timestamp_seconds);
        if state != NeuronState::Dissolved {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Neuron {} has NOT been dissolved. It is in state {:?}",
                    id.id, state
                ),
            ));
        }

        if !parent_neuron.kyc_verified {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Neuron is not kyc verified: {}", id.id),
            ));
        }
```

**File:** rs/nns/governance/src/governance.rs (L5852-5896)
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
    }

    /// Refreshes the neuron, getting both it's id and subaccount, if only one
    /// of them was provided as argument.
    async fn refresh_neuron_by_id_or_subaccount(
        &mut self,
        id: NeuronIdOrSubaccount,
        claim_or_refresh: &ClaimOrRefresh,
    ) -> Result<NeuronId, GovernanceError> {
        let (nid, subaccount) = match id {
            NeuronIdOrSubaccount::NeuronId(neuron_id) => {
                let neuron_subaccount =
                    self.with_neuron(&neuron_id, |neuron| neuron.subaccount())?;
                (neuron_id, neuron_subaccount)
            }
            NeuronIdOrSubaccount::Subaccount(subaccount_bytes) => {
                let subaccount = Self::bytes_to_subaccount(&subaccount_bytes)?;
                let neuron_id = self
                    .neuron_store
                    .get_neuron_id_for_subaccount(subaccount)
                    .ok_or_else(|| Self::no_neuron_for_subaccount_error(&subaccount.0))?;
                (neuron_id, subaccount)
            }
        };
        self.refresh_neuron(nid, subaccount, claim_or_refresh).await
    }
```

**File:** rs/nns/governance/src/governance.rs (L5900-5962)
```rust
    async fn refresh_neuron(
        &mut self,
        nid: NeuronId,
        subaccount: Subaccount,
        claim_or_refresh: &ClaimOrRefresh,
    ) -> Result<NeuronId, GovernanceError> {
        let account = neuron_subaccount(subaccount);
        // We need to lock the neuron to make sure it doesn't undergo
        // concurrent changes while we're checking the balance and
        // refreshing the stake.
        let now = self.env.now();
        let _neuron_lock = self.lock_neuron_for_command(
            nid.id,
            NeuronInFlightCommand {
                timestamp: now,
                command: Some(InFlightCommand::ClaimOrRefreshNeuron(
                    claim_or_refresh.clone(),
                )),
            },
        )?;

        // Get the balance of the neuron from the ledger canister.
        tla_log_locals! { neuron_id: nid.id };
        let balance = self.ledger.account_balance(account).await?;
        let min_stake = self.economics().neuron_minimum_stake_e8s;
        if balance.get_e8s() < min_stake {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Account does not have enough funds to refresh a neuron. \
                     Please make sure that account has at least {:?} e8s (was {:?} e8s)",
                    min_stake,
                    balance.get_e8s()
                ),
            ));
        }
        self.with_neuron_mut(&nid, |neuron| {
            match neuron.cached_neuron_stake_e8s.cmp(&balance.get_e8s()) {
                Ordering::Greater => {
                    println!(
                        "{}ERROR. Neuron cached stake was inconsistent.\
                     Neuron account: {} has less e8s: {} than the cached neuron stake: {}.\
                     Stake adjusted.",
                        LOG_PREFIX,
                        account,
                        balance.get_e8s(),
                        neuron.cached_neuron_stake_e8s
                    );
                    neuron.update_stake_adjust_age(balance.get_e8s(), now);
                }
                Ordering::Less => {
                    neuron.update_stake_adjust_age(balance.get_e8s(), now);
                }
                // If the stake is the same as the account balance,
                // just return the neuron id (this way this method
                // also serves the purpose of allowing to discover the
                // neuron id based on the memo and the controller).
                Ordering::Equal => (),
            };
        })?;

        Ok(nid)
    }
```

**File:** rs/nns/governance/src/governance.rs (L6010-6012)
```rust
        .with_followees(self.heap_data.default_followees.clone())
        .with_kyc_verified(true)
        .build();
```

**File:** rs/nns/governance/src/governance.rs (L6104-6148)
```rust
        // We run claim or refresh before we check whether a neuron exists because it
        // may not in the case of the neuron being claimed
        if let Some(Command::ClaimOrRefresh(claim_or_refresh)) = &mgmt.command {
            // Note that we return here, so none of the rest of this method is executed
            // in this case.
            return match &claim_or_refresh.by {
                Some(By::Memo(memo)) => {
                    let memo_and_controller = MemoAndController {
                        memo: *memo,
                        controller: None,
                    };
                    self.claim_or_refresh_neuron_by_memo_and_controller(
                        caller,
                        memo_and_controller,
                        claim_or_refresh,
                    )
                    .await
                    .map(ManageNeuronResponse::claim_or_refresh_neuron_response)
                }
                Some(By::MemoAndController(memo_and_controller)) => self
                    .claim_or_refresh_neuron_by_memo_and_controller(
                        caller,
                        memo_and_controller.clone(),
                        claim_or_refresh,
                    )
                    .await
                    .map(ManageNeuronResponse::claim_or_refresh_neuron_response),

                Some(By::NeuronIdOrSubaccount(_)) => {
                    let id = mgmt.get_neuron_id_or_subaccount()?.ok_or_else(|| {
                        GovernanceError::new_with_message(
                            ErrorType::NotFound,
                            "No neuron ID specified in the management request.",
                        )
                    })?;
                    self.refresh_neuron_by_id_or_subaccount(id, claim_or_refresh)
                        .await
                        .map(ManageNeuronResponse::claim_or_refresh_neuron_response)
                }
                None => Err(GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    "Need to provide a way by which to claim or refresh the neuron.",
                )),
            };
        }
```

**File:** rs/nns/governance/tests/governance.rs (L4861-4912)
```rust
fn refresh_neuron_by_memo(owner: PrincipalId, caller: PrincipalId) {
    let stake = Tokens::from_tokens(10_u64).unwrap();
    let memo = Memo(1234_u64);
    let (mut driver, mut gov, nid, subaccount) = governance_with_staked_neuron(
        INITIAL_NEURON_DISSOLVE_DELAY,
        stake.get_e8s(),
        0,
        owner,
        memo.0,
    );

    let neuron = gov.neuron_store.with_neuron(&nid, |n| n.clone()).unwrap();
    assert_eq!(neuron.cached_neuron_stake_e8s, stake.get_e8s());

    driver.add_funds_to_account(
        AccountIdentifier::new(GOVERNANCE_CANISTER_ID.get(), Some(subaccount)),
        stake.get_e8s(),
    );

    // stake shouldn't have changed.
    let neuron = gov.neuron_store.with_neuron(&nid, |n| n.clone()).unwrap();
    assert_eq!(neuron.cached_neuron_stake_e8s, stake.get_e8s());

    let manage_neuron_response = gov
        .manage_neuron(
            &caller,
            &ManageNeuron {
                neuron_id_or_subaccount: None,
                id: None,
                command: Some(Command::ClaimOrRefresh(ClaimOrRefresh {
                    by: Some(By::MemoAndController(MemoAndController {
                        memo: memo.0,
                        controller: Some(owner),
                    })),
                })),
            },
        )
        .now_or_never()
        .unwrap();

    let nid = match manage_neuron_response.command.unwrap() {
        CommandResponse::ClaimOrRefresh(response) => response.refreshed_neuron_id,
        CommandResponse::Error(error) => panic!("Error claiming neuron: {error:?}"),
        _ => panic!("Invalid response."),
    };

    assert!(nid.is_some());
    let nid = nid.unwrap();
    let neuron = gov.neuron_store.with_neuron(&nid, |n| n.clone()).unwrap();
    assert_eq!(neuron.controller(), owner);
    assert_eq!(neuron.cached_neuron_stake_e8s, stake.get_e8s() * 2);
}
```

**File:** rs/nns/governance/tests/governance.rs (L4922-4928)
```rust
/// Tests that a neuron can be refreshed by memo by proxy.
#[test]
#[cfg_attr(feature = "tla", with_tla_trace_check)]
fn test_refresh_neuron_by_memo_by_proxy() {
    let owner = *TEST_NEURON_1_OWNER_PRINCIPAL;
    let caller = *TEST_NEURON_2_OWNER_PRINCIPAL;
    refresh_neuron_by_memo(owner, caller);
```
