### Title
Missing Native Resource Charge for CREATE/CREATE2 Initcode Word Cost — (File: `evm_interpreter/src/instructions/host.rs`)

---

### Summary

In the `create` function, the dynamic initcode word cost is charged only in ergs (EVM gas) but not in native resources (proving cost). ZKsync OS maintains two coupled resource accounting variables — ergs and native — that must always be updated together. One code path updates only ergs, creating a direct analog to the external report's state inconsistency bug.

---

### Finding Description

ZKsync OS implements **Double Resource Accounting**: every chargeable operation must deduct both `ergs` (EVM gas) and `native` (RISC-V proving cycles) simultaneously. The canonical pattern is `spend_gas_and_native(gas, native)`.

In `evm_interpreter/src/instructions/host.rs`, the `create` function correctly charges the base CREATE cost with both resources:

```rust
self.gas.spend_gas_and_native(
    gas_constants::CREATE,
    if IS_CREATE2 { CREATE2_NATIVE_COST } else { CREATE_NATIVE_COST },
)?;
``` [1](#0-0) 

However, the dynamic initcode word cost is charged using only `spend_gas`, which updates **only ergs** with zero native:

```rust
let cost_per_word = if IS_CREATE2 {
    INITCODE_WORD_COST + SHA3WORD   // 8 gas/word
} else {
    INITCODE_WORD_COST              // 2 gas/word
};
let initcode_cost = cost_per_word * (len as u64).div_ceil(32);
self.gas.spend_gas(initcode_cost)?;   // ← ergs only, native = 0
``` [2](#0-1) 

`spend_gas` is defined to construct a resource from ergs only, leaving native untouched:

```rust
pub(crate) fn spend_gas(&mut self, to_spend: u64) -> Result<(), ExitCode> {
    let resource_cost = S::Resources::from_ergs(Ergs(ergs_cost));
    self.resources.charge(&resource_cost)?;
    Ok(())
}
``` [3](#0-2) 

`from_ergs` sets native to empty:

```rust
fn from_ergs(ergs: Ergs) -> Self {
    Self { ergs, native: Native::empty() }
}
``` [4](#0-3) 

This is the exact structural analog to the external report: two coupled accounting variables (`ergs` ↔ `balance_amount`, `native` ↔ `total_amount`) that must always be updated together are only partially updated in one code path.

---

### Impact Explanation

The native resource drives two critical outcomes:

1. **`deltaGas` adjustment**: At the end of every transaction, `compute_gas_refund` computes `deltaGas = (native_used / native_per_gas) - gas_used`. If `deltaGas > 0`, extra gas is charged to cover proving costs. [5](#0-4) 

2. **Native OOG enforcement**: If native runs out, the transaction reverts. [6](#0-5) 

Because the initcode word cost is not charged to native, `native_used` is understated. The `deltaGas` correction is therefore smaller than it should be, and the user pays less gas than the actual proving cost demands. For maximum initcode (`MAX_INITCODE_SIZE = 49152` bytes = 1536 words):

- **CREATE**: 1536 × 2 = **3072 gas** worth of native is never charged.
- **CREATE2**: 1536 × 8 = **12288 gas** worth of native is never charged.

The operator/prover bears the uncompensated proving cost for every such deployment. At scale, an attacker can repeatedly deploy maximum-initcode contracts to systematically drain proving compensation from the operator.

---

### Likelihood Explanation

The entry path requires no privilege: any unprivileged user can send a standard EVM CREATE or CREATE2 transaction with large initcode. The `create` function is reached via the normal EVM opcode dispatch loop for opcodes `0xf0` (CREATE) and `0xf5` (CREATE2). [7](#0-6) 

The maximum initcode size is enforced but still allows up to 49152 bytes per deployment. The attack is cheap to execute repeatedly.

---

### Recommendation

Replace the ergs-only charge with a dual charge that includes a proportional native cost for initcode word processing:

```rust
// Before (ergs only):
self.gas.spend_gas(initcode_cost)?;

// After (ergs + native):
let initcode_native_cost = INITCODE_WORD_NATIVE_COST * (len as u64).div_ceil(32);
self.gas.spend_gas_and_native(initcode_cost, initcode_native_cost)?;
```

`INITCODE_WORD_NATIVE_COST` should be calibrated to reflect the actual RISC-V cycle cost of processing one 32-byte word of initcode during proving, consistent with how other per-byte/per-word native costs are set in `native_resource_constants.rs`. [8](#0-7) 

---

### Proof of Concept

1. Craft a CREATE2 transaction whose initcode is exactly `MAX_INITCODE_SIZE` bytes (49152 bytes = 1536 words).
2. Execute the transaction. The EVM charges `(INITCODE_WORD_COST + SHA3WORD) × 1536 = 12288` gas via `spend_gas`, but `native` is not decremented for this cost.
3. Observe via `RefundInfo.native_used` that the native consumption is lower than it would be if `spend_gas_and_native` were used.
4. The `deltaGas` correction is correspondingly smaller, and the user's final `gas_used` (and thus fee paid) is less than the true proving cost.
5. Repeat to systematically underpay proving costs at scale. [9](#0-8) [10](#0-9)

### Citations

**File:** evm_interpreter/src/instructions/host.rs (L287-373)
```rust
    pub fn create<const IS_CREATE2: bool>(
        &mut self,
        system: &mut System<S>,
        external_call_dest: &mut Option<EVMCallRequest<S>>,
        tracer: &mut impl Tracer<S>,
    ) -> InstructionResult {
        self.gas.spend_gas_and_native(
            gas_constants::CREATE,
            if IS_CREATE2 {
                native_resource_constants::CREATE2_NATIVE_COST
            } else {
                native_resource_constants::CREATE_NATIVE_COST
            },
        )?;

        if self.is_static_frame() {
            return Err(EvmError::StateChangeDuringStaticCall.into());
        }
        self.clear_last_returndata();

        let (value, code_offset, len) = self.stack.pop_3()?;
        let value = *value;

        let (code_offset, len) =
            Self::cast_offset_and_len(code_offset, len, EvmError::InvalidOperandOOG.into())?;

        Self::resize_heap_implementation(&mut self.heap, &mut self.gas, code_offset, len)?;

        // Create code size is limited
        if len > MAX_INITCODE_SIZE {
            return Err(EvmError::CreateInitcodeSizeLimit.into());
        }

        // Charge for dynamic gas
        let cost_per_word = if IS_CREATE2 {
            INITCODE_WORD_COST + SHA3WORD
        } else {
            INITCODE_WORD_COST
        };
        let initcode_cost = cost_per_word * (len as u64).div_ceil(32);
        self.gas.spend_gas(initcode_cost)?;
        let end = code_offset + len; // can not overflow as we resized heap above using same values

        // TODO: not necessary once heaps get the same treatment as calldata
        let deployment_code = code_offset..end;

        let deployed_address = if IS_CREATE2 {
            let salt = self.stack.pop_1()?;
            Self::derive_address_for_deployment_create2(
                system,
                self.gas.resources_mut(),
                salt,
                &self.address,
                &self.heap[deployment_code.clone()],
            )?
        } else {
            let deployer_nonce = self.gas.resources.with_infinite_ergs(|inf_resources| {
                system
                    .io
                    .read_nonce(THIS_EE_TYPE, inf_resources, &self.address)
            })?;

            Self::derive_address_for_deployment_create(
                self.gas.resources_mut(),
                &self.address,
                deployer_nonce,
            )?
        };

        // at this preemption point we give all resources to the system
        let all_resources = self.gas.take_resources();

        self.pending_os_request = Some(PendingOsRequest::Create(deployed_address));

        tracer.evm_tracer().on_create_request(IS_CREATE2);

        *external_call_dest = Some(EVMCallRequest {
            ergs_to_pass: all_resources.ergs(),
            call_value: value,
            destination_address: deployed_address,
            input_data: deployment_code,
            modifier: CallModifier::Constructor,
            full_caller_resources: all_resources,
        });

        Err(ExitCode::ExternalCall)
    }
```

**File:** evm_interpreter/src/gas.rs (L67-74)
```rust
    pub(crate) fn spend_gas(&mut self, to_spend: u64) -> Result<(), ExitCode> {
        let Some(ergs_cost) = to_spend.checked_mul(ERGS_PER_GAS) else {
            return Err(EvmError::OutOfGas.into());
        };
        let resource_cost = S::Resources::from_ergs(Ergs(ergs_cost));
        self.resources.charge(&resource_cost)?;
        Ok(())
    }
```

**File:** zk_ee/src/reference_implementations/mod.rs (L146-151)
```rust
    fn from_ergs(ergs: Ergs) -> Self {
        Self {
            ergs,
            native: Native::empty(),
        }
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L58-81)
```rust
    // Note: for zero gas price, we use "unlimited native"
    let full_native_limit = if cfg!(feature = "unlimited_native") || native_per_gas == 0 {
        u64::MAX - 1
    } else {
        gas_limit.saturating_mul(native_per_gas)
    };
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
```

**File:** docs/double_resource_accounting.md (L18-19)
```markdown

If a transaction execution runs out of native resources, the entire transaction is reverted. If the same happens during transaction validation, the transaction is considered invalid.
```

**File:** evm_interpreter/src/interpreter.rs (L147-148)
```rust
                    opcodes::CREATE => self.create::<false>(system, external_call_dest, tracer),
                    opcodes::CREATE2 => self.create::<true>(system, external_call_dest, tracer),
```

**File:** evm_interpreter/src/native_resource_constants.rs (L63-91)
```rust
// Memory / Stack / Storage / Flow
pub const HEAP_EXPANSION_BASE_NATIVE_COST: u64 = 35;
pub const HEAP_EXPANSION_PER_BYTE_NATIVE_COST: u64 = 1;
pub const MLOAD_NATIVE_COST: u64 = 250;
pub const MSTORE_NATIVE_COST: u64 = 250;
pub const MSTORE8_NATIVE_COST: u64 = 250;
pub const COPY_BASE_NATIVE_COST: u64 = 80;
pub const COPY_BYTE_NATIVE_COST: u64 = 1;
pub const SLOAD_NATIVE_COST: u64 = 100;
pub const SSTORE_NATIVE_COST: u64 = 100;
pub const TLOAD_NATIVE_COST: u64 = 100;
pub const TSTORE_NATIVE_COST: u64 = 100;
pub const MSIZE_NATIVE_COST: u64 = 40;
pub const JUMP_NATIVE_COST: u64 = 70;
pub const JUMPI_NATIVE_COST: u64 = 300;
pub const PC_NATIVE_COST: u64 = 40;
pub const STOP_NATIVE_COST: u64 = 10;
pub const RETURN_NATIVE_COST: u64 = 40;
pub const REVERT_NATIVE_COST: u64 = 50;
pub const INVALID_NATIVE_COST: u64 = 50;
pub const SELFDESTRUCT_NATIVE_COST: u64 = 100;
pub const POP_NATIVE_COST: u64 = 40;
pub const JUMPDEST_NATIVE_COST: u64 = 40;
pub const CREATE_NATIVE_COST: u64 = 25_000;
pub const CREATE2_NATIVE_COST: u64 = 25_000;
pub const CALL_NATIVE_COST: u64 = 1_500;
pub const CALLCODE_NATIVE_COST: u64 = 1_500;
pub const DELEGATECALL_NATIVE_COST: u64 = 1_500;
pub const STATICCALL_NATIVE_COST: u64 = 1_500;
```
