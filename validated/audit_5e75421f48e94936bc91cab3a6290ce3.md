### Title
SNS Swap Permanent DOS via Single-Participant ICP Target Saturation - (File: rs/sns/swap/src/swap.rs)

### Summary

The SNS swap canister's `can_abort` logic allows any single participant to permanently abort an SNS decentralization swap by contributing the full `max_direct_participation_e8s` amount as the sole (or insufficient-count) participant. The heartbeat automatically transitions the swap to `ABORTED` when `icp_target_reached && !sufficient_participation`. The attacker recovers all contributed ICP via `finalize_swap`, paying only ICP ledger transfer fees. This is a structural analog to M-14: a permissionless state-locking trigger activated by threshold manipulation, where the attacker's net cost is negligible.

---

### Finding Description

The SNS swap lifecycle transitions automatically via the canister heartbeat. The abort condition is evaluated in `can_abort`:

```rust
pub fn can_abort(&self, now_seconds: u64) -> bool {
    if self.lifecycle() != Lifecycle::Open {
        return false;
    }
    (self.swap_due(now_seconds) || self.icp_target_progress().is_reached_or_exceeded())
        && !self.sufficient_participation()
}
``` [1](#0-0) 

`icp_target_progress().is_reached_or_exceeded()` becomes true when `current_direct_participation_e8s >= max_direct_participation_e8s`: [2](#0-1) 

`sufficient_participation` requires both `min_participation_reached()` (i.e., `buyers.len() >= min_participants`) AND `min_direct_participation_icp_e8s_reached()`: [3](#0-2) 

In `refresh_buyer_token_e8s`, the accepted increment is capped at `available_direct_participation_e8s()`, meaning a single participant can consume the entire remaining ICP capacity: [4](#0-3) 

`available_direct_participation_e8s` is simply `max_direct_participation_e8s - current_direct_participation_e8s`: [5](#0-4) 

`finalize_swap` is fully permissionless — any caller can invoke it: [6](#0-5) 

The swap proto explicitly documents that the ABORTED transition happens automatically on the heartbeat when `icp_target_reached && !sufficient_participation`: [7](#0-6) 

**Attack steps:**

1. Attacker transfers `max_direct_participation_e8s` ICP to their swap subaccount on the ICP ledger.
2. Attacker calls `refresh_buyer_tokens` — the swap accepts the full amount, setting `current_direct_participation_e8s = max_direct_participation_e8s`.
3. `icp_target_progress().is_reached_or_exceeded()` is now true. If `buyers.len() < min_participants` (e.g., attacker is the only participant), `sufficient_participation()` is false.
4. The canister heartbeat evaluates `can_abort` → `true` and calls `try_abort`, transitioning the swap to `LIFECYCLE_ABORTED` — a terminal state.
5. Attacker calls `finalize_swap` (permissionless). The sweep refunds all ICP to buyers, including the attacker.
6. Net attacker cost: two ICP ledger transfer fees (~0.0002 ICP total).

The SNS launch is permanently killed. The SNS governance, root, and ledger canisters remain deployed but the decentralization swap cannot be re-opened.

---

### Impact Explanation

The SNS swap is a one-shot mechanism. Once aborted, the swap is in a terminal state and cannot be re-opened. The SNS project fails to decentralize, the dapp control is returned to the fallback controllers, and all legitimate participants receive refunds but lose the opportunity to participate in the SNS launch. This is a permanent, irreversible denial-of-service against the SNS decentralization process — matching the "vault locked up" impact in M-14.

---

### Likelihood Explanation

The attack requires the attacker to hold `max_direct_participation_e8s` ICP transiently (it is fully recovered). For SNS swaps with lower caps (e.g., 50,000–500,000 ICP), this is feasible for a well-funded attacker or a competitor. The attacker's permanent cost is only two ICP transfer fees (~0.0002 ICP). No privileged access, no governance majority, and no cryptographic capability is required. The entry path is a standard ingress `update` call to `refresh_buyer_tokens` followed by `finalize_swap`, both callable by any unprivileged principal. [8](#0-7) 

---

### Recommendation

1. **Enforce a minimum time window before abort-on-target-reached**: Do not allow `icp_target_reached && !sufficient_participation` to trigger an immediate abort. Require the swap to remain open for at least `min_participant_window_seconds` after the ICP target is first reached, giving legitimate participants time to join.

2. **Cap single-participant contribution relative to `max_direct_participation_e8s`**: Prevent any single buyer from contributing more than `max_direct_participation_e8s / min_participants` in the early phase of the swap, so no single actor can saturate the ICP target alone.

3. **Permissioned abort trigger**: Rather than relying solely on the heartbeat, require that the abort-on-target-reached path be confirmed by the NNS governance canister or the SNS root canister, analogous to the M-14 recommendation to permission the emergency settlement function.

---

### Proof of Concept

```
Preconditions:
  - SNS swap is OPEN
  - min_participants = 100
  - max_direct_participation_e8s = 500_000 * E8
  - current_direct_participation_e8s = 0
  - buyers.len() = 0

Step 1: Attacker transfers 500_000 ICP to swap subaccount on ICP ledger.

Step 2: Attacker calls refresh_buyer_tokens({ buyer: attacker_principal })
  → available_direct_participation_e8s() = 500_000 * E8
  → actual_increment_e8s = min(500_000*E8, 500_000*E8) = 500_000*E8
  → buyers[attacker] = BuyerState { amount_e8s: 500_000*E8 }
  → current_direct_participation_e8s = 500_000*E8 = max_direct_participation_e8s
  → icp_target_progress() = Reached(500_000*E8)

Step 3: Heartbeat fires → can_abort(now) evaluated:
  → icp_target_progress().is_reached_or_exceeded() = true
  → sufficient_participation() = min_participation_reached() && ...
    → buyers.len() = 1 < min_participants = 100  → false
  → can_abort = true → try_abort() → lifecycle = ABORTED

Step 4: Attacker calls finalize_swap({})
  → sweep_icp: transfers 500_000 ICP back to attacker (minus 0.0001 ICP fee)
  → SNS launch permanently killed

Net attacker cost: ~0.0002 ICP in transfer fees.
``` [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/swap/src/swap.rs (L522-535)
```rust
    pub fn available_direct_participation_e8s(&self) -> u64 {
        let max_direct_participation_e8s = self.max_direct_participation_e8s();
        let current_direct_participation_e8s = self.current_direct_participation_e8s();
        max_direct_participation_e8s
            .checked_sub(current_direct_participation_e8s)
            .unwrap_or_else(|| {
                log!(
                    ERROR,
                    "max_direct_participation_e8s ({max_direct_participation_e8s}) \
                    < current_direct_participation_e8s ({current_direct_participation_e8s})"
                );
                0
            })
    }
```

**File:** rs/sns/swap/src/swap.rs (L1177-1224)
```rust
        let max_increment_e8s = self.available_direct_participation_e8s();

        // Check that the maximum number of participants has not been reached yet.
        {
            let num_direct_participants = self.buyers.len() as u64;
            let num_sns_neurons_per_basket = params
                .neuron_basket_construction_parameters
                .as_ref()
                .expect("neuron_basket_construction_parameters must be specified")
                .count;
            if (num_direct_participants + 1) * num_sns_neurons_per_basket
                > MAX_NEURONS_FOR_DIRECT_PARTICIPANTS
            {
                return Err(format!(
                    "The swap has reached the maximum number of direct participants ({num_direct_participants}) and does \
                     not accept new participants; existing participants may still increase their \
                     ICP participation amount. This constraint ensures that SNS neuron baskets can \
                     be created for all existing participants (SNS neuron basket size: {num_sns_neurons_per_basket}, \
                     MAX_NEURONS_FOR_DIRECT_PARTICIPANTS: {MAX_NEURONS_FOR_DIRECT_PARTICIPANTS}).",
                ));
            }
        }

        // Check that the minimum amount has been transferred before
        // actually creating an entry for the buyer.
        if e8s < params.min_participant_icp_e8s {
            return Err(format!(
                "Amount transferred: {}; minimum required to participate: {}",
                e8s, params.min_participant_icp_e8s
            ));
        }
        let max_participant_icp_e8s = params.max_participant_icp_e8s;

        let old_amount_icp_e8s = self
            .buyers
            .get(&buyer.to_string())
            .map_or(0, |buyer| buyer.amount_icp_e8s());

        if old_amount_icp_e8s >= e8s {
            // Already up-to-date. Strict inequality can happen if messages are re-ordered.
            return Ok(RefreshBuyerTokensResponse {
                icp_accepted_participation_e8s: old_amount_icp_e8s,
                icp_ledger_account_balance_e8s: e8s,
            });
        }
        // Subtraction safe because of the preceding if-statement.
        let requested_increment_e8s = e8s - old_amount_icp_e8s;
        let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
```

**File:** rs/sns/swap/src/swap.rs (L1500-1533)
```rust
    pub async fn finalize(
        &mut self,
        now_fn: fn(bool) -> u64,
        environment: &mut impl CanisterEnvironment,
    ) -> FinalizeSwapResponse {
        // Acquire the lock or return a FinalizeSwapResponse with an error message.
        if let Err(error_message) = self.lock_finalize_swap() {
            return FinalizeSwapResponse::with_error(error_message);
        }

        // The lock is now acquired and asynchronous calls to finalize are blocked.
        // Perform all subactions.
        let finalize_swap_response = self.finalize_inner(now_fn, environment).await;

        if finalize_swap_response.has_error_message() {
            log!(
                ERROR,
                "The swap did not finalize successfully. \n\
                finalize_swap_response: {finalize_swap_response:?}"
            );
        } else {
            log!(
                INFO,
                "The swap finalized successfully. \n\
                finalize_swap_response: {finalize_swap_response:?}"
            );
        }

        // Release the lock. Note, if there is a panic, the lock will
        // not be released. In that case, the Swap canister will need
        // to be upgraded to release the lock.
        self.unlock_finalize_swap();

        finalize_swap_response
```

**File:** rs/sns/swap/src/swap.rs (L2796-2836)
```rust
    pub fn sufficient_participation(&self) -> bool {
        self.min_participation_reached() && self.min_direct_participation_icp_e8s_reached()
    }

    /// The minimum number of participants have been achieved.
    pub fn min_participation_reached(&self) -> bool {
        if let (Some(params), Some(init)) = (&self.params, &self.init) {
            if init.neurons_fund_participation.is_some() {
                // Only count direct participants for determining swap's success.
                // Note that a valid Swap Init should either have `neurons_fund_participation` or
                // `cf_participants`, but not both at the same time; here, we defensively perform
                // the check again anyway.
                if !self.cf_participants.is_empty() {
                    log!(
                        ERROR,
                        "Inconsistent Swap Init: cf_participants has {} elements (starting with \
                        {:?}) while neurons_fund_participation is set.",
                        self.cf_participants.len(),
                        self.cf_participants[0],
                    );
                }
                (self.buyers.len() as u32) >= params.min_participants
            } else {
                (self.cf_participants.len().saturating_add(self.buyers.len()) as u32)
                    >= params.min_participants
            }
        } else {
            false
        }
    }

    pub fn min_direct_participation_icp_e8s_reached(&self) -> bool {
        if let Some(params) = &self.params {
            let Some(min_direct_participation_icp_e8s) = params.min_direct_participation_icp_e8s
            else {
                return false;
            };
            return self.current_direct_participation_e8s() >= min_direct_participation_icp_e8s;
        }
        false
    }
```

**File:** rs/sns/swap/src/swap.rs (L2840-2858)
```rust
    pub fn icp_target_progress(&self) -> IcpTargetProgress {
        if self.params.is_some() {
            let current_direct_participation_e8s = self.current_direct_participation_e8s();
            let max_direct_participation_e8s = self.max_direct_participation_e8s();
            match current_direct_participation_e8s.cmp(&max_direct_participation_e8s) {
                Ordering::Less => IcpTargetProgress::NotReached {
                    current_direct_participation_e8s,
                    max_direct_participation_e8s,
                },
                Ordering::Greater => IcpTargetProgress::Exceeded {
                    current_direct_participation_e8s,
                    max_direct_participation_e8s,
                },
                Ordering::Equal => IcpTargetProgress::Reached(max_direct_participation_e8s),
            }
        } else {
            IcpTargetProgress::Undefined
        }
    }
```

**File:** rs/sns/swap/src/swap.rs (L2899-2914)
```rust
    /// Returns true if the Swap can be aborted at the specified
    /// timestamp, and false otherwise.
    ///
    /// Conditions:
    /// 1. The lifecycle of Swap is `Lifecycle::Open`
    /// 2. The Swap has ended (either the Swap is due or the maximum ICP target was reached) and there
    ///    has not been sufficient participation reached.
    pub fn can_abort(&self, now_seconds: u64) -> bool {
        if self.lifecycle() != Lifecycle::Open {
            return false;
        }

        // if the swap is due or the ICP target is reached without sufficient participation, we can abort
        (self.swap_due(now_seconds) || self.icp_target_progress().is_reached_or_exceeded())
            && !self.sufficient_participation()
    }
```

**File:** rs/sns/swap/canister/canister.rs (L127-143)
```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    log!(INFO, "refresh_buyer_tokens");
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
    };
    let icp_ledger = create_real_icp_ledger(swap().init_or_panic().icp_ledger_or_panic());
    match swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
    {
        Ok(r) => r,
        Err(msg) => panic!("{}", msg),
    }
}
```

**File:** rs/sns/swap/canister/canister.rs (L150-159)
```rust
#[update]
async fn finalize_swap(_arg: FinalizeSwapRequest) -> FinalizeSwapResponse {
    log!(INFO, "finalize_swap");
    let mut clients = swap()
        .init_or_panic()
        .environment()
        .expect("unable to create canister clients");

    swap_mut().finalize(now_fn, &mut clients).await
}
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L56-66)
```text
//
// ```text
//                                                                     sufficient_participation
//                                                                     && (swap_due || icp_target_reached)
// PENDING -------------------> ADOPTED ---------------------> OPEN -----------------------------------------> COMMITTED
//         Swap receives a request        The opening delay      |                                                |
//         from NNS governance to         has elapsed            | not sufficient_participation                   |
//         schedule opening                                      | && (swap_due || icp_target_reached)            |
//                                                               v                                                v
//                                                            ABORTED ---------------------------------------> <DELETED>
// ```
```
