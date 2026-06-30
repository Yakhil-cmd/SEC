### Title
Fee-on-Transfer NEP-141 Token Causes ERC-20 Over-Minting and Bridge Insolvency — (`engine/src/engine.rs`)

### Summary

`Engine::receive_erc20_tokens` mints ERC-20 tokens using the caller-reported `args.amount` from `ft_on_transfer` without verifying the actual NEP-141 balance change in Aurora's account. If the bridged NEP-141 token applies a transfer fee (crediting Aurora with `amount - fee` while reporting `amount` in the callback), Aurora mints more ERC-20 tokens than the NEP-141 tokens it holds, creating a permanent insolvency in the bridge.

### Finding Description

The bridge deposit flow for NEP-141 → ERC-20 is:

1. A user calls `ft_transfer_call` on a NEP-141 token contract, specifying Aurora as the receiver.
2. The NEP-141 contract transfers tokens to Aurora's account and then calls `ft_on_transfer(sender_id, amount, msg)` on Aurora.
3. Aurora's `ft_on_transfer` entry point dispatches to `engine.receive_erc20_tokens()`.
4. `receive_erc20_tokens` reads `args.amount` directly and mints exactly that many ERC-20 tokens.

The critical flaw is at line 803 of `engine/src/engine.rs`:

```rust
let amount = args.amount.as_u128();
```

This value is then passed verbatim to `setup_receive_erc20_tokens_input` at line 831, which encodes a `mint(recipient, amount)` call to the ERC-20 contract. No balance-before/balance-after check is performed to confirm how many NEP-141 tokens Aurora actually received.

A fee-on-transfer NEP-141 token can credit Aurora with `amount - fee` while still reporting `amount` in the `ft_on_transfer` callback (the NEP-141 standard specifies the `amount` field as the amount being transferred, but a non-standard or fee-deducting implementation can diverge). Aurora then mints `amount` ERC-20 tokens, but only holds `amount - fee` NEP-141 tokens as backing. Each such deposit inflates the ERC-20 supply beyond the actual NEP-141 reserve.

The same structural issue exists in `receive_base_tokens` (line 778), though that path is gated by the trusted ETH connector predecessor check at line 81 of `engine/src/contract_methods/connector.rs`, making it less directly exploitable.

### Impact Explanation

Every deposit of a fee-on-transfer NEP-141 token mints `fee` excess ERC-20 tokens. Over repeated deposits the ERC-20 total supply exceeds the NEP-141 reserve held by Aurora. When users attempt to exit (burn ERC-20 via `withdrawToNear` → `ft_transfer` on the NEP-141 contract), the last users to exit will find Aurora's NEP-141 balance insufficient. This is a **permanent insolvency** of the bridge for that token: user funds are irretrievably locked.

**Impact: Critical — Insolvency / permanent freezing of funds.**

### Likelihood Explanation

Any NEP-141 token can be registered with Aurora via the permissionless `deploy_erc20_token` call. Once registered, any token holder can call `ft_transfer_call` on the NEP-141 contract to trigger the vulnerable path. No admin access or special privilege is required. The only prerequisite is that the NEP-141 token implements a fee-on-transfer mechanism, which is a realistic and known token design pattern.

### Recommendation

Before minting ERC-20 tokens, record Aurora's NEP-141 balance before the transfer and compute the actual received amount as the balance delta:

```rust
// Pseudocode
let balance_before = nep141_balance_of(aurora_account_id, token);
// ... transfer occurs via ft_on_transfer callback ...
let balance_after = nep141_balance_of(aurora_account_id, token);
let actual_amount = balance_after - balance_before;
setup_receive_erc20_tokens_input(&recipient, actual_amount)
```

Because `ft_on_transfer` is a callback (the transfer has already occurred when it is called), the balance delta is the correct amount to mint. Alternatively, return `args.amount` (refund all tokens) if the actual received amount does not match `args.amount`, preventing any minting when a fee discrepancy is detected.

### Proof of Concept

**Root cause location:** [1](#0-0) 

The `amount` variable at line 803 is taken directly from `args.amount.as_u128()` — the value reported by the NEP-141 contract — and is passed unchanged to `setup_receive_erc20_tokens_input` at line 831, which encodes a `mint(recipient, amount)` call. [2](#0-1) 

`setup_receive_erc20_tokens_input` encodes the ERC-20 `mint` selector with the unverified `amount`.

**Entry point:** [3](#0-2) 

`ft_on_transfer` is the public WASM entry point. When `predecessor_account_id` is any registered NEP-141 token (not the ETH connector), it calls `receive_erc20_tokens` with the unverified `args.amount`.

**ERC-20 mint function (no supply cap relative to NEP-141 reserve):** [4](#0-3) 

`mint` is called by the admin (Aurora engine address) with the over-reported amount, inflating ERC-20 supply beyond the actual NEP-141 backing.

**Attack scenario:**

1. Deploy or use an existing fee-on-transfer NEP-141 token (e.g., one that deducts 1% on transfer).
2. Register it with Aurora via `deploy_erc20_token` (permissionless).
3. Repeatedly call `ft_transfer_call` on the NEP-141 contract with Aurora as receiver.
4. Each call: Aurora receives `amount × 0.99` NEP-141 tokens but mints `amount` ERC-20 tokens.
5. After N deposits, ERC-20 total supply exceeds NEP-141 reserve by `N × amount × 0.01`.
6. The last `N × amount × 0.01` ERC-20 tokens can never be redeemed — those users' funds are permanently frozen.

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

**File:** engine/src/engine.rs (L1305-1314)
```rust
#[must_use]
pub fn setup_receive_erc20_tokens_input(recipient: &Address, amount: u128) -> Vec<u8> {
    let selector = ERC20_MINT_SELECTOR;
    let tail = ethabi::encode(&[
        ethabi::Token::Address(recipient.raw().0.into()),
        ethabi::Token::Uint(amount.into()),
    ]);

    [selector, tail.as_slice()].concat()
}
```

**File:** engine/src/contract_methods/connector.rs (L61-109)
```rust
#[named]
pub fn ft_on_transfer<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();
        let mut engine: Engine<_, _> = Engine::new(
            predecessor_address(&predecessor_account_id),
            current_account_id.clone(),
            io,
            env,
        )?;

        sdk::log!("Call ft_on_transfer");

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
    })
}
```

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L49-51)
```text
    function mint(address account, uint256 amount) public onlyAdmin {
        _mint(account, amount);
    }
```
