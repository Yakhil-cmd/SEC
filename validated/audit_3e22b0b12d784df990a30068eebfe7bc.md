### Title
L2AssetTracker `chainBalance` Inflated When L1 Refund Recipient Is Treasury Address — (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

In `process_l1_transaction.rs`, the `mint_base_token` helper always calls `notify_l2_asset_tracker` (recording a deposit in the L2AssetTracker's `chainBalance`) **before** calling `transfer_from_treasury` (which moves tokens from the treasury to the recipient). When an L1 transaction submitter sets `refund_recipient` (via `transaction.reserved[1]`) to the treasury address (`BASE_TOKEN_HOLDER_ADDRESS`), `transfer_from_treasury` performs a net-zero balance change (treasury -= amount, then treasury += amount), while the L2AssetTracker has already recorded the full refund amount as a bridged deposit. This inflates `chainBalance` without a corresponding increase in circulating supply, creating a persistent accounting inconsistency analogous to the external report's "shares deposited to whitelisted vaults that never accrue rewards."

---

### Finding Description

**Root cause — `mint_base_token` has no guard against the recipient being the treasury:** [1](#0-0) 

`mint_base_token` unconditionally calls `notify_l2_asset_tracker` first, then `transfer_from_treasury`. Inside `transfer_from_treasury`: [2](#0-1) 

Step 1 subtracts `amount` from `treasury_address`; step 2 adds `amount` to `to`. When `to == treasury_address`, both operations target the same account — the net balance change is **zero**. Yet `notify_l2_asset_tracker` has already incremented `chainBalance` by `amount`: [3](#0-2) 

**Attacker-controlled entry path — `refund_recipient` is taken verbatim from `transaction.reserved[1]`:** [4](#0-3) 

There is no validation that `refund_recipient != BASE_TOKEN_HOLDER_ADDRESS`. Any L1 transaction submitter can set `reserved[1]` to the treasury address.

**Exploit flow:**

1. Attacker submits an L1→L2 transaction with a high `gas_limit`, low actual gas usage, and `reserved[1]` = `BASE_TOKEN_HOLDER_ADDRESS`.
2. Bootloader computes `to_refund_recipient = gas_price * (gas_limit - gas_used)` — a large value.
3. `mint_base_token(to_refund_recipient, treasury)` is called:
   - `notify_l2_asset_tracker` records `to_refund_recipient` in `chainBalance`.
   - `transfer_from_treasury`: treasury -= `to_refund_recipient`, treasury += `to_refund_recipient` → net zero.
4. `chainBalance` is now inflated by `to_refund_recipient` with no corresponding increase in circulating supply.

The same path exists for the **value-mint step** if `transaction.from` is set to the treasury address, since `to_transfer` is minted to `from`: [5](#0-4) 

---

### Impact Explanation

The L2AssetTracker's `chainBalance` is the authoritative record of how many base tokens have been bridged from L1 and are in circulation on L2. The bootloader's own comment confirms this invariant must hold: [6](#0-5) 

Inflating `chainBalance` without a matching increase in circulating supply breaks this invariant. Downstream consequences include:

- **Migration logic corruption**: `_needToForceSetAssetMigrationOnL2` uses `totalSupply() == 0` as a sentinel; an inflated `chainBalance` relative to actual supply can cause this check to fire incorrectly or be suppressed.
- **Withdrawal over-accounting**: Any L1 withdrawal logic that uses `chainBalance` as an upper bound on claimable tokens could allow claims exceeding the actual locked amount on L1.
- **Repeated inflation**: The attack is repeatable across many L1 transactions, compounding the discrepancy.

---

### Likelihood Explanation

- The `refund_recipient` field (`reserved[1]`) is set by the L1 transaction submitter with no on-chain validation in ZKsync OS.
- The treasury address (`BASE_TOKEN_HOLDER_ADDRESS`) is a well-known constant, publicly visible in the codebase.
- No special privilege is required; any user who can submit an L1→L2 transaction can trigger this.
- The refund amount scales with `gas_price * gas_limit`, so a single transaction with a large gas limit can produce a significant inflation.

---

### Recommendation

Add a guard in `mint_base_token` (or `transfer_from_treasury`) that rejects or redirects transfers where `to == BASE_TOKEN_HOLDER_ADDRESS`:

```rust
fn mint_base_token(..., to: &B160, ...) -> Result<(), BootloaderSubsystemError> {
    // Prevent accounting inconsistency: sending tokens back to the treasury
    // would inflate chainBalance without increasing circulating supply.
    require_internal!(
        to != &system_hooks::addresses_constants::BASE_TOKEN_HOLDER_ADDRESS,
        "mint_base_token: recipient must not be treasury",
        system
    )?;
    notify_l2_asset_tracker::<S, Config>(...)?;
    transfer_from_treasury::<S>(system, amount, to, resources, Config::SIMULATION)
}
```

Alternatively, validate `refund_recipient` and `from` against the treasury address before processing the L1 transaction, consistent with how the external report's fix added `isWhitelistedVault(receiver)` checks before processing deposits and mints.

---

### Proof of Concept

```
L1 transaction parameters:
  from:              <any address>
  to:                <any address>
  gas_price:         1_000_000 (wei)
  gas_limit:         1_000_000
  value:             0
  total_deposited:   1_000_000 * 1_000_000 = 1e12 (locked on L1)
  reserved[1]:       BASE_TOKEN_HOLDER_ADDRESS  ← refund recipient = treasury

Execution:
  gas_used ≈ 21_000 (simple transfer)
  pay_to_operator = 21_000 * 1_000_000 = 2.1e10
  to_refund_recipient = 1e12 - 2.1e10 ≈ 9.79e11

notify_l2_asset_tracker called with 9.79e11  → chainBalance += 9.79e11
transfer_from_treasury(treasury, 9.79e11):
  treasury -= 9.79e11
  treasury += 9.79e11   (to == treasury)
  net treasury change = 0

Result: chainBalance inflated by ~9.79e11 with zero change to circulating supply.
Repeatable every L1 transaction.
```

### Citations

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L791-831)
```rust
    let treasury_address = &system_hooks::addresses_constants::BASE_TOKEN_HOLDER_ADDRESS;

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L836-854)
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
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L870-913)
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
```
