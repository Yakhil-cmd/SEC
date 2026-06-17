### Title
`native_per_pubdata` Truncates to Zero via Integer Division, Allowing Free Pubdata Writes - (`basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

In ZKsync OS's ZK transaction validation path, `native_per_pubdata` is computed using truncating integer division (`wrapping_div`). When `pubdata_price < native_price`, the result truncates to `0`. This causes the entire pubdata native-resource charge to be silently zeroed out for every transaction in the block, allowing any transaction sender to write unlimited pubdata without paying native resources for it.

---

### Finding Description

In `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`, the ratio of pubdata cost to native cost is computed as:

```rust
// line 142
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
``` [1](#0-0) 

`wrapping_div` is plain truncating integer division. When `pubdata_price < native_price`, the result is `0`. This is in direct contrast to the `native_per_gas` calculation on line 135, which correctly uses `div_ceil`:

```rust
// line 135
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [2](#0-1) 

The `native_per_pubdata` value flows into two downstream charging sites:

**1. Intrinsic pubdata overhead** in `create_resources_for_tx`:
```rust
let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
``` [3](#0-2) 

When `native_per_pubdata_byte = 0`, `intrinsic_pubdata_overhead = 0`, so no native resources are reserved for intrinsic pubdata.

**2. Execution pubdata charge** in `get_resources_to_charge_for_pubdata`:
```rust
let native = current_pubdata_spent
    .checked_mul(native_per_pubdata)
    .ok_or(out_of_native_resources!())?;
``` [4](#0-3) 

When `native_per_pubdata = 0`, `native = 0`, so all pubdata written during execution is charged 0 native resources.

The same truncation is also present in the API helper at `api/src/helpers.rs` line 427:
```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
``` [5](#0-4) 

The documentation confirms the intended formula is `nativePerPubdata := pubdataPrice / nativePrice`, but does not specify rounding direction. The `native_per_gas` formula uses `div_ceil` precisely to avoid this truncation-to-zero problem, but `native_per_pubdata` does not.

---

### Impact Explanation

When `pubdata_price < native_price`, `native_per_pubdata` becomes `0`. Every transaction in the block then:

- Pays **zero** native resources for intrinsic pubdata (account writes, nonce updates, etc.)
- Pays **zero** native resources for any storage writes, event logs, or other pubdata generated during execution

The native resource budget (`native_prepaid_from_gas = native_per_gas * gas_limit`) is only consumed by computational operations, not pubdata. A transaction sender can write the maximum amount of pubdata per block at no native cost. The operator bears the L1 data publication cost (calldata or blob fees) without being compensated through native resource charges.

This is a **resource accounting bug** — the direct analog of the Surge Protocol interest truncation: a fee that should be non-zero is silently zeroed by integer division, causing the protocol to subsidize a cost that should be borne by the user.

---

### Likelihood Explanation

`pubdata_price` and `native_price` are operator-set block context parameters. `native_price` reflects the cost of a single RISC-V proving cycle (in wei), while `pubdata_price` reflects the cost of one byte of L1 pubdata (in wei). These are independent quantities with different units and magnitudes.

During periods of low L1 gas prices (low blob/calldata fees) or high proving costs, it is entirely plausible that `pubdata_price < native_price`. Any transaction sender observing this condition can then write large amounts of pubdata (e.g., by writing to many storage slots) without paying native resources for it. The condition is detectable on-chain by reading the block context, making it trivially exploitable by any unprivileged transaction sender.

---

### Recommendation

Replace `wrapping_div` with `div_ceil` for the `native_per_pubdata` calculation, consistent with how `native_per_gas` is computed:

```rust
// Before (truncates to 0 when pubdata_price < native_price):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;

// After (rounds up, never silently zeroes):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

Apply the same fix to `api/src/helpers.rs` line 427. Optionally, add a validation check that rejects transactions (or blocks) where `native_per_pubdata == 0` and `pubdata_price != 0`, to make the truncation visible rather than silent.

---

### Proof of Concept

**Setup:**
- Operator sets `native_price = 1000` (1000 wei per RISC-V cycle)
- Operator sets `pubdata_price = 999` (999 wei per pubdata byte)
- These are valid, realistic values where pubdata is slightly cheaper than proving

**Calculation:**
```
native_per_pubdata = pubdata_price.wrapping_div(native_price)
                   = 999 / 1000
                   = 0  (integer truncation)
```

**Exploit:**
1. Attacker submits a transaction that writes to 100 storage slots (generating ~3200 bytes of pubdata)
2. `get_resources_to_charge_for_pubdata` computes: `native = 3200 * 0 = 0`
3. Attacker pays 0 native resources for 3200 bytes of pubdata
4. Operator publishes this pubdata to L1 at their own expense (~3200 × 999 = ~3.2M wei per such transaction)
5. Attacker can repeat this for every transaction in every block where `pubdata_price < native_price`

The attacker's entry path is a standard unprivileged L2 transaction. No privileged access, oracle manipulation, or external dependency is required — only the observation that `pubdata_price < native_price` in the current block context.

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L134-138)
```rust
        } else {
            u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
                InvalidTransaction::NativeResourcesAreTooExpensive,
            ))?
        }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L141-143)
```rust
    // We checked native_price != 0 above
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
        .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L352-353)
```rust
    let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
    let native_limit = match native_limit.checked_sub(intrinsic_pubdata_overhead) {
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L430-432)
```rust
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
```

**File:** api/src/helpers.rs (L426-427)
```rust
    // native_per_pubdata = pubdata_price / native_price
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
```
