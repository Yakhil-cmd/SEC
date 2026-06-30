### Title
Unrestricted `register_relayer` Allows Any Caller to Steal ETH from Bridge Recipients via Fee Manipulation - (File: `engine/src/contract_methods/admin.rs`)

---

### Summary

The `register_relayer` entrypoint in Aurora Engine has no caller access control. Any NEAR account can register itself as a relayer and associate an arbitrary EVM address with that account. The `ft_on_transfer` fee mechanism then deducts a sender-controlled fee from the **recipient's** ETH balance and credits it to the registered relayer EVM address. Because the fee amount is encoded in the bridge message by the sender (the attacker), an attacker can drain ETH from any bridge recipient.

---

### Finding Description

`register_relayer` in `engine/src/contract_methods/admin.rs` performs only a liveness check:

```rust
pub fn register_relayer<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;          // ← only guard: contract not paused
        let relayer_address = io.read_input_arr20()?;
        // ...
        engine.register_relayer(
            predecessor_account_id.as_bytes(),
            Address::from_array(relayer_address),
        );
        Ok(())
    })
}
``` [1](#0-0) 

There is no `require_owner_only`, `require_key_manager_only`, or any other caller restriction. Compare this with every other privileged mutative function in the same file, all of which call `require_owner_only` or `require_key_manager_only`. [2](#0-1) 

The internal `register_relayer` on the `Engine` struct simply writes `predecessor_account_id → evm_address` into storage with no validation:

```rust
pub fn register_relayer(&mut self, account_id: &[u8], evm_address: Address) {
    let key = Self::relayer_key(account_id);
    self.io.write_storage(&key, evm_address.as_bytes());
}
``` [3](#0-2) 

The WASM entrypoint exposes this with no additional guard:

```rust
#[unsafe(no_mangle)]
pub extern "C" fn register_relayer() {
    let io = Runtime;
    let env = Runtime;
    contract_methods::admin::register_relayer(io, &env)
        .map_err(ContractError::msg)
        .sdk_unwrap();
}
``` [4](#0-3) 

The fee mechanism is confirmed by the integration test `test_relayer_charge_fee`: after `register_relayer(alice, relayer_evm_addr)`, a call to `ft_on_transfer` with `alice` as `sender_id` and a fee encoded in the message causes the **recipient's** ETH balance to be reduced by `fee` and the registered relayer EVM address to receive `fee`. Critically, the test shows the fee (51 Wei) can exceed the NEP-141 amount being transferred (10 tokens), meaning the fee is not bounded by the bridged amount. [5](#0-4) 

---

### Impact Explanation

**Critical — Direct theft of user ETH funds at rest.**

An attacker who registers as a relayer can bridge any amount of NEP-141 tokens to Aurora targeting a victim's EVM address, encoding an arbitrarily large fee in the bridge message. The fee is deducted from the victim's existing ETH balance and transferred to the attacker's registered EVM address. The attacker spends only the cost of the NEP-141 tokens bridged (which they receive back as ERC-20 tokens at the destination) while extracting ETH from the victim. Any account holding ETH on Aurora is at risk whenever a registered-relayer account initiates a bridge transfer targeting them.

---

### Likelihood Explanation

**High.** The `register_relayer` entrypoint is publicly callable by any NEAR account with no preconditions beyond the contract being unpaused. No tokens, deposits, or permissions are required. The attack requires only two NEAR transactions (register + bridge transfer) and is fully self-contained.

---

### Recommendation

Add `require_owner_only` (or a dedicated allowlist check) to `register_relayer`, consistent with every other privileged function in `admin.rs`:

```rust
pub fn register_relayer<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
+       require_owner_only(&state, &env.predecessor_account_id())?;
        // ...
    })
}
```

Alternatively, bound the fee to the NEP-141 amount being transferred so that even a registered relayer cannot extract more than the bridged value.

---

### Proof of Concept

1. **Attacker** calls `register_relayer(attacker_evm_address)` from NEAR account `attacker.near`. No permission check fires; the mapping `attacker.near → attacker_evm_address` is written to storage.

2. **Attacker** initiates a NEP-141 `ft_transfer_call` to Aurora with:
   - `receiver_id`: Aurora engine contract
   - `amount`: 1 (minimum bridgeable amount)
   - `msg`: `<victim_evm_address_hex><fee_as_32_byte_big_endian_hex>` where `fee` equals the victim's full ETH balance

3. The NEP-141 contract calls `ft_on_transfer` on Aurora with `sender_id = attacker.near`. Aurora looks up the registered relayer for `attacker.near`, finds `attacker_evm_address`, deducts `fee` from the victim's ETH balance, and credits `attacker_evm_address`.

4. Victim's ETH balance is now zero. Attacker holds the stolen ETH at `attacker_evm_address` and also holds the ERC-20 mirror of the 1 NEP-141 token they bridged. [1](#0-0) [3](#0-2) [6](#0-5)

### Citations

**File:** engine/src/contract_methods/admin.rs (L104-110)
```rust
pub fn set_owner<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let mut state = state::get_state(&io)?;

        require_running(&state)?;
        require_owner_only(&state, &env.predecessor_account_id())?;

```

**File:** engine/src/contract_methods/admin.rs (L401-423)
```rust
#[named]
pub fn register_relayer<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let relayer_address = io.read_input_arr20()?;

        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();
        let mut engine: Engine<_, E, AuroraModExp> = Engine::new_with_state(
            state,
            predecessor_address(&predecessor_account_id),
            current_account_id,
            io,
            env,
        );
        engine.register_relayer(
            predecessor_account_id.as_bytes(),
            Address::from_array(relayer_address),
        );
        Ok(())
    })
}
```

**File:** engine/src/engine.rs (L706-709)
```rust
    pub fn register_relayer(&mut self, account_id: &[u8], evm_address: Address) {
        let key = Self::relayer_key(account_id);
        self.io.write_storage(&key, evm_address.as_bytes());
    }
```

**File:** engine/src/lib.rs (L296-303)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn register_relayer() {
        let io = Runtime;
        let env = Runtime;
        contract_methods::admin::register_relayer(io, &env)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```

**File:** engine-tests/src/tests/erc20_connector.rs (L293-335)
```rust
fn test_relayer_charge_fee() {
    let mut runner = AuroraRunner::new();
    // Standalone runner presently does not support ft_on_transfer
    runner.standalone_runner = None;
    let amount = Balance::new(10);
    let fee = 51;
    let nep141 = "tt.testnet";
    let alice = "alice";
    let token = runner.deploy_erc20_token(nep141);
    let recipient = runner.create_account().address;

    let recipient_balance = runner.get_balance(recipient);
    assert_eq!(recipient_balance, INITIAL_BALANCE);

    let relayer = create_ethereum_address();
    runner.register_relayer(alice, relayer).unwrap();
    let relayer_balance = runner.get_balance(relayer);
    assert_eq!(relayer_balance, Wei::zero());

    let balance = runner.balance_of(token, recipient, DEFAULT_AURORA_ACCOUNT_ID);
    assert_eq!(balance, U256::from(0));

    let fee_encoded = U256::from(fee).to_big_endian();

    runner.ft_on_transfer(
        nep141,
        alice,
        alice,
        amount,
        &format!("{}{}", recipient.encode(), hex::encode(fee_encoded)),
    );

    let recipient_balance_end = runner.get_balance(recipient);
    assert_eq!(
        recipient_balance_end,
        Wei::new_u64(INITIAL_BALANCE.raw().as_u64() - fee)
    );
    let relayer_balance = runner.get_balance(relayer);
    assert_eq!(relayer_balance, Wei::new_u64(fee));

    let balance = runner.balance_of(token, recipient, DEFAULT_AURORA_ACCOUNT_ID);
    assert_eq!(balance, U256::from(amount.as_u128()));
}
```
