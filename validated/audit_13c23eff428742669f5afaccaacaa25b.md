### Title
Silent ERC-20 Redirect to Fallback Address Without Per-Depositor Accounting Causes Permanent Fund Loss in Silo Mode - (File: `engine/src/engine.rs`)

### Summary

In Silo mode, when a user sends NEP-141 tokens via `ft_transfer_call` to Aurora with a recipient EVM address that is not in the address whitelist, `receive_erc20_tokens` silently mints the full token amount to the configured `erc20_fallback_address` and returns `Ok` (success). Because `ft_on_transfer` only triggers a NEP-141 refund on `Err`, the sender receives no refund and no per-depositor accounting is maintained at the fallback address. Multiple users' tokens are pooled there with no on-chain mechanism to track individual deposits or automatically return them.

### Finding Description

`receive_erc20_tokens` in `engine/src/engine.rs` contains the following logic:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;
}
// ... mints full `amount` to `recipient` (now fallback_address)
``` [1](#0-0) 

When this redirect fires, the function still returns `Ok(Some(result))`, signalling success. [2](#0-1) 

The caller, `ft_on_transfer` in `engine/src/contract_methods/connector.rs`, only returns the sender's tokens when the result is `Err`:

```rust
let amount_to_return = if let Err(_err) = &result {
    args.amount.as_u128()   // refund
} else {
    0                        // no refund
};
``` [3](#0-2) 

Because the redirect path returns `Ok`, `amount_to_return` is always `0` for any non-whitelisted deposit, and the NEP-141 standard never refunds the sender. All such deposits accumulate at the single `erc20_fallback_address` with no on-chain record of which NEAR account sent how much.

The `is_allow_receive_erc20_tokens` check that gates the redirect: [4](#0-3) 

### Impact Explanation

**High – Temporary freezing of funds / Critical – Permanent freezing of funds.**

- Every user who sends a registered NEP-141 token to Aurora in Silo mode with a non-whitelisted recipient address permanently loses custody of those tokens on-chain. The tokens are minted to the fallback address with no per-depositor ledger.
- Because there is no on-chain accounting, it is impossible to automatically refund a specific depositor. Recovery requires the fallback address owner to manually identify and return each user's share off-chain.
- If the fallback address is a contract without a withdrawal path, or if the owner is unresponsive or compromised, the funds are permanently frozen or stolen.

### Likelihood Explanation

Any unprivileged NEAR account that holds a NEP-141 token registered with Aurora can trigger this path by calling `ft_transfer_call` on the NEP-141 contract with `receiver_id = aurora` and any EVM address as `msg`. If the Address whitelist is enabled (required for Silo mode to be meaningful) and the supplied address is not listed, the redirect fires unconditionally. Users who are unaware of the whitelist, or who make a typo in the recipient address, will silently lose their tokens. No special privilege is required.

### Recommendation

When the silo fallback redirect is triggered, `receive_erc20_tokens` should return an `Err` instead of `Ok`, so that `ft_on_transfer` returns the full `args.amount` to the sender and the NEP-141 contract automatically refunds them. Alternatively, maintain an on-chain per-depositor ledger at the fallback address (mapping `sender_id → amount`) so that individual deposits can be identified and returned.

### Proof of Concept

1. Deploy Aurora Engine in Silo mode: set `erc20_fallback_address = 0xFALL` and enable the Address whitelist (empty — no addresses whitelisted).
2. Register a NEP-141 token `token.near` with Aurora (deploys an ERC-20 mirror).
3. Alice (`alice.near`) calls:
   ```
   token.near::ft_transfer_call(
     receiver_id: "aurora",
     amount: "1000",
     msg: "<alice_evm_address_hex>"   // not whitelisted
   )
   ```
4. Aurora's `ft_on_transfer` is called. `receive_erc20_tokens` detects `alice_evm_address` is not whitelisted, silently sets `recipient = 0xFALL`, mints 1000 tokens to `0xFALL`, and returns `Ok`.
5. `ft_on_transfer` outputs `"0"` — NEP-141 does not refund Alice.
6. Bob (`bob.near`) repeats with 500 tokens. Both deposits are now pooled at `0xFALL`.
7. There is no on-chain record distinguishing Alice's 1000 from Bob's 500. Neither can recover their tokens without off-chain intervention from the fallback address owner. [5](#0-4) [6](#0-5)

### Citations

**File:** engine/src/engine.rs (L818-843)
```rust
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

**File:** engine/src/contract_methods/silo/mod.rs (L140-143)
```rust
/// Check if a user has the right to receive erc20 tokens.
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}
```
