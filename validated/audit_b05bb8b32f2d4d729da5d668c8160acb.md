### Title
Missing Zero-Address Validation for `erc20_fallback_address` Causes Permanent ERC-20 Token Freeze - (File: `engine/src/contract_methods/silo/mod.rs`)

---

### Summary

`set_silo_params()` and `set_erc20_fallback_address()` accept `Address::zero()` as the `erc20_fallback_address` without any validation. When Silo mode is active and the fallback address is set to the zero address, all ERC-20 tokens bridged to non-whitelisted recipient addresses are permanently minted to `Address::zero()`, making them irrecoverable.

---

### Finding Description

In Silo mode, when a NEP-141 token transfer arrives via `ft_on_transfer` for a non-whitelisted EVM recipient, the engine redirects the mint to the configured `erc20_fallback_address`. The setter functions perform no validation on this address:

`engine/src/lib.rs` lines 806–814 (`set_erc20_fallback_address`): [1](#0-0) 

`engine/src/contract_methods/silo/mod.rs` lines 64–73 (`set_erc20_fallback_address`): [2](#0-1) 

`engine/src/lib.rs` lines 829–838 (`set_silo_params`): [3](#0-2) 

Neither function checks whether the supplied address is `Address::zero()`. The `SiloParamsArgs` struct's `erc20_fallback_address` field defaults to `Address::zero()` (Rust's `Default` for `Address`): [4](#0-3) 

The test suite itself uses `Address::zero()` as the fallback address, confirming this is a reachable configuration: [5](#0-4) 

When the fallback address is zero, `receive_erc20_tokens` in `engine/src/engine.rs` silently redirects the mint: [6](#0-5) 

The ERC-20 `mint` call then executes with `recipient = Address::zero()`, permanently locking the tokens at the zero address with no recovery path.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

Any ERC-20 tokens bridged via `ft_on_transfer` to a non-whitelisted address while `erc20_fallback_address = Address::zero()` is configured are minted to the zero address. The ERC-20 contract at `erc20_token` credits the balance to `0x0000...0000`. No private key controls that address; the tokens are permanently frozen. The NEP-141 tokens have already been transferred to Aurora's custody on the NEAR side, so the loss is complete and irreversible.

---

### Likelihood Explanation

The owner may set the fallback address to zero by mistake in several realistic scenarios:

1. Calling `set_silo_params` with a `SiloParamsArgs` value constructed from `Default::default()`, which yields `erc20_fallback_address = Address::zero()`.
2. Calling `set_erc20_fallback_address` with `Some(Address::zero())` intending to "clear" the fallback (the correct way to clear it is `None`, not `Some(zero)`).
3. Deploying a Silo configuration from a script that omits the fallback address field, relying on zero-initialization.

The test suite's own constant `ERC20_FALLBACK_ADDRESS: Address = Address::zero()` demonstrates that zero is treated as a normal, accepted value throughout the codebase, making accidental misconfiguration likely.

---

### Recommendation

Add a zero-address guard in both `set_erc20_fallback_address` and `set_silo_params`:

```rust
// In set_erc20_fallback_address:
if let Some(address) = address {
    if address == Address::zero() {
        return Err(/* ERR_INVALID_FALLBACK_ADDRESS */);
    }
    io.write_storage(&key, address.as_bytes());
} else {
    io.remove_storage(&key);
}
```

Apply the same guard inside `set_silo_params` before delegating to `set_erc20_fallback_address`. This mirrors the fix applied in the referenced report (switching to OpenZeppelin ERC-2981, which rejects `address(0)` as a royalty receiver).

---

### Proof of Concept

1. Owner calls `set_silo_params(Some(SiloParamsArgs { fixed_gas: X, erc20_fallback_address: Address::zero() }))`.
   - `set_erc20_fallback_address` stores `[0u8; 20]` under `ERC20_FALLBACK_KEY`.
2. Owner enables the `Address` whitelist via `set_whitelist_status`.
3. A NEAR user calls `ft_transfer_call` on a NEP-141 contract, sending tokens to Aurora with `msg = hex_encode(non_whitelisted_evm_address)`.
4. Aurora's `ft_on_transfer` is invoked; `receive_erc20_tokens` is called.
5. `silo::get_erc20_fallback_address` returns `Some(Address::zero())`.
6. `silo::is_allow_receive_erc20_tokens` returns `false` (address not whitelisted).
7. `recipient` is overwritten with `Address::zero()`.
8. The ERC-20 `mint(address(0), amount)` call succeeds, crediting the zero address.
9. The NEP-141 tokens remain in Aurora's custody; the ERC-20 tokens are permanently frozen at `0x0000000000000000000000000000000000000000`. [7](#0-6) [2](#0-1)

### Citations

**File:** engine/src/lib.rs (L806-814)
```rust
    pub extern "C" fn set_erc20_fallback_address() {
        let mut io = Runtime;
        let state = state::get_state(&io).sdk_unwrap();
        require_owner_and_running(&state, &io.predecessor_account_id())
            .map_err(ContractError::msg)
            .sdk_unwrap();

        let args: Erc20FallbackAddressArgs = io.read_input_borsh().sdk_unwrap();
        silo::set_erc20_fallback_address(&mut io, args.address);
```

**File:** engine/src/lib.rs (L829-838)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn set_silo_params() {
        let mut io = Runtime;
        let state = state::get_state(&io).sdk_unwrap();
        require_owner_and_running(&state, &io.predecessor_account_id())
            .map_err(ContractError::msg)
            .sdk_unwrap();

        let args: Option<SiloParamsArgs> = io.read_input_borsh().sdk_unwrap();
        silo::set_silo_params(&mut io, args);
```

**File:** engine/src/contract_methods/silo/mod.rs (L64-73)
```rust
/// Set ERC-20 fallback address.
pub fn set_erc20_fallback_address<I: IO>(io: &mut I, address: Option<Address>) {
    let key = erc20_fallback_address_key();

    if let Some(address) = address {
        io.write_storage(&key, address.as_bytes());
    } else {
        io.remove_storage(&key);
    }
}
```

**File:** engine-types/src/parameters/silo.rs (L15-24)
```rust
#[derive(Debug, Default, Clone, PartialEq, Eq, BorshSerialize, BorshDeserialize)]
pub struct SiloParamsArgs {
    /// Fixed amount of gas per transaction.
    pub fixed_gas: EthGas,
    /// EVM address, which is used for withdrawing ERC-20 base tokens in case
    /// a recipient of the tokens is not in the silo white list.
    /// Note: the logic described above works only if the fallback address
    /// is set by `set_silo_params` function. In other words, in Silo mode.
    pub erc20_fallback_address: Address,
}
```

**File:** engine-tests/src/tests/silo.rs (L28-32)
```rust
const ERC20_FALLBACK_ADDRESS: Address = Address::zero();
const SILO_PARAMS_ARGS: SiloParamsArgs = SiloParamsArgs {
    fixed_gas: FIXED_GAS,
    erc20_fallback_address: ERC20_FALLBACK_ADDRESS,
};
```

**File:** engine/src/engine.rs (L796-844)
```rust
    pub fn receive_erc20_tokens<P: PromiseHandler>(
        &mut self,
        token: &AccountId,
        args: &FtOnTransferArgs,
        current_account_id: &AccountId,
        handler: &mut P,
    ) -> Result<Option<SubmitResult>, ContractError> {
        let amount = args.amount.as_u128();
        // Parse message to determine recipient
        let mut recipient = {
            // The message should contain the recipient EOA address.
            let message = args.msg.strip_prefix("0x").unwrap_or(&args.msg);
            // Recipient - 40 characters (Address in hex without '0x' prefix)
            if message.len() < 40 {
                return Err(ParseOnTransferMessageError::WrongMessageFormat.into());
            }
            let mut address_bytes = [0; 20];
            hex::decode_to_slice(&message[..40], &mut address_bytes)
                .map_err(|_| ParseOnTransferMessageError::WrongMessageFormat)?;
            Address::from_array(address_bytes)
        };

        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }

        let erc20_token = get_erc20_from_nep141(&self.io, token)?;
        let erc20_admin_address = current_address(current_account_id);
        let result = self
            .call(
                &erc20_admin_address,
                &erc20_token,
                Wei::zero(),
                setup_receive_erc20_tokens_input(&recipient, amount),
                u64::MAX,
                Vec::new(), // TODO: are there values we should put here?
                Vec::new(),
                handler,
            )
            .and_then(submit_result_or_err)?;

        sdk::log!("Mint {amount} ERC-20 tokens for: {}", recipient.encode());

        // Return SubmitResult so that it can be accessed in standalone engine.
        // This is used to help with the indexing of bridge transactions.
        Ok(Some(result))
    }
```
