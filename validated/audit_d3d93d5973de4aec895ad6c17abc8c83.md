### Title
NEP-141 Fee-on-Transfer Token Accounting Mismatch in Bridge Minting Causes ERC-20 Over-Issuance and Bridge Insolvency - (File: `engine/src/engine.rs`)

---

### Summary

The Aurora Engine bridge mints ERC-20 mirror tokens based solely on the `amount` field supplied in `FtOnTransferArgs` by the calling NEP-141 token contract, without verifying the actual balance change in Aurora's NEP-141 account. For fee-on-transfer NEP-141 tokens — where the receiver (Aurora) receives less than the reported `amount` — this causes Aurora to over-mint ERC-20 tokens, creating an insolvency condition where the ERC-20 supply permanently exceeds the NEP-141 backing.

---

### Finding Description

When a NEP-141 token is bridged into Aurora via `ft_transfer_call`, the NEP-141 contract calls Aurora's `ft_on_transfer` entrypoint. The handler reads `args.amount` directly from the JSON payload supplied by the calling token contract and uses it verbatim to mint ERC-20 tokens:

**`engine/src/contract_methods/connector.rs` (routing):**
```rust
let args: FtOnTransferArgs = read_json_args(&io)?;
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

**`engine/src/engine.rs` — `receive_erc20_tokens`:**
```rust
let amount = args.amount.as_u128();
// ...
setup_receive_erc20_tokens_input(&recipient, amount),
```

The `amount` is taken directly from `args` and passed to `setup_receive_erc20_tokens_input`, which encodes an ERC-20 `mint(recipient, amount)` call. There is no balance-before / balance-after check on Aurora's NEP-141 account to confirm that `amount` tokens were actually received.

A fee-on-transfer NEP-141 token (one that deducts a fee from the transferred amount, so the receiver gets `amount - fee`) would call `ft_on_transfer` with the original `amount`, causing Aurora to mint `amount` ERC-20 tokens while only holding `amount - fee` NEP-141 tokens in reserve.

The `deploy_erc20_token` entrypoint has no access control beyond requiring the engine to be running — any unprivileged user can register any NEP-141 token:

```rust
pub fn deploy_erc20_token<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<PromiseOrValue<Address>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        // No owner/admin check
        ...
    })
}
```

---

### Impact Explanation

**Insolvency / Permanent fund freeze / Theft of user funds.**

After repeated deposits of a fee-on-transfer NEP-141 token, Aurora's ERC-20 supply for that token exceeds its NEP-141 reserve. When users attempt to exit (burn ERC-20 via `withdrawToNear` in `EvmErc20.sol` / `EvmErc20V2.sol`, which calls the `ExitToNear` precompile and triggers `ft_transfer` on the NEP-141 contract), Aurora will eventually lack sufficient NEP-141 balance to fulfill withdrawals. Early exiters drain the reserve at the expense of later exiters, whose ERC-20 tokens become permanently frozen with no NEP-141 backing.

---

### Likelihood Explanation

Any unprivileged NEAR account can:
1. Deploy a custom fee-on-transfer NEP-141 token contract.
2. Call `deploy_erc20_token` on Aurora to register the NEP-141 → ERC-20 mapping (no access control).
3. Call `ft_transfer_call` on the NEP-141 token to trigger the minting path.

No admin compromise, governance capture, or special privilege is required. The entry path is fully reachable by an ordinary token holder or contract deployer.

---

### Recommendation

In `receive_erc20_tokens` (and analogously `receive_base_tokens`), record the NEP-141 balance of Aurora's account before and after the transfer, and use the actual delta as the mint amount rather than trusting `args.amount`. Alternatively, document and enforce that only non-rebasing, non-fee-on-transfer NEP-141 tokens may be registered, and add a validation check in `deploy_erc20_token` or `ft_on_transfer` to reject known non-standard token behaviors.

---

### Proof of Concept

1. Deploy a NEP-141 token `fot.near` that charges a 10% fee on transfer (receiver gets 90% of `amount`).
2. Call `deploy_erc20_token` on Aurora to register `fot.near` → ERC-20 mapping (no access control check).
3. Call `fot.near::ft_transfer_call(receiver_id: "aurora", amount: "1000", msg: "<evm_address>")`.
4. `fot.near` transfers 900 tokens to Aurora's account but calls `ft_on_transfer` with `amount = "1000"`.
5. Aurora's `receive_erc20_tokens` mints 1000 ERC-20 tokens to `<evm_address>`.
6. Attacker calls `withdrawToNear` on the ERC-20 for 1000 tokens; Aurora attempts `ft_transfer` of 1000 `fot.near` tokens but only holds 900 → the last 100 ERC-20 tokens are permanently unbacked.
7. Repeat to drain the reserve of any legitimate bridgers of `fot.near`.

**Root cause lines:** [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** engine/src/engine.rs (L778-785)
```rust
        let amount = Wei::new_u128(args.amount.as_u128());
        let receipient = message_data.recipient;
        let balance = get_balance(&self.io, &receipient);
        let new_balance = balance
            .checked_add(amount)
            .ok_or(errors::ERR_BALANCE_OVERFLOW)?;

        set_balance(&mut self.io, &receipient, &new_balance);
```

**File:** engine/src/engine.rs (L803-831)
```rust
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
```

**File:** engine/src/contract_methods/connector.rs (L80-90)
```rust
        let args: FtOnTransferArgs = read_json_args(&io)?;
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
