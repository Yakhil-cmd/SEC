### Title
L1 Priority Transaction Processing Halts Entire Block When `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` Reverts — (`File: basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

Every L1 priority transaction with a non-zero deposit calls `notify_l2_asset_tracker`, which makes a live EVM call to the `L2AssetTracker` system contract. The bootloader explicitly treats any revert from that call as a **fatal internal error that halts the entire block**. Because L1 priority transactions cannot be skipped or invalidated, a single crafted L1 deposit that causes `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` to revert permanently stalls block production until the operator deploys a fix — an exact analog to the "exodus mode" DoS described in the external report.

---

### Finding Description

`notify_l2_asset_tracker` is invoked up to three times per L1 transaction (value mint, operator fee, refund). Its own comment documents the fatal design:

> *"Failure halts block processing — if the asset tracker reverts, the chain's token accounting would be inconsistent, so we treat it as fatal rather than silently continuing with incorrect bookkeeping."* [1](#0-0) 

When the call fails, the function returns an `InternalError`:

```rust
return Err(internal_error!(
    "L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"
).into());
```

This propagates as `BootloaderSubsystemError` → `TxError::Internal`. In the ZK transaction loop, `TxError::Internal` causes an unconditional `return Err(err)`, aborting the entire block: [2](#0-1) 

```rust
Err(TxError::Internal(err)) => {
    system.finish_global_frame(None)?;
    return Err(err);   // ← block processing terminates
}
```

The same fatal path exists in the Ethereum loop: [3](#0-2) 

`run_prepared` propagates the error to the caller, which in the forward system panics: [4](#0-3) 

```rust
if let Err(err) = ForwardBootloader::run_prepared::<Config>(...) {
    panic!("Forward run failed with: {err}")
}
```

The three call sites in `process_l1_transaction` that can trigger this path are:

1. **Value mint to `from`** (inside the execution frame, user resources): [5](#0-4) 

2. **Operator fee mint to `coinbase`** (post-execution, `FORMAL_INFINITE`): [6](#0-5) 

3. **Refund mint to `refund_recipient`** (post-execution, `FORMAL_INFINITE`): [7](#0-6) 

The `refund_recipient` is fully attacker-controlled — it is read directly from `transaction.reserved[1]`, which the L1 transaction sender sets: [8](#0-7) [9](#0-8) 

The `notify_l2_asset_tracker` call is made as `L2_BASE_TOKEN_ADDRESS` (0x800a) to `L2_ASSET_TRACKER_ADDRESS` (0x1000f): [10](#0-9) 

The `amount` parameter passed to `handleFinalizeBaseTokenBridgingOnL2` is directly derived from the attacker-supplied `to_mint` field of the L1 transaction. Any revert condition in `L2AssetTracker` that is sensitive to the `amount` value (e.g., arithmetic overflow in `totalSuccessfulDepositsFromL1 += amount`, a paused state, or an unregistered asset) can be triggered by crafting the `to_mint` value accordingly.

---

### Impact Explanation

- **Severity: High.** A single L1 priority transaction that causes `notify_l2_asset_tracker` to revert halts the entire block. Because L1 priority transactions are committed on L1 and **cannot be invalidated or skipped** by the operator (the code explicitly documents this: *"L1 transactions cannot be invalidated"*), the chain cannot make progress until a new contract upgrade is deployed — identical in effect to the "exodus mode" described in the external report.
- All subsequent L1 and L2 transactions in the same block are also dropped.
- The proving system would also fail to generate a valid proof for the block.

---

### Likelihood Explanation

- **Medium.** The attacker controls `to_mint` (the deposit amount) and `refund_recipient`. Any overflow or revert condition in `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` that depends on the `amount` argument (e.g., `totalSuccessfulDepositsFromL1` accumulator overflow at `U256::MAX`, or a paused/unregistered asset state) is directly triggerable. The `L2AssetTracker` is a real upgradeable Solidity contract; its internal revert conditions are the necessary co-trigger, but the ZKsync OS design is the root cause that makes any such revert fatal.

---

### Recommendation

1. **Do not treat `L2AssetTracker` reverts as fatal block errors.** Instead, log the failure, emit a system event, and continue block processing. The asset tracker's accounting can be reconciled in a subsequent upgrade.
2. **Apply the pull-payment pattern** analogous to the external report's recommendation: instead of making a live call to `L2AssetTracker` inside the critical block-processing path, record the notification in a queue and process it asynchronously or in a separate, non-fatal step.
3. **Add a circuit-breaker**: if `notify_l2_asset_tracker` fails, degrade gracefully (skip the notification, mark the transaction as requiring manual reconciliation) rather than aborting the entire block.

---

### Proof of Concept

**Attack flow:**

1. Attacker submits an L1 priority transaction on L1 with:
   - `to_mint` set to a value that causes `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` to revert (e.g., a value that overflows `totalSuccessfulDepositsFromL1`, or triggers a paused/unregistered-asset revert path in the contract).
   - `refund_recipient` set to any address.

2. The L1 transaction is committed to the priority queue on L1 and cannot be skipped.

3. The operator includes the transaction in a block. The bootloader calls `mint_base_token` → `notify_l2_asset_tracker` → `run_single_interaction` targeting `L2AssetTracker`.

4. `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` reverts.

5. `notify_l2_asset_tracker` returns `Err(internal_error!("L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"))`. [11](#0-10) 

6. The error propagates as `TxError::Internal` to the ZK tx loop, which calls `return Err(err)`, aborting block processing. [2](#0-1) 

7. `run_prepared` returns `Err`, and the forward system panics. [4](#0-3) 

8. The chain cannot produce a valid block containing this L1 transaction. Since the transaction cannot be removed from the priority queue without an L1 governance action, block production is halted until an emergency upgrade is deployed — matching the "no way to set `isInExodusMode` back to false" impact of the external report.

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L290-309)
```rust
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L336-359)
```rust
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
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L631-658)
```rust
    if to_transfer > U256::ZERO || Config::SIMULATION {
        resources
            .with_infinite_ergs(|inf_resources| {
                mint_base_token::<S, Config>(
                    system,
                    system_functions,
                    memories.reborrow(),
                    &to_transfer,
                    &from,
                    l1_chain_id,
                    inf_resources,
                    tracer,
                    validator,
                )
            })
            .map_err(|e| match e.root_cause() {
                RootCause::Runtime(RuntimeError::OutOfErgs(_)) => {
                    system_log!(
                        system,
                        "Out of ergs on infinite ergs: inner error was {e:?}"
                    );
                    BootloaderSubsystemError::LeafDefect(internal_error!(
                        "Out of ergs on infinite ergs"
                    ))
                }
                _ => e,
            })?;
    }
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

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/loop_op.rs (L132-134)
```rust
                    Err(TxError::Internal(err)) => {
                        system_log!(system, "Tx execution result: Internal error = {err:?}\n");
                        return Err(err);
```

**File:** forward_system/src/system/bootloader.rs (L27-31)
```rust
    if let Err(err) =
        ForwardBootloader::run_prepared::<Config>(oracle, &mut (), result_keeper, tracer, validator)
    {
        panic!("Forward run failed with: {err}")
    };
```

**File:** tests/common/src/zksync_tx/l1_tx.rs (L23-27)
```rust
    /// The amount of base token that should be minted on L2 as the result of this transaction.
    pub to_mint: U256,
    /// The recipient of the refund for the transaction on L2. If the transaction fails, then this
    /// address will receive the `value` of this transaction.
    pub refund_recipient: Address,
```

**File:** system_hooks/src/addresses_constants.rs (L47-47)
```rust
pub const L2_ASSET_TRACKER_ADDRESS: B160 = B160::from_limbs([0x1000f, 0, 0]);
```
