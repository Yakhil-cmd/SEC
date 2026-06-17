### Title
`native_per_pubdata` Truncates to Zero via Floor Division When `pubdata_price < native_price`, Allowing Pubdata to Be Written at Zero Native-Resource Cost — (`basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

When the operator-set `pubdata_price` is less than `native_price`, the integer floor division used to compute `native_per_pubdata` truncates to zero. Because every downstream pubdata-cost check multiplies by this zero value, any amount of pubdata written during a transaction costs the user **zero native resources**. The user still pays EVM gas (ergs) for SSTORE opcodes, but the native-resource dimension — which models proving cost — is entirely bypassed. This is the direct analog of the "round-up shares" rounding-to-zero class: a division that silently collapses to zero lets a user consume a protocol resource for free.

---

### Finding Description

**Root cause — asymmetric rounding between `native_per_gas` and `native_per_pubdata`**

In `validate_and_compute_fee_for_transaction` (L2 transaction path):

```rust
// line 135 — rounds UP: user always pays ≥ 1 native per gas
u256_try_to_u64(&gas_price.div_ceil(native_price))

// line 142 — rounds DOWN (floor): collapses to 0 when pubdata_price < native_price
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
``` [1](#0-0) 

The same floor-division pattern is replicated in the off-chain helper used for pre-flight validation:

```rust
// api/src/helpers.rs line 424 — div_ceil for gas
let native_per_gas = u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(())?;
// line 427 — wrapping_div (floor) for pubdata
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
``` [2](#0-1) 

**Propagation — zero multiplied through every pubdata charge**

`native_per_pubdata` is passed into `create_resources_for_tx` (intrinsic pubdata overhead) and into `get_resources_to_charge_for_pubdata` / `check_enough_resources_for_pubdata` (execution pubdata):

```rust
// gas_helpers.rs line 430-432
let native = current_pubdata_spent
    .checked_mul(native_per_pubdata)   // 0 × anything = 0
    .ok_or(out_of_native_resources!())?;
``` [3](#0-2) 

When `native_per_pubdata == 0`, `resources_for_pubdata` is always the zero resource, so `check_enough_resources_for_pubdata` always returns `true` regardless of how many pubdata bytes were written. [4](#0-3) 

The same zero is used in the post-execution pubdata check inside the ZK transaction body: [5](#0-4) 

---

### Impact Explanation

When `pubdata_price < native_price` (e.g., `pubdata_price = 50`, `native_price = 100`), the operator intends users to pay `0.5` native units per pubdata byte, but the floor division yields `native_per_pubdata = 0`. Every SSTORE a user executes writes pubdata to L1 at **zero native-resource cost**. The user pays only EVM gas (ergs), while the operator must prove and publish the pubdata without receiving the corresponding native-resource compensation. Repeated across many transactions or many SSTORE-heavy contracts, this constitutes a systematic under-payment for proving work — a resource-accounting loss for the operator/protocol.

---

### Likelihood Explanation

The condition `pubdata_price < native_price` is a plausible operational state: `native_price` reflects the cost of a single proving cycle (a computational unit), while `pubdata_price` reflects the cost of publishing one byte to L1. These are independently set by the operator and can legitimately satisfy `pubdata_price < native_price` during periods of low L1 data costs or high proving costs. No attacker action is required to create the condition; the attacker only needs to submit SSTORE-heavy transactions while the condition holds.

---

### Recommendation

Replace `wrapping_div` with `div_ceil` for `native_per_pubdata`, mirroring the treatment of `native_per_gas`:

```rust
// Before (truncates to 0 when pubdata_price < native_price):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;

// After (rounds up, consistent with native_per_gas):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

Apply the same fix in `api/src/helpers.rs` line 427. Optionally, add an explicit guard: if `pubdata_price > 0` then `native_per_pubdata` must be `>= 1`.

---

### Proof of Concept

**Setup:**
- Operator sets `native_price = 100`, `pubdata_price = 50`.
- `native_per_pubdata = 50 / 100 = 0` (floor division).

**Attacker steps:**
1. Submit an L2 transaction with `gas_limit = 1_000_000`, `max_fee_per_gas = 100` (satisfies `native_per_gas = ceil(100/100) = 1`).
2. Transaction body executes a loop of SSTORE operations writing to 40+ distinct storage slots (each SSTORE costs ~20 000 gas, writing one pubdata slot each).
3. Post-execution: `check_enough_resources_for_pubdata` computes `40 * 0 = 0` native required → always passes.
4. Transaction succeeds; attacker pays only EVM gas for the SSTOREs, zero native resources for the pubdata.
5. Operator must prove and publish 40 pubdata slots to L1 without receiving the intended `40 * 0.5 = 20` native units of compensation.

Repeat across many transactions to systematically drain the operator's proving budget relative to collected fees. [6](#0-5) [7](#0-6)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L135-143)
```rust
            u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
                InvalidTransaction::NativeResourcesAreTooExpensive,
            ))?
        }
    };

    // We checked native_price != 0 above
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
        .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

**File:** api/src/helpers.rs (L424-427)
```rust
    let native_per_gas = u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(())?;

    // native_per_pubdata = pubdata_price / native_price
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L422-435)
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
}
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L445-455)
```rust
pub fn check_enough_resources_for_pubdata<S: EthereumLikeTypes>(
    system: &mut System<S>,
    native_per_pubdata: u64,
    resources: &S::Resources,
    base_pubdata: Option<u64>,
) -> Result<(bool, S::Resources, u64), SystemError> {
    let (pubdata_used, resources_for_pubdata) =
        get_resources_to_charge_for_pubdata(system, native_per_pubdata, base_pubdata)?;
    system_log!(system, "Checking gas for pubdata, resources_for_pubdata: {resources_for_pubdata:?}, resources: {resources:?}\n");
    let enough = resources.has_enough(&resources_for_pubdata);
    Ok((enough, resources_for_pubdata, pubdata_used))
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L880-897)
```rust
        let (has_enough, to_charge_for_pubdata, pubdata_used) = check_enough_resources_for_pubdata(
            system,
            context.native_per_pubdata,
            &resources_for_check,
            Some(context.validation_pubdata),
        )?;
        if !has_enough {
            execution_result = execution_result.to_reverted();
            system_log!(system, "Not enough gas for pubdata after execution\n");
            // Burn all remaining ergs.
            context.resources.main_resources.exhaust_ergs();
            Ok((
                execution_result.to_reverted(),
                CachedPubdataInfo {
                    pubdata_used,
                    to_charge_for_pubdata,
                },
            ))
```
