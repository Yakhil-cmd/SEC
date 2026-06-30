### Title
Rebasable NEP-141 Token Bridging Breaks ERC-20 Mirror Accounting — (`engine/src/engine.rs`, `engine-precompiles/src/native.rs`)

### Summary
Aurora Engine's NEP-141→ERC-20 bridge mints and burns ERC-20 tokens using the strict `amount` field from `ft_on_transfer` / `withdrawToNear` calldata. For rebasable NEP-141 tokens (whose balances change without explicit transfers), the actual NEP-141 balance held by Aurora will diverge from the ERC-20 total supply, causing either insolvency (negative rebase) or permanent fund freeze (positive rebase).

### Finding Description

When a NEP-141 token is bridged to Aurora, two accounting operations occur:

**Deposit path** — `ft_on_transfer` → `receive_erc20_tokens`:

The engine reads `args.amount` directly from the NEP-141 callback and mints exactly that many ERC-20 tokens:

```rust
// engine/src/engine.rs
let amount = args.amount.as_u128();
// ...
setup_receive_erc20_tokens_input(&recipient, amount)
```

`setup_receive_erc20_tokens_input` encodes a `mint(recipient, amount)` call to the `EvmErc20` contract using the callback-reported amount, not Aurora's actual post-transfer NEP-141 balance.

**Withdrawal path** — `withdrawToNear` → `exit_to_near` precompile → `ft_transfer`:

The ERC-20 contract burns `amount` tokens and passes that same value to the precompile:

```solidity
// etc/eth-contracts/contracts/EvmErc20.sol
function withdrawToNear(bytes memory recipient, uint256 amount) external override {
    _burn(_msgSender(), amount);
    bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
    // calls exit_to_near precompile
```

The precompile then calls `ft_transfer` on the NEP-141 contract with the burned amount:

```rust
// engine-precompiles/src/native.rs
format!(
    r#"{{"receiver_id":"{}","amount":"{}"}}"#,
    exit_params.receiver_account_id,
    exit_params.amount.as_u128()
),
```

For a rebasable NEP-141 token, the actual balance Aurora holds can increase or decrease independently of any transfer. Neither the deposit nor the withdrawal path queries Aurora's real NEP-141 balance — both use only the caller-supplied `amount`.

There is no restriction preventing rebasable NEP-141 tokens from being bridged. `deploy_erc20_token` is a public entrypoint callable by any NEAR account, and `ft_on_transfer` accepts any NEP-141 predecessor.

### Impact Explanation

**Negative rebase (insolvency — Critical):** Aurora's NEP-141 balance shrinks below the ERC-20 total supply. When users attempt to withdraw via `withdrawToNear`, the `ft_transfer` call to the NEP-141 contract will fail for the last withdrawers because Aurora no longer holds enough tokens. The ERC-20 tokens held by those users become permanently unredeemable — direct theft of user funds via bridge insolvency.

**Positive rebase (permanent fund freeze — Critical):** Aurora's NEP-141 balance grows above the ERC-20 total supply. The excess NEP-141 tokens are locked in Aurora's account with no mechanism to distribute or recover them, since the ERC-20 supply does not reflect the surplus and no withdrawal path can access it.

### Likelihood Explanation

Rebasable fungible tokens are a well-established pattern on NEAR (e.g., liquid staking derivatives such as stNEAR). Any such token can be bridged to Aurora by any unprivileged user calling `deploy_erc20_token`. Once bridged, every rebase event silently corrupts the 1:1 peg between the NEP-141 balance held by Aurora and the ERC-20 total supply. No special attacker action is required beyond bridging a rebasable token — the rebase mechanism itself is the trigger.

### Recommendation

1. Document and enforce that only non-rebasable NEP-141 tokens may be bridged via `deploy_erc20_token`. Add an explicit warning in the contract and consider a registry or allowlist of approved token types.
2. Alternatively, implement a share-based accounting model (similar to ERC-4626 vaults) where the ERC-20 represents a share of Aurora's actual NEP-141 balance rather than a fixed nominal amount, so rebases are automatically reflected.
3. At minimum, add a check in `receive_erc20_tokens` that verifies Aurora's post-transfer NEP-141 balance increased by at least `args.amount` before minting, rejecting tokens whose actual credit differs from the reported amount.

### Proof of Concept

1. Deploy a rebasable NEP-141 token `rebase.near` on NEAR (total supply 1000, balances multiply by a factor each epoch).
2. Call `deploy_erc20_token` on Aurora to create `EvmErc20` mirror at address `0xABC`.
3. User Alice calls `ft_transfer_call` on `rebase.near`, transferring 100 tokens to Aurora. Aurora's `ft_on_transfer` fires with `amount = 100`; `receive_erc20_tokens` mints 100 ERC-20 tokens to Alice's EVM address.
4. The NEP-141 token rebases downward by 50% (all balances halved). Aurora's actual `rebase.near` balance is now 50, but the ERC-20 total supply is still 100.
5. Alice calls `withdrawToNear(100)` on `0xABC`. The ERC-20 burns 100 tokens and the `exit_to_near` precompile calls `ft_transfer` on `rebase.near` for `amount = 100`. The call fails because Aurora only holds 50 tokens — Alice's 100 ERC-20 tokens are now permanently frozen.

**Key code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** engine/src/engine.rs (L796-837)
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
```

**File:** engine/src/engine.rs (L1306-1313)
```rust
pub fn setup_receive_erc20_tokens_input(recipient: &Address, amount: u128) -> Vec<u8> {
    let selector = ERC20_MINT_SELECTOR;
    let tail = ethabi::encode(&[
        ethabi::Token::Address(recipient.raw().0.into()),
        ethabi::Token::Uint(amount.into()),
    ]);

    [selector, tail.as_slice()].concat()
```

**File:** engine-precompiles/src/native.rs (L627-646)
```rust
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
