### Title
`gas_per_pubdata_limit` Not Enforced for L2 ZK Transactions — (File: `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

The `gas_per_pubdata_limit` field present in ABI-encoded ZK L2 transactions is never validated against the block's actual pubdata price during L2 ZK transaction processing. While L1→L2 transactions correctly use the user-specified `gas_per_pubdata_limit` as the pubdata pricing parameter, L2 ZK transactions silently ignore this field and unconditionally apply the operator-set block-level `pubdata_price / native_price` ratio. This is a direct analog to the external report's "lack of slippage protection" pattern: a user-specified bound exists in the transaction format but is never enforced at execution time.

---

### Finding Description

The `Transaction<A>` enum exposes `gas_per_pubdata_limit()` for both transaction formats:

```rust
// basic_bootloader/src/bootloader/transaction/mod.rs
pub fn gas_per_pubdata_limit(&self) -> U256 {
    match self {
        Self::Rlp(_) => U256::ZERO,
        Self::Abi(tx) => U256::from(tx.gas_per_pubdata_limit.read()),
    }
}
``` [1](#0-0) 

For **L1→L2 transactions**, the user-specified `gas_per_pubdata_limit` is read directly from the transaction and used as the pubdata pricing parameter:

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs
let gas_per_pubdata = transaction.gas_per_pubdata_limit.read();
``` [2](#0-1) 

For **L2 ZK transactions**, the validation function `validate_and_compute_fee_for_transaction` reads `pubdata_price` and `native_price` exclusively from block-level metadata (operator-controlled), and computes `native_per_pubdata` from those values — **never reading or checking `transaction.gas_per_pubdata_limit()`**:

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs
let pubdata_price = system.get_pubdata_price();   // operator-set block param
let native_price  = system.get_native_price();    // operator-set block param
// ...
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
// gas_per_pubdata_limit from the transaction is never read here
``` [3](#0-2) 

The `native_per_pubdata` derived from block metadata is then passed into `create_resources_for_tx`, which uses it to charge the user for pubdata: [4](#0-3) 

The `pubdata_price` and `native_price` are block-level parameters set by the operator and provided via the oracle: [5](#0-4) 

The docs explicitly acknowledge these parameters are not yet committed to the block header: *"Currently it misses `gas_per_pubdata` and `native_price`, but we are already working on design and implementation to solve this issue."* [6](#0-5) 

---

### Impact Explanation

A user submitting an ABI-encoded ZK L2 transaction sets `gas_per_pubdata_limit` believing it caps the pubdata cost they will pay. Because this field is never validated, the operator can include the transaction in a block with a `pubdata_price` arbitrarily higher than the user's limit. Two concrete outcomes:

1. **Silent overcharge**: The user is charged native resources (and thus gas via `deltaGas`) at a rate exceeding their stated limit, paying more than they consented to.
2. **Unexpected revert with full gas consumed**: If the block's pubdata price is high enough that the user's native budget is exhausted post-execution, the transaction reverts and the full `gas_limit` is consumed — as confirmed by the existing test `test_l2_tx_not_enough_native_for_pubdata_uses_full_gas_limit`. [7](#0-6) 

The `gas_per_pubdata_limit` field in the ABI transaction format provides a false guarantee of slippage protection that the state transition function does not honour.

---

### Likelihood Explanation

`pubdata_price` and `native_price` are block-level parameters that change between blocks as the operator adjusts them to reflect L1 data costs. A user signs a ZK transaction at one pubdata price and it may be included in a block with a materially different price. Because `gas_per_pubdata_limit` is ignored, there is no mechanism to reject or revert the transaction at the user's specified threshold. This is a structural gap that affects every ABI-encoded ZK L2 transaction that sets a non-zero `gas_per_pubdata_limit`.

---

### Recommendation

In `validate_and_compute_fee_for_transaction` (`validation_impl.rs`), after computing `native_per_pubdata`, add a check against the transaction's `gas_per_pubdata_limit` (when non-zero):

```rust
let gas_per_pubdata_limit = transaction.gas_per_pubdata_limit();
if !gas_per_pubdata_limit.is_zero() {
    // native_per_pubdata is pubdata_price / native_price
    // gas_per_pubdata_limit is the user's max acceptable gas per pubdata byte
    // effective pubdata gas cost = native_per_pubdata / native_per_gas
    // Reject if block pubdata price exceeds user's limit
    require!(
        native_per_pubdata <= native_per_gas.saturating_mul(u256_try_to_u64(&gas_per_pubdata_limit).unwrap_or(u64::MAX)),
        TxError::Validation(InvalidTransaction::PubdataPriceTooHigh),
        system
    )?;
}
```

This mirrors the protection already in place for L1→L2 transactions, where `gas_per_pubdata` from the transaction is used directly.

---

### Proof of Concept

1. User signs an ABI-encoded ZK L2 transaction with `gas_per_pubdata_limit = 100`.
2. At signing time, block `pubdata_price = 100`, `native_price = 10`, so `native_per_pubdata = 10`. The user's effective pubdata gas cost is within their limit.
3. Operator includes the transaction in a block with `pubdata_price = 10_000`, `native_price = 10`, so `native_per_pubdata = 1000` — 100× the user's stated limit.
4. `validate_and_compute_fee_for_transaction` reads `pubdata_price` and `native_price` from block metadata (lines 106–107), computes `native_per_pubdata = 1000`, and never reads `gas_per_pubdata_limit` from the transaction.
5. The transaction executes with `native_per_pubdata = 1000`. If the transaction generates pubdata, the user is charged at 100× their stated acceptable rate. If native resources are exhausted, the transaction reverts and the full `gas_limit` is consumed — with no recourse for the user. [3](#0-2) [1](#0-0)

### Citations

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L77-80)
```rust
    // For L1->L2 transactions we always use the pubdata price provided by the transaction.
    // This is needed to ensure DDoS protection. All the excess expenditure
    // will be refunded to the user.
    let gas_per_pubdata = transaction.gas_per_pubdata_limit.read();
```

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L192-202)
```rust
    // Now we will materialize resources, from which we will try to charge intrinsic cost on top.
    let tx_resources = create_resources_for_tx::<S, L2ResourcesPolicy>(
        system,
        tx_gas_limit,
        native_per_gas == 0,
        native_prepaid_from_gas,
        native_per_pubdata,
        intrinsic_gas,
        intrinsic_computational_native,
        intrinsic_pubdata,
    )?;
```

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L193-203)
```rust
impl ZkSpecificPricingMetadata for BlockMetadataFromOracle {
    fn get_pubdata_price(&self) -> U256 {
        self.pubdata_price
    }
    fn native_price(&self) -> U256 {
        self.native_price
    }
    fn get_pubdata_limit(&self) -> u64 {
        self.pubdata_limit
    }
}
```

**File:** docs/bootloader/bootloader.md (L36-36)
```markdown
Currently it misses `gas_per_pubdata` and `native_price`, but we already working on design and implementation to solve this issue.
```

**File:** tests/instances/transactions/src/native_charging.rs (L167-236)
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
```
