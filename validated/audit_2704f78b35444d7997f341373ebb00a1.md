### Title
ERC-20 Mirror Insolvency and Permanent Fund Freeze for Rebasable NEP-141 Tokens — (`engine/src/engine.rs`, `etc/eth-contracts/contracts/EvmErc20.sol`, `engine-precompiles/src/native.rs`)

---

### Summary

Aurora Engine bridges NEP-141 tokens to ERC-20 mirrors on the Aurora EVM using a strict 1:1 accounting model: deposit `N` NEP-141 → mint `N` ERC-20; burn `N` ERC-20 → transfer `N` NEP-141 back. This model is broken for rebasable NEP-141 tokens (tokens whose balances change without explicit transfers). After a negative rebase, Aurora's actual NEP-141 balance falls below the ERC-20 `totalSupply`, making the bridge insolvent. After a positive rebase, excess NEP-141 tokens accumulate in Aurora's account with no redemption path, permanently freezing them.

---

### Finding Description

**Deposit path** — `ft_on_transfer` → `receive_erc20_tokens`:

When a user bridges a NEP-141 token to Aurora via `ft_transfer_call`, the NEP-141 contract calls `ft_on_transfer` on Aurora. Aurora reads `args.amount` and mints exactly that many ERC-20 tokens:

```rust
// engine/src/engine.rs
let amount = args.amount.as_u128();
// ...
setup_receive_erc20_tokens_input(&recipient, amount)
``` [1](#0-0) 

**Withdrawal path** — `withdrawToNear` → `ExitToNear` precompile → `ft_transfer`:

When a user calls `withdrawToNear(recipient, amount)` on the ERC-20 mirror, the contract burns exactly `amount` ERC-20 tokens and calls the `ExitToNear` precompile with that same `amount`:

```solidity
// EvmErc20.sol
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);
    // calls precompile with `amount`
}
``` [2](#0-1) [3](#0-2) 

The precompile then schedules an `ft_transfer` on the NEP-141 contract for exactly `exit_params.amount`:

```rust
// engine-precompiles/src/native.rs
format!(
    r#"{{"receiver_id":"{}","amount":"{}"}}"#,
    exit_params.receiver_account_id,
    exit_params.amount.as_u128()
)
``` [4](#0-3) 

**The invariant that breaks with rebasable tokens:**

At all times the system assumes:

```
Aurora's NEP-141 balance == ERC-20 totalSupply
```

A rebasable NEP-141 token can change Aurora's NEP-141 balance without any `ft_transfer` call — the balance simply changes in the NEP-141 contract's storage. Neither `receive_erc20_tokens` nor `exit_erc20_token_to_near` reads or adjusts for the actual current NEP-141 balance held by Aurora. There is no mechanism anywhere in the engine to detect or compensate for a rebase event. [5](#0-4) 

---

### Impact Explanation

**Negative rebase (supply contraction):**

Aurora's NEP-141 balance decreases to `X * f` (where `f < 1`), but the ERC-20 `totalSupply` remains `X`. The bridge is now insolvent: the sum of all ERC-20 balances exceeds the NEP-141 tokens Aurora can actually transfer. The last users to call `withdrawToNear` will have their `ft_transfer` promises fail on the NEP-141 side (insufficient balance), while their ERC-20 tokens have already been burned. Those users permanently lose their funds.

**Impact: Critical — Insolvency / Permanent fund freeze.**

**Positive rebase (supply expansion):**

Aurora's NEP-141 balance increases to `X * f` (where `f > 1`), but the ERC-20 `totalSupply` remains `X`. The excess `(f-1)*X` NEP-141 tokens sit in Aurora's account with no ERC-20 representation. No user can ever claim them through the bridge because the ERC-20 mirror will never mint additional tokens to represent the rebased surplus. Those tokens are permanently frozen.

**Impact: Critical — Permanent freezing of funds.**

---

### Likelihood Explanation

- Any unprivileged user can call `deploy_erc20_token` on Aurora to create an ERC-20 mirror for any NEP-141 token, including rebasable ones. No admin action is required.
- Any user can then bridge tokens via the standard `ft_transfer_call` flow.
- Rebasable NEP-141 tokens are a realistic token design on NEAR (supply-adjusting stablecoins, liquid staking tokens with rebasing mechanics, etc.).
- The rebase itself is triggered by the token's own contract logic, not by Aurora — it is an externally reachable event from the perspective of the bridge.
- No privileged access, leaked keys, or social engineering is required.

**Likelihood: Medium** — depends on whether a rebasable NEP-141 token is bridged, but the path is fully permissionless.

---

### Recommendation

The ERC-20 mirror bridge should not assume a fixed 1:1 ratio between ERC-20 supply and NEP-141 balance for rebasable tokens. Two approaches:

1. **Shares-based accounting**: Instead of recording raw token amounts, record each depositor's proportional share of Aurora's total NEP-141 balance. On withdrawal, compute the redeemable NEP-141 amount as `(user_shares / total_shares) * current_nep141_balance`. This mirrors how the LOB report recommends recalculating balances based on current total supply.

2. **Blocklist rebasable tokens**: Detect or document that rebasable NEP-141 tokens are unsupported and reject `deploy_erc20_token` calls for known rebasable token contracts.

The `receive_erc20_tokens` mint path and the `exit_erc20_token_to_near` withdrawal path both need to be updated to use the shares model if approach 1 is chosen. [6](#0-5) [7](#0-6) 

---

### Proof of Concept

1. Deploy a rebasable NEP-141 token `rebase.near` on NEAR. Its `ft_balance_of(aurora)` can be changed by calling `rebase()` on the token contract.

2. Call `deploy_erc20_token` on Aurora with `nep141 = "rebase.near"` → ERC-20 mirror `REBASE` is created.

3. Alice calls `ft_transfer_call` on `rebase.near`, transferring `1000` tokens to Aurora with message `alice_evm_address`. Aurora's `ft_on_transfer` fires, `receive_erc20_tokens` mints `1000 REBASE` to Alice's EVM address.
   - State: `rebase.near.ft_balance_of(aurora) = 1000`, `REBASE.totalSupply() = 1000`.

4. The `rebase.near` contract executes a negative rebase of 50%: `rebase.near.ft_balance_of(aurora)` becomes `500`.
   - State: `rebase.near.ft_balance_of(aurora) = 500`, `REBASE.totalSupply() = 1000`.

5. Alice calls `REBASE.withdrawToNear(alice_near_account, 1000)`:
   - ERC-20 burns `1000` tokens (succeeds, Alice had 1000).
   - `ExitToNear` precompile schedules `ft_transfer(receiver_id: alice_near_account, amount: 1000)` on `rebase.near`.
   - The `ft_transfer` promise **fails** because Aurora only holds `500` NEP-141 tokens.
   - Alice's `1000 REBASE` are burned. Alice receives `0` NEP-141 tokens. Funds are lost. [8](#0-7) [2](#0-1) [9](#0-8)

### Citations

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

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L53-63)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
        uint input_size = 1 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

**File:** etc/eth-contracts/contracts/EvmErc20V2.sol (L53-64)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        address sender = _msgSender();
        _burn(sender, amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", sender, amount_b, recipient);
        uint input_size = 1 + 20 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

**File:** engine-precompiles/src/native.rs (L444-447)
```rust
                ExitToNearParams::Erc20TokenParams(ref exit_params) => {
                    exit_erc20_token_to_near(context, exit_params, &self.io)?
                }
            };
```

**File:** engine-precompiles/src/native.rs (L558-647)
```rust
fn exit_erc20_token_to_near<I: IO>(
    context: &Context,
    exit_params: &Erc20TokenParams,
    io: &I,
) -> Result<
    (
        AccountId,
        String,
        events::ExitToNear,
        String,
        Option<TransferNearArgs>,
    ),
    ExitError,
> {
    // In case of withdrawing ERC-20 tokens, the `apparent_value` should be zero. In opposite way
    // the funds will be locked in the address of the precompile without any possibility
    // to withdraw them in the future. So, in case if the `apparent_value` is not zero, the error
    // will be returned to prevent that.
    if context.apparent_value != U256::zero() {
        return Err(ExitError::Other(Cow::from(
            "ERR_ETH_ATTACHED_FOR_ERC20_EXIT",
        )));
    }

    let erc20_address = context.caller; // because ERC-20 contract calls the precompile.
    let nep141_account_id = get_nep141_from_erc20(erc20_address.as_bytes(), io)?;

    let (nep141_account_id, args, method, transfer_near_args, event) = match exit_params.message {
        // wNEAR address should be set via the `factory_set_wnear_address` transaction first.
        Some(Message::UnwrapWnear) if erc20_address == get_wnear_address(io).raw() =>
        // The flow is following here:
        // 1. We call `near_withdraw` on wNEAR account id on `aurora` behalf.
        // In such way we unwrap wNEAR to NEAR.
        // 2. After that, we call callback `exit_to_near_precompile_callback` on the `aurora`
        // in which make transfer of unwrapped NEAR to the `target_account_id`.
        {
            (
                nep141_account_id,
                format!(r#"{{"amount":"{}"}}"#, exit_params.amount.as_u128()),
                "near_withdraw",
                Some(TransferNearArgs {
                    target_account_id: exit_params.receiver_account_id.clone(),
                    amount: exit_params.amount.as_u128(),
                }),
                events::ExitToNear::Legacy(ExitToNearLegacy {
                    sender: Address::new(erc20_address),
                    erc20_address: Address::new(erc20_address),
                    dest: exit_params.receiver_account_id.to_string(),
                    amount: exit_params.amount,
                }),
            )
        }
        // In this flow, we're just forwarding the `msg` to the `ft_transfer_call` transaction.
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
        // The legacy flow. Just withdraw the tokens to the NEAR account id.
        // P.S. We use underscore here instead of `None` to handle the case when a user
        // could add the `unwrap` suffix for non wNEAR ERC-20 token by mistake.
        _ => {
            // There is no way to inject json, given the encoding of both arguments
            // as decimal and valid account id respectively.
            (
                nep141_account_id,
                format!(
                    r#"{{"receiver_id":"{}","amount":"{}"}}"#,
                    exit_params.receiver_account_id,
                    exit_params.amount.as_u128()
                ),
                "ft_transfer",
                None,
                events::ExitToNear::Legacy(ExitToNearLegacy {
                    sender: Address::new(erc20_address),
                    erc20_address: Address::new(erc20_address),
                    dest: exit_params.receiver_account_id.to_string(),
                    amount: exit_params.amount,
                }),
            )
        }
    };
```
