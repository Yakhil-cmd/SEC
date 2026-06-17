### Title
Missing `gas_per_pubdata_limit` Enforcement for L2 ZKsync Transactions - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

### Summary
The `gasPerPubdataByteLimit` field in L2 ZKsync (EIP-712 type) transactions is parsed but never validated against the block-level pubdata price. The bootloader computes `native_per_pubdata` exclusively from operator-controlled block parameters, ignoring the user's stated maximum. A transaction can be included in a block where the actual pubdata cost exceeds the user's limit, causing a post-execution revert that burns the full gas limit.

### Finding Description

In `validate_and_compute_fee_for_transaction` (`validation_impl.rs`), `native_per_pubdata` is derived entirely from block-level oracle values:

```rust
let pubdata_price = system.get_pubdata_price();
let native_price = system.get_native_price();
// ...
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
``` [1](#0-0) 

There is no check that this derived value is within the user's `gas_per_pubdata_limit`. The field is explicitly annotated `#[allow(dead_code)]` in `AbiEncodedTransaction`, confirming it is never read during L2 transaction processing:

```rust
/// The maximum amount of gas the user is willing to pay for a byte of pubdata.
#[allow(dead_code)]
pub gas_per_pubdata_limit: ParsedValue<u32>,
``` [2](#0-1) 

This is a direct inconsistency with L1→L2 transaction handling, where `gas_per_pubdata_limit` from the transaction IS used to compute `native_per_pubdata`:

```rust
// For L1->L2 transactions we always use the pubdata price provided by the transaction.
let gas_per_pubdata = transaction.gas_per_pubdata_limit.read();
// ...
let native_per_pubdata = (gas_per_pubdata as u64)
    .checked_mul(native_per_gas)
    .unwrap_or_else(|| { ... });
``` [3](#0-2) 

The `Transaction::gas_per_pubdata_limit()` accessor exists and returns the user's value for ABI-encoded transactions, but is never called in the L2 validation path: [4](#0-3) 

When pubdata costs exceed the user's native budget post-execution, the transaction reverts and the full gas limit is burned, as confirmed by the existing test: [5](#0-4) 

### Impact Explanation

A user submits a ZKsync L2 transaction with `gasPerPubdataByteLimit = X`, intending to cap their pubdata exposure. The sequencer includes the transaction in a block where `pubdata_price / native_price > X`. Because the bootloader never validates the user's limit against the block price, the transaction proceeds through validation and execution. Post-execution pubdata charging then exhausts the native budget, causing a revert that burns the entire `gas_limit`. The user loses their full gas fee while receiving no execution result, despite having specified a protective limit. The financial loss is bounded by `gas_limit × max_fee_per_gas` but is total within that bound.

**Impact: Medium** — direct, deterministic loss of user funds (gas fees) with no execution benefit.

### Likelihood Explanation

Pubdata prices (`pubdata_price` in `BlockContext`) fluctuate with L1 gas prices and are set per-block by the sequencer. A user who submits a transaction during a low-pubdata-price period with a conservative `gas_per_pubdata_limit` can have that transaction included in a later block with a higher pubdata price. No malicious sequencer behavior is required — normal market-driven pubdata price increases are sufficient. The sequencer has no protocol-level obligation to filter transactions by `gas_per_pubdata_limit` before inclusion.

**Likelihood: Low-Medium** — requires pubdata price to rise between transaction submission and inclusion, which is a normal market event on ZKsync.

### Recommendation

In `validate_and_compute_fee_for_transaction`, after computing `native_per_pubdata` from block prices, compare it against the user's stated limit. The equivalent check already exists for L1 transactions and should be mirrored for L2:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;

// Add: enforce user's gas_per_pubdata_limit
let user_gas_per_pubdata = transaction.gas_per_pubdata_limit();
if !user_gas_per_pubdata.is_zero() {
    // native_per_pubdata = pubdata_price / native_price
    // user limit in native units = user_gas_per_pubdata * native_per_gas
    let user_native_per_pubdata = u256_try_to_u64(
        &user_gas_per_pubdata.saturating_mul(U256::from(native_per_gas))
    ).unwrap_or(u64::MAX);
    require!(
        native_per_pubdata <= user_native_per_pubdata,
        InvalidTransaction::PubdataPriceTooHigh,
        system
    )?;
}
```

Remove the `#[allow(dead_code)]` annotation from `gas_per_pubdata_limit` in `AbiEncodedTransaction` once the field is actively used.

### Proof of Concept

1. Deploy a contract that writes to 10 storage slots (generating ~320 bytes of pubdata).
2. Submit a ZKsync L2 EIP-712 transaction with `gasPerPubdataByteLimit = 1` (1 gas per pubdata byte) and `gas_limit = 250_000`, `max_fee_per_gas = 1000`.
3. Execute the block with `BlockContext { pubdata_price: 700_000, native_price: 1, eip1559_basefee: 1000 }` — making the actual pubdata cost ~700,000 native per byte, far exceeding the user's limit of 1.
4. Observe: the bootloader accepts the transaction through validation (no `PubdataPriceTooHigh` error), executes it, then reverts post-execution with `gas_used == gas_limit` (full gas burned).
5. Expected behavior: the transaction should be rejected at validation time with `InvalidTransaction::PubdataPriceTooHigh` because the block's pubdata price exceeds `gasPerPubdataByteLimit`.

This mirrors the existing test `test_l2_tx_not_enough_native_for_pubdata_uses_full_gas_limit` which already demonstrates the revert-with-full-gas-burn outcome, confirming the execution path exists. [6](#0-5)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L106-143)
```rust
    let pubdata_price = system.get_pubdata_price();
    let native_price = system.get_native_price();

    let gas_price = if transaction.is_service() {
        // Service transactions do not pay gas fees,
        // their gas price is allowed to be < block base fee.
        U256::ZERO
    } else {
        get_gas_price::<S, Config>(
            system,
            transaction.max_fee_per_gas(),
            transaction.max_priority_fee_per_gas(),
        )?
    };

    let native_per_gas = {
        if native_price.is_zero() {
            return Err(internal_error!("Native price cannot be 0").into());
        }

        if cfg!(feature = "resources_for_tester") {
            crate::bootloader::constants::TESTER_NATIVE_PER_GAS
        } else if Config::SIMULATION && gas_price.is_zero() {
            // For simulation, if gas price isn't set, we use base fee
            // for native calculation
            u256_try_to_u64(&system.get_eip1559_basefee().div_ceil(native_price)).ok_or(
                TxError::Validation(InvalidTransaction::NativeResourcesAreTooExpensive),
            )?
        } else {
            u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
                InvalidTransaction::NativeResourcesAreTooExpensive,
            ))?
        }
    };

    // We checked native_price != 0 above
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
        .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

**File:** basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs (L49-52)
```rust
    /// The maximum amount of gas the user is willing to pay for a byte of pubdata.
    #[allow(dead_code)]
    pub gas_per_pubdata_limit: ParsedValue<u32>,
    /// The maximum fee per gas that the user is willing to pay.
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L77-88)
```rust
    // For L1->L2 transactions we always use the pubdata price provided by the transaction.
    // This is needed to ensure DDoS protection. All the excess expenditure
    // will be refunded to the user.
    let gas_per_pubdata = transaction.gas_per_pubdata_limit.read();

    // It's important to ensure that the amount of pubdata estimated during
    // transaction simulation is never less than the amount estimated
    // during execution of the same transaction.
    // Since the introduction of the asset tracker calls during the base token
    // minting, the pubdata for the storage diff of the first of such calls
    // depends indirectly on the gas price, which can fluctuate from simulation
    // to execution.
```

**File:** basic_bootloader/src/bootloader/transaction/mod.rs (L150-156)
```rust
    /// Returns the gas per pubdata limit.
    pub fn gas_per_pubdata_limit(&self) -> U256 {
        match self {
            Self::Rlp(_) => U256::ZERO,
            Self::Abi(tx) => U256::from(tx.gas_per_pubdata_limit.read()),
        }
    }
```

**File:** tests/instances/transactions/src/native_charging.rs (L167-237)
```rust
#[test]
fn test_l2_tx_not_enough_native_for_pubdata_uses_full_gas_limit() {
    let wallet = testing_signer(0);
    let from = wallet.address();
    let gas_limit = 250_000;
    let bytecode = hex::decode(
        "602a600052600160005560016001556001600255600160035560016004556001600555600160065560016007556001600855600160095560206000f3",
    )
    .unwrap();

    let make_tx = || {
        let tx = TxEip1559 {
            chain_id: 37u64,
            nonce: 0,
            max_fee_per_gas: 1000,
            max_priority_fee_per_gas: 1000,
            gas_limit,
            to: TxKind::Call(TO),
            value: U256::ZERO,
            input: Default::default(),
            access_list: Default::default(),
        };
        ZKsyncTxEnvelope::from_eth_tx(tx, wallet.clone())
    };

    // Control execution should succeed, so the failing case below is specific to
    // post-execution pubdata charging.
    let control_context = BlockContext {
        eip1559_basefee: U256::from(1000),
        native_price: U256::ONE,
        pubdata_price: U256::ONE,
        ..Default::default()
    };
    let mut control_tester = TestingFramework::new()
        .with_evm_contract(TO, &bytecode)
        .with_balance(from, U256::from(1_000_000_000_000_000_u64))
        .with_block_context(control_context);
    let control_output = control_tester.execute_block(vec![make_tx()]);
    let control_tx = control_output.tx_results[0]
        .as_ref()
        .expect("Control tx should be processed");
    assert!(
        control_tx.is_success(),
        "Control tx must succeed with regular pubdata pricing"
    );

    // Expensive pubdata causes a post-execution revert due to insufficient native.
    let expensive_pubdata_context = BlockContext {
        eip1559_basefee: U256::from(1000),
        native_price: U256::ONE,
        pubdata_price: U256::from(700_000u64),
        ..Default::default()
    };
    let mut tester = TestingFramework::new()
        .with_evm_contract(TO, &bytecode)
        .with_balance(from, U256::from(1_000_000_000_000_000_u64))
        .with_block_context(expensive_pubdata_context);
    let output = tester.execute_block(vec![make_tx()]);
    let tx_result = output.tx_results[0]
        .as_ref()
        .expect("Tx should be processed even when reverted");

    assert!(
        !tx_result.is_success(),
        "Tx should revert when pubdata cannot be paid after execution"
    );
    assert_eq!(
        tx_result.gas_used, gas_limit,
        "Tx reverted by post-execution pubdata charging must consume full gas limit"
    );
}
```
