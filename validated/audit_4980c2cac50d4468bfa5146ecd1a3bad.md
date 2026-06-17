### Title
L2AssetTracker Revert Halts Entire Block Processing During L1→L2 Deposit Handling - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

The `notify_l2_asset_tracker` function in `process_l1_transaction.rs` calls `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` as part of every L1→L2 deposit transaction. If this call reverts for any reason, the bootloader escalates it to a **fatal internal error that halts the entire block processing** — not just the current transaction. This is the ZKsync OS analog of the external report's ETH-transfer griefing pattern: a call to an external contract that can revert, blocking the entire operation for all parties.

---

### Finding Description

In `process_l1_transaction.rs`, the function `notify_l2_asset_tracker` is invoked up to **three times per L1→L2 transaction** with a non-zero deposit (`total_deposited > 0`):

1. **Value mint** — inside `execute_l1_transaction_and_notify_result` via `mint_base_token` (line 634), called with the deposit-minus-fee amount to the `from` address.
2. **Operator fee** — after execution (line 290), via `mint_base_token` to `coinbase`.
3. **Refund** — after execution (line 338), via `mint_base_token` to `refund_recipient` (a user-controlled address from `transaction.reserved[1]`). [1](#0-0) 

Each call executes `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2(l1_chain_id, amount)` via `run_single_interaction` with `L2_BASE_TOKEN_ADDRESS` as the caller. If the call returns a failed result, the bootloader does **not** gracefully degrade — it immediately returns a fatal `internal_error!`:

```rust
if failed {
    // A revert here means the chain's token accounting would be inconsistent.
    // Treated as a fatal system error — block processing cannot continue.
    return Err(internal_error!(
        "L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted"
    ).into());
}
``` [2](#0-1) 

This error propagates all the way up through `process_l1_transaction` → the block loop, halting the entire block. The `L2AssetTracker` is an upgradeable EVM contract (`OwnableUpgradeable`) predeploy at a fixed address. Its `handleFinalizeBaseTokenBridgingOnL2` function contains internal checks (asset registration, chain ID matching, migration number validation) that can revert under unexpected state conditions. [3](#0-2) 

The `l1_chain_id` passed to the call is read from `L2AssetTracker` storage slot 154 at the **start** of `process_l1_transaction` (line 172), before the execution frame. If the main transaction body modifies `L2AssetTracker` storage (e.g., changes `L1_CHAIN_ID`), the pre-read value passed to the post-execution `notify_l2_asset_tracker` calls may no longer match the contract's current state, causing a revert. [4](#0-3) [5](#0-4) 

---

### Impact Explanation

**Impact: High**

If `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2` reverts during any of the three notification calls, the entire block processing halts. This means:

- **All L1→L2 transactions with non-zero deposits in the block are unprocessable.** The sequencer cannot finalize the block.
- **Chain liveness is broken** for the L1→L2 bridge path — deposits from L1 cannot be credited on L2.
- Unlike the external report where only a single refund is blocked, here the entire block is halted, affecting all users in the block.

This is a **valid-execution unprovability** / liveness issue: a valid L1→L2 transaction that was accepted on L1 cannot be processed on L2, and the block cannot be sealed.

---

### Likelihood Explanation

**Likelihood: Low**

The `L2AssetTracker` is a system contract designed to handle these calls. Under normal operation it succeeds. However, the revert can be triggered by:

1. **A bug in `L2AssetTracker`** — the contract has complex internal state (`isAssetRegistered`, `assetMigrationNumber`, `interopInfo` mappings). An edge case in any of these checks could cause a revert.
2. **A malicious or buggy upgrade** — `L2AssetTracker` is `OwnableUpgradeable`; a governance-approved upgrade that introduces a revert condition would immediately halt all L1→L2 deposit processing.
3. **State manipulation via the main tx body** — the `l1_chain_id` is read before execution but used after. If the main transaction body calls a function on `L2AssetTracker` that changes `L1_CHAIN_ID` (slot 154), the stale `l1_chain_id` passed to post-execution notifications will mismatch, causing a revert.

Scenario 3 is the most directly attacker-reachable: an unprivileged user submits an L1→L2 transaction whose `to` address is `L2AssetTracker` and whose calldata invokes a state-changing function (if any such function is accessible without owner privileges).

---

### Recommendation

The bootloader should not treat a revert from `L2AssetTracker` as a fatal block-halting error. Options:

1. **Degrade gracefully**: If `notify_l2_asset_tracker` reverts, log the failure and continue processing the transaction (accepting that the asset tracker's accounting may be temporarily inconsistent, to be reconciled later).
2. **Isolate the fatal path**: Only halt block processing if the revert occurs during a protocol-upgrade transaction (where correctness is mandatory), not for ordinary priority L1→L2 transactions.
3. **Re-read `l1_chain_id` immediately before each notification call** rather than once at the start of `process_l1_transaction`, to avoid stale-value mismatches.
4. **Apply the same claiming/pull-pattern logic** recommended in the external report: store pending notifications and process them in a separate step, so a single revert cannot block the entire block.

---

### Proof of Concept

**Attack path via stale `l1_chain_id` (no privileged access required if `L2AssetTracker` exposes a state-changing function to unprivileged callers):**

1. Attacker deploys or identifies a path to call a function on `L2AssetTracker` that modifies `L1_CHAIN_ID` (slot 154) to a different value (e.g., `0`).
2. Attacker submits an L1→L2 priority transaction with:
   - `to = L2_ASSET_TRACKER_ADDRESS`
   - `calldata` = function that changes `L1_CHAIN_ID`
   - `to_mint > 0` (non-zero deposit)
3. `process_l1_transaction` reads `l1_chain_id = old_value` at line 172.
4. The main tx body executes and changes `L1_CHAIN_ID` in `L2AssetTracker` storage to `new_value`.
5. Post-execution, `notify_l2_asset_tracker` is called with `l1_chain_id = old_value`.
6. `L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2(old_value, amount)` checks `old_value == new_value` → fails → reverts.
7. `notify_l2_asset_tracker` returns `internal_error!("L2AssetTracker.handleFinalizeBaseTokenBridgingOnL2 reverted")`.
8. Block processing halts entirely. [4](#0-3) [6](#0-5) [7](#0-6) [8](#0-7) [2](#0-1)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L172-172)
```rust
    let l1_chain_id = read_l1_chain_id(system);
```

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L631-657)
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L870-912)
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
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L920-944)
```rust
/// passed as `_fromChainId` to `handleFinalizeBaseTokenBridgingOnL2`.
fn read_l1_chain_id<S: EthereumLikeTypes>(system: &mut System<S>) -> U256
where
    S::IO: IOSubsystemExt,
{
    // L2AssetTracker storage layout (verified via `forge inspect`):
    //   slots 0-100:   Initializable + OwnableUpgradeable + Ownable2StepUpgradeable
    //   slots 101-150: Ownable2Step __gap
    //   slot 151:      mapping chainBalance
    //   slot 152:      mapping assetMigrationNumber
    //   slot 153:      mapping isAssetRegistered
    //   slot 154:      uint256 L1_CHAIN_ID
    let l1_chain_id_slot = Bytes32::from_u256_be(&U256::from(154));
    let mut inf_resources = S::Resources::FORMAL_INFINITE;
    let chain_id = system
        .io
        .storage_read::<false>(
            ExecutionEnvironmentType::NoEE,
            &mut inf_resources,
            &L2_ASSET_TRACKER_ADDRESS,
            &l1_chain_id_slot,
        )
        .expect("must read L2AssetTracker L1_CHAIN_ID");
    U256::from_be_bytes(chain_id.as_u8_array())
}
```
