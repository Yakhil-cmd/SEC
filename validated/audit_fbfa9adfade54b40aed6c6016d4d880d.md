### Title
Truncating Floor Division in `native_per_pubdata` Computation Undercharges Users for Pubdata — (`basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

The `native_per_pubdata` ratio — which controls how many native resources a user is charged per byte of pubdata — is computed using truncating (floor) integer division. In contrast, `native_per_gas` uses ceiling division. This asymmetry causes systematic undercharging for pubdata consumption, with precision loss up to 100% when `pubdata_price < native_price`. Any unprivileged transaction sender benefits from this discount at the operator's expense.

---

### Finding Description

In `validate_and_compute_fee_for_transaction`, the two key resource ratios are computed as follows:

**`native_per_gas` — uses ceiling division (protects the protocol):** [1](#0-0) 

**`native_per_pubdata` — uses floor/truncating division (undercharges users):** [2](#0-1) 

`wrapping_div` performs truncating integer division (floor toward zero). When `pubdata_price` is not an exact multiple of `native_price`, the result is rounded down. The same floor division appears in the public API helper: [3](#0-2) 

The `native_per_pubdata` value flows directly into pubdata charging: [4](#0-3) 

And into the intrinsic pubdata overhead deduction from the native limit: [5](#0-4) 

The design intent is documented in `docs/double_resource_accounting.md`: [6](#0-5) 

The `nativePerGas` ratio is explicitly rounded up (ceiling) to protect the protocol. No equivalent protection exists for `native_per_pubdata`.

---

### Impact Explanation

**Precision loss scenario:**
- `native_price = 1000`, `pubdata_price = 1500`
- True ratio: `1500 / 1000 = 1.5` native units per pubdata byte
- Floor result: `native_per_pubdata = 1`
- Undercharge: 33% per pubdata byte

**Extreme scenario (pubdata becomes free):**
- `native_price = 1000`, `pubdata_price = 999`
- `native_per_pubdata = floor(999/1000) = 0`
- `intrinsic_pubdata_overhead = 0 * intrinsic_pubdata = 0`
- `get_resources_to_charge_for_pubdata` returns 0 native for any pubdata amount
- Users generate unlimited pubdata with zero native resource cost

The operator sets `pubdata_price` to reflect real L1 data availability costs. When `native_per_pubdata` is truncated, the operator absorbs the difference between what users pay and the true L1 cost. Any unprivileged user submitting storage-writing transactions benefits from this discount.

---

### Likelihood Explanation

**Medium.** The precision loss occurs whenever `pubdata_price % native_price != 0`. Both values are set by the operator as block-level oracle parameters: [7](#0-6) 

Real-world L1 gas prices and native cycle costs are independent quantities with no reason to be exact multiples of each other. The larger `native_price` is relative to `pubdata_price`, the larger the relative precision loss. The asymmetry with `native_per_gas` (which uses `div_ceil`) confirms this is an unintentional inconsistency rather than a deliberate design choice.

---

### Recommendation

Apply ceiling division to `native_per_pubdata`, consistent with `native_per_gas`:

```rust
// In validation_impl.rs line 142
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

Apply the same fix in `api/src/helpers.rs` line 427:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price)).ok_or(())?;
```

---

### Proof of Concept

1. Operator sets block context: `native_price = 1000`, `pubdata_price = 999`, `eip1559_basefee = 1000`.
2. User submits a transaction that writes to 10 storage slots (generating ~320 bytes of pubdata).
3. Bootloader computes: `native_per_pubdata = floor(999/1000) = 0`.
4. `intrinsic_pubdata_overhead = 0 * intrinsic_pubdata = 0` — no native deducted for pubdata at setup.
5. Post-execution: `get_resources_to_charge_for_pubdata(system, 0, ...)` returns `native = 0 * 320 = 0`.
6. User pays **zero** native resources for 320 bytes of pubdata.
7. The operator pays the full L1 data availability cost for those 320 bytes.

The attacker-controlled entry path is a standard EVM transaction with storage writes — no privileged access required. The root cause is the `wrapping_div` at line 142 of `validation_impl.rs`. [2](#0-1) [8](#0-7)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L135-138)
```rust
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

**File:** api/src/helpers.rs (L426-427)
```rust
    // native_per_pubdata = pubdata_price / native_price
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L351-359)
```rust
    // Charge intrinsic pubdata
    let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
    let native_limit = match native_limit.checked_sub(intrinsic_pubdata_overhead) {
        Some(val) => val,
        None => P::handle_arithmetic_error(
            system,
            P::native_underflow_error("subtracting pubdata overhead"),
        )?,
    };
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L422-434)
```rust
pub fn get_resources_to_charge_for_pubdata<S: EthereumLikeTypes>(
    system: &mut System<S>,
    native_per_pubdata: u64,
    base_pubdata: Option<u64>,
) -> Result<(u64, S::Resources), SystemError> {
    let current_pubdata_spent = system
        .net_pubdata_used()?
        .saturating_sub(base_pubdata.unwrap_or(0));
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
    let native = <S::Resources as zk_ee::system::Resources>::Native::from_computational(native);
    Ok((current_pubdata_spent, S::Resources::from_native(native)))
```

**File:** docs/double_resource_accounting.md (L37-42)
```markdown
First we define the ratio between EVM gas and native resource as:
  `nativePerGas := gasPrice/nativePrice`
Note: for call simulation we use a constant for it, as gasPrice might be set to 0.

Next we define the limit for the native resource as:
  `nativeLimit := gasLimit * nativePerGas`
```

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L122-125)
```rust
    pub eip1559_basefee: U256,
    pub pubdata_price: U256,
    pub native_price: U256,
    pub coinbase: B160,
```
