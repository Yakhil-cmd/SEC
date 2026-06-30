### Title
Silo Mode Whitelist Not Enforced in `receive_base_tokens`, Allowing ETH Deposits to Non-Whitelisted Addresses That Cannot Be Exited — (File: `engine/src/engine.rs`)

---

### Summary

In Aurora Engine's Silo mode, the `receive_base_tokens` function (invoked via `ft_on_transfer`) mints ETH to any recipient address without checking the silo whitelist. The only exit paths — the `exitToNear` and `exitToEthereum` precompiles — require submitting an EVM transaction, which enforces `is_allow_submit` (checking both the Account and Address whitelists). This asymmetry between the deposit path and the withdrawal path allows ETH to be deposited to non-whitelisted addresses that are then permanently unable to exit their funds without operator intervention.

---

### Finding Description

**Deposit path — no whitelist check:**

`receive_base_tokens` in `engine/src/engine.rs` mints ETH to any recipient unconditionally:

```rust
pub fn receive_base_tokens(
    &mut self,
    args: &FtOnTransferArgs,
) -> Result<Option<SubmitResult>, ContractError> {
    let message_data = FtTransferMessageData::try_from(args.msg.as_str())?;
    let amount = Wei::new_u128(args.amount.as_u128());
    let receipient = message_data.recipient;
    let balance = get_balance(&self.io, &receipient);
    let new_balance = balance
        .checked_add(amount)
        .ok_or(errors::ERR_BALANCE_OVERFLOW)?;
    set_balance(&mut self.io, &receipient, &new_balance);
    Ok(None)
}
```

No call to `is_allow_submit`, `is_allow_receive_erc20_tokens`, or any silo whitelist function is made. [1](#0-0) 

This function is reached via `ft_on_transfer` in `engine/src/contract_methods/connector.rs` when the predecessor is the registered eth-connector account:

```rust
let result = if predecessor_account_id == get_connector_account_id(&io)? {
    engine.receive_base_tokens(&args)
} else {
    engine.receive_erc20_tokens(...)
};
``` [2](#0-1) 

**Withdrawal path — whitelist enforced:**

The only exit paths for ETH are the `exitToNear` and `exitToEthereum` precompiles, both of which are invoked from within EVM transactions submitted via `submit`. The silo module enforces `is_allow_submit` on the `submit` path, which requires both the Account whitelist and the Address whitelist to pass:

```rust
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}
``` [3](#0-2) 

Each individual whitelist check returns `true` only if the list is disabled or the entry exists:

```rust
fn is_address_allowed<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Address);
    !list.is_enabled() || list.is_exist(address)
}
fn is_account_allowed<I: IO + Copy>(io: &I, account: &AccountId) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Account);
    !list.is_enabled() || list.is_exist(account)
}
``` [4](#0-3) 

**Contrast with ERC-20 deposit path:**

Notably, `receive_erc20_tokens` *does* apply a partial whitelist check — it redirects tokens to a fallback address when the recipient is not in the Address whitelist (if a fallback is configured):

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;
}
``` [5](#0-4) 

The base-token deposit path has no equivalent guard at all.

---

### Impact Explanation

When a Silo deployment has the Account or Address whitelist enabled, any ETH deposited via `ft_on_transfer` to a non-whitelisted address is minted and recorded in engine storage, but the recipient has no mechanism to exit those funds: `exitToNear` and `exitToEthereum` are only reachable through EVM transactions, which are blocked by `is_allow_submit`. The funds remain locked until the silo operator explicitly adds the address/account to the whitelist. If the operator is unresponsive or the silo is abandoned, the freeze is permanent.

**Impact: High — Temporary (potentially permanent) freezing of funds.**

---

### Likelihood Explanation

Two realistic triggering scenarios exist:

1. **Self-inflicted lock**: A user who holds ETH on NEAR (as NEP-141 tokens on the eth-connector) calls `ft_transfer_call` targeting their own Aurora address on a Silo where they are not whitelisted. They may not know their whitelist status before depositing.

2. **Griefing**: Any third party can call `ft_transfer_call` on the eth-connector specifying a victim's Aurora address as the recipient. The victim's address receives ETH it cannot exit, at the cost of the attacker's own ETH.

Silo mode is a production feature of Aurora Engine. Any Silo deployment that enables the Account or Address whitelist is exposed.

---

### Recommendation

Apply the same whitelist guard in `receive_base_tokens` that `receive_erc20_tokens` applies for ERC-20 tokens. If a fallback address is configured and the recipient is not whitelisted, redirect the ETH credit to the fallback address rather than the specified recipient. If no fallback is configured, reject the deposit (return the full amount to the sender) rather than minting to a non-whitelisted address.

Additionally, consider adding a silo-aware exit path (e.g., a privileged `engine_exit` method callable by the operator on behalf of a user) so that funds deposited before a whitelist change are not permanently stranded.

---

### Proof of Concept

1. Deploy Aurora Engine in Silo mode with the Account whitelist enabled.
2. Do **not** add `victim.near` or its derived EVM address to any whitelist.
3. From any NEAR account holding ETH (NEP-141 on the eth-connector), call:
   ```
   ft_transfer_call(
     receiver_id: "aurora",
     amount: "1000000000000000000",
     msg: "<victim_evm_address_hex>"
   )
   ```
4. Observe that `receive_base_tokens` mints 1 ETH to the victim's EVM address with no whitelist check. [1](#0-0) 
5. From `victim.near`, attempt to submit an EVM transaction calling `exitToNear`. The call is rejected by `is_allow_submit` because the Account whitelist is enabled and `victim.near` is not listed. [3](#0-2) 
6. The 1 ETH is permanently inaccessible to the victim without operator intervention.

### Citations

**File:** engine/src/engine.rs (L773-790)
```rust
    pub fn receive_base_tokens(
        &mut self,
        args: &FtOnTransferArgs,
    ) -> Result<Option<SubmitResult>, ContractError> {
        let message_data = FtTransferMessageData::try_from(args.msg.as_str())?;
        let amount = Wei::new_u128(args.amount.as_u128());
        let receipient = message_data.recipient;
        let balance = get_balance(&self.io, &receipient);
        let new_balance = balance
            .checked_add(amount)
            .ok_or(errors::ERR_BALANCE_OVERFLOW)?;

        set_balance(&mut self.io, &receipient, &new_balance);

        sdk::log!("Mint {amount} base tokens for: {}", receipient.encode());

        Ok(None)
    }
```

**File:** engine/src/engine.rs (L818-822)
```rust
        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }
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

**File:** engine/src/contract_methods/silo/mod.rs (L136-138)
```rust
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L155-163)
```rust
fn is_address_allowed<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Address);
    !list.is_enabled() || list.is_exist(address)
}

fn is_account_allowed<I: IO + Copy>(io: &I, account: &AccountId) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Account);
    !list.is_enabled() || list.is_exist(account)
}
```
