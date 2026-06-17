### Title
EIP-4844 Blob Fee Check Skipped in Simulation Causes `eth_estimateGas` vs Execution Divergence - (`basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

In ZKsync OS's ZK transaction validation path, the blob base fee check for EIP-4844 transactions is conditionally bypassed during simulation (`eth_estimateGas`/`eth_call`), while the balance check and the actual fee-to-prepay computation use different blob fee sources. This creates a divergence: simulation succeeds and returns a gas estimate, but actual execution rejects the same transaction. A user or tool relying on `eth_estimateGas` is misled into believing an EIP-4844 transaction is viable when it will fail on submission.

---

### Finding Description

In `validate_and_compute_fee_for_transaction` inside `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`, three logically related checks use inconsistent values:

**1. Blob base fee check — skipped in simulation (line 405):**

```rust
if &block_base_fee_per_blob_gas > tx_max_fee_per_blob_gas && !Config::SIMULATION {
    return Err(TxError::Validation(
        InvalidTransaction::BlobBaseFeeGreaterThanMaxFeePerBlobGas,
    ));
}
```

The `!Config::SIMULATION` guard means this check is entirely absent during simulation. A transaction with `max_fee_per_blob_gas = 0` and `block_blob_base_fee = 1` passes this gate in simulation but fails it in actual execution.

**2. Balance check — uses `tx.max_fee_per_blob_gas` via `required_balance()` (lines 439–451):**

```rust
let Some(total_required_balance) = transaction.required_balance() else { ... };
if total_required_balance > originator_account_data.nominal_token_balance.0 { ... }
```

`required_balance()` for EIP-4844 computes:

```rust
value + (tx.max_fee_per_blob_gas * num_blobs * GAS_PER_BLOB) + (max_fee_per_gas * gas_limit)
```

When `max_fee_per_blob_gas = 0`, the blob fee contribution to the balance check is zero.

**3. Actual fee charged — uses `block_blob_base_fee` (lines 466–491):**

```rust
let fee_for_blob_gas = system.get_blob_base_fee_per_gas()
    .checked_mul(U256::from(blob_gas_used));
let fee_to_prepay = gas_fee_amount.checked_add(fee_for_blob_gas)?;
```

`fee_to_prepay` uses the live `block_blob_base_fee`, not `tx.max_fee_per_blob_gas`.

**The divergence path:**

| Step | Simulation (`SIMULATION=true`) | Actual Execution (`SIMULATION=false`) |
|---|---|---|
| Blob fee check (line 405) | **Skipped** | Enforced → `BlobBaseFeeGreaterThanMaxFeePerBlobGas` |
| Balance check (line 444) | Uses `max_fee_per_blob_gas=0` → passes | Never reached (rejected above) |
| `fee_to_prepay` (line 489) | Uses `block_blob_base_fee` | N/A |
| Result | **Success + gas estimate** | **Validation failure** |

The test `test_simulation_4844_zero_blob_fee_allowed` in `tests/instances/transactions/src/lib.rs` explicitly asserts and confirms this divergence is present — simulation succeeds with `max_fee_per_blob_gas=0` and `blob_fee=1`.

---

### Impact Explanation

Any caller of `eth_estimateGas` (which routes through `simulate_tx` → `BasicBootloaderCallSimulationConfig` with `SIMULATION=true`) submitting an EIP-4844 transaction with `max_fee_per_blob_gas < block_blob_base_fee` receives a successful gas estimate. When the same transaction is submitted for actual inclusion, it is rejected at validation with `BlobBaseFeeGreaterThanMaxFeePerBlobGas`. The user is misled about transaction viability — the simulation result is inconsistent with execution reality. Wallets and sequencer-facing tooling that rely on `eth_estimateGas` to pre-validate transactions will silently accept transactions that will always fail on-chain.

---

### Likelihood Explanation

The entry path is fully unprivileged: any external caller can invoke `eth_estimateGas` / `eth_call` with a crafted EIP-4844 transaction. The trigger condition (`max_fee_per_blob_gas < block_blob_base_fee`) is realistic whenever blob fees are non-zero and a user sets `max_fee_per_blob_gas` to zero (e.g., to probe gas cost). The docs note EIP-4844 is not currently enabled in production, which reduces immediate likelihood, but the code path is present and the inconsistency is structural.

---

### Recommendation

Apply the blob base fee check unconditionally, or mirror the same conditional logic in the balance check. The simplest fix is to remove `&& !Config::SIMULATION` from the blob fee guard so that simulation and execution enforce the same constraint:

```rust
// Before (inconsistent):
if &block_base_fee_per_blob_gas > tx_max_fee_per_blob_gas && !Config::SIMULATION {

// After (consistent):
if &block_base_fee_per_blob_gas > tx_max_fee_per_blob_gas {
```

Alternatively, if simulation must remain permissive for tooling reasons, the balance check should also use `block_blob_base_fee` (not `tx.max_fee_per_blob_gas`) so that `required_balance()` and `fee_to_prepay` are computed from the same source, and the simulation result accurately reflects what execution would charge.

---

### Proof of Concept

The existing test in `tests/instances/transactions/src/lib.rs` at line 1579 directly demonstrates the divergence:

```rust
// max_fee_per_blob_gas = 0, but block blob_fee = 1
let tx = TxEip4844 { max_fee_per_blob_gas: 0, ... };
let block_context = BlockContext { blob_fee: U256::from(1), ... };
let result_simulation = tester.simulate_block(vec![tx_envelope]);
// Asserts simulation SUCCEEDS — but actual execution would fail
assert!(result_simulation.tx_results[0].is_ok(), ...);
```

The blob fee check at line 405 of `validation_impl.rs` would reject this transaction in actual execution (`block_blob_base_fee=1 > max_fee_per_blob_gas=0`), but simulation skips it entirely. The simulation returns a gas estimate that is unreachable in practice. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L400-409)
```rust
    let blobs = if let Some(blobs_list) = transaction.blobs() {
        let tx_max_fee_per_blob_gas = transaction.max_fee_per_blob_gas().ok_or(internal_error!(
            "Tx with blobs must define max_fee_per_blob_gas"
        ))?;

        if &block_base_fee_per_blob_gas > tx_max_fee_per_blob_gas && !Config::SIMULATION {
            return Err(TxError::Validation(
                InvalidTransaction::BlobBaseFeeGreaterThanMaxFeePerBlobGas,
            ));
        }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L438-451)
```rust
    // Balance check - originator must cover fee prepayment plus whatever "value" it would like to send along
    let Some(total_required_balance) = transaction.required_balance() else {
        return Err(TxError::Validation(
            InvalidTransaction::OverflowPaymentInTransaction,
        ));
    };
    if total_required_balance > originator_account_data.nominal_token_balance.0 {
        return Err(TxError::Validation(
            InvalidTransaction::LackOfFundForMaxFee {
                fee: total_required_balance,
                balance: originator_account_data.nominal_token_balance.0,
            },
        ));
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L464-491)
```rust
    // Note: no need to feature gate this part, as for non-EIP4844 transactions
    // num_blobs will be 0.
    let num_blobs = system.metadata.num_blobs();
    // NOTE: it's a special resource - not transaction gas. Will be used to charge fee only
    let blob_gas_used = num_blobs as u64 * GAS_PER_BLOB;
    let fee_for_blob_gas = if blob_gas_used > 0 {
        system_log!(
            system,
            "Blob gas price = {}\n",
            &system.get_blob_base_fee_per_gas()
        );

        let Some(value) = system
            .get_blob_base_fee_per_gas()
            .checked_mul(U256::from(blob_gas_used))
        else {
            return Err(TxError::Validation(
                InvalidTransaction::OverflowPaymentInTransaction,
            ));
        };

        value
    } else {
        U256::ZERO
    };
    let fee_to_prepay = gas_fee_amount
        .checked_add(fee_for_blob_gas)
        .ok_or(internal_error!("gfa+ffbg"))?;
```

**File:** basic_bootloader/src/bootloader/transaction/rlp_encoded/transaction.rs (L224-241)
```rust
    pub fn required_balance(&self) -> Option<U256> {
        match &self.inner {
            RlpEncodedTxInner::EIP4844(tx, _) => {
                let gas_fee = self
                    .max_fee_per_gas()
                    .checked_mul(U256::from(self.gas_limit()))?;
                let blob_gas = GAS_PER_BLOB.checked_mul(tx.blob_versioned_hashes.count as u64)?;
                let blob_fee = tx.max_fee_per_blob_gas.checked_mul(U256::from(blob_gas))?;
                self.value().checked_add(blob_fee)?.checked_add(gas_fee)
            }
            _ => {
                let fee_amount = self
                    .max_fee_per_gas()
                    .checked_mul(U256::from(self.gas_limit()))?;
                self.value().checked_add(U256::from(fee_amount))
            }
        }
    }
```

**File:** basic_bootloader/src/bootloader/config.rs (L1-32)
```rust
pub trait BasicBootloaderExecutionConfig: 'static + Clone + Copy + core::fmt::Debug {
    /// Flag to disable EOA signature validation.
    /// It can be used to optimize forward run.
    const VALIDATE_EOA_SIGNATURE: bool;
    /// Simulation flag(used for `eth_call` and `estimate_gas`)
    const SIMULATION: bool;
}

#[derive(Clone, Copy, Debug)]
pub struct BasicBootloaderProvingExecutionConfig;

impl BasicBootloaderExecutionConfig for BasicBootloaderProvingExecutionConfig {
    const SIMULATION: bool = false;
    const VALIDATE_EOA_SIGNATURE: bool = true;
}

#[derive(Clone, Copy, Debug)]
pub struct BasicBootloaderForwardSimulationConfig;

impl BasicBootloaderExecutionConfig for BasicBootloaderForwardSimulationConfig {
    const VALIDATE_EOA_SIGNATURE: bool = false;
    const SIMULATION: bool = false;
}

#[derive(Clone, Copy, Debug)]
pub struct BasicBootloaderCallSimulationConfig;

impl BasicBootloaderExecutionConfig for BasicBootloaderCallSimulationConfig {
    // doesn't really matter, as `SIMULATION` disables signature validation anyway
    const VALIDATE_EOA_SIGNATURE: bool = true;
    const SIMULATION: bool = true;
}
```

**File:** tests/instances/transactions/src/lib.rs (L1579-1614)
```rust
#[test]
fn test_simulation_4844_zero_blob_fee_allowed() {
    let mut tester = TestingFramework::new();
    let wallet = tester.prefunded_random_signer();
    let target_address = common_target_address();

    let tx = TxEip4844 {
        chain_id: 37u64,
        nonce: 0,
        max_fee_per_gas: 1_000,
        max_priority_fee_per_gas: 1_000,
        gas_limit: 75_000,
        to: target_address,
        value: U256::ZERO,
        input: Default::default(),
        access_list: Default::default(),
        blob_versioned_hashes: vec![b256!(
            "0x011122223333444455556666777788889999aaaabbbbccccddddeeeeffff0000"
        )],
        max_fee_per_blob_gas: 0,
    };
    let tx_envelope = ZKsyncTxEnvelope::from_eth_tx(tx, wallet.clone());

    let block_context = BlockContext {
        blob_fee: U256::from(1),
        ..Default::default()
    };

    tester = tester.with_block_context(block_context);
    let result_simulation = tester.simulate_block(vec![tx_envelope]);
    assert!(
        result_simulation.tx_results[0].is_ok(),
        "EIP-4844 tx should pass simulation when blob_fee > 0 and max_fee_per_blob_gas = 0, got: {:?}",
        result_simulation.tx_results[0]
    );
}
```

**File:** forward_system/src/run/mod.rs (L475-516)
```rust
pub fn simulate_tx<S: ReadStorage, PS: PreimageSource>(
    transaction: EncodedTx,
    block_context: BlockContext,
    storage: S,
    preimage_source: PS,
    tracer: &mut impl Tracer<CallSimulationSystem>,
    validator: &mut impl TxValidator<CallSimulationSystem>,
) -> Result<TxResult, ForwardSubsystemError> {
    let tx_source = TxListSource {
        transactions: vec![transaction].into(),
    };

    let block_metadata_responder = BlockMetadataResponder {
        block_metadata: block_context,
    };
    let tx_data_responder = TxDataResponder {
        tx_source,
        next_tx: None,
        next_tx_format: None,
        next_tx_from: None,
    };
    let preimage_responder = GenericPreimageResponder { preimage_source };
    let storage_responder = ReadStorageResponder { storage };

    let mut oracle = ZkEENonDeterminismSource::default();
    oracle.add_external_processor(block_metadata_responder);
    oracle.add_external_processor(tx_data_responder);
    oracle.add_external_processor(preimage_responder);
    oracle.add_external_processor(storage_responder);

    let mut result_keeper = ForwardRunningResultKeeper::new(NoopTxCallback);

    CallSimulationBootloader::run_prepared::<BasicBootloaderCallSimulationConfig>(
        oracle,
        &mut (),
        &mut result_keeper,
        tracer,
        validator,
    )
    .map_err(wrap_error!())?;
    let mut block_output: BlockOutput = result_keeper.into();
    Ok(block_output.tx_results.remove(0))
```
