### Title
`ExitToNear` Omni Path Emits Event with Full Amount Before `ft_transfer_call` Resolves, Causing Permanent Fund Freeze on Partial Refund — (File: `engine-precompiles/src/native.rs`)

---

### Summary

The `ExitToNear` precompile's Omni path schedules a NEAR `ft_transfer_call` promise and simultaneously emits the `ExitToNear` EVM log with the **full** requested amount. Because `ft_transfer_call` (NEP-141) allows the receiver's `ft_on_transfer` to return unused tokens, the actual amount delivered to the recipient can be less than what the event records. The `exit_to_near_precompile_callback` only branches on full promise success or full promise failure; it never inspects the returned-token value from `ft_resolve_transfer`. Tokens returned by the receiver accumulate in Aurora's NEP-141 balance with no corresponding EVM credit, permanently freezing them.

---

### Finding Description

**Step 1 — Event emitted before promise resolves.**

In `ExitToNear::run()`, both the promise log and the exit-event log are assembled and returned together as the precompile output: [1](#0-0) 

The `ExitToNearOmni` event encodes `amount: context.apparent_value` (base-token path) or `amount: exit_params.amount` (ERC-20 path) — the full requested amount — at EVM execution time, before any NEAR promise has run.

**Step 2 — Omni path selects `ft_transfer_call`, not `ft_transfer`.**

For the base-token Omni branch: [2](#0-1) 

For the ERC-20 Omni branch: [3](#0-2) 

Both branches pass `"ft_transfer_call"` as the NEAR method.

### Citations

**File:** engine-precompiles/src/native.rs (L484-500)
```rust
        let promise_log = Log {
            address: exit_to_near::ADDRESS.raw(),
            topics: Vec::new(),
            data: borsh::to_vec(&promise).unwrap(),
        };
        let ethabi::RawLog { topics, data } = exit_event.encode();
        let exit_event_log = Log {
            address: exit_to_near::ADDRESS.raw(),
            topics: topics.into_iter().map(|h| H256::from(h.0)).collect(),
            data,
        };

        Ok(PrecompileOutput {
            logs: vec![promise_log, exit_event_log],
            cost: required_gas,
            output: Vec::new(),
        })
```

**File:** engine-precompiles/src/native.rs (L519-535)
```rust
        Some(Message::Omni(msg)) => Ok((
            eth_connector_account_id,
            ft_transfer_call_args(
                &exit_params.receiver_account_id,
                context.apparent_value,
                msg,
            )?,
            events::ExitToNear::Omni(ExitToNearOmni {
                sender: Address::new(context.caller),
                erc20_address: events::ETH_ADDRESS,
                dest: exit_params.receiver_account_id.to_string(),
                amount: context.apparent_value,
                msg: msg.to_string(),
            }),
            "ft_transfer_call".to_string(),
            None,
        )),
```

**File:** engine-precompiles/src/native.rs (L611-623)
```rust
        Some(Message::Omni(msg)) => (
            nep141_account_id,
            ft_transfer_call_args(&exit_params.receiver_account_id, exit_params.amount, msg)?,
            "ft_transfer_call",
            None,
            events::ExitToNear::Omni(ExitToNearOmni {
                sender: Address::new(erc20_address),
                erc20_address: Address::new(erc20_address),
                dest: exit_params.receiver_account_id.to_string(),
                amount: exit_params.amount,
                msg: msg.to_string(),
            }),
        ),
```
