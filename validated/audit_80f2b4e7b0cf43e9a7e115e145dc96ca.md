### Title
ERC-20 Tokens and ETH Permanently Frozen When `ExitToEthereum` NEAR Promise Fails — (`engine-precompiles/src/native.rs`)

### Summary

The `ExitToEthereum` precompile deducts EVM-side balances (burns ERC-20 tokens or transfers ETH to the precompile address) before scheduling a NEAR promise to call `withdraw` on the eth-connector. No error-recovery callback is ever attached to this promise. If the NEAR `withdraw` call fails for any reason, the EVM-side deduction is irreversible and the funds are permanently frozen.

### Finding Description

In `EvmErc20.withdrawToEthereum` (`etc/eth-contracts/contracts/EvmErc20.sol`), the ERC-20 tokens are burned first, then the `ExitToEthereum` precompile is called:

```solidity
function withdrawToEthereum(address recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);          // <-- tokens destroyed here
    // ... calls ExitToEthereum precompile
}
```

Inside the `ExitToEthereum::run` implementation in `engine-precompiles/src/native.rs`, the precompile constructs a NEAR promise to call `withdraw` on the eth-connector and emits it as a log — but attaches **no callback** to handle promise failure:

```rust
let withdraw_promise = PromiseCreateArgs { ... method: "withdraw" ... };
let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
// No PromiseArgs::Callback wrapping; no error handler.
```

This is structurally different from `ExitToNear`, which (when the `error_refund` feature is compiled in) wraps the transfer promise in a `PromiseArgs::Callback` pointing to `exit_to_near_precompile_callback`, which calls `refund_on_error` to re-mint burned tokens on failure. `ExitToEthereum` has no equivalent path at all.

For the ETH base-token case, the ETH is transferred from the caller to the precompile address (`0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`) inside the EVM before the NEAR promise runs. If the promise fails, the ETH is stranded at the precompile address with no code path to recover it.

### Impact Explanation

- **ERC-20 path**: tokens are burned in the EVM; if the NEAR `withdraw` promise fails, the ERC-20 supply is permanently reduced with no corresponding Ethereum-side credit. Affected users lose their tokens entirely — **permanent fund freeze / insolvency of the ERC-20 mirror**.
- **ETH base-token path**: ETH is moved to the precompile address in the EVM; if the NEAR promise fails, it is stranded there with no recovery mechanism — **permanent fund freeze**.

Both outcomes match the "permanent freezing of funds" impact class.

### Likelihood Explanation

The NEAR `withdraw` call on the eth-connector can fail in several realistic, non-admin-required scenarios:

1. The `engine_withdraw` feature is paused on the eth-connector (a normal operational action already tested in the connector test suite).
2. The eth-connector contract runs out of gas for the promise.
3. The eth-connector contract panics due to any internal error (e.g., serialization failure, storage corruption).

Any EVM user who calls `withdrawToEthereum` while any of these conditions hold will lose their funds permanently. The user has no way to predict or prevent this at call time.

### Recommendation

Mirror the `ExitToNear` error-recovery pattern for `ExitToEthereum`:

1. Wrap the `withdraw_promise` in a `PromiseArgs::Callback` that calls a new `exit_to_ethereum_precompile_callback` method on the engine.
2. In that callback, if the promise result is not `Successful`, call `refund_on_error` to re-mint the burned ERC-20 tokens (or transfer ETH back from the precompile address to the original caller).
3. Ensure the refund address is included in the precompile input (analogous to the `refund_address` field already present in `ExitToNear` under `error_refund`).

### Proof of Concept

**Step 1 — Burn and call precompile (ERC-20 path):**

`EvmErc20.withdrawToEthereum` burns tokens then calls the precompile: [1](#0-0) 

**Step 2 — Precompile schedules promise with no error callback:** [2](#0-1) 

**Step 3 — Contrast with `ExitToNear`, which does attach a callback:** [3](#0-2) 

**Step 4 — `exit_to_near_precompile_callback` calls `refund_on_error` on failure; no equivalent exists for `ExitToEthereum`:** [4](#0-3) 

**Step 5 — `refund_on_error` re-mints burned ERC-20 tokens; only reachable from `ExitToNear` path:** [5](#0-4) 

**Step 6 — Test confirms that without the refund path, tokens are permanently lost:** [6](#0-5)

### Citations

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L65-76)
```text
    function withdrawToEthereum(address recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes20 recipient_b = bytes20(recipient);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient_b);
        uint input_size = 1 + 32 + 20;

        assembly {
            let res := call(gas(), 0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

**File:** engine-precompiles/src/native.rs (L449-483)
```rust
        let callback_args = ExitToNearPrecompileCallbackArgs {
            #[cfg(feature = "error_refund")]
            refund: refund_call_args(&exit_to_near_params, &exit_event),
            #[cfg(not(feature = "error_refund"))]
            refund: None,
            transfer_near: transfer_near_args,
        };
        let attached_gas = if method == "ft_transfer_call" {
            costs::FT_TRANSFER_CALL_GAS
        } else {
            costs::FT_TRANSFER_GAS
        };

        let transfer_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method,
            args: args.into_bytes(),
            attached_balance: Yocto::new(1),
            attached_gas,
        };

        let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
            PromiseArgs::Create(transfer_promise)
        } else {
            PromiseArgs::Callback(PromiseWithCallbackArgs {
                base: transfer_promise,
                callback: PromiseCreateArgs {
                    target_account_id: self.current_account_id.clone(),
                    method: "exit_to_near_precompile_callback".to_string(),
                    args: borsh::to_vec(&callback_args).unwrap(),
                    attached_balance: Yocto::new(0),
                    attached_gas: costs::EXIT_TO_NEAR_CALLBACK_GAS,
                },
            })
        };
```

**File:** engine-precompiles/src/native.rs (L977-1003)
```rust
        let withdraw_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method: "withdraw".to_string(),
            args: serialized_args,
            attached_balance: Yocto::new(1),
            attached_gas: costs::WITHDRAWAL_GAS,
        };

        let promise = borsh::to_vec(&PromiseArgs::Create(withdraw_promise)).unwrap();
        let promise_log = Log {
            address: exit_to_ethereum::ADDRESS.raw(),
            topics: Vec::new(),
            data: promise,
        };
        let ethabi::RawLog { topics, data } = exit_event.encode();
        let exit_event_log = Log {
            address: exit_to_ethereum::ADDRESS.raw(),
            topics: topics.into_iter().map(|h| H256::from(h.0)).collect(),
            data,
        };

        Ok(PrecompileOutput {
            logs: vec![promise_log, exit_event_log],
            cost: required_gas,
            output: Vec::new(),
        })
    }
```

**File:** engine/src/contract_methods/connector.rs (L231-239)
```rust
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
```

**File:** engine/src/engine.rs (L1176-1224)
```rust
pub fn refund_on_error<I: IO + Copy, E: Env, P: PromiseHandler>(
    io: I,
    env: &E,
    state: EngineState,
    args: &RefundCallArgs,
    handler: &mut P,
) -> EngineResult<SubmitResult> {
    let current_account_id = env.current_account_id();
    if let Some(erc20_address) = args.erc20_address {
        // ERC-20 exit; re-mint burned tokens
        let erc20_admin_address = current_address(&current_account_id);
        let mut engine: Engine<_, _> =
            Engine::new_with_state(state, erc20_admin_address, current_account_id, io, env);

        let refund_address = args.recipient_address;
        let amount = U256::from_big_endian(&args.amount);
        let input = setup_refund_on_error_input(amount, refund_address);

        engine.call(
            &erc20_admin_address,
            &erc20_address,
            Wei::zero(),
            input,
            u64::MAX,
            Vec::new(),
            Vec::new(),
            handler,
        )
    } else {
        // ETH exit; transfer ETH back from precompile address
        let exit_address = exit_to_near::ADDRESS;
        let mut engine: Engine<_, _> =
            Engine::new_with_state(state, exit_address, current_account_id, io, env);
        let refund_address = args.recipient_address;
        let amount = Wei::new(U256::from_big_endian(&args.amount));
        engine.call(
            &exit_address,
            &refund_address,
            amount,
            Vec::new(),
            u64::MAX,
            vec![
                (exit_address.raw(), Vec::new()),
                (refund_address.raw(), Vec::new()),
            ],
            Vec::new(),
            handler,
        )
    }
```

**File:** engine-tests/src/tests/erc20_connector.rs (L656-665)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();

        assert_eq!(
            erc20_balance(&erc20, ft_owner_address, &aurora).await,
            balance
        );
```
