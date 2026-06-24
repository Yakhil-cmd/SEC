### Title
SNS Swap `finalize_swap_in_progress` Lock Not Released on Panic — Permanent Token Lock - (File: rs/sns/swap/src/swap.rs)

### Summary

The SNS Swap canister's `finalize` function acquires a boolean lock (`finalize_swap_in_progress`) before executing `finalize_inner`, which makes multiple sequential inter-canister calls across message boundaries. The lock is only released via an explicit `unlock_finalize_swap()` call at the end of `finalize`. The code itself acknowledges that a panic in `finalize_inner` will leave the lock permanently set. Because `finalize_inner` contains reachable `panic!`/`.expect()` call sites that execute *after* await points (i.e., after the lock state has been committed to stable storage), a panic in any of those sites permanently prevents all future `finalize_swap` calls, locking all ICP and SNS tokens in the swap canister with no trustless recovery path.

### Finding Description

`finalize` in `rs/sns/swap/src/swap.rs` acquires the lock and then delegates to `finalize_inner`:

```rust
// Release the lock. Note, if there is a panic, the lock will
// not be released. In that case, the Swap canister will need
// to be upgraded to release the lock.
self.unlock_finalize_swap();
``` [1](#0-0) 

`finalize_inner` makes at least five sequential inter-canister calls across await points:

1. `sweep_icp` → ICP ledger
2. `settle_neurons_fund_participation` → NNS governance
3. `sweep_sns` → SNS ledger
4. `claim_swap_neurons` → SNS governance
5. `set_sns_governance_to_normal_mode` → SNS governance [2](#0-1) 

Each `await` is a message boundary. The IC execution model commits state at each boundary. The lock (`finalize_swap_in_progress = Some(true)`) is committed to stable state before the first await. If any subsequent message execution panics, that message's local changes are rolled back, but the lock — committed in an earlier message — is **not** rolled back.

Inside `claim_swap_neurons`, which executes after three prior await points, there are explicit `.expect()` calls that can panic:

```rust
let neuron_id = neuron_recipe.neuron_id.clone().expect(
    "NeuronRecipe.neuron_id is always set by \
    SnsNeuronRecipe::to_neuron_recipe",
);
``` [3](#0-2) 

Additionally, `init.nns_governance_or_panic()` and `init.sns_transaction_fee_e8s_or_panic()` are called unconditionally after the `init_and_validate()` guard: [4](#0-3) 

The `finalize_swap_in_progress` field is stored in the `Swap` protobuf state: [5](#0-4) 

Once the lock is stuck at `true`, every subsequent call to `finalize` returns immediately with:

```
"The Swap canister has finalize_swap call already in progress"
``` [6](#0-5) 

The proto-level comment acknowledges the only recovery is a canister upgrade: [7](#0-6) 

### Impact Explanation

If the lock becomes permanently stuck:

- All ICP contributed by buyers (held in the swap canister's ICP ledger subaccounts) cannot be swept to the SNS governance treasury or refunded.
- All SNS tokens pre-loaded into the swap canister cannot be distributed to participants.
- The SNS governance canister remains in `PreInitializationSwap` mode indefinitely, preventing normal governance operations.
- The only recovery path is a canister upgrade with a post-upgrade hook — but upgrading the SNS Swap canister requires SNS governance approval, which itself may be blocked because governance is stuck in `PreInitializationSwap` mode.

This is a direct analog to the Loihi/Shell protocol bug: a single unexpected failure in one external dependency (here, a panic triggered by malformed state or an unexpected external response) permanently bricks the entire swap finalization flow and locks all participant funds.

### Likelihood Explanation

The `finalize_swap` endpoint is publicly callable by any unprivileged user (`#[update]` with no access control). The panic paths inside `claim_swap_neurons` are reachable if:

- `SnsNeuronRecipe::to_neuron_recipe` returns a `NeuronRecipe` with `neuron_id = None` due to a bug or unexpected state (the `.expect()` at line 1680 would fire).
- The SNS governance canister returns a response that causes a Candid decode panic inside the CDK (before the `Result` is returned to the caller).
- Any future code change introduces a new `unwrap()`/`expect()` inside `finalize_inner` without a corresponding RAII lock guard.

The acknowledged comment in the source code ("if there is a panic, the lock will not be released") confirms the developers are aware of the risk but have not mitigated it structurally.

### Recommendation

Replace the manual lock/unlock pattern with a Rust RAII guard that releases the lock on `Drop`, ensuring the lock is always released regardless of whether `finalize_inner` returns normally or panics:

```rust
struct FinalizeLockGuard<'a>(&'a mut Swap);
impl Drop for FinalizeLockGuard<'_> {
    fn drop(&mut self) { self.0.finalize_swap_in_progress = Some(false); }
}
```

Acquire the guard before calling `finalize_inner` and let it go out of scope naturally. This eliminates the entire class of lock-stuck-on-panic bugs without requiring any changes to `finalize_inner` itself.

Additionally, audit all `.expect()` and `_or_panic()` calls inside `finalize_inner` and its callees and replace them with graceful error returns that set `error_message` and return early, consistent with the existing error-handling pattern already used in `sweep_icp` and `settle_neurons_fund_participation`.

### Proof of Concept

1. A committed SNS swap exists with `neuron_recipes` containing at least one recipe whose `to_neuron_recipe()` produces a `NeuronRecipe` with `neuron_id = None` (e.g., due to a missing `NeuronId` field in the recipe's `investor` variant).
2. Any user calls `finalize_swap({})`.
3. `finalize` acquires the lock: `finalize_swap_in_progress = Some(true)` — state committed.
4. `sweep_icp` completes (await #1, state committed with lock=true).
5. `settle_neurons_fund_participation` completes (await #2).
6. `sweep_sns` completes (await #3).
7. `claim_swap_neurons` is entered; the loop reaches the recipe with `neuron_id = None`; `.expect(...)` fires — **panic**.
8. The IC rolls back the state changes from message #4 (the `claim_swap_neurons` message), but the lock set in message #1 is **not** rolled back.
9. All subsequent calls to `finalize_swap` return `"The Swap canister has finalize_swap call already in progress"`.
10. All ICP and SNS tokens are permanently locked. Recovery requires an SNS governance upgrade proposal, which may itself be blocked. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1444-1451)
```rust
    pub fn lock_finalize_swap(&mut self) -> Result<(), String> {
        match self.is_finalize_swap_locked() {
            true => Err("The Swap canister has finalize_swap call already in progress".to_string()),
            false => {
                self.finalize_swap_in_progress = Some(true);
                Ok(())
            }
        }
```

**File:** rs/sns/swap/src/swap.rs (L1500-1534)
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
    }
```

**File:** rs/sns/swap/src/swap.rs (L1544-1543)
```rust

```

**File:** rs/sns/swap/src/swap.rs (L1556-1612)
```rust
        // Transfer the ICP tokens from the Swap canister.
        finalize_swap_response
            .set_sweep_icp_result(self.sweep_icp(now_fn, environment.icp_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        // Settle the Neurons' Fund participation in the token swap.
        finalize_swap_response.set_settle_neurons_fund_participation_result(
            self.settle_neurons_fund_participation(environment.nns_governance_mut())
                .await,
        );
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        if self.should_restore_dapp_control() {
            // Restore controllers of dapp canisters to their original
            // owners (i.e. self.init.fallback_controller_principal_ids).
            finalize_swap_response.set_set_dapp_controllers_result(
                self.restore_dapp_controllers_for_finalize(environment.sns_root_mut())
                    .await,
            );

            // In the case of returning control of the dapp(s) to the fallback
            // controllers, finalize() need not do any more work, so always return
            // and end execution.
            return finalize_swap_response;
        }

        // Create the SnsNeuronRecipes based on the contribution of direct and NF participants
        finalize_swap_response
            .set_create_sns_neuron_recipes_result(self.create_sns_neuron_recipes());
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        // Transfer the SNS tokens from the Swap canister.
        finalize_swap_response
            .set_sweep_sns_result(self.sweep_sns(now_fn, environment.sns_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        // Once SNS tokens have been distributed to the correct accounts, claim
        // them as neurons on behalf of the Swap participants.
        finalize_swap_response.set_claim_neuron_result(
            self.claim_swap_neurons(environment.sns_governance_mut())
                .await,
        );
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        finalize_swap_response.set_set_mode_call_result(
            Self::set_sns_governance_to_normal_mode(environment.sns_governance_mut()).await,
        );
```

**File:** rs/sns/swap/src/swap.rs (L1634-1715)
```rust
    pub async fn claim_swap_neurons(
        &mut self,
        sns_governance_client: &mut impl SnsGovernanceClient,
    ) -> SweepResult {
        if self.lifecycle() != Lifecycle::Committed {
            log!(
                ERROR,
                "Halting claim_neurons(). SNS Neurons cannot be distributed if \
                Lifecycle is not COMMITTED. Current Lifecycle: {:?}",
                self.lifecycle()
            );
            return SweepResult::new_with_global_failures(1);
        }

        let init = match self.init_and_validate() {
            Ok(init) => init,
            Err(error_message) => {
                log!(
                    ERROR,
                    "Halting claim_neurons(). State is missing or corrupted: {:?}",
                    error_message
                );
                return SweepResult::new_with_global_failures(1);
            }
        };

        // The following methods are safe to call since we validated Init in the above block
        let nns_governance = init.nns_governance_or_panic();
        let sns_transaction_fee_e8s = init.transaction_fee_e8s_or_panic();

        let mut sweep_result = SweepResult::default();

        // Create an index of NeuronId -> &mut SnsNeuronRecipe such that the SnsNeuronRecipe can
        // be accessed in O(1) time.
        let mut claimable_neurons_index = btreemap! {};

        // The `NeuronRecipe`s that will be used to create neurons. We are converting
        // `SnsNeuronRecipe`s to a type with a similar name, `NeuronRecipe`, as this is the type
        // expected by the SNS Governance canister.
        let mut neuron_recipes = vec![];

        for recipe in &mut self.neuron_recipes {
            // Here we convert the SnsNeuronRecipe (a Swap concept) to an SnsNeuronRecipe (an SNS
            // Governance concept).
            match recipe.to_neuron_recipe(nns_governance, sns_transaction_fee_e8s) {
                Ok(neuron_recipe) => {
                    let neuron_id = neuron_recipe.neuron_id.clone().expect(
                        "NeuronRecipe.neuron_id is always set by \
                        SnsNeuronRecipe::to_neuron_recipe",
                    );
                    claimable_neurons_index.insert(neuron_id, recipe);
                    neuron_recipes.push(neuron_recipe);
                }
                Err((error_type, error_message)) => {
                    log!(ERROR, "Error creating neuron recipe: {:?}", error_message);
                    match error_type {
                        // In the case of a bug due to programmer error, increment the invalid field.
                        ConversionError::Invalid => sweep_result.invalid += 1,
                        // If we've already processed ths neuron, increment the `skip` field.
                        ConversionError::AlreadyProcessed => sweep_result.skipped += 1,
                    }
                }
            }
        }

        // If neuron_recipes is empty, all recipes are either Invalid or Skipped and there
        // is no work to do.
        if neuron_recipes.is_empty() {
            return sweep_result;
        }

        sweep_result.consume(
            Self::batch_claim_swap_neurons(
                sns_governance_client,
                &mut neuron_recipes,
                &mut claimable_neurons_index,
            )
            .await,
        );

        sweep_result
    }
```

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L191-200)
```rust
    /// A lock stored in Swap state. If set to true, then a finalize_swap
    /// call is in progress. In that case, new finalize_swap calls return
    /// immediately without doing any real work.
    ///
    /// The implementation of the lock should result in the lock being
    /// released when the finalize_swap method returns. If
    /// a lock is not released, upgrades of the Swap canister can
    /// release the lock in the post upgrade hook.
    #[prost(bool, optional, tag = "10")]
    pub finalize_swap_in_progress: ::core::option::Option<bool>,
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L232-240)
```text
  // A lock stored in Swap state. If set to true, then a finalize_swap
  // call is in progress. In that case, new finalize_swap calls return
  // immediately without doing any real work.
  //
  // The implementation of the lock should result in the lock being
  // released when the finalize_swap method returns. If
  // a lock is not released, upgrades of the Swap canister can
  // release the lock in the post upgrade hook.
  optional bool finalize_swap_in_progress = 10;
```
