### Title
Double Minting of SNS Tokens via Unremoved Disbursement Entry After Successful Transfer - (File: `rs/sns/governance/src/governance.rs`)

### Summary
In the SNS governance canister, `maybe_finalize_disburse_maturity` mints SNS tokens to a neuron owner's account via `transfer_funds`. If the transfer succeeds but the subsequent `get_neuron_result_mut` call fails (returning `Err`), the code logs an error and `continue`s the loop **without removing** the `DisburseMaturityInProgress` entry. The `is_finalizing_disburse_maturity` flag is then unconditionally reset to `None` at the end of the function, allowing the next periodic invocation to process the same disbursement again and mint the same tokens a second time.

### Finding Description

`maybe_finalize_disburse_maturity` in `rs/sns/governance/src/governance.rs` collects all ready disbursements before any async calls, then iterates over them:

```rust
let transfer_result = self.ledger.transfer_funds(...).await;
match transfer_result {
    Ok(block_index) => {
        let neuron = match self.get_neuron_result_mut(&neuron_id) {
            Ok(neuron) => neuron,
            Err(e) => {
                log!(ERROR, "Failed updating DisburseMaturityInProgress-entry ...");
                continue;   // ← transfer succeeded, but entry NOT removed
            }
        };
        neuron.disburse_maturity_in_progress.remove(0);
    }
    Err(e) => { log!(ERROR, ...); }
}
```

After the loop, the guard flag is unconditionally cleared:

```rust
self.proto.is_finalizing_disburse_maturity = None;
``` [1](#0-0) 

Because `is_finalizing_disburse_maturity` is reset to `None` regardless of whether all disbursement entries were properly cleaned up, the next heartbeat/timer invocation of `maybe_finalize_disburse_maturity` will pass the `can_finalize_disburse_maturity()` check and re-process the same entry, issuing a second minting transfer for the same disbursement. [2](#0-1) 

This contrasts sharply with the NNS governance implementation (`rs/nns/governance/src/governance/disburse_maturity.rs`), which correctly **pops** the disbursement entry from the neuron *before* the async ledger call and only pushes it back on failure, making double minting structurally impossible: [3](#0-2) 

The SNS implementation never adopted this safer pattern.

### Impact Explanation

If `get_neuron_result_mut` returns `Err` after a successful `transfer_funds` call (e.g., because another concurrent message removed or invalidated the neuron between the two calls — which is possible on IC since inter-canister call boundaries allow interleaving), the disbursement entry persists in `disburse_maturity_in_progress`. On the next periodic task execution, the same entry is found again, `transfer_funds` is called again, and the same amount of SNS tokens is minted a second time to the same account. This inflates the SNS token supply beyond what is backed by actual maturity, violating ledger conservation.

**Impact: 4** — Unauthorized token inflation; SNS token supply integrity broken.

### Likelihood Explanation

The trigger requires `get_neuron_result_mut` to fail after `transfer_funds` succeeds. On IC, between the `await` on `transfer_funds` and the subsequent synchronous `get_neuron_result_mut` call, other ingress messages or timer callbacks can execute and mutate neuron state. A neuron can be removed from `self.proto.neurons` if it is fully dissolved with zero stake and zero maturity. An attacker who controls a neuron can engineer this: initiate a maturity disbursement, then dissolve and disburse the principal stake to zero out the neuron, timing the removal to coincide with the disbursement callback window. The `is_finalizing_disburse_maturity` flag does not prevent this because it is reset unconditionally at line 5082 after the loop completes normally (without a trap).

**Likelihood: 2** — Requires precise timing of concurrent state mutation, but the window is real and the entry path (calling `disburse_maturity` then dissolving a neuron) is fully unprivileged.

### Recommendation

Adopt the same pattern used by NNS governance: **remove the disbursement entry from `disburse_maturity_in_progress` before the async `transfer_funds` call**, and restore it on failure. This ensures that a successful transfer always results in a removed entry, regardless of what happens after the await returns. Specifically:

1. Pop `disburse_maturity_in_progress[0]` before calling `transfer_funds`.
2. On `transfer_funds` failure, push the entry back to the front.
3. On `transfer_funds` success, do not restore the entry.

This is exactly the pattern in `try_finalize_maturity_disbursement` in the NNS governance canister. [4](#0-3) 

### Proof of Concept

1. Attacker creates an SNS neuron with maturity `M` and calls `disburse_maturity(100%)`. This pushes a `DisburseMaturityInProgress { amount_e8s: M, ... }` entry and reduces `maturity_e8s_equivalent` to 0.
2. Attacker dissolves the neuron and calls `disburse` to withdraw all staked tokens, reducing the neuron's stake to 0. Depending on SNS governance rules, the neuron may now be eligible for removal from `self.proto.neurons`.
3. The periodic task fires `maybe_finalize_disburse_maturity`. It collects the neuron's disbursement entry, acquires the neuron lock, and calls `transfer_funds` — which succeeds, minting `M` tokens to the attacker's account.
4. After the `await`, the attacker's concurrent message (or a prior one) has removed the neuron from `self.proto.neurons`. `get_neuron_result_mut` returns `Err`. The code logs an error and `continue`s — the `DisburseMaturityInProgress` entry is **not removed**.
5. `is_finalizing_disburse_maturity` is reset to `None` at line 5082.
6. The next periodic task invocation finds the same entry again and calls `transfer_funds` a second time, minting another `M` tokens.
7. The attacker has received `2M` tokens backed by only `M` maturity. [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4935-4935)
```rust
        self.proto.is_finalizing_disburse_maturity = Some(true);
```

**File:** rs/sns/governance/src/governance.rs (L5047-5082)
```rust
            match transfer_result {
                Ok(block_index) => {
                    log!(
                        INFO,
                        "Transferring DisburseMaturityInProgress-entry {:?} for neuron {} at block {}.",
                        disbursement,
                        neuron_id,
                        block_index
                    );
                    let neuron = match self.get_neuron_result_mut(&neuron_id) {
                        Ok(neuron) => neuron,
                        Err(e) => {
                            log!(
                                ERROR,
                                "Failed updating DisburseMaturityInProgress-entry {:?} for neuron {}: {}.",
                                disbursement,
                                neuron_id,
                                e
                            );
                            continue;
                        }
                    };
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
