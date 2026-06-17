### Title
Native Resource Overcharge on No-Op SSTORE (Same-Value Write) - (File: `basic_system/src/system_implementation/system/mod.rs`)

### Summary

`charge_storage_write_extra` in `EthereumLikeStorageAccessCostModel` correctly zeroes the EVM ergs cost when `new_value == current_value` (a no-op SSTORE), but unconditionally charges a non-zero native resource cost regardless of whether the write is a no-op. This causes every no-op SSTORE to consume native resources it should not, inflating `native_used`, which in turn inflates `deltaGas` and causes the user to be charged more EVM gas than the EVM specification requires.

### Finding Description

In `charge_storage_write_extra`:

```rust
// basic_system/src/system_implementation/system/mod.rs  lines 80-113
let ergs = match ee_type {
    ExecutionEnvironmentType::EVM => {
        let total_cost = if new_value == current_value {
            0   // ← ergs correctly zeroed for no-op
        } else { ... };
        Ergs(total_cost * ERGS_PER_GAS)
    }
};
// ← native cost is NOT gated on new_value != current_value
let native = if is_new_slot {
    R::Native::from_computational(COLD_NEW_STORAGE_WRITE_EXTRA_NATIVE_COST)
} else {
    R::Native::from_computational(COLD_EXISTING_STORAGE_WRITE_EXTRA_NATIVE_COST)
};
resources.charge(&R::from_ergs_and_native(ergs, native))  // native always charged
```

The EVM ergs branch at line 83 correctly returns `0` when `new_value == current_value`. However, the native cost block at lines 105–113 has no such guard: it always charges either `COLD_NEW_STORAGE_WRITE_EXTRA_NATIVE_COST` (`native_with_delegations!(100_000, 0, 1300)`) or `COLD_EXISTING_STORAGE_WRITE_EXTRA_NATIVE_COST` (`native_with_delegations!(40_000, 0, 660)`) unconditionally.

This function is called from `apply_write_impl` in `generic_pubdata_aware_plain_storage.rs` on every SSTORE, including no-op ones:

```rust
// basic_system/src/system_implementation/caches/generic_pubdata_aware_plain_storage.rs  lines 263-271
self.resources_policy.charge_storage_write_extra(
    ee_type,
    &val_at_tx_start,
    val_current,
    new_value,
    resources,
    is_warm_read.0,
    is_new_slot,
)?;
```

The `sstore` opcode handler in `evm_interpreter/src/instructions/host.rs` (lines 148–188) calls `system.io.storage_write` unconditionally, which routes through `apply_write_impl`, so every SSTORE — including no-ops — reaches this charging path.

### Impact Explanation

ZKsync OS uses a double resource accounting model. After execution, `compute_gas_refund` in `basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs` computes:

```
deltaGas = (native_used / native_per_gas) - gas_used
if deltaGas > 0: gas_used += deltaGas
```

Every no-op SSTORE inflates `native_used` by `COLD_EXISTING_STORAGE_WRITE_EXTRA_NATIVE_COST` (≈40,000 native units) without any corresponding EVM gas consumption. This makes `deltaGas` positive, causing the user to be charged extra EVM gas beyond what the EVM specification mandates. A contract that performs many no-op SSTOREs (e.g., repeatedly setting a flag that is already set, or re-confirming an existing value) will be systematically overcharged. In the worst case, a transaction can exhaust its native resource budget and revert, even though the equivalent EVM execution would succeed within the declared gas limit.

### Likelihood Explanation

No-op SSTOREs are common in production Solidity contracts: reentrancy guards that are already set, idempotent state updates, and ERC-20 `approve` calls that re-approve the same amount all produce `new_value == current_value` writes. Any unprivileged user sending an EVM transaction to such a contract triggers the overcharge. The entry path requires no special privileges, no oracle manipulation, and no governance access.

### Recommendation

Gate the native cost on the same condition used for ergs — skip the native charge when `new_value == current_value`:

```rust
let native = if new_value == current_value {
    R::Native::empty()
} else if is_new_slot {
    R::Native::from_computational(COLD_NEW_STORAGE_WRITE_EXTRA_NATIVE_COST)
} else {
    R::Native::from_computational(COLD_EXISTING_STORAGE_WRITE_EXTRA_NATIVE_COST)
};
```

### Proof of Concept

1. Deploy an EVM contract containing:
   ```solidity
   function noopSstore() external {
       assembly { sstore(0, sload(0)) }  // write current value back
   }
   ```
2. Call `noopSstore()` with a known `gas_price` and `native_price`.
3. Observe that `gas_used` reported by the bootloader exceeds the EVM-only gas cost (100 gas for a warm no-op SSTORE per EIP-2200), because `native_used` is inflated by `COLD_EXISTING_STORAGE_WRITE_EXTRA_NATIVE_COST` ≈ 40,000 native units, which adds `40_000 / native_per_gas` to `deltaGas` and thus to the final `gas_used`.
4. Repeat with a loop of 1,000 no-op SSTOREs; the cumulative native overcharge can exhaust the native budget and cause an OOG revert that would not occur on a standard EVM. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** basic_system/src/system_implementation/system/mod.rs (L80-113)
```rust
        let ergs = match ee_type {
            ExecutionEnvironmentType::NoEE => Ergs::empty(),
            ExecutionEnvironmentType::EVM => {
                let total_cost = if new_value == current_value {
                    0
                } else if current_value == initial_value {
                    if initial_value.is_zero() {
                        // we do not purge slots, so we use another indicator here
                        SSTORE_SET_EXTRA
                    } else {
                        SSTORE_RESET_EXTRA
                    }
                } else {
                    0
                };

                let total_cost =
                    // In EVM spec there's a discrepancy for cold read and cold write costs. Cold
                    // writes add another 100 from thin air.
                    if is_warm_write == false { total_cost + 100 }
                    else { total_cost };

                Ergs(total_cost * ERGS_PER_GAS)
            }
        };
        let native = if is_new_slot {
            R::Native::from_computational(
                crate::system_implementation::flat_storage_model::cost_constants::COLD_NEW_STORAGE_WRITE_EXTRA_NATIVE_COST,
            )
        } else {
            R::Native::from_computational(
          crate::system_implementation::flat_storage_model::cost_constants::COLD_EXISTING_STORAGE_WRITE_EXTRA_NATIVE_COST,)
        };
        resources.charge(&R::from_ergs_and_native(ergs, native))
```

**File:** basic_system/src/system_implementation/caches/generic_pubdata_aware_plain_storage.rs (L257-271)
```rust
        let val_current = addr_data.current().value();

        // Try to get initial value at the beginning of the tx.
        let val_at_tx_start = addr_data.committed().value().clone();

        let is_new_slot = addr_data.element_properties().is_new_element();
        self.resources_policy.charge_storage_write_extra(
            ee_type,
            &val_at_tx_start,
            val_current,
            new_value,
            resources,
            is_warm_read.0,
            is_new_slot,
        )?;
```

**File:** evm_interpreter/src/instructions/host.rs (L148-170)
```rust
    pub fn sstore(
        &mut self,
        system: &mut System<S>,
        tracer: &mut impl Tracer<S>,
    ) -> InstructionResult {
        self.gas.spend_gas_and_native(0, SSTORE_NATIVE_COST)?;
        if self.is_static_frame() {
            return Err(EvmError::StateChangeDuringStaticCall.into());
        }
        if self.gas.gas_left() <= CALL_STIPEND {
            return Err(EvmError::InvalidOperandOOG.into());
        }
        let (index, value) = self.stack.pop_2()?;
        let index = Bytes32::from_u256_be(index);
        let value = Bytes32::from_u256_be(value);

        system.io.storage_write::<false>(
            THIS_EE_TYPE,
            self.gas.resources_mut(),
            &self.address,
            &index,
            &value,
        )?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L64-84)
```rust
    let native_used = full_native_limit.saturating_sub(resources.native().remaining().as_u64());

    #[cfg(not(feature = "unlimited_native"))]
    {
        // Adjust gas_used with difference with used native
        let delta_gas = if native_per_gas == 0 {
            0
        } else {
            (native_used / native_per_gas) as i64 - (gas_used as i64)
        };

        if delta_gas > 0 {
            // In this case, the native resource consumption is more than the
            // gas consumption accounted for. Consume extra gas.
            gas_used += delta_gas as u64;
        }
        // TODO: return delta_gas to gas_used?
    }

    let total_gas_refund = gas_limit - gas_used;
    system_log!(system, "Refund after accounting for unused gas, refund counters and native cost: {total_gas_refund}\n");
```

**File:** basic_system/src/system_implementation/flat_storage_model/cost_constants.rs (L19-22)
```rust
pub const COLD_EXISTING_STORAGE_WRITE_EXTRA_NATIVE_COST: u64 =
    native_with_delegations!(40_000, 0, 660);
pub const COLD_NEW_STORAGE_WRITE_EXTRA_NATIVE_COST: u64 =
    native_with_delegations!(100_000, 0, 1300);
```
