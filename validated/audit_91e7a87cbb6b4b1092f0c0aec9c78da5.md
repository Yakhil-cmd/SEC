### Title
Missing ERC-20 Contract Existence Check Before EVM Mint Call Allows Permanent Locking of Bridged NEP-141 Tokens - (File: `engine/src/engine.rs`)

---

### Summary

`receive_erc20_tokens` in `engine/src/engine.rs` calls the EVM at the registered ERC-20 address without first verifying that contract code exists at that address. If the ERC-20 contract has been self-destructed via the EVM `SELFDESTRUCT` opcode, the EVM call to `mint` silently succeeds with empty return data. The caller (`ft_on_transfer`) interprets this as a successful mint and retains all bridged NEP-141 tokens, permanently locking them with no ERC-20 tokens minted for the recipient.

---

### Finding Description

`receive_erc20_tokens` (called from `ft_on_transfer` when a NEP-141 token is transferred to Aurora) performs the following steps:

1. Looks up the registered ERC-20 address from the bijection map via `get_erc20_from_nep141`: [1](#0-0) 

2. Calls the EVM at that address with `mint` calldata, then checks the result via `submit_result_or_err`: [2](#0-1) 

3. `submit_result_or_err` treats any `TransactionStatus::Succeed(_)` as success, regardless of return data content: [3](#0-2) 

**The missing step**: there is no check that the ERC-20 contract code at the registered address is non-empty before making the EVM call.

In the EVM, calling an address with no deployed code always returns `ExitSucceed::Stopped` → `TransactionStatus::Succeed(Vec::new())`. This is the same EVM design property described in the original report ("the account called is non-existent" → returns success). `submit_result_or_err` accepts this as a successful mint.

The bijection map (`Nep141Erc20Map` / `Erc20Nep141Map`) is never cleaned up when a contract is destroyed: [4](#0-3) 

The `get_code` and `get_code_size` helpers exist and are used elsewhere in the engine (e.g., in `exists`, `is_account_empty`) but are not consulted here: [5](#0-4) 

The `ft_on_transfer` entry point returns 0 (keep all tokens) on `Ok`, and only refunds on `Err`: [6](#0-5) 

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

When `receive_erc20_tokens` silently succeeds against a destroyed ERC-20 contract:
- `ft_on_transfer` returns `"0"` to the NEP-141 contract, signaling "keep all tokens"
- The NEP-141 tokens are permanently locked inside Aurora's connector
- No ERC-20 tokens are minted for the recipient
- There is no recovery path: the bijection map still maps the NEP-141 to the destroyed address, so every subsequent bridge attempt for that token also silently fails and locks more tokens

---

### Likelihood Explanation

**Medium.** Aurora is a general-purpose EVM where any EVM bytecode can be deployed. `SELFDESTRUCT` is a standard EVM opcode. If the deployed `EvmErc20.bin` contract or any contract registered in the bijection map includes or can be made to invoke `SELFDESTRUCT`, the condition is directly triggerable. The bijection map is populated by `deploy_erc20_token` and `register_token`: [7](#0-6) 

Once a contract is self-destructed, the stale map entry persists indefinitely. Any user bridging that NEP-141 token afterward is affected. The entry path (`ft_on_transfer`) is a standard, unprivileged NEAR cross-contract call reachable by any NEP-141 token holder.

---

### Recommendation

Before calling `mint` on the ERC-20 address in `receive_erc20_tokens`, verify that the contract code is non-empty using the existing `get_code_size` helper:

```rust
let erc20_token = get_erc20_from_nep141(&self.io, token)?;
// Add existence check:
if get_code_size(&self.io, &erc20_token) == 0 {
    return Err(/* ERR_ERC20_CONTRACT_NOT_FOUND */);
}
```

This ensures that if the ERC-20 contract has been destroyed, `ft_on_transfer` returns the full token amount to the sender instead of locking it.

---

### Proof of Concept

1. NEP-141 token `token.near` has its ERC-20 mirror deployed at address `0xABCD…` on Aurora via `deploy_erc20_token`. The bijection map records `token.near → 0xABCD…`.
2. The ERC-20 contract at `0xABCD…` is self-destructed (via `SELFDESTRUCT` opcode). Contract code is removed from Aurora's storage; the bijection map entry remains.
3. A user calls `ft_transfer_call` on `token.near`, transferring 1,000 tokens to Aurora.
4. Aurora's `ft_on_transfer` is invoked with `predecessor_account_id = token.near`. [8](#0-7) 
5. `receive_erc20_tokens` is called:
   - `get_erc20_from_nep141` returns `0xABCD…` from the stale map entry ✓
   - `self.call(erc20_admin, 0xABCD…, mint_calldata)` executes against an empty address
   - EVM returns `TransactionStatus::Succeed(Vec::new())` (no code → no-op success)
   - `submit_result_or_err` returns `Ok(result)` ✓
6. `ft_on_transfer` returns `"0"` — all 1,000 NEP-141 tokens are retained by Aurora.
7. The user receives 0 ERC-20 tokens. The 1,000 NEP-141 tokens are permanently locked.

### Citations

**File:** engine/src/engine.rs (L722-741)
```rust
    pub fn register_token(
        &mut self,
        erc20_token: Address,
        nep141_token: AccountId,
    ) -> Result<(), RegisterTokenError> {
        match get_erc20_from_nep141(&self.io, &nep141_token) {
            Err(GetErc20FromNep141Error::Nep141NotFound) => (),
            Err(GetErc20FromNep141Error::InvalidNep141AccountId) => {
                return Err(RegisterTokenError::InvalidNep141AccountId);
            }
            Err(GetErc20FromNep141Error::InvalidAddress) => {
                return Err(RegisterTokenError::InvalidAddress);
            }
            Ok(_) => return Err(RegisterTokenError::TokenAlreadyRegistered),
        }

        let erc20_token = ERC20Address(erc20_token);
        let nep141_token = NEP141Account(nep141_token);
        nep141_erc20_map(self.io).insert(&nep141_token, &erc20_token);
        Ok(())
```

**File:** engine/src/engine.rs (L824-824)
```rust
        let erc20_token = get_erc20_from_nep141(&self.io, token)?;
```

**File:** engine/src/engine.rs (L826-837)
```rust
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
```

**File:** engine/src/engine.rs (L1430-1438)
```rust
pub fn get_code<I: IO>(io: &I, address: &Address) -> Vec<u8> {
    io.read_storage(&address_to_key(KeyPrefix::Code, address))
        .map(|s| s.to_vec())
        .unwrap_or_default()
}

pub fn get_code_size<I: IO>(io: &I, address: &Address) -> usize {
    io.read_storage_len(&address_to_key(KeyPrefix::Code, address))
        .unwrap_or(0)
```

**File:** engine/src/engine.rs (L1494-1509)
```rust
pub const fn nep141_erc20_map<I: IO>(io: I) -> BijectionMap<NEP141Account, ERC20Address, I> {
    BijectionMap::new(KeyPrefix::Nep141Erc20Map, KeyPrefix::Erc20Nep141Map, io)
}

pub fn get_erc20_from_nep141<I: IO>(
    io: &I,
    nep141_account_id: &AccountId,
) -> Result<Address, GetErc20FromNep141Error> {
    let key = bytes_to_key(KeyPrefix::Nep141Erc20Map, nep141_account_id.as_bytes());
    io.read_storage(&key)
        .map(|v| {
            let mut buf = [0u8; 20];
            v.copy_to_slice(&mut buf);
            Address::from_array(buf)
        })
        .ok_or(GetErc20FromNep141Error::Nep141NotFound)
```

**File:** engine/src/engine.rs (L2085-2088)
```rust
fn submit_result_or_err(submit_result: SubmitResult) -> Result<SubmitResult, EngineError> {
    match submit_result.status {
        TransactionStatus::Succeed(_) => Ok(submit_result),
        TransactionStatus::Revert(bytes) => {
```

**File:** engine/src/contract_methods/connector.rs (L81-90)
```rust
        let result = if predecessor_account_id == get_connector_account_id(&io)? {
            engine.receive_base_tokens(&args)
        } else {
            engine.receive_erc20_tokens(
                &predecessor_account_id,
                &args,
                &current_account_id,
                handler,
            )
        };
```

**File:** engine/src/contract_methods/connector.rs (L92-107)
```rust
        #[allow(clippy::used_underscore_binding)]
        let amount_to_return = if let Err(_err) = &result {
            sdk::log!("Error in ft_on_transfer: {_err:?}");
            // An error occurred, so we need to return the amount of tokens to the sender.
            args.amount.as_u128()
        } else {
            // Everything is ok, so return 0.
            0
        };

        let output = crate::prelude::format!("\"{amount_to_return}\"");
        io.return_output(output.as_bytes());

        // In case of an error, we just return Ok(None) to avoid a panic in the contract. It's ok
        // because in case of an error, we already returned the amount of tokens to the sender.
        Ok(result.unwrap_or(None))
```
