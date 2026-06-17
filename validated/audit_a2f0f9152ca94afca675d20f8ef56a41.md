### Title
`is_first_tx` Not Updated on Validation Failure Allows Upgrade Transactions to Bypass Position Invariant — (File: `basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs`)

---

### Summary

In the ZK transaction loop (`ZKHeaderStructureTxLoop`), the `is_first_tx` flag is only cleared to `false` when a transaction **successfully** completes and passes block-limit checks. When a transaction fails validation or is rejected due to block limits, `is_first_tx` is never updated. This is the direct analog of the external report's `i == 0` gate: a boolean that is supposed to track "have we processed a real transaction yet" is never advanced past its initial state when the first element(s) fail, causing all subsequent transactions to be incorrectly treated as the first transaction in the block.

---

### Finding Description

In `basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs`, `is_first_tx` is initialized to `true` and is only set to `false` at line 182, deep inside the `else` branch that handles a transaction that both succeeded and passed block-limit checks:

```rust
// line 34
let mut is_first_tx = true;

// ...inside the loop...
match tx_result {
    Err(TxError::Validation(err)) => {
        // Validation failure path — is_first_tx is NEVER updated here
        system.finish_global_frame(Some(&pre_tx_rollback_handle))?;
        result_keeper.tx_processed(Err(err));
    }
    Ok(tx_processing_result) => {
        if let Err(err) = check_for_block_limits(...) {
            // Block-limit failure path — is_first_tx is NEVER updated here
            system.finish_global_frame(Some(&pre_tx_rollback_handle))?;
            result_keeper.tx_processed(Err(err));
        } else {
            // ...
            is_first_tx = false;   // ← only updated on full success
        }
    }
}
``` [1](#0-0) [2](#0-1) [3](#0-2) 

This stale `is_first_tx = true` is then consumed in two critical places:

**1. Upgrade-transaction position check** (`process_transaction.rs` line 29):

```rust
if transaction.is_upgrade() {
    if !is_first_tx {
        Err(TxError::Validation(InvalidTransaction::UpgradeTxNotFirst))
    } else {
        // accepted and executed as an upgrade tx
    }
}
``` [4](#0-3) 

If the first N transactions in a block all fail validation, the (N+1)th transaction still sees `is_first_tx = true`. An upgrade transaction at position N+1 is therefore accepted as if it were the first transaction, bypassing the `UpgradeTxNotFirst` invariant.

**2. Service-block invariant** (`tx_loop.rs` line 131):

```rust
let starts_service_block = check_for_service_block_invariants(
    is_service_block,
    is_first_tx,          // ← stale true
    tx_processing_result.is_service_tx,
    can_start_service_block_after_upgrade,
)?;
``` [5](#0-4) 

Inside `check_for_service_block_invariants`, `starts_service_block = is_service_tx && (is_first_tx || ...)`. With a stale `is_first_tx = true`, a service transaction at position N+1 (after N failed txs) is treated as starting a service block. Any subsequent normal L2 transaction then triggers:

```rust
} else if is_service_block {
    Err(internal_error!("Non-service tx in service block"))
}
``` [6](#0-5) 

This `InternalError` propagates via `?` and causes the **entire block to abort**, reverting all transactions.

**Contrast with the Ethereum loop**, which correctly uses a counter that is always incremented regardless of outcome:

```rust
let tx_result = BasicBootloader::<S, F>::process_transaction::<Config>(
    ...,
    tx_counter == 0,   // ← always advances
    ...
);
// ...
tx_counter += 1;       // ← incremented unconditionally
``` [7](#0-6) 

The ZK loop diverges from this correct pattern.

---

### Impact Explanation

**Impact 1 — Upgrade-tx position invariant bypass (state-transition bug):** A protocol upgrade transaction (`UPGRADE_TX_TYPE = 0x7e`) can be placed at a non-first position in a ZK block, preceded by one or more transactions that fail validation. The bootloader accepts it as "first" and executes it. This violates the protocol invariant that upgrade transactions must be the first accepted transaction in a block, potentially allowing upgrades to be applied in unexpected block positions.

**Impact 2 — Service-block invariant corruption / block-level DoS:** A service transaction at a non-first position (after failed txs) is incorrectly treated as starting a service block. Any subsequent normal L2 transaction causes an `InternalError` that aborts the entire block, reverting all included transactions and their state changes. This is a block-level denial-of-service reachable through oracle/prover input.

---

### Likelihood Explanation

The prover and sequencer control the oracle input that determines block contents. A malicious or buggy sequencer can construct a block containing one or more invalid transactions (e.g., bad signature, insufficient balance — conditions any user can trigger by submitting a malformed L2 transaction) followed by an upgrade or service transaction. Because the `is_first_tx` flag is never advanced past the failed transactions, the invariant check is bypassed. The entry path is through "prover/forward execution input," which is explicitly in scope. Likelihood is medium: it requires a sequencer that either deliberately or accidentally includes failed transactions before upgrade/service transactions.

---

### Recommendation

Set `is_first_tx = false` unconditionally after any transaction is **attempted** (regardless of success or failure), mirroring the Ethereum loop's `tx_counter += 1` pattern. Concretely, move `is_first_tx = false` out of the success-only `else` branch and place it at the end of the outer `Ok((_next_tx_len_bytes, ...))` arm, so it executes after every transaction attempt:

```rust
// After all match tx_result { ... } arms:
is_first_tx = false;
```

This ensures that once any transaction has been attempted (even if rejected), subsequent transactions are never treated as "first."

---

### Proof of Concept

**Scenario A — Upgrade-tx bypass:**
1. Construct a ZK block: `[invalid_L2_tx (bad signature), upgrade_tx]`
2. `invalid_L2_tx` fails with `TxError::Validation(IncorrectFrom)` → `is_first_tx` remains `true`
3. `upgrade_tx` is processed with `is_first_tx = true` → the `UpgradeTxNotFirst` check passes
4. The upgrade transaction executes at block position 1 (not position 0), bypassing the invariant

**Scenario B — Service-block DoS:**
1. Construct a ZK block: `[invalid_L2_tx, service_tx, normal_L2_tx]`
2. `invalid_L2_tx` fails validation → `is_first_tx` remains `true`
3. `service_tx` is processed with `is_first_tx = true` → `starts_service_block = true`, `is_service_block = true`
4. `normal_L2_tx` is processed → `check_for_service_block_invariants` returns `Err(internal_error!("Non-service tx in service block"))`
5. The `?` operator propagates this as a `BootloaderSubsystemError`, aborting the entire block

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L34-34)
```rust
        let mut is_first_tx = true;
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L114-122)
```rust
                        Err(TxError::Validation(err)) => {
                            system_log!(
                                system,
                                "Tx execution result: Validation error = {err:?}\n",
                            );
                            // Revert to state before transaction
                            system.finish_global_frame(Some(&pre_tx_rollback_handle))?;
                            result_keeper.tx_processed(Err(err));
                        }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L131-136)
```rust
                            let starts_service_block = check_for_service_block_invariants(
                                is_service_block,
                                is_first_tx,
                                tx_processing_result.is_service_tx,
                                can_start_service_block_after_upgrade,
                            )?;
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L160-182)
```rust
                                // Revert to state before transaction
                                system.finish_global_frame(Some(&pre_tx_rollback_handle))?;
                                result_keeper.tx_processed(Err(err));
                            } else {
                                // Now update the accumulators
                                block_data.block_gas_used = next_block_gas_used;
                                block_data.block_computational_native_used =
                                    next_block_computational_native_used;
                                block_data.block_pubdata_used = next_block_pubdata_used;
                                block_data.block_blob_gas_used = next_block_blob_gas_used;

                                if starts_service_block {
                                    is_service_block = true;
                                    can_start_service_block_after_upgrade = false;
                                } else if is_first_tx && tx_processing_result.is_upgrade_tx {
                                    can_start_service_block_after_upgrade = true;
                                } else if can_start_service_block_after_upgrade
                                    && !tx_processing_result.is_service_tx
                                {
                                    can_start_service_block_after_upgrade = false;
                                }

                                is_first_tx = false;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/process_transaction.rs (L28-42)
```rust
                if transaction.is_upgrade() {
                    if !is_first_tx {
                        Err(TxError::Validation(InvalidTransaction::UpgradeTxNotFirst))
                    } else {
                        let r = F::process_l1_transaction::<Config>(
                            system,
                            system_functions,
                            memories,
                            zk_tx,
                            false,
                            tracer,
                            validator,
                        )?;
                        Ok(r)
                    }
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/mod.rs (L121-123)
```rust
    } else if is_service_block {
        Err(internal_error!("Non-service tx in service block"))
    } else {
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/loop_op.rs (L116-156)
```rust
                let tx_result = BasicBootloader::<S, F>::process_transaction::<Config>(
                    initial_calldata_buffer,
                    system,
                    system_functions,
                    memories.reborrow(),
                    tx_counter == 0,
                    block_data_keeper,
                    tracer,
                    validator,
                );

                cycle_marker::end!("process_transaction");

                tracer.finish_tx();

                match tx_result {
                    Err(TxError::Internal(err)) => {
                        system_log!(system, "Tx execution result: Internal error = {err:?}\n");
                        return Err(err);
                    }
                    Err(TxError::Validation(err)) => {
                        system_log!(system, "Tx execution result: Validation error = {err:?}\n");
                        result_keeper.tx_processed(Err(err));
                    }
                    Ok(result) => {
                        let tx_processing_result = result.into_bookkeeper_output();
                        system_log!(
                            system,
                            "Tx execution result = {:?}\n",
                            &tx_processing_result
                        );
                        // anything that is not related to actual validity
                        result_keeper.tx_processed(Ok(tx_processing_result));
                        system.finish_valid_tx()?;
                    }
                }

                system_log!(system, "TX execution ends for transaction {tx_counter}\n");
                system_log!(system, "====================================\n");

                tx_counter += 1;
```
