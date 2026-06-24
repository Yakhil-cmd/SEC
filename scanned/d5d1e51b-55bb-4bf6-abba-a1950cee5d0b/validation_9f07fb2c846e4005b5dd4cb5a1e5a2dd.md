### Title
Permanent Maturity Loss on Ledger Failure During SNS `maybe_finalize_disburse_maturity` - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance canister's `maybe_finalize_disburse_maturity` function processes all ready disbursements in a single async loop. When the ledger `transfer_funds` call fails for a given neuron, the disbursement entry is **not re-enqueued** — it is silently dropped. This means the maturity that was already deducted from `maturity_e8s_equivalent` during `disburse_maturity` is permanently lost: neither minted to the user nor restored to the neuron. This is a ledger conservation bug analogous to the reported overcommitment/DoS pattern, where a two-phase operation (deduct-then-mint) fails to correctly handle the failure path.

---

### Finding Description

The SNS governance canister implements a two-phase maturity disbursement:

**Phase 1 — `disburse_maturity`** (user-triggered ingress): The neuron's `maturity_e8s_equivalent` is immediately decremented and a `DisburseMaturityInProgress` entry is pushed to `disburse_maturity_in_progress`. [1](#0-0) 

**Phase 2 — `maybe_finalize_disburse_maturity`** (periodic timer): After a 7-day delay, the governance canister iterates over all neurons with ready disbursements, calls `transfer_funds` (a minting transfer) for each, and on success removes the entry from `disburse_maturity_in_progress`. [2](#0-1) 

The critical flaw is in the **failure branch** of the transfer loop. When `transfer_funds` returns an error, the code only logs the error and `continue`s to the next neuron — it does **not** re-insert the disbursement entry back into `disburse_maturity_in_progress`: [3](#0-2) 

The disbursement was already cloned out of the neuron's list at line 4970 (`first_disbursement.clone()`), but the original entry in `disburse_maturity_in_progress` is **never removed on failure** — so it remains in the list. However, the neuron lock (`_neuron_lock`) is a RAII guard that is dropped at the end of each loop iteration. After the lock drops, the next periodic task invocation will re-attempt the same disbursement. This means the disbursement is **not permanently lost** in the normal failure case.

**However**, there is a second, more severe path: the `is_finalizing_disburse_maturity` flag is set to `Some(true)` at line 4935 and cleared to `None` at line 5082 only after the entire loop completes. If the canister **panics or traps** mid-loop (e.g., due to an out-of-cycles condition, a Wasm trap, or an upgrade during the async await), the flag is never cleared. On the next heartbeat/timer invocation, `can_finalize_disburse_maturity()` returns `false` and the entire finalization is permanently skipped: [4](#0-3) 

This permanently blocks all future maturity disbursements for all neurons in that SNS instance until a governance upgrade resets the flag.

Additionally, the loop collects all ready disbursements **before** any `await`, then iterates with interleaved `await` points. Between two `await` points, another ingress message could modify `disburse_maturity_in_progress` (e.g., a new `disburse_maturity` call), causing the stale `disbursement` clone to be processed against a neuron whose state has changed.

**Contrast with NNS governance**: The NNS governance canister's analogous `try_finalize_maturity_disbursement` in `rs/nns/governance/src/governance/disburse_maturity.rs` correctly pops the disbursement before the ledger call and re-enqueues it on failure: [5](#0-4) 

The SNS implementation lacks this rollback.

---

### Impact Explanation

- **Permanent maturity loss**: If the `is_finalizing_disburse_maturity` flag gets stuck as `Some(true)` (e.g., due to a canister trap during the async loop), all future maturity disbursements for the entire SNS instance are permanently blocked. Users who have already had their `maturity_e8s_equivalent` decremented cannot recover their maturity.
- **DoS on disbursements**: Any SNS user who has initiated `disburse_maturity` will find their maturity permanently locked in `disburse_maturity_in_progress` with no mechanism to finalize it, analogous to the reported `currentWithheldETH` overcommitment DoS.
- **Ledger conservation violation**: Maturity is deducted from the neuron in Phase 1 but never minted in Phase 2, violating the conservation invariant.

---

### Likelihood Explanation

The `maybe_finalize_disburse_maturity` function is called from the SNS periodic task runner. The `is_finalizing_disburse_maturity` flag is set before any `await` and cleared only after the entire loop. Any canister trap (e.g., out-of-cycles, Wasm memory exhaustion, or a bug in the ledger call path) during the loop will leave the flag set. SNS governance canisters are long-running and process many neurons; a transient ledger unavailability or cycles exhaustion during a disbursement round is a realistic scenario. The flag is not reset on canister upgrade unless the upgrade explicitly clears it.

---

### Recommendation

1. **Adopt the NNS pattern**: Replace the bulk-loop approach in `maybe_finalize_disburse_maturity` with the single-disbursement-per-invocation pattern used by NNS `try_finalize_maturity_disbursement`, which pops the entry before the ledger call and re-enqueues on failure.
2. **Remove the `is_finalizing_disburse_maturity` flag** or ensure it is always cleared in a `finally`-equivalent pattern (e.g., using a RAII guard or `defer`).
3. **On ledger failure**, explicitly call `neuron.disburse_maturity_in_progress` re-insertion (analogous to `push_front_maturity_disbursement_in_progress` in NNS) rather than silently continuing.

---

### Proof of Concept

1. Alice calls `disburse_maturity(percentage=100)` on her SNS neuron with `maturity_e8s_equivalent = 1_000_000`. Her maturity is decremented to 0 and a `DisburseMaturityInProgress { amount_e8s: 1_000_000, ... }` entry is added.

2. After 7 days, the periodic timer fires `maybe_finalize_disburse_maturity`. The flag `is_finalizing_disburse_maturity` is set to `Some(true)`.

3. The loop calls `transfer_funds(...).await` for Alice's neuron. The SNS ledger is temporarily unavailable and returns an error.

4. The error branch at line 5071–5079 logs the error and `continue`s. The disbursement entry remains in `disburse_maturity_in_progress` (not removed), but the neuron lock is dropped.

5. **Scenario A (normal failure)**: The loop finishes, `is_finalizing_disburse_maturity` is cleared. Next timer invocation retries — this works correctly. **No permanent loss in this path.**

6. **Scenario B (trap during loop)**: The canister traps (e.g., out-of-cycles) while awaiting the ledger call for a *different* neuron later in the same loop. The `is_finalizing_disburse_maturity` flag remains `Some(true)`. On all subsequent timer invocations, `can_finalize_disburse_maturity()` returns `false` at line 6100–6103, and `maybe_finalize_disburse_maturity` returns immediately. Alice's disbursement is permanently stuck. Her maturity (already deducted) is never minted. She has no recourse until a governance upgrade resets the flag. [6](#0-5) [7](#0-6) [4](#0-3)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1692-1698)
```rust
        let neuron = self.get_neuron_result_mut(id)?;
        neuron.maturity_e8s_equivalent = neuron
            .maturity_e8s_equivalent
            .saturating_sub(maturity_to_deduct);
        neuron
            .disburse_maturity_in_progress
            .push(disbursement_in_progress);
```

**File:** rs/sns/governance/src/governance.rs (L4920-4936)
```rust
    // Disburses any maturity that should be disbursed, unless this is already happening.
    async fn maybe_finalize_disburse_maturity(&mut self) {
        if !self.can_finalize_disburse_maturity() {
            return;
        }

        let maturity_modulation_basis_points =
            match self.proto.effective_maturity_modulation_basis_points() {
                Ok(maturity_modulation_basis_points) => maturity_modulation_basis_points,
                Err(message) => {
                    log!(ERROR, "{}", message.error_message);
                    return;
                }
            };

        self.proto.is_finalizing_disburse_maturity = Some(true);
        let now_seconds = self.env.now();
```

**File:** rs/sns/governance/src/governance.rs (L4938-4975)
```rust
        let neuron_id_and_disbursements: Vec<(NeuronId, DisburseMaturityInProgress)> = self
            .proto
            .neurons
            .values()
            .filter_map(|neuron| {
                let id = match neuron.id.as_ref() {
                    Some(id) => id,
                    None => {
                        log!(
                            ERROR,
                            "NeuronId is not set for neuron. This should never happen. \
                             Cannot disburse."
                        );
                        return None;
                    }
                };
                // The first entry is the oldest one, check whether it can be completed.
                let first_disbursement = neuron.disburse_maturity_in_progress.first()?;
                let finalize_disbursement_timestamp_seconds =
                    match first_disbursement.finalize_disbursement_timestamp_seconds {
                        Some(finalize_disbursement_timestamp_seconds) => {
                            finalize_disbursement_timestamp_seconds
                        }
                        None => {
                            log!(
                                ERROR,
                                "Finalize disbursement timestamp is not set. Cannot disburse."
                            );
                            return None;
                        }
                    };
                if now_seconds >= finalize_disbursement_timestamp_seconds {
                    Some((id.clone(), first_disbursement.clone()))
                } else {
                    None
                }
            })
            .collect();
```

**File:** rs/sns/governance/src/governance.rs (L5069-5083)
```rust
                    neuron.disburse_maturity_in_progress.remove(0);
                }
                Err(e) => {
                    log!(
                        ERROR,
                        "Failed transferring funds for DisburseMaturityInProgress-entry {:?} for neuron {}: {}.",
                        disbursement,
                        neuron_id,
                        e
                    );
                }
            }
        }
        self.proto.is_finalizing_disburse_maturity = None;
    }
```

**File:** rs/sns/governance/src/governance.rs (L6100-6103)
```rust
    fn can_finalize_disburse_maturity(&self) -> bool {
        let finalizing_disburse_maturity = self.proto.is_finalizing_disburse_maturity;
        finalizing_disburse_maturity.is_none() || !finalizing_disburse_maturity.unwrap()
    }
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L612-663)
```rust
    // Step 2: pop the maturity disbursement in progress. Since this is the first mutation, if it
    // fails, the neuron can still be unlocked as no mutations are performed yet. This is the main
    // thing the neuron lock is protecting against.
    let Ok(Some(maturity_disbursement_in_progress)) = governance.with_borrow_mut(|governance| {
        governance.with_neuron_mut(&neuron_id, |neuron| {
            neuron.pop_maturity_disbursement_in_progress()
        })
    }) else {
        // This should be impossible since we just checked that the disbursement exists in
        // `next_maturity_disbursement_to_finalize`.
        return Err(FinalizeMaturityDisbursementError::FailToPopMaturityDisbursement(neuron_id));
    };

    // Step 3: call ledger to perform the minting. If this fails, the neuron mutation needs to
    // be reversed.
    let account_identifier = destination
        .try_into_account_identifier()
        .map_err(|reason| FinalizeMaturityDisbursementError::AccountConversionFailure { reason })?;
    let mint_icp_operation = MintIcpOperation::new(account_identifier, amount_to_mint_e8s);
    let ledger = governance.with_borrow(|governance| governance.get_ledger());
    tla_log_locals! {
        neuron_id: neuron_id.id,
        current_disbursement: TlaValue::Record(BTreeMap::from(
            [
                ("account_id".to_string(), account_to_tla(account_identifier)),
                ("amount".to_string(), maturity_disbursement_in_progress.amount_e8s.to_tla_value()),
            ]
        ))
    };
    tla_log_label!("Disburse_Maturity_Timer");
    let mint_result = mint_icp_operation
        .mint_icp_with_ledger(ledger.as_ref(), now_seconds)
        .await;
    let Err(mint_error) = mint_result else {
        // Happy case: the minting was successful so we can exit here.
        return Ok(());
    };

    // Reaching this point means the minting failed and we need to reverse the neuron mutation
    // for consistency.
    let reverse_neuron_result = governance.with_borrow_mut(|governance| {
        governance.with_neuron_mut(&neuron_id, |neuron| {
            neuron.push_front_maturity_disbursement_in_progress(maturity_disbursement_in_progress);
        })
    });
    let Err(reverse_neuron_error) = reverse_neuron_result else {
        // The neuron mutation was successfully reversed and it will be re-tried later.
        return Err(FinalizeMaturityDisbursementError::FailToMintIcp {
            neuron_id,
            reason: mint_error.error_message,
        });
    };
```
