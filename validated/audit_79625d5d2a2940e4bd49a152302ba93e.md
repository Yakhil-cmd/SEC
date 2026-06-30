### Title
Partial `ft_transfer_call` Refund Not Re-minted in `exit_to_near_precompile_callback` — (File: `engine/src/contract_methods/connector.rs`)

---

### Summary

When a user exits ERC-20 tokens to NEAR using the Omni message format, the `exit_to_near_precompile_callback` only re-mints burned ERC-20 tokens if the `ft_transfer_call` promise **completely fails**. If the promise succeeds but the NEP-141 receiver partially rejects tokens (returning a non-zero amount from `ft_on_transfer`), the callback ignores the partial refund and does not re-mint the corresponding ERC-20 tokens. The user's ERC-20 tokens are permanently burned while the refunded NEP-141 tokens are stranded in Aurora's balance, inaccessible to the user.

---

### Finding Description

The `ExitToNear` precompile handles ERC-20 token exits to NEAR. When a user calls `withdrawToNear` with an Omni message, the flow is:

1. The ERC-20 contract burns the user's tokens and calls the exit precompile.
2. The precompile schedules a `ft_transfer_call` promise on the NEP-141 contract.
3. The NEP-141 contract calls `ft_on_transfer` on the receiver.
4. The receiver returns the amount to refund (0 = accept all; non-zero = partial/full rejection).
5. `ft_resolve_transfer` refunds the rejected amount to Aurora's NEP-141 balance and returns the net transferred amount.
6. Aurora's `exit_to_near_precompile_callback` is invoked with the promise result.

The critical flaw is in `exit_to_near_precompile_callback`:

```rust
let maybe_result = if let Some(PromiseResult::Successful(_)) = handler.promise_result(0) {
    // Promise succeeded — no refund triggered, returned value ignored
    None
} else if let Some(args) = args.refund {
    // Promise failed — re-mint ERC-20 tokens
    let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;
    ...
}
```

The wildcard `_` in `Successful(_)` discards the actual value returned by `ft_resolve_transfer`. In the NEP-141 standard, `ft_transfer_call` is considered **successful** even when the receiver partially rejects tokens — `ft_resolve_transfer` refunds the rejected portion to the sender (Aurora's NEP-141 balance) and returns the net transferred amount as the promise result. Because the callback only triggers `refund_on_error` on a failed promise, a partial rejection is silently swallowed: the ERC-20 tokens remain burned for the full amount, but only a fraction of the NEP-141 tokens were actually transferred.

The Omni exit path is selected in `exit_erc20_token_to_near` when the user provides a JSON Omni message:

```rust
Some(Message::Omni(msg)) => (
    nep141_account_id,
    ft_transfer_call_args(&exit_params.receiver_account_id, exit_params.amount, msg)?,
    "ft_transfer_call",
    None,
    ...
),
```

The `refund_call_args` function (when `error_refund` is enabled) correctly encodes the full burned amount and the ERC-20 address into the callback args, but the callback never reads the promise return value to detect a partial refund.

---

### Impact Explanation

- **Direct theft of user funds (Critical):**