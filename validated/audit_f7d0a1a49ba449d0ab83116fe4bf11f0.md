### Title
`set_fixed_gas` / `set_silo_params` Lacks Input Validation, Enabling Temporary Freeze of All Silo Transactions - (File: `engine/src/lib.rs`)

---

### Summary

The `set_fixed_gas` and `set_silo_params` contract entry points in Aurora Engine's Silo mode accept any `EthGas` value without bounds validation. Setting `fixed_gas` to a value exceeding any practical transaction gas limit causes every subsequent EVM transaction to be rejected with `FixedGasOverflow`, temporarily freezing all user funds in the silo until the owner corrects the parameter.

---

### Finding Description

The `set_fixed_gas` entry point in `engine/src/lib.rs` performs only an ownership check before writing the value directly to storage:

```rust
pub extern "C" fn set_fixed_gas() {
    let mut io = Runtime;
    let state = state::get_state(&io).sdk_unwrap();
    require_owner_and_running(&state, &io.predecessor_account_id())
        .map_err(ContractError::msg)
        .sdk_unwrap();

    let args: FixedGasArgs = io.read_input_borsh().sdk_unwrap();
    silo::set_fixed_gas(&mut io, args.fixed_gas);  // no range validation
}
``` [1](#0-0) 

The underlying `silo::set_fixed_gas` also performs no validation:

```rust
pub fn set_fixed_gas<I: IO>(io: &mut I, gas: Option<EthGas>) {
    let key = fixed_gas_key();
    if let Some(gas) = gas {
        io.write_borsh(&key, &gas);
    } else {
        io.remove_storage(&key);
    }
}
``` [2](#0-1) 

Similarly, `set_silo_params` passes `fixed_gas` through without any bounds check: [3](#0-2) 

The stored `fixed_gas` value is read at the start of every EVM transaction submission in `submit_with_alt_modexp`:

```rust
let fixed_gas = silo::get_fixed_gas(&io);
// ...
if fixed_gas.is_some_and(|gas| gas.as_u256() > transaction.gas_limit) {
    return Err(EngineErrorKind::FixedGasOverflow.into());
}
``` [4](#0-3) 

If `fixed_gas` is set to any value exceeding the gas limit a user can practically include in a transaction (e.g., `u64::MAX = 18_446_744_073_709_551_615`, far above the ~30 million typical block gas limit), the guard fires for **every** transaction, making the silo completely non-functional.

---

### Impact Explanation

In Silo mode, all EVM transactions — ETH transfers, ERC-20 transfers, contract interactions, and withdrawals — flow through `submit` / `submit_with_alt_modexp`. With an oversized `fixed_gas` stored, the `FixedGasOverflow` check rejects every transaction before any state change occurs. Users cannot move or withdraw their funds until the owner corrects the value. This constitutes **temporary freezing of funds** (High impact).

---

### Likelihood Explanation

The likelihood is low but non-zero. The owner could accidentally set an invalid `fixed_gas` value due to unit confusion (e.g., treating gas as wei, multiplying by `1e18`), a copy-paste error, or a misconfigured deployment script. No on-chain safeguard prevents the mistake. The original Bond Protocol report was accepted under identical circumstances — an authorized admin function with no input validation that could break core protocol functionality.

---

### Recommendation

Add a maximum bound check in `set_fixed_gas` (and transitively in `set_silo_params`) before persisting the value. For example, reject any `fixed_gas` value that exceeds a protocol-defined ceiling (e.g., 30,000,000 gas, matching Ethereum's block gas limit), and reject `fixed_gas = 0` (which should use `None` to disable the feature instead).

---

### Proof of Concept

1. Aurora Engine is deployed in Silo mode with a legitimate `fixed_gas` (e.g., `1_000_000`).
2. The owner calls `set_fixed_gas` with `fixed_gas = u64::MAX` (e.g., due to a unit-conversion mistake).
3. The value is written to storage with no rejection.
4. A user submits a standard ETH transfer with `gas_limit = 21_000`.
5. `submit_with_alt_modexp` reads `fixed_gas = u64::MAX`, evaluates `18_446_744_073_709_551_615 > 21_000 = true`, and returns `EngineErrorKind::FixedGasOverflow`.
6. Every transaction from every user fails identically — no ETH or ERC-20 can be moved.
7. All silo user funds are frozen until the owner issues a corrective `set_fixed_gas` call.

### Citations

**File:** engine/src/lib.rs (L783-793)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn set_fixed_gas() {
        let mut io = Runtime;
        let state = state::get_state(&io).sdk_unwrap();
        require_owner_and_running(&state, &io.predecessor_account_id())
            .map_err(ContractError::msg)
            .sdk_unwrap();

        let args: FixedGasArgs = io.read_input_borsh().sdk_unwrap();
        silo::set_fixed_gas(&mut io, args.fixed_gas);
    }
```

**File:** engine/src/contract_methods/silo/mod.rs (L31-38)
```rust
pub fn set_silo_params<I: IO>(io: &mut I, args: Option<SiloParamsArgs>) {
    let (cost, address) = args.map_or((None, None), |params| {
        (Some(params.fixed_gas), Some(params.erc20_fallback_address))
    });

    set_fixed_gas(io, cost);
    set_erc20_fallback_address(io, address);
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L48-56)
```rust
pub fn set_fixed_gas<I: IO>(io: &mut I, gas: Option<EthGas>) {
    let key = fixed_gas_key();

    if let Some(gas) = gas {
        io.write_borsh(&key, &gas);
    } else {
        io.remove_storage(&key);
    }
}
```

**File:** engine/src/engine.rs (L1049-1068)
```rust
    let fixed_gas = silo::get_fixed_gas(&io);

    // Check if the sender has rights to submit transactions or deploy code.
    assert_access(&io, env, &transaction)?;

    // Validate the chain ID, if provided inside the signature:
    if let Some(chain_id) = transaction.chain_id
        && U256::from(chain_id) != U256::from_big_endian(&state.chain_id)
    {
        return Err(EngineErrorKind::InvalidChainId.into());
    }

    sdk::log!("signer_address {:?}", sender);

    check_nonce(&io, &sender, &transaction.nonce)?;

    // Check that fixed gas is not greater than the gas limit from the transaction.
    if fixed_gas.is_some_and(|gas| gas.as_u256() > transaction.gas_limit) {
        return Err(EngineErrorKind::FixedGasOverflow.into());
    }
```
