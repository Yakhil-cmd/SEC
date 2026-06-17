### Title
L2AssetTracker State Updated Before Treasury Transfer Completes — Partial-Failure State Inconsistency in `mint_base_token` - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

In `mint_base_token`, `notify_l2_asset_tracker` commits an accounting update to the `L2AssetTracker` contract **before** `transfer_from_treasury` actually moves tokens. If `transfer_from_treasury` subsequently fails (e.g., treasury has insufficient balance), the `L2AssetTracker`'s `totalSuccessfulDepositsFromL1` counter has already been permanently incremented for an amount that was never actually transferred. For the operator-fee and refund payment calls — which execute **outside** any rollback frame — this creates an unrecoverable state inconsistency and halts block processing.

---

### Finding Description

`mint_base_token` is the single function responsible for both notifying the asset tracker and performing the actual treasury-to-recipient token transfer:

```rust
fn mint_base_token(...) -> Result<(), BootloaderSubsystemError> {
    // Step 1: Commit L2AssetTracker state (totalSuccessfulDepositsFromL1 += amount)
    notify_l2_asset_tracker::<S, Config>(...)?;

    // Step 2: Actually move tokens from treasury to recipient (can fail)
    transfer_from_treasury::<S>(system, amount, to, resources, Config::SIMULATION)
}
``` [1](#0-0) 

`notify_l2_asset_tracker` runs the `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` call inside its own isolated sub-frame (`should_make_frame = true`). When the call succeeds, that sub-frame is **committed** — the storage write to `interopInfo[assetId].totalSuccessfulDepositsFromL1` is durable within the block's state. [2](#0-1) 

`transfer_from_treasury` then performs a two-step balance update with no atomicity guarantee:

1. Subtract `amount` from `BASE_TOKEN_HOLDER_ADDRESS` (treasury) — can fail with `TreasuryTransferFailed`
2. Add `amount` to the recipient — can fail with `MintingBalanceOverflow` [3](#0-2) 

`mint_base_token` is called **three times** per L1 deposit transaction:

| Call site | Purpose | Inside rollback frame? |
|---|---|---|
| `execute_l1_transaction_and_notify_result` line 634 | Value mint to sender | **Yes** — rolled back if outer frame reverts |
| `process_l1_transaction` line 290 | Operator fee to coinbase | **No** — after `finish_global_frame(None)` |
| `process_l1_transaction` line 338 | Refund to refund recipient | **No** — after `finish_global_frame(None)` | [4](#0-3) [5](#0-4) 

For the operator-fee and refund calls, there is **no enclosing rollback frame**. If `notify_l2_asset_tracker` succeeds (committing the `L2AssetTracker` storage write) and `transfer_from_treasury` then fails, the `L2AssetTracker` counter is permanently inflated by the amount that was never transferred. The error propagates as an internal error, halting block processing.

The tx loop handles internal errors by calling `finish_global_frame(None)` — a **commit**, not a rollback — before returning the error: [6](#0-5) 

The TODO comment on that line (`// TODO should we use pre_tx_rollback_handle here?`) itself signals uncertainty about whether the correct behavior is to commit or roll back on internal error, leaving the L2AssetTracker state inconsistency potentially durable.

---

### Impact Explanation

**State inconsistency / resource accounting bug**: The `L2AssetTracker.interopInfo[assetId].totalSuccessfulDepositsFromL1` counter is incremented for a deposit that was never actually transferred from the treasury. This counter is used for cross-chain interoperability accounting and settlement-layer proofs. An inflated counter means the protocol believes more base tokens were successfully bridged from L1 than actually were, corrupting the canonical deposit record.

**Liveness / block halt**: Because the operator-fee and refund `mint_base_token` calls run on `FORMAL_INFINITE` resources outside any rollback frame, a `TreasuryTransferFailed` error propagates as an internal error that halts block processing entirely. L1 priority transactions cannot be skipped, so a treasury shortfall for any L1 deposit's fee or refund payment causes a persistent chain halt.

---

### Likelihood Explanation

The `TreasuryTransferFailed` path is reachable whenever `BASE_TOKEN_HOLDER_ADDRESS` has insufficient balance to cover `pay_to_operator` or `to_refund_recipient`. The treasury balance is not directly attacker-controlled, but it can be depleted through normal protocol operation (e.g., many large deposits that are subsequently refunded). An attacker who can influence the treasury balance (e.g., by submitting many L1 transactions that consume treasury funds) can trigger this condition. The `MintingBalanceOverflow` path requires a recipient balance near `U256::MAX` and is practically unreachable.

---

### Recommendation

Wrap the `notify_l2_asset_tracker` + `transfer_from_treasury` sequence in a rollback frame so that if the treasury transfer fails, the `L2AssetTracker` storage write is also reverted:

```rust
fn mint_base_token(...) -> Result<(), BootloaderSubsystemError> {
    let rollback = system.start_global_frame()?;
    let result = (|| {
        notify_l2_asset_tracker::<S, Config>(...)?;
        transfer_from_treasury::<S>(system, amount, to, resources, Config::SIMULATION)
    })();
    match result {
        Ok(()) => { system.finish_global_frame(None)?; Ok(()) }
        Err(e) => { system.finish_global_frame(Some(&rollback))?; Err(e) }
    }
}
```

Alternatively, call `notify_l2_asset_tracker` only **after** `transfer_from_treasury` succeeds, reversing the call order (noting the comment about `totalSupply() == 0` consistency would need to be re-evaluated).

---

### Proof of Concept

1. Deploy a ZKsync OS chain. Set `BASE_TOKEN_HOLDER_ADDRESS` balance to exactly `gas_limit * gas_price` (enough for the value mint but not for the operator fee).
2. Submit an L1 priority transaction with `total_deposited = gas_limit * gas_price + value`, `value > 0`.
3. `execute_l1_transaction_and_notify_result` runs: the value mint (`to_transfer`) is attempted inside the rollback frame. Since `to_transfer = total_deposited - max_fee_commitment = value`, and the treasury has exactly `gas_limit * gas_price`, the value mint fails with `TreasuryTransferFailed` and the frame is rolled back cleanly.
4. Alternatively, set treasury balance to `gas_limit * gas_price + value` (enough for value mint) but not enough for the operator fee. The value mint succeeds. Then `mint_base_token` for `pay_to_operator` is called outside any rollback frame: `notify_l2_asset_tracker` succeeds (L2AssetTracker records `pay_to_operator` as deposited), then `transfer_from_treasury` fails with `TreasuryTransferFailed`.
5. The error propagates as `TxError::Internal`. The tx loop calls `finish_global_frame(None)` (commit). The L2AssetTracker's `totalSuccessfulDepositsFromL1` is now inflated by `pay_to_operator` for a transfer that never occurred.
6. Block processing halts. The chain cannot make progress on L1 transactions until the treasury is replenished, and the L2AssetTracker accounting is permanently inconsistent for that block's committed state. [7](#0-6) [8](#0-7)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L288-309)
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L336-360)
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
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L741-769)
```rust
fn mint_base_token<'a, S: EthereumLikeTypes + 'a, Config: BasicBootloaderExecutionConfig>(
    system: &mut System<S>,
    system_functions: &mut HooksStorage<S, S::Allocator>,
    memories: RunnerMemoryBuffers<'a>,
    amount: &U256,
    to: &B160,
    l1_chain_id: U256,
    resources: &mut S::Resources,
    tracer: &mut impl Tracer<S>,
    validator: &mut impl TxValidator<S>,
) -> Result<(), BootloaderSubsystemError>
where
    S::IO: IOSubsystemExt,
    S::Metadata: ZkSpecificPricingMetadata
        + BasicMetadata<S::IOTypes, TransactionMetadata = TxLevelMetadata<S::IOTypes>>,
{
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
}
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L793-831)
```rust
    let _ = system
        .io
        .update_account_nominal_token_balance(
            zk_ee::execution_environment_type::ExecutionEnvironmentType::EVM,
            resources,
            treasury_address,
            nominal_token_value,
            true, // true = subtract from balance
            fee_payment_in_simulation,
        )
        .map_err(|e| -> BootloaderSubsystemError {
            match e {
                SubsystemError::LeafUsage(balance_error) => {
                    system_log!(system, "Treasury transfer failed: {balance_error:?}");
                    interface_error!(BootloaderInterfaceError::TreasuryTransferFailed)
                }
                _ => wrap_error!(e),
            }
        })?;

    let _ = system
        .io
        .update_account_nominal_token_balance(
            zk_ee::execution_environment_type::ExecutionEnvironmentType::EVM,
            resources,
            to,
            nominal_token_value,
            false, // false = add to balance
            fee_payment_in_simulation,
        )
        .map_err(|e| -> BootloaderSubsystemError {
            match e {
                SubsystemError::LeafUsage(balance_error) => {
                    system_log!(system, "Error while minting: {balance_error:?}");
                    interface_error!(BootloaderInterfaceError::MintingBalanceOverflow)
                }
                _ => wrap_error!(e),
            }
        })?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L870-914)
```rust
    if amount > U256::ZERO || Config::SIMULATION {
        // Encode calldata for handleFinalizeBaseTokenBridgingOnL2(uint256,uint256):
        // selector 0x03117c8c + abi-encoded (fromChainId, amount)
        let mut calldata = [0u8; 68];
        calldata[0..4].copy_from_slice(&[0x03, 0x11, 0x7c, 0x8c]);
        calldata[4..36].copy_from_slice(&l1_chain_id.to_be_bytes::<32>());
        calldata[36..68].copy_from_slice(&amount.to_be_bytes::<32>());

        let failed = resources.with_infinite_ergs(|inf_ergs| {
            let CompletedExecution {
                resources_returned,
                result: asset_tracker_result,
            } = BasicBootloader::<S, ZkTransactionFlowOnlyEOA<S>>::run_single_interaction(
                system,
                system_functions,
                memories,
                &calldata,
                &L2_BASE_TOKEN_ADDRESS,
                &L2_ASSET_TRACKER_ADDRESS,
                inf_ergs.clone(),
                &U256::ZERO,
                true, // should_make_frame - isolate state changes
                tracer,
                validator,
            )?;
            // Overwrite resources inside the closure so that
            // with_infinite_ergs correctly restores ergs afterwards.
            *inf_ergs = resources_returned;
            Ok::<bool, BootloaderSubsystemError>(asset_tracker_result.failed())
        })?;

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
        }
    }
    Ok(())
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L108-113)
```rust
                        Err(TxError::Internal(err)) => {
                            system_log!(system, "Tx execution result: Internal error = {err:?}\n",);
                            // Finish the frame opened before processing the tx
                            system.finish_global_frame(None)?; // TODO should we use pre_tx_rollback_handle here?
                            return Err(err);
                        }
```
