### Title
Hardcoded `L1_TX_NATIVE_PRICE` Constant Causes Stale Native Resource Accounting for L1→L2 Transactions - (`basic_bootloader/src/bootloader/constants.rs`)

---

### Summary

`L1_TX_NATIVE_PRICE` is a compile-time constant set to `10` used exclusively for L1→L2 (priority) transactions to compute the `native_per_gas` ratio. For L2 transactions the operator supplies a dynamic `native_price` per block, but for L1→L2 transactions this hardcoded value is used instead. The constant is explicitly marked with a `TODO` acknowledging it has no principled value. If the real proving cost diverges from `10`, L1→L2 transactions receive a `native_limit` that is either far too large (attacker gets proving work for free) or far too small (bridge becomes unusable).

---

### Finding Description

ZKsync OS implements a double resource accounting model. The `native_per_gas` ratio — which converts EVM gas into native RISC-V proving cycles — is computed as `gas_price / native_price`. For L2 transactions, `native_price` is a dynamic operator-supplied value from `BlockMetadataFromOracle.native_price`.

For L1→L2 transactions, however, `prepare_and_check_resources` in `process_l1_transaction.rs` ignores the block-level `native_price` entirely and substitutes the hardcoded constant:

```rust
// For L1->L2 txs, we use a constant native price to avoid censorship.
let native_price = L1_TX_NATIVE_PRICE;
``` [1](#0-0) 

The constant is defined as:

```rust
// Default native price for L1->L2 transactions.
// TODO (EVM-1157): find a reasonable value for it.
pub const L1_TX_NATIVE_PRICE: U256 = U256::from_limbs([10, 0, 0, 0]);
``` [2](#0-1) 

The `native_per_gas` and `native_limit` are then derived from this constant:

```rust
u256_try_to_u64(&gas_price.div_ceil(native_price))  // native_per_gas = gas_price / 10
...
let native_prepaid_from_gas = native_per_gas.checked_mul(gas_limit);  // native_limit = gas_limit * native_per_gas
``` [3](#0-2) 

For L2 transactions, the operator dynamically adjusts `native_price` in `BlockMetadataFromOracle` to reflect actual proving costs: [4](#0-3) 

The docs explicitly confirm the asymmetry: *"Note: for L1->L2 transactions we use a code constant instead of one provided by operator."* [5](#0-4) 

---

### Impact Explanation

**Scenario A — Real proving cost rises above 10 (e.g., `native_price = 1000` for L2 txs):**

An L1→L2 transaction with `gas_price = 100` receives `native_per_gas = 100/10 = 10`. An equivalent L2 transaction receives `native_per_gas = 100/1000 = 1`. The L1→L2 transaction is allocated **10× more native resources** for the same fee. An attacker can submit computationally expensive L1→L2 transactions (e.g., large calldata, complex EVM execution) and force the prover to perform work far exceeding what was paid for. This is a direct financial loss for the operator/prover and a sustained DoS vector against the proving infrastructure.

**Scenario B — Real proving cost falls below 10:**

L1→L2 transactions receive fewer native resources than they should, causing them to run out of native resources mid-execution and revert. The L1→L2 bridge becomes unreliable or unusable without a code upgrade.

Since L1→L2 transactions **cannot be invalidated** (doing so would halt the chain), the system must process them regardless, amplifying the impact of Scenario A. [6](#0-5) 

---

### Likelihood Explanation

The `TODO (EVM-1157)` comment in the source code explicitly acknowledges that no principled value has been determined for `L1_TX_NATIVE_PRICE`. The value `10` matches the test default `native_price` in `BlockMetadataFromOracle::new_for_test()`, suggesting it was copied from a test fixture rather than calibrated against real proving costs. As the system matures and the operator adjusts `native_price` for L2 transactions to reflect actual costs, the divergence from the hardcoded `10` will grow. Any unprivileged user with access to the L1 bridge can trigger this path. [7](#0-6) 

---

### Recommendation

Replace the hardcoded `L1_TX_NATIVE_PRICE` with a value derived from the current block's operator-supplied `native_price`, subject to a floor/cap to preserve the censorship-resistance property. Alternatively, if a fixed constant is required for anti-censorship reasons, it should be set via a governance-updatable parameter rather than a compile-time constant, and its value should be calibrated against actual proving benchmarks (tracking issue EVM-1157).

---

### Proof of Concept

1. Operator sets `native_price = 1000` in block metadata (reflecting real proving costs).
2. Attacker submits an L1→L2 priority transaction with `gas_price = 100`, `gas_limit = 1_000_000`, and maximum-complexity calldata/execution.
3. `prepare_and_check_resources` computes `native_per_gas = ceil(100 / 10) = 10` using the hardcoded constant instead of `ceil(100 / 1000) = 1`.
4. `native_limit = 1_000_000 * 10 = 10_000_000` native cycles are allocated.
5. The correct allocation would be `1_000_000 * 1 = 1_000_000` cycles.
6. The attacker's transaction consumes up to 10× more proving resources than paid for.
7. Because L1→L2 transactions cannot be invalidated, the sequencer must include and prove the transaction regardless. [8](#0-7) [2](#0-1)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L422-431)
```rust
///
/// Compute and perform some checks on fee/resource parameters.
/// This function handles cases that for L2 transactions would be
/// validation errors, as "invalidating" an L1 transaction can halt
/// the chain (due to the priority queue).
/// Note that the "validation errors" are practically unreachable, as
/// gas_limit, gas_price and gas_per_pubdata are either checked or set
/// by the L1 contracts. We decide to handle these cases as a fallback in
/// case the L1 contracts aren't properly updated to reflect a change in
/// ZKsync OS.
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L453-496)
```rust
    // For L1->L2 txs, we use a constant native price to avoid censorship.
    let native_price = L1_TX_NATIVE_PRICE;
    let native_per_gas = if is_priority_op {
        if gas_price.is_zero() {
            if Config::SIMULATION {
                u256_try_to_u64(&system.get_eip1559_basefee().div_ceil(native_price))
                    .unwrap_or_else(|| {
                        system_log!(
                            system,
                            "Native per gas calculation for L1 tx overflows, using saturated arithmetic instead");
                        u64::MAX
                    })
            } else {
                FREE_L1_TX_NATIVE_PER_GAS
            }
        } else {
            u256_try_to_u64(&gas_price.div_ceil(native_price)).unwrap_or_else(|| {
                system_log!(
                    system,
                    "Native per gas calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
            })
        }
    } else {
        // Upgrade txs are paid by the protocol, so we use a fixed native per gas
        FREE_L1_TX_NATIVE_PER_GAS
    };

    let native_per_pubdata = (gas_per_pubdata as u64)
        .checked_mul(native_per_gas)
        .unwrap_or_else(|| {
            system_log!(
                system,
                "Native per pubdata calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
        });

    let native_prepaid_from_gas = native_per_gas.checked_mul(gas_limit)
        .unwrap_or_else(|| {
            system_log!(
                system,
                "Native prepaid from gas calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
        });
```

**File:** basic_bootloader/src/bootloader/constants.rs (L64-66)
```rust
// Default native price for L1->L2 transactions.
// TODO (EVM-1157): find a reasonable value for it.
pub const L1_TX_NATIVE_PRICE: U256 = U256::from_limbs([10, 0, 0, 0]);
```

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L122-131)
```rust
    pub eip1559_basefee: U256,
    pub pubdata_price: U256,
    pub native_price: U256,
    pub coinbase: B160,
    pub gas_limit: u64,
    pub pubdata_limit: u64,
    /// Source of randomness, currently holds the value
    /// of prevRandao.
    pub mix_hash: U256,
    pub blob_fee: U256,
```

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L206-221)
```rust
    pub fn new_for_test() -> Self {
        BlockMetadataFromOracle {
            eip1559_basefee: U256::from(1000u64),
            pubdata_price: U256::from(0u64),
            native_price: U256::from(10),
            block_number: 1,
            timestamp: 42,
            chain_id: 37,
            gas_limit: u64::MAX / 256,
            pubdata_limit: u64::MAX,
            coinbase: B160::ZERO,
            block_hashes: BlockHashes::default(),
            mix_hash: U256::ONE,
            blob_fee: U256::ZERO,
        }
    }
```

**File:** docs/double_resource_accounting.md (L34-34)
```markdown
- `nativePrice` be a constant set by the operator, reflecting the "cost of processing a single cycle". Note: for L1->L2 transactions we use a code constant instead of one provided by operator.
```
