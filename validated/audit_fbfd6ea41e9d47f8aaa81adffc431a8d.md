### Title
Silo Mode Fixed Gas Fee and Whitelist Enforcement Absent from `call` Entrypoint - (`engine/src/contract_methods/evm_transactions.rs`)

---

### Summary

In Silo mode, Aurora Engine enforces a fixed gas fee (`fixed_gas`) and whitelist access controls on every EVM transaction submitted via `submit` / `submit_with_args`. However, the `call` entrypoint — which also executes EVM logic — performs neither check. Any NEAR account can invoke `call` to interact with EVM contracts in a Silo deployment without paying the fixed gas fee and without being whitelisted, directly bypassing the Silo operator's fee and access-control configuration.

---

### Finding Description

Aurora Engine's Silo mode provides two enforcement mechanisms applied inside `submit_with_alt_modexp` in `engine/src/engine.rs`:

1. **Whitelist check** via `assert_access`, which calls `silo::is_allow_submit` (or `is_allow_deploy`) to verify both the NEAR predecessor account and the EVM sender address are whitelisted.
2. **Fixed gas fee** via `engine.charge_gas(…, fixed_gas)`, which deducts `fixed_gas × effective_gas_price` from the sender's EVM balance before execution.

```rust
// engine/src/engine.rs
let fixed_gas = silo::get_fixed_gas(&io);          // line 1049
assert_access(&io, env, &transaction)?;             // line 1052
// ...
let prepaid_amount = match engine.charge_gas(       // line 1101
    &sender, &transaction, max_gas_price, fixed_gas
) { ... };
```

The `call` entrypoint in `engine/src/contract_methods/evm_transactions.rs` takes a `CallArgs` payload from any NEAR account and executes EVM logic directly:

```rust
// engine/src/contract_methods/evm_transactions.rs
pub fn call<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<SubmitResult, ContractError> {
    with_logs_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;                   // only check: contract not paused
        let bytes = io.read_input().to_vec();
        let args = CallArgs::deserialize(&bytes)…;
        // …
        let result = engine.call_with_args(args, handler)?;  // no fixed_gas, no whitelist
```

`call_with_args` invokes `engine.call(…, u64::MAX, …)` with no gas charging and no access check. Neither `silo::get_fixed_gas` nor `assert_access` is called anywhere in this path.

The same omission exists in `deploy_code`, which also skips both checks.

---

### Impact Explanation

**Theft of unclaimed yield (High):** The Silo operator configures `fixed_gas` to collect a fee on every EVM interaction. Because `call` bypasses `charge_gas`, a user who routes interactions through `call` instead of `submit` pays zero fixed gas fees. The Silo operator loses all fee revenue from such interactions.

**Whitelist bypass (High):** The Silo operator enables `WhitelistKind::Account` and `WhitelistKind::Address` to restrict which NEAR accounts and EVM addresses may interact with the EVM. Because `call` bypasses `assert_access`, any NEAR account — including those explicitly excluded from the whitelist — can call arbitrary EVM contracts in the Silo.

---

### Likelihood Explanation

The `call` entrypoint is a standard, publicly documented NEAR function on the Aurora Engine contract. It requires no signed Ethereum transaction, only a Borsh-encoded `CallArgs` payload. Any NEAR account can construct and submit such a call trivially. No special privileges, leaked keys, or social engineering are required.

---

### Recommendation

Move the fixed gas charge and whitelist enforcement into the shared execution path so they apply regardless of which NEAR entrypoint is used. Concretely, add the following to the `call` (and `deploy_code`) handler before EVM execution:

```rust
// In evm_transactions.rs::call
let fixed_gas = silo::get_fixed_gas(&io);
// Derive the EVM address from the predecessor, same as submit does
let origin = predecessor_address(&predecessor_account_id);
// Check whitelist
if !silo::is_allow_submit(&io, &predecessor_account_id, &origin) {
    return Err(errors::ERR_NOT_ALLOWED.into());
}
// Charge fixed gas if set
if let Some(gas) = fixed_gas {
    engine.charge_gas_fixed(&origin, gas)?;
}
```

Alternatively, enforce these checks at the NEAR entrypoint layer in `lib.rs` before dispatching to the implementation functions, so no entrypoint can bypass them.

---

### Proof of Concept

**Setup:** Silo operator calls `set_silo_params` with `fixed_gas = 1_000_000` and enables `WhitelistKind::Account` + `WhitelistKind::Address` whitelists. Attacker's NEAR account and EVM address are **not** whitelisted.

**Attack:**
1. Attacker constructs a `CallArgs::V1` (or V2) payload targeting any EVM contract in the Silo.
2. Attacker calls the `call` NEAR entrypoint on the Aurora Engine contract with this payload.
3. `call` passes `require_running`, skips `assert_access` and `charge_gas`, and executes the EVM call.
4. The attacker successfully interacts with the EVM contract: (a) without being whitelisted, and (b) without paying the `fixed_gas` fee.

**Relevant code paths:**

`call` entrypoint — no whitelist, no fixed gas: [1](#0-0) 

`submit_with_alt_modexp` — fixed gas retrieved and whitelist enforced: [2](#0-1) 

`charge_gas` — where fixed gas fee is deducted: [3](#0-2) 

`assert_access` — whitelist enforcement: [4](#0-3) 

`is_allow_submit` — the whitelist check that `call` never invokes: [5](#0-4) 

`call_with_args` — executes with `u64::MAX` gas, no fee deduction: [6](#0-5)

### Citations

**File:** engine/src/contract_methods/evm_transactions.rs (L46-71)
```rust
pub fn call<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<SubmitResult, ContractError> {
    with_logs_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let bytes = io.read_input().to_vec();
        let args = CallArgs::deserialize(&bytes).ok_or(errors::ERR_BORSH_DESERIALIZE)?;
        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();

        let mut engine: Engine<_, E, AuroraModExp> = Engine::new_with_state(
            state,
            predecessor_address(&predecessor_account_id),
            current_account_id,
            io,
            env,
        );
        let result = engine.call_with_args(args, handler)?;
        let result_bytes = borsh::to_vec(&result).map_err(|_| errors::ERR_SERIALIZE)?;
        io.return_output(&result_bytes);
        Ok(result)
    })
}
```

**File:** engine/src/engine.rs (L468-515)
```rust
    pub fn charge_gas(
        &mut self,
        sender: &Address,
        transaction: &NormalizedEthTransaction,
        max_gas_price: Option<U256>,
        fixed_gas: Option<EthGas>,
    ) -> Result<GasPaymentResult, GasPaymentError> {
        let block_base_fee_per_gas = self.block_base_fee_per_gas();
        if transaction.max_fee_per_gas.is_zero()
            && fixed_gas.is_none()
            && block_base_fee_per_gas.is_zero()
        {
            return Ok(GasPaymentResult::default());
        }

        if transaction.max_fee_per_gas < block_base_fee_per_gas {
            return Err(GasPaymentError::MaxFeePerGasLessThanBaseFee);
        }

        let priority_fee_per_gas = transaction
            .max_priority_fee_per_gas
            .min(transaction.max_fee_per_gas - block_base_fee_per_gas);
        let priority_fee_per_gas = max_gas_price.map_or(priority_fee_per_gas, |price| {
            price.min(priority_fee_per_gas)
        });
        let effective_gas_price = priority_fee_per_gas + block_base_fee_per_gas;
        // First, we try to use `fixed_gas`. At this point we already know that the `fixed_gas` is
        // less than the `gas_limit`. It allows avoiding refunding unused gas to the sender later.
        let prepaid_amount = fixed_gas
            .map_or(transaction.gas_limit, EthGas::as_u256)
            .checked_mul(effective_gas_price)
            .map(Wei::new)
            .ok_or(GasPaymentError::EthAmountOverflow)?;

        let new_balance = get_balance(&self.io, sender)
            .checked_sub(prepaid_amount)
            .ok_or(GasPaymentError::OutOfFund)?;

        set_balance(&mut self.io, sender, &new_balance);

        self.gas_price = effective_gas_price;

        Ok(GasPaymentResult {
            prepaid_amount,
            effective_gas_price,
            priority_fee_per_gas,
        })
    }
```

**File:** engine/src/engine.rs (L582-621)
```rust
    /// Call the EVM contract with arguments
    pub fn call_with_args<P: PromiseHandler>(
        &mut self,
        args: CallArgs,
        handler: &mut P,
    ) -> EngineResult<SubmitResult> {
        let origin = Address::new(self.origin());
        match args {
            CallArgs::V2(call_args) => {
                let contract = call_args.contract;
                let value = call_args.value.into();
                let input = call_args.input;
                self.call(
                    &origin,
                    &contract,
                    value,
                    input,
                    u64::MAX,
                    Vec::new(),
                    Vec::new(),
                    handler,
                )
            }
            CallArgs::V1(call_args) => {
                let contract = call_args.contract;
                let value = Wei::zero();
                let input = call_args.input;
                self.call(
                    &origin,
                    &contract,
                    value,
                    input,
                    u64::MAX,
                    Vec::new(),
                    Vec::new(),
                    handler,
                )
            }
        }
    }
```

**File:** engine/src/engine.rs (L1049-1052)
```rust
    let fixed_gas = silo::get_fixed_gas(&io);

    // Check if the sender has rights to submit transactions or deploy code.
    assert_access(&io, env, &transaction)?;
```

**File:** engine/src/engine.rs (L1756-1775)
```rust
fn assert_access<I: IO + Copy, E: Env>(
    io: &I,
    env: &E,
    transaction: &NormalizedEthTransaction,
) -> Result<(), EngineError> {
    let allowed = if transaction.to.is_some() {
        silo::is_allow_submit(io, &env.predecessor_account_id(), &transaction.address)
    } else {
        silo::is_allow_deploy(io, &env.predecessor_account_id(), &transaction.address)
    };

    if !allowed {
        return Err(EngineError {
            kind: EngineErrorKind::NotAllowed,
            gas_used: 0,
        });
    }

    Ok(())
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L135-138)
```rust
/// Check if a user has the right to submit transactions.
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}
```
