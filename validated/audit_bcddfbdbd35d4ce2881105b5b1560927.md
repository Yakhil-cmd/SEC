### Title
Block-Level DoS via Unguarded Fatal Error on `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` Revert During L1 Deposit Processing — (`File: basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

Every L1→L2 deposit transaction causes the bootloader to call `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` up to three times (value mint, operator fee, refund). If any of those calls reverts, `notify_l2_asset_tracker` returns a fatal `InternalError` that propagates up through `process_l1_transaction` → `process_transaction` → `loop_op`, causing the **entire block** to abort. There is no graceful per-transaction error path; the block is simply halted. An attacker who can put `L2AssetTracker` into a state where that function reverts can therefore grief every subsequent block that contains any L1 deposit.

---

### Finding Description

`notify_l2_asset_tracker` in `process_l1_transaction.rs` calls `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` via `run_single_interaction`. If the call fails, the function immediately returns a fatal `InternalError`:

```rust
if failed {
    // A revert here means the chain's token accounting would be inconsistent.
    // Treated as a fatal system error — block processing cannot continue.
    return Err(internal_error!(
        "L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"
    )
    .into());
}
``` [1](#0-0) 

This error is classified as `TxError::Internal` in the transaction loop:

```rust
Err(TxError::Internal(err)) => {
    system.finish_global_frame(None)?;
    return Err(err);   // ← exits loop_op entirely
}
``` [2](#0-1) 

Unlike `TxError::Validation` (which rolls back only the offending transaction and continues), `TxError::Internal` terminates the entire block's transaction loop. There is no "NoThrow" variant that would skip the failing L1 transaction and continue processing the rest of the block.

`notify_l2_asset_tracker` is invoked three times per L1 deposit — for the value mint, the operator fee, and the refund: [3](#0-2) [4](#0-3) 

Each call goes through `mint_base_token` → `notify_l2_asset_tracker`, and any revert at any of the three call sites halts the block. [5](#0-4) 

The `L2AssetTracker` contract is a real, upgradeable EVM contract deployed at a fixed address. Its `handleFinalizeBaseTokenBridgingOnL2` function requires, among other things, that the base token asset is registered (`isAssetRegistered[BASE_TOKEN_ASSET_ID] == true`) and that the settlement-layer chain ID matches the stored `L1_CHAIN_ID`. If an attacker can manipulate any of these storage slots — or if the contract is upgraded to a version that reverts — every subsequent block containing an L1 deposit will fail. [6](#0-5) 

---

### Impact Explanation

A single L1 deposit transaction whose `notify_l2_asset_tracker` call reverts causes `loop_op` to return `Err`, aborting the entire block. All other transactions in the block — including unrelated L2 transactions — are discarded. The sequencer must rebuild and re-submit the block without the offending L1 transaction, but if the `L2AssetTracker` remains in the revert-triggering state, every block containing any L1 deposit will fail indefinitely. This is a **block-level DoS** affecting liveness of the entire chain.

---

### Likelihood Explanation

`L2AssetTracker` is an upgradeable contract. Its `handleFinalizeBaseTokenBridgingOnL2` function depends on storage state (registered assets, chain IDs) that can be changed. If any function on `L2AssetTracker` is callable by an unprivileged user and can put the contract into a state where `handleFinalizeBaseTokenBridgingOnL2` reverts, the attack is directly reachable. Even without a direct unprivileged path, a governance compromise or a bug in the contract's upgrade logic would be sufficient. The ZKsync OS code provides no defensive layer — it unconditionally treats the revert as fatal with no fallback.

---

### Recommendation

- **Short term:** Wrap the `notify_l2_asset_tracker` call in a recoverable error path. If the call reverts, treat the L1 transaction as failed (similar to `TxError::Validation`) rather than halting the entire block. Log the failure and continue processing remaining transactions.
- **Long term:** Audit all code paths where a revert from an external system contract is treated as a fatal `InternalError` that terminates block processing. Apply the principle from the external report: batch operations must have NoThrow variants so that one failing item cannot grief the entire batch.

---

### Proof of Concept

1. Attacker manipulates `L2AssetTracker` storage (e.g., via a privileged call, upgrade, or any unprivileged function that alters `isAssetRegistered` or `L1_CHAIN_ID`) so that `handleFinalizeBaseTokenBridgingOnL2` reverts.
2. Any user submits a legitimate L1→L2 deposit transaction (type `0x7F`) with `total_deposited > 0`.
3. The bootloader processes the block. When it reaches the L1 deposit, it calls `mint_base_token` → `notify_l2_asset_tracker`.
4. `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` reverts.
5. `notify_l2_asset_tracker` returns `Err(internal_error!("L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"))`.
6. `process_l1_transaction` propagates the error as `BootloaderSubsystemError`.
7. `loop_op` matches `TxError::Internal` and calls `return Err(err)`, aborting the entire block.
8. All transactions in the block are discarded. The block cannot be sealed as long as any L1 deposit is present and `L2AssetTracker` remains in the revert state.

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L288-310)
```rust
    let coinbase = system.get_coinbase();
    // Mint operator fee portion of the deposit to coinbase.
    mint_base_token::<S, Config>(
        system,
        system_functions,
        memories.reborrow(),
        &pay_to_operator,
        &coinbase,
        l1_chain_id,
        &mut inf_resources,
        tracer,
        validator,
    )
    .map_err(|e| match e.root_cause() {
        RootCause::Runtime(RuntimeError::OutOfErgs(_)) => {
            internal_error!("Out of ergs on infinite ergs").into()
        }
        RootCause::Runtime(RuntimeError::FatalRuntimeError(_)) => {
            internal_error!("Out of native on infinite").into()
        }
        _ => e,
    })?;

```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L335-360)
```rust
    // Mint refund portion of the deposit to the refund recipient.
    if to_refund_recipient > U256::ZERO {
        let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
        mint_base_token::<S, Config>(
            system,
            system_functions,
            memories.reborrow(),
            &to_refund_recipient,
            &refund_recipient,
            l1_chain_id,
            &mut inf_resources,
            tracer,
            validator,
        )
        .map_err(|e| -> BootloaderSubsystemError {
            match e.root_cause() {
                RootCause::Runtime(RuntimeError::OutOfErgs(_)) => {
                    internal_error!("Out of ergs on infinite ergs").into()
                }
                RootCause::Runtime(RuntimeError::FatalRuntimeError(_)) => {
                    internal_error!("Out of native on infinite").into()
                }
                _ => e,
            }
        })?;
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L757-768)
```rust
    notify_l2_asset_tracker::<S, Config>(
        system,
        system_functions,
        memories,
        *amount,
        l1_chain_id,
        resources,
        tracer,
        validator,
    )?;

    transfer_from_treasury::<S>(system, amount, to, resources, Config::SIMULATION)
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L836-855)
```rust
/// Notify L2AssetTracker about base token bridging from L1.
///
/// Calls handleFinalizeBaseTokenBridgingOnL2(uint256 _fromChainId, uint256 _amount)
/// as L2_BASE_TOKEN_ADDRESS (0x800a) to pass the onlyBaseTokenHolderOrL2BaseToken modifier.
///
/// This is called separately for each token movement (value mint, operator
/// payment, refund) so that the asset tracker's accounting stays correct even
/// if the main transaction body reverts.
///
/// Resource usage depends on the caller — value-mint tracks native against user resources;
/// operator-fee and refund use FORMAL_INFINITE.
///
/// Failure halts block processing — if the asset tracker reverts, the
/// chain's token accounting would be inconsistent, so we treat it as
/// fatal rather than silently continuing with incorrect bookkeeping.
///
/// If no contract is deployed at L2AssetTracker, the call succeeds silently
/// (a call to an empty address returns success with no returndata in EVM).
/// However, we are certain that L2AssetTracker is available after the upgrade.
fn notify_l2_asset_tracker<'a, S: EthereumLikeTypes + 'a, Config: BasicBootloaderExecutionConfig>(
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L901-911)
```rust
        if failed {
            system_log!(
                system,
                "L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 failed for amount {amount:?}\n"
            );
            // A revert here means the chain's token accounting would be inconsistent.
            // Treated as a fatal system error — block processing cannot continue.
            return Err(internal_error!(
                "L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"
            )
            .into());
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L108-112)
```rust
                        Err(TxError::Internal(err)) => {
                            system_log!(system, "Tx execution result: Internal error = {err:?}\n",);
                            // Finish the frame opened before processing the tx
                            system.finish_global_frame(None)?; // TODO should we use pre_tx_rollback_handle here?
                            return Err(err);
```
